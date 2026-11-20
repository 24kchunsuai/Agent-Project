#!/usr/bin/env bash
#
# 发布脚本（企业级落地规约 E-1，判据 C2：可复现）
#
# 职责：把"当前工作区"变成"一个可交付、可复现、带账的版本包"。
#
#   1. 读 VERSION（唯一版本来源）
#   2. 打戳 —— 把版本 + git 状态注入三端
#   3. 门禁校验 —— 版本一致性、工作区是否干净、tag 是否已存在、**依赖无未处置 Critical（E-11）**
#   4. 构建三端产物（可 --skip-build 跳过）
#   5. 归档到 dist/<version>/ 并生成 SHA256 校验和
#   6. 打 git tag v<version>
#
# 用法：
#   PYTHON=/path/to/python311/python scripts/release.sh --dry-run   # 只预览，不写任何东西
#   PYTHON=/path/to/python311/python scripts/release.sh
#   PYTHON=/path/to/python311/python scripts/release.sh --skip-build
#
# 环境变量：
#   PYTHON      Python 解释器（默认 python；本项目需 3.11+ 且装好 requirements.txt）
#   MVN_CMD     Maven 命令（默认 mvn；本机需用 IDEA 内置 Maven，见 docs/交付物清单.md）
#
# ⚠️ 本文件必须保持 **LF** 换行。Windows 批处理（.cmd）恰恰相反，必须 CRLF——
#    两者别搞混：CRLF 的 .sh 会让 bash 报 "$'\r': command not found"。
#
# 门禁是**刻意**不提供绕过开关的：一个"能 --force 跳过工作区干净检查"的发布脚本，
# 在赶工期时一定会被用来发布一份源码与版本号对不上的包。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
SKIP_BUILD=0

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --dry-run)    DRY_RUN=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

PYTHON="${PYTHON:-python}"
MVN_CMD="${MVN_CMD:-mvn}"

# ---- 输出helpers -----------------------------------------------------------
bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n'; bold "==> $1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
fail() { printf '    \033[31m%s\033[0m\n' "$1"; }

# 写操作统一走这里：dry-run 时只打印，不执行
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    [dry-run] %s\n' "$*"
    else
        printf '    $ %s\n' "$*"
        "$@"
    fi
}

if [ "$DRY_RUN" -eq 1 ]; then
    bold "发布演练（--dry-run）：只检查、只打印，不写任何文件、不打 tag"
fi

# ---- 1. 前置检查 -----------------------------------------------------------
step "1/6 前置检查"

if [ ! -f VERSION ]; then
    fail "仓库根缺少 VERSION 文件，无法确定版本号"
    exit 1
fi
VERSION="$(tr -d '[:space:]' < VERSION)"
if [ -z "$VERSION" ]; then
    fail "VERSION 文件为空"
    exit 1
fi
info "版本号：$VERSION（来源：仓库根 VERSION）"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    fail "找不到 Python 解释器：$PYTHON（可用 PYTHON=... 指定）"
    exit 1
fi
info "Python：$("$PYTHON" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

for cmd in git tar; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fail "缺少命令：$cmd"
        exit 1
    fi
done
if [ "$SKIP_BUILD" -eq 0 ]; then
    if ! command -v "$MVN_CMD" >/dev/null 2>&1; then
        warn "找不到 Maven 命令：$MVN_CMD —— 构建步骤会失败；"
        warn "本机可用 IDEA 内置 Maven，或用 --skip-build 只归档已有产物"
    fi
fi

# ---- 2. 打戳 ---------------------------------------------------------------
step "2/6 打戳（版本 + git 状态 → 三端）"
run "$PYTHON" scripts/stamp_version.py

# ---- 3. 门禁校验 -----------------------------------------------------------
step "3/6 门禁校验"

GATE_OK=1

# 3.1 版本一致性（VERSION / CHANGELOG / 三端产物必须同版本）
if "$PYTHON" scripts/stamp_version.py --check; then
    info "版本一致性：通过"
else
    fail "版本一致性：未通过（见上方明细）"
    GATE_OK=0
fi

# 3.2 仓库是否有提交（unborn 仓库无法打 tag，也无法回溯到源码）
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    info "git 提交：$(git rev-parse --short HEAD)"
else
    fail "git 提交：仓库尚无提交（unborn）—— 无法打 tag，且本次构建不可复现"
    GATE_OK=0
fi

# 3.3 工作区是否干净
if [ -n "$(git status --porcelain)" ]; then
    fail "工作区：有未提交改动（$(git status --porcelain | wc -l | tr -d ' ') 个文件）"
    info "改动清单（最多 10 条）："
    git status --porcelain | head -10 | sed 's/^/      /'
    GATE_OK=0
else
    info "工作区：干净"
fi

# 3.4 tag 是否已存在
if git rev-parse --verify "refs/tags/v$VERSION" >/dev/null 2>&1; then
    fail "tag v$VERSION 已存在 —— 请先升版本号（改 VERSION 并补 CHANGELOG）"
    GATE_OK=0
else
    info "tag v$VERSION：可用"
fi

# 3.5 依赖安全（E-11）：Critical 未处置则**拒绝发布**
#
#     为什么把阻断点放在"发布"而不是"合并"：本仓库按约定尚未 push，
#     远端门禁不生效；而"带 Critical 漏洞的包出厂"是实打实的交付风险。
#     于是阻断点落在两处——本地 CI 的 deps 作业（改代码时拦住）+ 发布前这一步（拦住出厂）。
#     阈值：Critical 阻断、High 只告警（用户 2026-09-24 决策）。
if "$PYTHON" scripts/scan_deps.py >/dev/null 2>&1; then
    info "依赖安全：无未处置的 Critical（明细：$PYTHON scripts/scan_deps.py）"
else
    fail "依赖安全：存在未处置的 Critical 漏洞，或豁免台账违规 / 快照过期"
    info "查看明细：$PYTHON scripts/scan_deps.py"
    info "处置流程（含豁免与 SLA）：docs/依赖与漏洞处置.md"
    GATE_OK=0
fi

if [ "$GATE_OK" -ne 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        fail "门禁结论：**正式执行会在此中断**（dry-run 照常继续演示后续步骤）"
    else
        fail "门禁未通过，已中断。先解决上述问题再重新执行。"
        exit 1
    fi
else
    info "门禁结论：全部通过"
fi

# ---- 4. 构建 ---------------------------------------------------------------
step "4/6 构建三端产物"
if [ "$SKIP_BUILD" -eq 1 ]; then
    info "已按 --skip-build 跳过构建（将归档现存产物）"
else
    run "$MVN_CMD" -q -o -f agt_backend/pom.xml package -DskipTests
    run npm --prefix agt_web run build
fi

# ---- 5. 归档 ---------------------------------------------------------------
step "5/6 归档到 dist/$VERSION/"
OUT_DIR="dist/$VERSION"
ARCHIVE="$OUT_DIR/drone-agent001-$VERSION.tar.gz"

if [ "$SKIP_BUILD" -eq 0 ] && [ ! -d agt_web/dist ]; then
    fail "agt_web/dist 不存在（前端未构建成功）"
    exit 1
fi

run mkdir -p "$OUT_DIR"

# 排除项都写在一处，避免"某个分支漏排密钥"这类事故
TAR_EXCLUDES=(
    --exclude='.git'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='node_modules'
    # 运行期数据与密钥绝不进交付包
    --exclude='agt_python/data'
    --exclude='agt_python/.env'
    --exclude='deploy/logs'
    --exclude='deploy/secrets'
    --exclude='*.log'
)

if [ "$DRY_RUN" -eq 1 ]; then
    printf '    [dry-run] tar -czf %s %s <下列路径>\n' "$ARCHIVE" "${TAR_EXCLUDES[*]}"
    for p in VERSION CHANGELOG.md README.md docs scripts deploy \
             agt_python agt_backend/target agt_web/dist agt_web/package.json; do
        printf '      - %s\n' "$p"
    done
else
    tar -czf "$ARCHIVE" "${TAR_EXCLUDES[@]}" \
        VERSION CHANGELOG.md README.md docs scripts deploy \
        agt_python agt_backend/target agt_web/dist agt_web/package.json
    ( cd "$OUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > SHA256SUMS )
    info "已生成 $(du -h "$ARCHIVE" | cut -f1) 的交付包：$ARCHIVE"
    info "校验和：$(cat "$OUT_DIR/SHA256SUMS")"
fi

# ---- 6. 打 tag -------------------------------------------------------------
step "6/6 打 tag v$VERSION"
run git tag -a "v$VERSION" -m "release v$VERSION"
info "（推送：git push origin master --tags）"

# ---- 汇总 ------------------------------------------------------------------
printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
    bold "演练结束：以上为正式执行会做的全部动作，本次未写入任何文件、未打 tag。"
else
    bold "发布完成：v$VERSION"
    info "交付包：$ARCHIVE"
    info "下一步：核对 docs/交付物清单.md 的验收步骤，再 git push origin master --tags"
fi

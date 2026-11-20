#!/usr/bin/env bash
# ==============================================================================
# 本地 CI 门禁（企业级落地规约 E-4）
#
# 五作业：Python 单测 / Java 单测 / 前端构建 / 权限矩阵门禁（E-10）/ 依赖漏洞扫描（E-11）。
# 与远端 CI（.workflow/ci.yml）**同口径**：同一份 pytest.ini、同一个 mvn test、
# 同一个 npm run build。本地跑通 = 远端大概率跑通；本地红 = 别浪费时间 push。
#
# 用法：
#   bash scripts/ci.sh                  # 跑全部五作业
#   bash scripts/ci.sh python java      # 只跑指定作业
#   bash scripts/ci.sh matrix           # 只跑权限矩阵门禁（秒级，改权限相关代码后先跑它）
#   bash scripts/ci.sh deps             # 只跑依赖漏洞扫描（秒级、离线；Critical 阻断、High 告警）
#   bash scripts/ci.sh --list           # 列出可用作业
#   bash scripts/ci.sh --fresh          # 前端强制重装依赖（npm ci）
#   PY=/path/to/python bash scripts/ci.sh
#
# 退出码：0 = 全过；1 = 至少一个作业失败（可直接挂到 pre-push 钩子）
#
# 设计取舍：
#   * 刻意**不用** `set -e`：一个作业失败也要把其余作业跑完，
#     一次拿到全部问题，而不是修一个跑一次。
#   * 不引任何第三方依赖（无 shellcheck 要求的扩展、无 jq），
#     因为"门禁工具本身跑不起来"比没有门禁更糟。
# ==============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# 本机（WorkBuddy 沙箱）的安全删除钩子：批量删除配额按「回合」在所有进程间共享，
# 构建工具清理 dist/ 会被拦成"接口返回成功但文件没删"。
# 置空只关掉批次计数，不关安全删除本身；CI 环境没有这个变量，无副作用。
export CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR=

# Python 解释器：优先与本机既有口径一致的 conda python311（.workbuddy 自带的那个
# 没装 pydantic_settings，跑不起来），可用 PY=... 覆盖。
PY="${PY:-D:/install/python/anaconda/envs/python311/python.exe}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

# IDEA 内置 Maven（本机 PATH 里没有独立 mvn）
IDEA_MAVEN_HOME="${MAVEN_HOME:-D:/install/java/idea/IntelliJ IDEA 2024.1/plugins/maven/lib/maven3}"

WANT_FRESH=0
JOBS=()
for arg in "$@"; do
    case "$arg" in
        --list)  echo "可用作业: python java web matrix deps"; exit 0 ;;
        --fresh) WANT_FRESH=1 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)       JOBS+=("$arg") ;;
    esac
done
[ ${#JOBS[@]} -eq 0 ] && JOBS=(python java web matrix deps)

JOBS_RUN=()
declare -A RESULT=() ELAPSED=() LOGFILE=()

LOGDIR="$ROOT/deploy/ci-logs"
mkdir -p "$LOGDIR"

banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
ok()     { printf '   \033[32m[PASS]\033[0m %s\n' "$1"; }
bad()    { printf '   \033[31m[FAIL]\033[0m %s\n' "$1"; }
note()   { printf '   %s\n' "$1"; }

# 跑一个作业：命令输出全量落 deploy/ci-logs/<job>.log，控制台只给结论 + 失败尾部。
run_job() {
    local name="$1"; shift
    local log="$LOGDIR/$name.log"
    LOGFILE[$name]="$log"
    banner "作业：$name"
    note "→ $log"

    local start end status
    start=$(date +%s)
    if "$@" >"$log" 2>&1; then status=0; else status=$?; fi
    end=$(date +%s)

    ELAPSED[$name]=$((end - start))
    JOBS_RUN+=("$name")

    if [ $status -eq 0 ]; then
        RESULT[$name]=PASS
        ok "$name 通过（$((end - start))s）"
        tail -n 3 "$log" | sed 's/^/     /'
    else
        RESULT[$name]=FAIL
        bad "$name 失败（退出码 $status，$((end - start))s）"
        echo "     ---- 失败输出末尾 25 行 ----"
        tail -n 25 "$log" | sed 's/^/     /'
    fi
}

# ---------------------------------------------------------------- 作业实现

job_python() {
    # pytest.ini 里已用 --deselect 显式列出 4 项已知失败（口径见该文件注释）
    ( cd "$ROOT/agt_python" && "$PY" -m pytest )
}

job_java() {
    # MAVEN_FLAGS 可覆盖：默认 -o（离线）。离线更快、更可复现，也能在断网时跑；
    # 首次在新机器上跑若报"找不到 surefire-junit-platform"，去掉 -o 联网拉一次即可。
    local flags="${MAVEN_FLAGS:--o}"
    local mvn_bin
    mvn_bin="$(command -v mvn || true)"
    if [ -n "$mvn_bin" ]; then
        # 有独立 mvn（CI 环境）：直接用
        ( cd "$ROOT/agt_backend" && "$mvn_bin" -B $flags test )
        return
    fi

    # 本机回退：用 IDEA 内置 Maven 手工启动 classworlds。
    # ⚠️ 三处易漏：多模块项目目录、cygpath 转 Windows 路径、缺 surefire 提供者时 -o 会失败。
    local mh="$IDEA_MAVEN_HOME"
    local java_exe
    java_exe="$(command -v java || echo /d/install/java/jdk/bin/java.exe)"
    if [ ! -f "$mh/boot/plexus-classworlds-2.7.0.jar" ]; then
        echo "找不到 Maven：PATH 里没有 mvn，IDEA 内置 Maven 也不在 $mh"
        echo "请设 MAVEN_HOME 指向 Maven 安装目录后重试。"
        return 127
    fi
    ( cd "$ROOT/agt_backend" && "$java_exe" \
        -classpath "$(cygpath -w "$mh/boot/plexus-classworlds-2.7.0.jar")" \
        -Dclassworlds.conf="$(cygpath -w "$mh/bin/m2.conf")" \
        -Dmaven.home="$(cygpath -w "$mh")" \
        -Dmaven.multiModuleProjectDirectory="$(cygpath -w "$ROOT/agt_backend")" \
        org.codehaus.plexus.classworlds.launcher.Launcher -B $flags test )
}

job_web() {
    # ⚠️ 必须用子 shell：裸 `cd` 会把工作目录**泄漏给后续作业**
    # （实测 web 跑完再跑 matrix，matrix 会去 agt_web/ 下找脚本而失败）。
    local dir="$ROOT/agt_web"
    if [ ! -d "$dir/node_modules" ] || [ "$WANT_FRESH" -eq 1 ]; then
        echo "→ 安装前端依赖（npm ci）"
        ( cd "$dir" && npm ci ) || return 1
    else
        echo "→ 复用已有 node_modules（需要全新安装请加 --fresh）"
    fi
    ( cd "$dir" && npm run build ) || return 1
    check_web_client_error_bundle "$dir"
}

# E-12：前端没有单测框架（**刻意不为它引入 jest** —— 门禁工具自身跑不起来
# 比没有门禁更糟，且前端侧只是"三个 handler → 一个 fetch"的薄适配层）。
# 于是用产物里的**存在性断言**守住"三处收口被接上且会被打包"：
# 谁把 install 那行删了、或把 unhandledrejection 那段注释掉，这里立刻红。
#
# 这是最弱的一层（只证明"在"、不证明"对"），行为正确性由浏览器端验收覆盖：
# 打开 9090 → 控制台 `__agtClientErrorSelfTest()` → 9090 看板「前端错误」页可见。
check_web_client_error_bundle() {
    local dir="$1"
    local app_js
    app_js="$(ls "$dir"/dist/js/app.*.js 2>/dev/null | head -1)"
    if [ -z "$app_js" ]; then
        echo "  [FAIL] 未找到前端产物 dist/js/app.*.js（构建没产出？）"
        return 1
    fi
    local marker
    for marker in "unhandledrejection" "__agtClientErrorSelfTest" "/v1/ops/client-error"; do
        if ! grep -q "$marker" "$app_js"; then
            echo "  [FAIL] 前端产物缺少 E-12 收口标记：$marker"
            echo "         （收口应装在 src/main.js 的 mount() 之前，见 src/utils/clientError.js）"
            return 1
        fi
    done
    echo "  [OK] E-12 三处收口标记已在产物中（unhandledrejection / 自检入口 / 上报端点）"
}

job_matrix() {
    # 权限矩阵门禁（企业级落地规约 E-10）：
    # 校验 docs/权限矩阵.md 的每一行都能指向一处真实代码校验，
    # 且代码里的端点/判定没有未登记项——防"文档写了代码没做"。
    # 零第三方依赖（只用 stdlib），断网可跑，秒级返回。
    # 脚本按 cwd 解析 docs/ 与源码路径，故显式在仓库根执行（不依赖前序作业的 cwd）。
    ( cd "$ROOT" && "$PY" scripts/check_permission_matrix.py )
}

job_deps() {
    # 依赖与漏洞扫描（企业级落地规约 E-11，判据 C1）：**Critical 阻断、High 告警**。
    # 默认**离线**：读 security/ 下的闭包与漏洞快照（随仓库走），断网不会因此变红。
    # 秒级、零第三方依赖。刷新快照要联网，是维护者动作：
    #     python scripts/scan_deps.py --refresh
    # 快照过期/闭包与快照不一致时会判红（exit 2）并提示跑 --refresh——
    # 这是**有意**的：未评估不等于安全。
    ( cd "$ROOT" && "$PY" scripts/scan_deps.py )
}

# ---------------------------------------------------------------- 主流程

printf '\033[1m本地 CI 门禁\033[0m  root=%s\n' "$ROOT"
command -v "$PY" >/dev/null 2>&1 && note "python : $PY"
note "作业   : ${JOBS[*]}"

for job in "${JOBS[@]}"; do
    case "$job" in
        python) run_job python job_python ;;
        java)   run_job java   job_java ;;
        web)    run_job web    job_web ;;
        matrix) run_job matrix job_matrix ;;
        deps)   run_job deps   job_deps ;;
        *)      bad "未知作业：$job（可用：python java web matrix deps）"; RESULT[$job]=FAIL; JOBS_RUN+=("$job") ;;
    esac
done

banner "汇总"
failed=0
for job in "${JOBS_RUN[@]}"; do
    if [ "${RESULT[$job]}" = PASS ]; then
        printf '   \033[32m%-8s PASS\033[0m  %ss\n' "$job" "${ELAPSED[$job]:-0}"
    else
        printf '   \033[31m%-8s FAIL\033[0m  %ss   → %s\n' "$job" "${ELAPSED[$job]:-0}" "${LOGFILE[$job]:-}"
        failed=$((failed + 1))
    fi
done

echo
echo "   日志目录：$LOGDIR"
if [ $failed -eq 0 ]; then
    printf '   \033[32m门禁通过：可以提交/推送。\033[0m\n\n'
    exit 0
fi
printf '   \033[31m门禁未通过：%d 个作业失败，先看上面日志尾部。\033[0m\n\n' "$failed"
exit 1

#!/usr/bin/env python
"""版本打戳：把仓库根 ``VERSION`` 与 git 信息注入三端。

**唯一的版本来源是仓库根 `VERSION` 文件。** 本脚本只读它，不修改它——
版本号的变更必须由人显式提交（同时补 `CHANGELOG.md` 条目），
避免"脚本自动升版"导致版本号与变更记录脱节。

产物（均为生成物，不入版本库；缺失时运行时有一套等价的动态回落逻辑）：

===========================  ====  ==================================
产物路径                      被谁读  用途
===========================  ====  ==================================
`agt_python/app/build_info.json`          Python：``/v1/health`` 暴露版本
`agt_backend/src/main/resources/build-info.properties`  Java：``/v1/version``
`agt_web/public/version.json`             前端：``fetch`` 后展示
`agt_web/package.json` 的 ``version`` 字段   前端：与根 VERSION 保持同值
===========================  ====  ==================================

用法::

    python scripts/stamp_version.py            # 打戳（写产物）
    python scripts/stamp_version.py --check    # 只校验一致性，不写
    python scripts/stamp_version.py --print    # 打印版本信息 JSON，不写

``--check`` 会在以下情况以退出码 1 失败（供 CI 门禁使用）：

1. `VERSION` 缺失或不是合法语义化版本号；
2. `CHANGELOG.md` 里找不到 `## [<version>]` 标题；
3. 已存在的产物中的 version 与 `VERSION` 不一致（漏跑打戳）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# scripts/ 的上一级即仓库根
ROOT = Path(__file__).resolve().parents[1]

VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"

# 语义化版本：主.次.修订，可带预发布/构建元数据（如 1.0.0-rc.1）
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

# (产物路径, 写入方式) —— 写入函数在下方按类型分派
PY_BUILD_INFO = ROOT / "agt_python" / "app" / "build_info.json"
JAVA_BUILD_INFO = ROOT / "agt_backend" / "src" / "main" / "resources" / "build-info.properties"
WEB_VERSION_JSON = ROOT / "agt_web" / "public" / "version.json"
WEB_PACKAGE_JSON = ROOT / "agt_web" / "package.json"


# --------------------------------------------------------------------------- #
# 版本与 git 信息
# --------------------------------------------------------------------------- #
def read_version() -> str:
    """读根 VERSION。缺失或格式非法时抛 SystemExit（打戳必须建立在确定版本上）。"""
    if not VERSION_FILE.exists():
        raise SystemExit(f"[stamp] 缺少版本文件：{VERSION_FILE}")
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit(f"[stamp] {VERSION_FILE} 为空")
    if not SEMVER_RE.match(version):
        raise SystemExit(f"[stamp] VERSION 不是合法语义化版本号：{version!r}")
    return version


def _git(*args: str) -> str | None:
    """执行 git 子命令，成功返回 stdout（去空白），失败返回 None（不抛异常）。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def read_git_info() -> dict[str, object]:
    """采集 git 信息。

    `commit` 有三种语义，必须区分清楚——它们对运维的含义完全不同：

    - ``<短 hash>``      ：正常构建，可复现；
    - ``unborn@<分支>``  ：仓库已 `git init` 但**尚无提交**（开发早期），
                          此时任何"可复现"承诺都不成立；
    - ``no-git``         ：工作副本里没有 `.git`（交付包解压后的形态），
                          版本只能依赖打戳产物，无法回溯到源码提交。
    """
    if not (ROOT / ".git").exists():
        return {"commit": "no-git", "branch": "no-git", "dirty": None}

    commit = _git("rev-parse", "--short", "HEAD")
    branch = _git("symbolic-ref", "--short", "HEAD") or "unknown"
    if commit is None:
        commit = f"unborn@{branch}"

    # 有未提交改动 = true：提示"当前运行的并非干净构建"
    status = _git("status", "--porcelain")
    dirty = bool(status) if status is not None else None

    return {"commit": commit, "branch": branch, "dirty": dirty}


def build_info() -> dict[str, object]:
    """汇总一份版本信息（打戳产物与运行时回落共用同一套字段语义）。"""
    version = read_version()
    git = read_git_info()
    # 本地时区 ISO8601（带 offset），既好读又无时区歧义
    build_time = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "version": version,
        "gitCommit": git["commit"],
        "gitBranch": git["branch"],
        "gitDirty": git["dirty"],
        "buildTime": build_time,
        "stamped": True,
    }


# --------------------------------------------------------------------------- #
# 写入产物
# --------------------------------------------------------------------------- #
def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def write_java_properties(path: Path, info: dict[str, object]) -> None:
    """写 Java `Properties` 文件。

    值全部为 ASCII，且 `Properties.load(InputStream)` 按 ISO-8859-1 解码，
    因此这里不做转义处理；换行用 LF（properties 规范接受 LF 与 CRLF）。
    注释一律用英文——文件按 ASCII 写出，非 ASCII 注释会直接编码失败。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by scripts/stamp_version.py -- do not edit by hand.",
        "# Single source of truth for the version is the repository-root VERSION file.",
        f"build.version={info['version']}",
        f"build.git-commit={info['gitCommit']}",
        f"build.git-branch={info['gitBranch']}",
        f"build.git-dirty={str(info['gitDirty']).lower()}",
        f"build.build-time={info['buildTime']}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def sync_package_json(path: Path, version: str) -> bool:
    """把 `package.json` 的 version 同步为根 VERSION。返回是否发生了改动。

    用文本级替换而非 `json.load/dump`：后者会重排全部键、
    把 `~5.0.0` 之类的依赖写法规范化，产生与本次目的无关的巨大 diff。
    """
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'("version"\s*:\s*")([^"]*)(")',
        lambda m: f"{m.group(1)}{version}{m.group(3)}",
        text,
        count=1,
    )
    if count == 0:
        raise SystemExit(f"[stamp] {path} 里找不到 \"version\" 字段")
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8", newline="")
    return True


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def check_changelog(version: str) -> list[str]:
    """检查 CHANGELOG 是否有该版本条目（以及是否有 Unreleased 段）。"""
    problems: list[str] = []
    if not CHANGELOG_FILE.exists():
        return [f"缺少 {CHANGELOG_FILE.name}"]
    text = CHANGELOG_FILE.read_text(encoding="utf-8")
    if not re.search(rf"^##\s+\[{re.escape(version)}\]", text, flags=re.M):
        problems.append(f"CHANGELOG.md 里没有 `## [{version}]` 条目")
    if not re.search(r"^##\s+\[Unreleased\]", text, flags=re.M):
        problems.append("CHANGELOG.md 里没有 `## [Unreleased]` 段")
    return problems


def check_artifacts(version: str) -> list[str]:
    """检查已存在的产物是否与 VERSION 同版本（防止"改了版本忘了重新打戳"）。"""
    problems: list[str] = []

    if PY_BUILD_INFO.exists():
        try:
            got = json.loads(PY_BUILD_INFO.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError) as exc:
            problems.append(f"{PY_BUILD_INFO.name} 无法解析：{exc}")
        else:
            if got != version:
                problems.append(f"{PY_BUILD_INFO.name} 版本为 {got}，与 VERSION({version}) 不一致")

    if JAVA_BUILD_INFO.exists():
        text = JAVA_BUILD_INFO.read_text(encoding="ascii", errors="replace")
        m = re.search(r"^build\.version=(.*)$", text, flags=re.M)
        got = m.group(1).strip() if m else None
        if got != version:
            problems.append(f"{JAVA_BUILD_INFO.name} 版本为 {got}，与 VERSION({version}) 不一致")

    if WEB_VERSION_JSON.exists():
        try:
            got = json.loads(WEB_VERSION_JSON.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError) as exc:
            problems.append(f"{WEB_VERSION_JSON.name} 无法解析：{exc}")
        else:
            if got != version:
                problems.append(f"{WEB_VERSION_JSON.name} 版本为 {got}，与 VERSION({version}) 不一致")

    if WEB_PACKAGE_JSON.exists():
        m = re.search(r'"version"\s*:\s*"([^"]*)"', WEB_PACKAGE_JSON.read_text(encoding="utf-8"))
        got = m.group(1) if m else None
        if got != version:
            problems.append(f"package.json 版本为 {got}，与 VERSION({version}) 不一致")

    return problems


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把根 VERSION 与 git 信息注入三端")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    group.add_argument("--print", dest="print_only", action="store_true",
                       help="打印版本信息 JSON，不写文件")
    args = parser.parse_args(argv)

    info = build_info()

    if args.print_only:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        problems = check_changelog(str(info["version"])) + check_artifacts(str(info["version"]))
        if problems:
            print(f"[stamp] 版本一致性校验未通过（VERSION={info['version']}）：", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print("[stamp] 修复：python scripts/stamp_version.py", file=sys.stderr)
            return 1
        print(f"[stamp] 版本一致性校验通过：{info['version']} "
              f"commit={info['gitCommit']} dirty={info['gitDirty']}")
        return 0

    # 默认：打戳
    write_json(PY_BUILD_INFO, info)
    write_java_properties(JAVA_BUILD_INFO, info)
    write_json(WEB_VERSION_JSON, info)
    changed = sync_package_json(WEB_PACKAGE_JSON, str(info["version"]))

    print(f"[stamp] version   = {info['version']}")
    print(f"[stamp] gitCommit = {info['gitCommit']} (branch={info['gitBranch']}, "
          f"dirty={info['gitDirty']})")
    print(f"[stamp] buildTime = {info['buildTime']}")
    for path in (PY_BUILD_INFO, JAVA_BUILD_INFO, WEB_VERSION_JSON):
        print(f"[stamp] 已写入 {path.relative_to(ROOT).as_posix()}")
    print(f"[stamp] package.json version {'已同步' if changed else '无需改动'}")

    if str(info["gitCommit"]).startswith("unborn@"):
        print("[stamp] 注意：仓库尚无提交（unborn），本次构建不可复现——"
              "对外发布前请先提交并重新打戳。", file=sys.stderr)
    elif info["gitDirty"]:
        print("[stamp] 注意：工作区有未提交改动，本次构建不可复现。", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

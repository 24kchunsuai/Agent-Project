"""构建版本信息的唯一读取入口（企业级落地规约 E-1，判据 C2）。

要解决的问题
------------
"现在跑的是哪个版本？"——在 E-1 之前，这个问题的答案散在三处
（Python `config.app_version` 硬编码 `0.1.0`、pom 的 `0.0.1-SNAPSHOT`、
`package.json` 的 `0.1.0`），互不关联，且都不是从源码推出来的。

现在版本号的**唯一来源是仓库根 `VERSION`**，本模块负责把它读成运行时可用的对象。

取值优先级
----------
1. **打戳产物** `app/build_info.json`（`scripts/stamp_version.py` 生成）：
   含构建时刻与当时的 git 状态，是"发布构建"的权威口径。
2. **运行时求值**（产物缺失时，如未跑打戳的开发机器）：
   读仓库根 `VERSION` 取版本号，实时调 git 取提交信息。
   保证 `/v1/health` 在任何情况下都答得出，且**不是硬编码字符串**。
3. **unknown**：连 `VERSION` 都读不到（交付包残缺）——如实报未知，绝不猜。

三种 git 状态的语义（与 `scripts/stamp_version.py` 保持一致）
------------------------------------------------------------
- `<短 hash>`：正常，可复现
- `unborn@<分支>`：仓库已初始化但**尚无提交**——任何"可复现"承诺都不成立
- `no-git`：工作副本没有 `.git`（交付包解压后的形态），只能依赖打戳产物

实现约束
--------
只依赖标准库 + `app.core.paths`（路径唯一入口），**不 import `config`**：
避免与 config 形成循环依赖——本模块要能被 config 反过来使用。
结果按进程缓存（`lru_cache`）：服务是**非 reload** 启动的，
重新打戳后需重启进程才会反映新版本，这与项目既有的重启约定一致。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from app.core.paths import BASE_DIR

#: `agt_python/app` —— 打戳产物所在目录
APP_DIR = BASE_DIR / "app"
#: 仓库根（三端同一仓库，`agt_python` 的上一级）
REPO_ROOT = BASE_DIR.parent

STAMP_FILE = APP_DIR / "build_info.json"
VERSION_FILE = REPO_ROOT / "VERSION"

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    """一次构建的版本画像。字段与打戳产物的 JSON 键一一对应。"""

    version: str
    git_commit: str
    git_branch: str
    git_dirty: bool | None
    build_time: str | None
    #: 取值来源：`stamp`（打戳产物）/ `runtime`（实时求值）/ `unknown`
    source: str

    def as_dict(self) -> dict[str, object]:
        """转成对外 JSON（camelCase，前端与运维直接消费）。"""
        return {
            "version": self.version,
            "gitCommit": self.git_commit,
            "gitBranch": self.git_branch,
            "gitDirty": self.git_dirty,
            "buildTime": self.build_time,
            "versionSource": self.source,
        }

    def describe(self) -> str:
        """一行摘要，用于启动日志。"""
        dirty = "有未提交改动" if self.git_dirty else ("干净" if self.git_dirty is False else "未知")
        return f"{self.version} ({self.git_commit}, {dirty}, source={self.source})"


def _git(*args: str) -> str | None:
    """执行 git 子命令；任何失败都返回 None（版本信息绝不能让服务启动失败）。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        # 交付包里通常没有 git 命令，这是正常情况而非错误
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def read_version_file() -> str:
    """读仓库根 `VERSION`；读不到返回 `unknown`。"""
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    return version or UNKNOWN


def _read_stamp() -> BuildInfo | None:
    """尝试读打戳产物；不存在或格式坏掉返回 None（交由运行时求值兜底）。"""
    try:
        raw = json.loads(STAMP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("version"):
        return None

    dirty = raw.get("gitDirty")
    return BuildInfo(
        version=str(raw["version"]),
        git_commit=str(raw.get("gitCommit") or UNKNOWN),
        git_branch=str(raw.get("gitBranch") or UNKNOWN),
        git_dirty=dirty if isinstance(dirty, bool) else None,
        build_time=raw.get("buildTime"),
        source="stamp",
    )


def _evaluate_at_runtime() -> BuildInfo:
    """未打戳时的实时求值：版本读 VERSION 文件，git 信息现问现答。"""
    version = read_version_file()

    if not (REPO_ROOT / ".git").exists():
        return BuildInfo(version, "no-git", "no-git", None, None, "runtime")

    commit = _git("rev-parse", "--short", "HEAD")
    branch = _git("symbolic-ref", "--short", "HEAD") or UNKNOWN
    dirty_raw = _git("status", "--porcelain")

    return BuildInfo(
        version=version,
        git_commit=commit or f"unborn@{branch}",
        git_branch=branch,
        git_dirty=None if dirty_raw is None else bool(dirty_raw),
        build_time=None,
        source="runtime",
    )


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    """取当前构建信息（按进程缓存；重新打戳后需重启进程）。"""
    return _read_stamp() or _evaluate_at_runtime()

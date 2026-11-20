"""健康探针。Java 后端与运维可直接轮询此接口。

除存活状态外，这里同时回答运维最常问的两个问题（企业级落地规约 E-1，判据 C2）：

- **跑的是哪个版本**（`version`，唯一来源是仓库根 `VERSION`）
- **这份构建能不能复现**（`gitCommit` / `gitDirty`：脏工作区或 unborn 仓库
  都意味着"当前进程跑的代码并不等于任何一个已提交状态"）

版本字段由 `app.core.build_info` 提供，**不是硬编码字符串**：优先取打戳产物，
无产物时实时读 VERSION 文件 + 问 git。
"""

import sys
from pathlib import Path

from fastapi import APIRouter

from app.core.build_info import get_build_info
from app.core.config import settings
from app.core.response import ApiResponse, ok

router = APIRouter(tags=["health"])


def _runtime_env() -> str:
    """识别当前进程所在的运行环境目录名（如 conda 的 python311）。"""
    parent = Path(sys.executable).parent
    # Windows 虚拟环境 / conda 环境脚本目录是 Scripts，类 Unix 是 bin
    if parent.name.lower() in {"scripts", "bin"}:
        return parent.parent.name
    return parent.name


@router.get("/health", response_model=ApiResponse, summary="存活探针")
async def health() -> ApiResponse:
    build = get_build_info()
    return ok(
        {
            "status": "up",
            "app": settings.app_name,
            # ---- 构建画像（E-1） ----
            "version": build.version,
            "gitCommit": build.git_commit,
            "gitBranch": build.git_branch,
            "gitDirty": build.git_dirty,
            "buildTime": build.build_time,
            "versionSource": build.source,
            # ---- 运行环境 ----
            "llmConfigured": bool(settings.llm_api_key),
            "python": sys.version.split()[0],
            "pythonEnv": _runtime_env(),
        }
    )

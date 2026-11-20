"""验收脚本用的服务间鉴权头（企业级落地规约 §S-1）。

## 背景
S-1 之后，Python 服务除探针（`/v1/health`、`/`、`/docs`、`/openapi.json`）外
**全部要求**请求头 `X-Agent-Key`。于是所有**直连** 8000 / 隔离实例（8001…）的
验收脚本都必须带上同一个密钥，否则会拿到一片 4010。

⚠️ 走 8080（Java 透传）的脚本**不需要**用它：Java 的 AgentClient 会统一加头。

## 密钥来源（按优先级）
1. 进程环境变量 `AGENT_SHARED_KEY`；
2. `agt_python/.env` 里的 `AGENT_SHARED_KEY`（与服务端同一份文件）。

## 为什么不用 `app.core.config.settings`
settings 从**当前工作目录**的 `.env` 读；而验收脚本可能被人从别的目录、或从 CI
里调用，那时读出空串，表现为"所有请求 4010"，看起来像服务端配置坏了 —— 排查
成本高。这里按**脚本自身位置**定位 `.env`，与 cwd 无关。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: 仓库内 agt_python 根（本文件在 agt_python/scripts/ 下）
PYTHON_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = PYTHON_ROOT / ".env"
_ENV_NAME = "AGENT_SHARED_KEY"


def agent_shared_key() -> str:
    """取服务间共享密钥；都取不到则返回空串（调用方会收到 4010，错误现象明确）。"""
    from_env = (os.environ.get(_ENV_NAME) or "").strip()
    if from_env:
        return from_env
    try:
        text = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(rf"^{_ENV_NAME}=(.*)$", text, flags=re.M)
    return (match.group(1).strip() if match else "")


def key_headers() -> dict[str, str]:
    """httpx / requests / urllib 通用的鉴权头。"""
    return {"X-Agent-Key": agent_shared_key()}

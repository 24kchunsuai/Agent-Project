"""用户画像接口（docs/specs/会话记忆规约.md §6 M-5）。

画像的知情权与控制权（规约验收明确要求"可查看、可删除"）：
  GET    /v1/profile/{user_id}        查看全部画像条目（含来源/置信度/时间）
  DELETE /v1/profile/{user_id}/{key}  删除单条（如猜错的 preference_style）
  DELETE /v1/profile/{user_id}        清空该用户画像

抽取开关在配置层（settings.profile_enabled / .env），不在 API 层——
画像猜错的根治手段是"删掉 + 关开关"，两者分离符合最小权限。

调用链：Vue(9090) → Java agt_backend(8080) → 本服务(8000)，权限在 Java 层收口
（与既有 kb 接口同一约定），Python 侧不加鉴权，便于本地直调调试。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Path

from app.core.response import ApiResponse, StatusCode, ok
from app.services.user_profile import profile_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["profile"])

USER_DESC = "用户 ID（登录用户的数字 ID，与聊天请求里的 userId 一致）"


@router.get("/{user_id}", summary="查看用户画像")
def get_profile(user_id: int = Path(description=USER_DESC)):
    """全部画像条目。列表含 key/value/source/confidence/updatedTs。"""
    try:
        return ok(profile_store.get_all(user_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("画像查询失败 user=%s", user_id)
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"画像库暂不可用：{type(e).__name__}", data=None)


@router.delete("/{user_id}/{key}", summary="删除单条画像")
def delete_profile_key(user_id: int = Path(description=USER_DESC),
                       key: str = Path(description="画像键名，如 preference_style / topics")):
    try:
        removed = profile_store.delete(user_id, key)
        return ok({"removed": removed})
    except Exception as e:  # noqa: BLE001
        logger.exception("画像删除失败 user=%s key=%s", user_id, key)
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"画像库暂不可用：{type(e).__name__}", data=None)


@router.delete("/{user_id}", summary="清空用户画像")
def delete_profile_all(user_id: int = Path(description=USER_DESC)):
    try:
        removed = profile_store.delete(user_id, None)
        return ok({"removed": removed})
    except Exception as e:  # noqa: BLE001
        logger.exception("画像清空失败 user=%s", user_id)
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"画像库暂不可用：{type(e).__name__}", data=None)

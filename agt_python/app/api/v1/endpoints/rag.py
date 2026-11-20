"""RAG 检索接口。

POST /v1/rag/search：混合检索 + rerank + 过滤，供 Java 后端与前端调试调用。
生成（对话回答）由 chat 链路负责，本接口只产出证据，不做 LLM 调用。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.response import ok
from app.rag.glm import GlmApiError
from app.rag.retriever import retriever

logger = logging.getLogger(__name__)
router = APIRouter(tags=["rag"])


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="用户问题")
    user: str | None = Field(
        default=None, max_length=64,
        description="请求方用户标识（owner 鉴权）；不传只检索 public 语料",
    )
    category: str | None = Field(
        default=None, max_length=64,
        description="可选，按分类过滤（如 regulation / concept）",
    )
    top_k: int | None = Field(default=None, ge=5, le=50, description="召回候选数，默认 20")
    min_keep: int | None = Field(default=None, ge=1, le=20, description="过滤保底条数，默认 3")
    max_keep: int | None = Field(default=None, ge=1, le=20, description="过滤封顶条数，默认 8")
    score_floor: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="跨域硬阈值，默认 0.01；top1 低于此值判为完全跨域",
    )


@router.post("/search")
def rag_search(req: RagSearchRequest):
    """检索知识库证据。

    decision 含义：
      kept                  正常返回证据（3~8 条）
      cross_domain          完全跨域，证据为空 → 上层应拒答/转闲聊
      no_hit                检索无任何召回
      insufficient_evidence 证据不足 min_keep 条 → 生成端需谨慎作答或说明
    """
    try:
        data = retriever.search(
            req.query,
            user=req.user,
            top_k=req.top_k,
            min_keep=req.min_keep,
            max_keep=req.max_keep,
            score_floor=req.score_floor,
            category=req.category,
        )
    except GlmApiError as e:
        # GLM 侧不可用（余额/限流）：返回明确错误码，Java 侧可降级为「知识库暂不可用」
        logger.error("GLM 调用失败 | query=%s err=%s", req.query[:50], e)
        from app.core.response import ApiResponse, MESSAGES, StatusCode

        return ApiResponse(
            code=int(StatusCode.AGENT_CALL_ERROR),
            message=f"检索服务暂不可用：{e}",
            data=None,
        )
    return ok(data)

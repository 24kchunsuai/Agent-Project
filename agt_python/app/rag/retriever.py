"""检索服务：混合检索 → rerank → 过滤，RAG 链路的检索段。

链路（与设计文档对齐，实测标定见 deploy/milvus/kb-drone-baseline.json）
----------------------------------------------------------------
  混合检索 top20（语义 dense + 关键词 BM25 各一路，RRF 融合）
  → rerank-pro 全量重排
  → 过滤：
      1) top1 < score_floor(0.01) → 判「完全跨域」，返回空候选
         （该阈值只能拦完全跨域，实测依据 DEV_ISSUES 6.17）
      2) 其余按排名保留 [min_keep, max_keep] 条（默认 3~8）；
         分数低于 score_floor 的片在到达 min_keep 前提前截断，
         避免把跨域噪音塞进生成上下文

「领域内无答案」不在本层判断——实测 rerank 对领域内相似内容打 0.99+，
分数无法区分，必须由生成端 LLM judge / prompt 约束兜底（DEV_ISSUES 6.17）。

鉴权过滤
--------
owner/visibility 字段支持 Milvus 标量过滤（已建 INVERTED 索引）：
  - 匿名/默认：仅可见 visibility == "public"
  - 指定用户：额外放行 owner == <user>
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker

from app.core.config import settings
from app.rag import glm

logger = logging.getLogger(__name__)

# 混合检索取回字段（引用追踪 + 生成上下文所需的最小集合）
OUTPUT_FIELDS = [
    "chunk_id", "doc_id", "doc_title", "heading", "chunk_index",
    "char_start", "char_end", "owner", "visibility", "category",
    "source", "source_type", "doc_version", "doc_updated", "char_len", "text",
]


def _auth_expr(user: str | None) -> str:
    """构造 Milvus 标量过滤表达式（字符串字面量必须用双引号）。"""
    if user:
        return f'(visibility == "public" || owner == "{user}")'
    return 'visibility == "public"'


class RagRetriever:
    """Milvus 混合检索 + GLM rerank + 过滤。线程安全（懒加载连接）。"""

    def __init__(self) -> None:
        self._client: MilvusClient | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> MilvusClient:
        with self._lock:
            if self._client is None:
                self._client = MilvusClient(uri=settings.milvus_uri)
                logger.info("Milvus 已连接 | uri=%s collection=%s",
                            settings.milvus_uri, settings.milvus_collection)
            return self._client

    def search(
        self,
        query: str,
        *,
        user: str | None = None,
        top_k: int | None = None,
        min_keep: int | None = None,
        max_keep: int | None = None,
        score_floor: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """
        执行一次检索。

        参数
        ----
        query       : 用户问题
        user        : 请求方用户标识（owner 鉴权），None 时只看 public
        top_k       : 混合检索候选数，默认 settings.rag_recall_topk(20)
        min_keep/max_keep : 过滤后保留条数上下限，默认 3/8
        score_floor : 跨域硬阈值，默认 settings.rag_score_floor(0.01)
        category    : 可选，按分类过滤（如只搜 regulation）
        """
        t0 = time.time()
        top_k = top_k or settings.rag_recall_topk
        min_keep = min_keep if min_keep is not None else settings.rag_min_keep
        max_keep = max_keep if max_keep is not None else settings.rag_max_keep
        score_floor = score_floor if score_floor is not None else settings.rag_score_floor

        client = self._get_client()

        # ---- 1. 混合检索 ----
        t1 = time.time()
        qvec = glm.embed_query(query)
        expr = _auth_expr(user)
        if category:
            expr = f'({expr} && category == "{category}")'
        # ⚠️ hybrid_search 的过滤必须加在每个 AnnSearchRequest 的 expr 上：
        # 底层 Prepare.search_requests_with_expr 用的是 req.expr，
        # 传给 hybrid_search 的 kwargs 里的 filter 会被静默忽略（实测确认）
        reqs = [
            AnnSearchRequest(
                data=[query], anns_field="sparse",
                param={"metric_type": "BM25"}, limit=top_k, expr=expr,
            ),
            AnnSearchRequest(
                data=[qvec], anns_field="dense",
                param={"metric_type": "COSINE"}, limit=top_k, expr=expr,
            ),
        ]
        hits = client.hybrid_search(
            collection_name=settings.milvus_collection,
            reqs=reqs,
            ranker=RRFRanker(),
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
        )
        cand = [h["entity"] for h in (hits[0] if hits else [])]
        t_retrieve = time.time() - t1

        if not cand:
            return self._result(query, [], "no_hit", t0, t_retrieve, 0.0,
                                top1_score=None)

        # ---- 2. rerank ----
        t2 = time.time()
        ranked = glm.rerank(query, [c["text"] for c in cand])
        t_rerank = time.time() - t2

        top1_score = ranked[0][1] if ranked else 0.0

        # ---- 3. 过滤 ----
        if top1_score < score_floor:
            # 完全跨域：阈值硬兜底（只对「完全跨域」有效，见 6.17）
            return self._result(query, [], "cross_domain", t0, t_retrieve,
                                t_rerank, top1_score)

        kept: list[dict] = []
        for rank, (idx, score) in enumerate(ranked, 1):
            if rank > max_keep:
                break
            if score < score_floor and len(kept) >= min_keep:
                break          # 已够保底条数，后面的低分噪音不再进入上下文
            c = cand[idx]
            kept.append(
                {
                    "rank": rank,
                    "score": round(score, 6),
                    "chunk_id": c.get("chunk_id"),
                    "doc_id": c.get("doc_id"),
                    "doc_title": c.get("doc_title"),
                    "heading": c.get("heading"),
                    "chunk_index": c.get("chunk_index"),
                    "char_start": c.get("char_start"),
                    "char_end": c.get("char_end"),
                    "category": c.get("category"),
                    "source": c.get("source"),
                    "source_type": c.get("source_type"),
                    "char_len": c.get("char_len"),
                    "text": c.get("text"),
                }
            )

        decision = "kept"
        if len(kept) < min_keep:
            # 召回过少：如实标记，由上层决定是否拒答（生成端兜底范畴）
            decision = "insufficient_evidence"
        return self._result(query, kept, decision, t0, t_retrieve, t_rerank,
                            top1_score)

    @staticmethod
    def _result(
        query: str,
        candidates: list[dict],
        decision: str,
        t0: float,
        t_retrieve: float,
        t_rerank: float,
        top1_score: float | None,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "decision": decision,          # kept / cross_domain / no_hit / insufficient_evidence
            "top1_score": top1_score,
            "candidates": candidates,
            "trace": {
                "total_ms": round((time.time() - t0) * 1000),
                "retrieve_ms": round(t_retrieve * 1000),
                "rerank_ms": round(t_rerank * 1000),
                "n_candidates": len(candidates),
                "collection": settings.milvus_collection,
            },
        }


# 模块级单例：FastAPI 多个请求共享连接
retriever = RagRetriever()

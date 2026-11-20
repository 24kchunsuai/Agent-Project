"""GLM embedding / rerank 客户端（同步 httpx）。

设计要点（均为实测踩坑，见 DEV_ISSUES 6.6 / 6.9 / 6.15）：
1. **429 要先看响应体 error.code**：1113=余额不足，重试永远无效，必须直接抛出；
   真限流才指数退避重试
2. **rerank 必须用 rerank-pro**：基础版给无关文档打 0.86~0.95，分数挤在
   0.99999~1.0，任何阈值都失效；rerank-pro 分布拉开到 0.001~0.999
3. **两个模型返回结构不同**：rerank 回 `index`，rerank-pro 回 `document`
   （不回 index，只能靠文本反查，documents 里不能有重复文本）
"""

from __future__ import annotations

import time
from typing import Literal

import httpx

from app.core.config import settings


class GlmApiError(RuntimeError):
    """GLM 接口错误（余额不足 / 重试耗尽等），上层应转 5xx 而非静默降级。"""


def _client() -> httpx.Client:
    return httpx.Client(timeout=60)


def _base_and_key(kind: Literal["embedding", "rerank"]) -> tuple[str, str]:
    if kind == "embedding":
        base = (settings.embedding_base_url or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        key = settings.embedding_api_key
    else:
        base = (settings.rerank_base_url or settings.embedding_base_url
                or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        key = settings.rerank_api_key or settings.embedding_api_key
    if not key:
        raise GlmApiError("缺少 GLM API Key（.env 的 EMBEDDING_API_KEY）")
    return base, key


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化。429 真限流指数退避；1113 余额不足直接抛错。"""
    base, key = _base_and_key("embedding")
    out: list[list[float]] = []
    with _client() as c:
        for bi in range(0, len(texts), settings.embedding_batch):
            batch = texts[bi : bi + settings.embedding_batch]
            for attempt in range(5):
                r = c.post(
                    f"{base}/embeddings",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": settings.embedding_model, "input": batch},
                )
                if r.status_code == 429:
                    body = r.text[:200]
                    if '"1113"' in body or "余额不足" in body:
                        raise GlmApiError("GLM 余额不足（error 1113），请充值")
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    raise GlmApiError(f"embedding HTTP {r.status_code}: {r.text[:200]}")
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
                break
            else:
                raise GlmApiError("embedding 限流重试耗尽")
            time.sleep(settings.embedding_sleep)
    return out


def embed_query(text: str) -> list[float]:
    """单条查询向量化。"""
    return embed_texts([text])[0]


def rerank(
    query: str, documents: list[str], top_n: int | None = None
) -> list[tuple[int, float]]:
    """
    重排，返回 [(原始下标, 相关性分数)] 降序。

    ⚠️ rerank-pro 不回 index 只回 document 原文，靠 `documents.index()` 反查，
    因此 documents 中不能有重复文本（重复时只匹配第一处）。
    """
    if not documents:
        return []
    base, key = _base_and_key("rerank")
    payload = {
        "model": settings.rerank_model,
        "query": query,
        "documents": documents,
        "top_n": top_n or len(documents),
        "return_documents": False,
    }
    body = None
    with _client() as c:
        for attempt in range(4):
            r = c.post(
                f"{base}/rerank",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
            if r.status_code == 429:
                if '"1113"' in r.text or "余额不足" in r.text:
                    raise GlmApiError("GLM 余额不足（error 1113），请充值")
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise GlmApiError(f"rerank HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            break
        else:
            raise GlmApiError("rerank 限流重试耗尽")
    assert body is not None

    out: list[tuple[int, float]] = []
    for it in body["results"]:
        if "index" in it:
            idx = int(it["index"])
        else:
            try:
                idx = documents.index(it["document"])
            except ValueError:
                continue
        out.append((idx, float(it["relevance_score"])))
    return out

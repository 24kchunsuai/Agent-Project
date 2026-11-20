"""历史相关性过滤（会话记忆规约 §6 M-3，缺口 G13）。

职责边界（规约 §6 M-3，与 P0 改写层的分工）
--------------------------------------------
- 改写层（app/rag/rewrite.py）：把查询规范化，**给检索用**；
- 过滤层（本模块）：从会话历史里挑出与当前问题相关的轮次，**给生成用**。
两者输入同为 history，用途不同，互不替代。共享同一个短查询判定。

5 步流水线（对齐 open-agent HistoryFilter，取舍见规约 §2）
----------------------------------------------------------
1. 指代消解：**复用 P0 的 `_needs_llm` 判据**（import 同一份实现，禁止复制——
   两套阈值必然漂移，§6.57 的教训）。命中（含指代词 / 短句追问）时，打分用查询
   = 上一轮 user 消息 + 本轮查询拼接（零 LLM 成本的启发式近似；LLM 消解是
   改写层的职责，不在这里重复花钱）。
2. 轮次分组：**复用 `memory_trim.group_turns`**（同一份实现，不许两套）。
3. 相关性打分：cosine(查询向量, 轮向量)。
   - 向量走 glm.embed_texts **一次批量调用**（查询 + 全部中间轮合并一批），
     进程内 LRU 缓存（键 = 文本 sha1）。缓存只在**读路径**惰性写入——
     `save()` / `record_turn()` 主链路零 embedding 调用（规约铁律：不得像
     open-agent 那样在保存路径同步阻塞 HTTP）。
   - 低于 `memory_filter_min_similarity` 直接归零（防低分堆叠挤占 topK）。
4. topK 筛选：锚点轮（头 `memory_head_turns` + 尾 `memory_tail_turns`，与 M-2
   同一组配置）**无条件保留**，只对中间候选打分；分数 ≥ `memory_filter_min_score`
   的按分数取前 `memory_filter_top_k` 轮。注意即使中间候选 ≤ top_k，
   min_score 仍会淘汰无关轮——topK 是上限不是保底。
5. 按原始时间序重建（**不是按分数排序**——历史顺序是模型理解对话的前提）。

关键词打分项（暂缺，规约 §8 开放问题 1 已按倾向拍板）
------------------------------------------------------
BM25 的中文分词在 **Milvus 服务端**（vector_store.py 的
`analyzer_params={"type": "chinese"}`），Python 侧没有可复用的分词器，
也不为本项新增 jieba 依赖（规约 §5.1：不新增外部依赖）。按规约"暂缺则先只跑
相似度项（权重设 1.0）"路径：**当前打分 = 纯相似度**；
`memory_filter_similarity_weight` 配置保留，将来接中文分词后关键词项
（权重 1 - weight）再启用。

降级（铁律 1 / 3）
------------------
embedding 异常 → 计数（PATH_MEMORY_FILTER_DEGRADED）+ 返回**全量**历史 +
degraded 标记，绝不因过滤层故障让对话拿不到上下文。

已知取舍（与 M-2 摘要的交互，如实记录）
----------------------------------------
过滤发生在裁剪（trim_history）**之前**：被过滤掉的中间轮次若赶上"超预算触发
摘要"，摘要只覆盖幸存轮次。可接受的依据：① 原始历史在 SQLite 里完好
（`_raw_history` 不经过过滤，改写层与排查口径不受影响）；② 摘要随话题切换会
自然重建，偏向当前话题反而省掉为无关内容花的摘要 LLM 成本；③ 数据源不丢，
丢的只是"派生视图"。
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.memory_trim import group_turns

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """过滤结果。未触发过滤时 `messages` 原样返回。"""

    messages: list[dict] = field(default_factory=list)
    was_filtered: bool = False   # 是否真的淘汰了轮次
    dropped_turns: int = 0       # 被淘汰的中间轮数
    scored_turns: int = 0        # 参与打分的中间轮数
    degraded: bool = False       # embedding 失败，已降级为返回全量


# ---- 向量缓存（读路径惰性写入；save 路径零 embedding） ----

_CACHE_MAX = 4096
_vec_cache: dict[str, list[float]] = {}
_cache_order: list[str] = []
_cache_lock = threading.Lock()


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """embedding 打桩点：单测 monkeypatch 本函数即可，不必 mock glm 客户端。"""
    from app.rag.glm import embed_texts

    return embed_texts(texts)


def _embed_cached(texts: list[str]) -> list[list[float]]:
    """带 LRU 缓存的批量向量化：只 embed 未命中的文本，合并成一次 HTTP 批量调用。

    线程模型：找未命中（锁内，快）→ 批量 embed（锁外，HTTP 慢不持锁）→
    回填并读取（锁内）。极端竞态下个别键在读取时被挤出，退化为锁外单条补算
    再读一遍（最多两轮，实际到不了）。
    """
    with _cache_lock:
        missing: list[str] = []
        for t in texts:
            k = _key(t)
            if k not in _vec_cache and t not in missing:
                missing.append(t)

    for _attempt in range(2):
        if not missing:
            break
        vecs = _embed_batch(missing)
        if len(vecs) != len(missing):
            raise ValueError("embedding 返回条数与输入不匹配")
        with _cache_lock:
            for t, v in zip(missing, vecs):
                k = _key(t)
                if k in _vec_cache:
                    continue
                while len(_cache_order) >= _CACHE_MAX:
                    old = _cache_order.pop(0)
                    _vec_cache.pop(old, None)
                _vec_cache[k] = v
                _cache_order.append(k)
        with _cache_lock:
            missing = [t for t in texts if _key(t) not in _vec_cache]

    with _cache_lock:
        return [list(_vec_cache[_key(t)]) for t in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python，维度以模型实际返回为准，不引 numpy）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _turn_text(turn: list[dict]) -> str:
    """轮的打分表示：user + assistant 内容拼接截断（控 embedding 成本与噪声）。"""
    parts = [(m.get("content") or "").strip() for m in turn]
    return "\n".join(p for p in parts if p)[:600]


def _scoring_query(query: str, messages: list[dict]) -> str:
    """① 指代消解（启发式）：短查询/含指代词时，拼上一轮 user 消息增强查询表示。

    判据复用 P0 改写的 `_needs_llm`（跨模块 import 私有名是**刻意的**：
    判定必须只有一份，复制判据早晚漂移）。只增强"打分用的查询表示"，
    不改用户可见文本；LLM 消解是改写层的事。
    """
    from app.rag.rewrite import _needs_llm

    if not query or not _needs_llm(query):
        return query
    prev_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            prev_user = (m.get("content") or "").strip()
            break
    if prev_user and prev_user != query:
        return f"{prev_user}\n{query}"
    return query


def _record_degraded(exc: Exception) -> None:
    """降级计数（铁律 3：降级必须计数）。埋点失败也不能影响主链路。"""
    logger.warning("记忆相关性过滤降级（embedding 异常），本轮返回全量历史: %s", exc)
    try:
        from app.services.telemetry import PATH_MEMORY_FILTER_DEGRADED, telemetry

        telemetry.record_fault(
            PATH_MEMORY_FILTER_DEGRADED,
            detail=f"记忆过滤降级：{type(exc).__name__}: {exc}"[:200],
        )
    except Exception:  # noqa: BLE001
        pass


def filter_history(messages: list[dict], query: str) -> FilterResult:
    """主入口：从历史中挑与 query 相关的轮次（锚点轮无条件保留）。

    任何异常都不抛出——最差返回全量（degraded 标记 + 降级计数），
    主链路绝不因过滤层中断（铁律 1）。
    """
    original = list(messages)
    if not original or not (query or "").strip():
        return FilterResult(messages=original)

    # 总开关：关闭即退回 M-2 行为（全量交给裁剪器）
    if not settings.memory_filter_enabled:
        return FilterResult(messages=original)

    # ② 轮次分组（与裁剪器共用同一实现）
    turns = group_turns(original)
    head_n = max(0, int(settings.memory_head_turns))
    tail_n = max(0, int(settings.memory_tail_turns))
    # 锚点之外没有中间候选 → 无可过滤对象（也覆盖 head+tail ≥ 总轮数的切片防御）
    if len(turns) <= head_n + tail_n:
        return FilterResult(messages=original)
    # ③ 短会话门：轮数不够不触发（过滤无收益，白付 embedding 成本，规约验收 2）
    if len(turns) <= int(settings.memory_filter_min_turns):
        return FilterResult(messages=original)

    head = turns[:head_n]
    tail = turns[len(turns) - tail_n:]
    middle = turns[head_n:len(turns) - tail_n]
    if not middle:
        return FilterResult(messages=original)

    # ① 指代消解 + ③ 打分：查询与中间轮合并成**一次**批量 embedding 调用
    squery = _scoring_query(query.strip(), original)
    try:
        vecs = _embed_cached([squery, *[_turn_text(t) for t in middle]])
    except Exception as exc:  # noqa: BLE001
        _record_degraded(exc)
        return FilterResult(messages=original, degraded=True)
    qvec, tvecs = vecs[0], vecs[1:]

    # 打分：v1 关键词项暂缺（BM25 分词在 Milvus 服务端，Python 侧无现成分词器，
    # 不新增依赖）→ 相似度权重按 1.0 计（规约 §6 M-3 预设路径）。
    scored: list[tuple[int, float]] = []
    for idx, tvec in enumerate(tvecs):
        sim = _cosine(qvec, tvec)
        if sim < settings.memory_filter_min_similarity:
            sim = 0.0  # 归零线：防低分堆叠挤占 topK
        scored.append((idx, sim))

    # ④ topK：分数达 min_score 的按分数取前 top_k 轮
    eligible = [(i, s) for i, s in scored if s >= settings.memory_filter_min_score]
    eligible.sort(key=lambda x: x[1], reverse=True)
    keep_idx = {i for i, _ in eligible[: max(0, int(settings.memory_filter_top_k))]}

    kept = [t for i, t in enumerate(middle) if i in keep_idx]
    dropped = len(middle) - len(kept)
    if dropped <= 0:
        # 中间候选全数入选：不过滤（返回原对象，避免无谓拷贝改变调用方语义）
        return FilterResult(messages=original, scored_turns=len(middle))

    # ⑤ 按原始时间序重建：head / kept / tail 本就按序切片，顺序拼接即时间升序
    out: list[dict] = []
    for t in [*head, *kept, *tail]:
        out.extend(t)

    logger.info(
        "记忆相关性过滤：中间 %d 轮入选 %d 轮（淘汰 %d），分数=%s",
        len(middle), len(keep_idx), dropped,
        ["%.2f" % s for _, s in scored],
    )
    return FilterResult(
        messages=out, was_filtered=True,
        dropped_turns=dropped, scored_turns=len(middle),
    )

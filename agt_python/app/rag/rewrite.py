"""多轮查询改写（Query Rewrite）：口语词典 + 结合历史的指代/省略补全。

背景（为什么需要，实测依据见 DEV_ISSUES 6.4 / 6.25 / 6.37）
------------------------------------------------------------
检索链路的全部环节（BM25 + 向量 → rerank → judge → 生成）此前都直接吃
**用户原始消息**。这在两种情况明显吃亏：

1. **口语/超短查询**：中文分词不认口语复合词。「黑飞」被切开后与语料里的
   「未经批准」「违规飞行」对不上（penalty 篇正文根本没有「黑飞」二字），
   BM25 于是把含「黑」「飞」的无关文档顶上来；「限高」两个字向量语义太空。
   实测这两题期望文档都掉到第 8 名（eval-039 / eval-040）。
2. **多轮指代/省略**：用户第二轮说「那轻型的要求呢？」「罚款多少？」，
   原文拿去检索完全没有上文，等于换了个人在问，召回必然跑偏。

两层结构（先便宜后贵，任一层都能独立生效）
------------------------------------------
  L1 本地词典（方案⑥）—— 纯确定性、零 LLM 成本、零延迟
     口语 → 规范术语映射，外加极短查询补全。读 term_dict.yml。
  L2 LLM 改写（方案⑨）—— 只有在**需要**时才调
     结合 history 做指代消解 / 省略补全 / 术语规范化。
     触发条件：有历史 且 （该轮存在指代、或明显省略）。
     已完整清晰的独立问题不调 LLM，省一次往返。

失败方向（关键）
----------------
改写是**增强层**，不是必经环节。任何一层出问题都退回「上一层的产物」，
最差退回原文——绝不因为改写失败而让用户拿不到答案。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.prompts import load_prompt

logger = logging.getLogger(__name__)

_DICT_PATH = Path(__file__).resolve().parent / "term_dict.yml"

# 指代词：出现这些词说明这一轮的语义依赖上文，需要 LLM 消解
_PRONOUNS = (
    "它", "他", "她", "它们", "这个", "那个", "这些", "那些", "这款", "那款",
    "这", "那", "此", "该", "上述", "前面", "刚才", "上面说的", "你说的",
    "还有呢", "然后呢", "那么", "呢？", "呢?",
)

# 省略型追问：句子很短且只有疑问尾巴（「罚款多少」「那多久」「为什么」）
_OMISSION_Q = re.compile(r"[^，。？！,.?!]{0,8}(多少|多久|多高|多远|几种|哪些|怎么|为什么|"
                         r"什么|是不是|能不能|要不要|可以吗|行吗|呢)[?？。]?$")

# 标点：判断「整句是否就是某个极短词」时先剥掉
_PUNCT = re.compile(r"[\s，。？！、,.?!；;：:\"'“”‘’（）()《》【】\-—_]+")


@dataclass
class RewriteResult:
    """改写产物。original 恒为原文，query 为最终用于检索的查询。"""

    original: str
    query: str
    source: str = "none"            # none / dict / dict_suffix / llm / llm+dict / failed
    hits: list[str] = field(default_factory=list)   # 命中的口语词，供留痕


@lru_cache(maxsize=1)
def _load_dict() -> tuple[dict[str, list[str]], dict[str, str]]:
    """加载口语词典。文件缺失/格式错时返回空表（改写退化为不生效，不影响检索）。"""
    try:
        raw = yaml.safe_load(_DICT_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("口语词典加载失败，跳过本地改写: %s", exc)
        return {}, {}

    terms: dict[str, list[str]] = {}
    for k, v in (raw.get("terms") or {}).items():
        vals = [str(x).strip() for x in (v if isinstance(v, list) else [v])]
        terms[str(k).strip()] = [x for x in vals if x]

    suffix_raw = raw.get("suffix") or {}
    suffix = {str(k).strip(): str(v).strip() for k, v in suffix_raw.items() if v}
    return terms, suffix


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text or "")


def apply_dict(query: str) -> RewriteResult:
    """L1：本地口语词典改写（确定性，无副作用）。

    优先级：极短查询补全 > 子串术语扩写。
    只做「追加规范表述」，不删除原文——原文里可能有词典没收录的具体约束
    （重量、距离、高度数字），删掉反而丢信息。
    """
    terms, suffix = _load_dict()
    if not terms and not suffix:
        return RewriteResult(original=query, query=query)

    # ① 极短查询补全：整句就是某个口语词（剥标点后相等）→ 换成完整问句
    norm = _normalize(query)
    if norm in suffix and len(norm) <= 6:
        return RewriteResult(original=query, query=suffix[norm],
                             source="dict_suffix", hits=[norm])

    # ② 子串术语扩写：命中则把规范表述追加在原文后面
    hits: list[str] = []
    extra: list[str] = []
    for term, repls in terms.items():
        if term and term in query:
            hits.append(term)
            for r in repls:
                if r not in query and r not in extra:
                    extra.append(r)
    if not hits:
        return RewriteResult(original=query, query=query)
    return RewriteResult(original=query, query=f"{query} {' '.join(extra)}",
                         source="dict", hits=hits)


def _needs_llm(query: str) -> bool:
    """判断这一轮是否需要 LLM 改写（没有上文依赖就不必多花一次调用）。"""
    if any(p in query for p in _PRONOUNS):
        return True
    # 极短句 + 疑问尾巴（「罚款多少」「那多久呢」）大概率是省略型追问
    return len(_normalize(query)) <= 12 and bool(_OMISSION_Q.search(query))


def _history_snippet(history: list[dict[str, str]], max_turns: int) -> str:
    """把最近几轮历史压成「用户/助手：」文本，供改写模型消解指代。"""
    lines: list[str] = []
    for m in history[-max_turns:]:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # 助手长回答截断：改写只需要知道「上文在说什么」，不需要全文
        lines.append(f"{role}：{content[:200]}")
    return "\n".join(lines)


async def _llm_rewrite(question: str, history: list[dict[str, str]]) -> str:
    """L2：结合历史做指代消解 / 省略补全。失败返回空串（由上层回退）。"""
    snippet = _history_snippet(history, settings.rag_rewrite_history_turns)
    if not snippet:
        return ""
    messages = [
        {"role": "system", "content": load_prompt("rag_rewrite")},
        {"role": "user", "content": f"上文：\n{snippet}\n\n本轮用户输入：{question}\n"
                                    f"改写："},
    ]
    resp = await llm_client.chat(
        messages,
        temperature=settings.rag_rewrite_temperature,
        max_tokens=settings.rag_rewrite_max_tokens,
    )
    return (resp.content or "").strip()


def _sanitize(text: str, original: str) -> str:
    """清理模型输出：剥引号/前缀、去换行、限长。不合格返回空串（回退原文）。"""
    t = (text or "").strip()
    if not t:
        return ""
    # 去掉「改写：」「改写后：」等前缀
    t = re.sub(r"^(改写后?|结果|输出|Query|查询)\s*[:：]\s*", "", t)
    # 去掉成对引号包裹
    t = t.strip().strip('"\'“”‘’')
    # 多行只取第一行（模型偶尔会带解释）
    t = t.splitlines()[0].strip()
    if not t or len(t) > settings.rag_rewrite_max_chars:
        return ""
    # 明显是「回答问题」而不是改写（过长/含答案口吻）→ 丢弃
    if _normalize(t) == _normalize(original):
        return ""          # 与原文等价，视为未改写
    return t


def _mark_failed(res: RewriteResult, question: str, reason: str) -> RewriteResult:
    """LLM 层未生效：保留词典层成果，但把 source 标成 failed 以便埋点留痕。

    词典层若已改写成功，就仍算有效改写（dict 系列）——只丢弃 LLM 那一层。
    """
    from app.services.telemetry import PATH_RAG_REWRITE_FAILED, telemetry

    telemetry.record_fault(PATH_RAG_REWRITE_FAILED, detail=f"改写未生效，已回退（{reason[:120]}）")
    if res.source != "none":
        return res
    return RewriteResult(original=question, query=question, source="failed",
                         hits=list(res.hits))


async def rewrite_query(question: str, history: list[dict[str, str]] | None = None) -> RewriteResult:
    """改写入口：本地词典 → （按需）LLM。

    任何一步异常都不抛出——最差返回原样的 RewriteResult(original, original)。
    """
    question = (question or "").strip()
    if not question or not settings.rag_rewrite_enabled:
        return RewriteResult(original=question, query=question)

    # ---- L1 本地词典：永远先跑，零成本 ----
    res = apply_dict(question)

    # ---- L2 LLM：仅在有上文依赖时调用 ----
    history = history or []
    use_llm = settings.rag_rewrite_llm_enabled and bool(history) and _needs_llm(question)
    if not use_llm:
        if res.source != "none":
            logger.info("查询改写[词典] %s → %s", question[:30], res.query[:50])
        return res

    try:
        raw = await _llm_rewrite(question, history)
        cleaned = _sanitize(raw, question)
    except LLMError as exc:
        logger.warning("查询改写 LLM 失败，回退上一层结果: %s", exc)
        return _mark_failed(res, question, f"LLMError: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询改写异常，回退上一层结果: %s: %s", type(exc).__name__, exc)
        return _mark_failed(res, question, f"{type(exc).__name__}: {exc}")

    if not cleaned:
        # 模型输出不合格（空/过长/与原句等价）——也算一次未生效的改写尝试
        return _mark_failed(res, question, "LLM 输出不合格，已回退")

    # LLM 结果再过一遍词典：模型可能仍留下口语词（双保险）
    l2 = apply_dict(cleaned)
    source = "llm+dict" if l2.source != "none" else "llm"
    hits = list(dict.fromkeys(res.hits + l2.hits))
    logger.info("查询改写[%s] %s → %s", source, question[:30], l2.query[:60])
    return RewriteResult(original=question, query=l2.query, source=source, hits=hits)

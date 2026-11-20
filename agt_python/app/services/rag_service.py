"""RAG 问答服务：检索证据 → LLM judge → 带证据生成（或如实拒答）。

链路（对齐链路设计文档与 DEV_ISSUES 6.17 实测结论）：

    用户问题 → 意图(rag) → 检索服务(retriever)
      ├─ cross_domain / no_hit      → fallback：降级闲聊（问题不在知识域）
      ├─ 检索异常(Milvus/GLM 挂)     → fallback：降级闲聊（检索失败 ≠ 会话失败）
      └─ kept → LLM judge 评证据充分性
            ├─ sufficient=false     → refusal：如实拒答（领域内无答案检索端
            │                          拦不住，只能生成端兜底）
            └─ sufficient=true      → generate：rag_system 提示词 + 证据生成
                （judge 调用/解析失败 → 跳过评审继续生成，由生成 prompt 的
                  「证据不足如实说明」条款兜底）

为何需要独立的 judge 一次调用：实测 rerank 对「领域内相似但无答案」的内容
打 0.99+ 高分（KV值→电机结构），分数无法区分；生成模型又倾向于硬答。
judge 是低成本的独立视角（temperature=0，只输出 JSON）。

prepare/answer 两段式：prepare 完成「检索+judge+提示词装配」，非流式 answer()
直接复用；流式 chat_stream 在 prepare 后对生成段做 SSE。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.prompts import load_prompt
from app.rag.glm import GlmApiError
from app.rag.retriever import retriever
from app.rag.rewrite import rewrite_query
from app.services.telemetry import (
    PATH_RAG_DEGRADED, PATH_RAG_REFUSED, PATH_RAG_REWRITE_FAILED, telemetry,
)

logger = logging.getLogger(__name__)

# prepare 的三种结局
PrepMode = Literal["generate", "refusal", "fallback"]


@dataclass
class RagPrep:
    """prepare 阶段产物：生成所需的一切都在这里，路由层据此分派。"""

    mode: PrepMode
    decision: str = ""                       # kept / cross_domain / no_hit / insufficient_evidence / degraded
    judged: str = ""                         # sufficient / insufficient / skipped
    system_prompt: str = ""                  # mode=generate 时的 system 提示词
    history: list[dict[str, str]] = field(default_factory=list)
    question: str = ""
    refusal_content: str = ""                # mode=refusal 时的固定回复
    citations: list[dict] = field(default_factory=list)
    # 查询改写留痕（DEV_ISSUES 6.37）：检索实际用的查询 vs 用户原话
    search_query: str = ""                   # 实际送进检索的查询（未改写时 == question）
    rewrite_source: str = "none"             # none / dict / dict_suffix / llm / llm+dict


@dataclass
class RagAnswer:
    content: str
    citations: list[dict] = field(default_factory=list)
    decision: str = ""
    judged: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    rag_rewrite: str = ""                    # 查询改写来源（DEV_ISSUES 6.37 埋点用）


def _build_evidence_block(candidates: list[dict]) -> str:
    """把检索结果装配成提示词里的证据区（带编号与来源，供引用标注）。"""
    lines: list[str] = []
    for c in candidates:
        src = f"{c.get('doc_title') or c.get('doc_id', '?')}"
        if c.get("heading"):
            src += f" · {c['heading']}"
        lines.append(f"[{c['rank']}] 来源：{src}（相关度 {c['score']:.2f}）")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines).strip()


def _judge_parse(text: str) -> dict | None:
    """从 judge 输出提取判定结果；解析失败返回 None（交上层降级）。

    返回 {"sufficient": bool, "covered": [...], "missing": [...]}；
    旧版 judge 只输出 sufficient 也能兼容（covered/missing 缺省空表）。
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    value = data.get("sufficient")
    if isinstance(value, bool):
        sufficient = value
    elif isinstance(value, str):
        sufficient = value.strip().lower() == "true"
    else:
        return None

    def _strs(key: str) -> list[str]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()][:4]

    return {
        "sufficient": sufficient,
        "covered": _strs("covered"),
        "missing": _strs("missing"),
    }


def _citations(candidates: list[dict]) -> list[dict]:
    """引用列表：给前端展示「来源」用，只挑去重后的文档级条目。"""
    seen: set[str] = set()
    out: list[dict] = []
    for c in candidates:
        if c["doc_id"] in seen:
            continue
        seen.add(c["doc_id"])
        out.append({
            "docId": c["doc_id"],
            "docTitle": c["doc_title"],
            "score": c["score"],
            "category": c.get("category") or "",
        })
    return out


def _coverage_section(covered: list[str], missing: list[str]) -> str:
    """把 judge 的覆盖清单装配成生成提示词里的硬约束区块（v0.3 新增）。

    教训（6.25）：partial_coverage 题 judge 判充分后，生成端只靠自觉遵守
    「部分覆盖要如实说明」条款，实测仍会编造（理赔流程/机型报价表），
    甚至用 [编号] 给编造内容镀金。把「哪些没覆盖」逐条列明后，遵从变成
    机械动作而不是模型自行判断。
    """
    if not covered and not missing:
        return ""
    lines = ["", "## 本题覆盖情况（评审员判定，必须逐条遵守）", ""]
    if covered:
        lines.append(f"- 证据已覆盖：{'、'.join(covered)}——这些部分按证据作答并标注来源。")
    if missing:
        lines.append(f"- 证据未覆盖：{'、'.join(missing)}——"
                     "这些方面**一个字都不许答**，不得给出任何步骤/材料/数字/型号/价格，"
                     '必须明确写"知识库中暂未收录关于……的说明"。'
                     "它们才是本问题的关键部分时，也只能如实说明，不能用近似内容冒充。")
    lines.append("")
    lines.append("上面未覆盖清单具有一票否决权：即使你\"知道\"答案，只要它出现在未覆盖清单里，"
                 "就只允许说明未收录。禁止给任何编造内容标注 [编号]——来源标注只能放在证据真实支持的句子上。")
    return "\n".join(lines)


def _refusal_reply(question: str, candidates: list[dict]) -> str:
    """领域内无答案的如实拒答：不装懂，指出知识库现状。"""
    topics = "、".join(dict.fromkeys(
        c.get("doc_title") or c.get("doc_id", "") for c in candidates[:3]
    ))
    return (
        "关于这个问题，我查了无人机知识库，现有内容只覆盖了相关主题"
        f"（如{topics}），但没有能直接回答「{question[:40]}」的资料。"
        "为了避免给你不准确的信息，这个问题我先如实说明：知识库暂未收录。\n\n"
        "你可以换个角度问问我知识库里有的话题，或者查阅官方技术资料。"
    )


def _out_of_scope_reply() -> str:
    """完全跨域的如实拒答（固定话术，不调模型——零编造风险、零成本）。

    实测教训：跨域题降级给闲聊人设会直接编造（游戏名/菜谱/价格），
    闲聊人设没有知识边界感，拒答必须在检索层给出确定性回复。
    """
    return (
        "这个问题超出了我的知识库范围，为了不给你不准确的信息，我如实说明：我目前"
        "专注于无人机领域的知识问答，能回答的方向包括——\n"
        "- 法规政策：实名登记、执照考取、空域申请、禁飞限飞\n"
        "- 飞行操作：起飞降落流程、安全规范、异常处置\n"
        "- 设备知识：机型参数、动力系统、电池与图传、维护保养\n"
        "- 行业应用：测绘、巡检、植保、航拍等场景方案\n\n"
        "欢迎换一个无人机相关的问题问我；上面这个问题建议咨询对应领域的专业渠道。"
    )


class RagAnswerService:
    """一次 RAG 问答的完整编排。失败方向全部收敛为「降级」而非报错。"""

    async def prepare(self, question: str, history: list[dict[str, str]]) -> RagPrep:
        """检索 + judge + 提示词装配。除「拒答/降级」外，产出可直接生成的 system prompt。"""
        # ---- 0. 查询改写（DEV_ISSUES 6.37）----
        # 放在检索之前：口语词（黑飞/限高）与多轮指代（那轻型的要求呢）都会让
        # BM25/向量双路跑偏。改写失败一律回退原文，绝不阻断检索。
        rw = await rewrite_query(question, history)

        # ---- 1. 检索（retriever 是同步实现，丢线程池避免卡事件循环）----
        try:
            result = await asyncio.to_thread(retriever.search, rw.query)
        except Exception as exc:  # noqa: BLE001
            # 检索链路任何故障（Milvus 连不上 / GLM 余额 / 网络超时）都降级闲聊，
            # 绝不把 5xx 抛给用户——知识库不可用不该拖垮整个会话
            logger.error("RAG 检索失败，降级闲聊: %s: %s", type(exc).__name__, exc)
            telemetry.record_fault(
                PATH_RAG_DEGRADED, detail=f"本应正常检索，实际降级闲聊"
                                          f"（原因：{type(exc).__name__}: {exc}）",
            )
            return RagPrep(mode="fallback", decision="degraded",
                           search_query=rw.query, rewrite_source=rw.source)

        decision = result["decision"]
        if decision in ("cross_domain", "no_hit"):
            # 问题不在知识域：固定话术如实拒答（v0.2 起）——
            # 曾降级给闲聊人设，实测会编造（游戏名/菜谱），跨域拒答必须是确定性回复
            logger.info("RAG 判定 %s（top1=%s），跨域拒答", decision, result["top1_score"])
            telemetry.record_fault(
                PATH_RAG_REFUSED, detail=f"本应知识问答，实际拒答（原因：{decision}）",
            )
            return RagPrep(
                mode="refusal",
                decision=decision,
                question=question,
                refusal_content=_out_of_scope_reply(),
                search_query=rw.query,
                rewrite_source=rw.source,
            )

        candidates = result["candidates"]
        evidence_block = _build_evidence_block(candidates)
        cites = _citations(candidates)

        # ---- 2. LLM judge：证据是否足以回答 ----
        judged = "skipped"
        coverage: tuple[list[str], list[str]] = ([], [])
        if settings.rag_judge_enabled:
            try:
                judge_messages = [
                    {"role": "system", "content": load_prompt("rag_judge")},
                    # v0.3：给 judge 完整证据块而非 120 字摘要——
                    # 摘要截断会让覆盖清单误报 missing（微型/轻型免执照在库却判缺失），
                    # 生成端被一票否决拦住造成过度拒答（eval-003 实测）
                    {"role": "user", "content": f"问题：{question}\n证据：{evidence_block}"},
                ]
                jr = await llm_client.chat(judge_messages, temperature=0.0, max_tokens=320)
                verdict = _judge_parse(jr.content)
                if verdict is not None and verdict["sufficient"] is False:
                    logger.info("judge 判证据不足，拒答 | question=%s", question[:40])
                    telemetry.record_fault(
                        PATH_RAG_REFUSED,
                        detail="本应生成回答，实际拒答（原因：judge 判证据不足）",
                    )
                    return RagPrep(
                        mode="refusal",
                        decision="insufficient_evidence",
                        judged="insufficient",
                        question=question,
                        refusal_content=_refusal_reply(question, candidates),
                        citations=cites,
                        search_query=rw.query,
                        rewrite_source=rw.source,
                    )
                if verdict is not None:
                    judged = "sufficient"
                    coverage = (verdict["covered"], verdict["missing"])
                # verdict=None（解析失败）→ 跳过评审继续生成，生成 prompt 条款兜底
            except LLMError as exc:
                # judge 只是增强层，挂了不阻断回答，交给生成 prompt 的约束条款兜底
                logger.warning("judge 调用失败，跳过评审直接生成: %s", exc)

        # ---- 3. 装配生成提示词（judge 有覆盖清单时追加硬约束区块）----
        system = load_prompt("rag_system").replace("{evidence_block}", evidence_block)
        section = _coverage_section(*coverage)
        if section:
            system = system.rstrip() + "\n\n" + section
        return RagPrep(
            mode="generate",
            decision=decision,
            judged=judged,
            system_prompt=system,
            history=history,
            question=question,
            citations=cites,
            search_query=rw.query,
            rewrite_source=rw.source,
        )

    def build_generate_messages(self, prep: RagPrep) -> list[dict[str, str]]:
        """由 prep 装配发给 LLM 的 messages（非流式/流式共用）。"""
        messages: list[dict[str, str]] = [{"role": "system", "content": prep.system_prompt}]
        messages.extend(prep.history[-6:])     # 只带近几轮，证据已占大半上下文
        messages.append({"role": "user", "content": prep.question})
        return messages

    async def answer(self, question: str, history: list[dict[str, str]]) -> RagAnswer:
        """非流式入口：prepare + 一次生成。"""
        prep = await self.prepare(question, history)

        if prep.mode == "refusal":
            return RagAnswer(
                content=prep.refusal_content,
                citations=prep.citations,
                decision=prep.decision,
                judged=prep.judged,
                rag_rewrite=prep.rewrite_source,
            )
        if prep.mode == "fallback":
            return RagAnswer(content="", decision=prep.decision, judged=prep.judged,
                             rag_rewrite=prep.rewrite_source)

        try:
            gen = await llm_client.chat(
                self.build_generate_messages(prep), temperature=0.3, max_tokens=1024
            )
        except LLMError:
            raise                                # 生成失败交给上层统一处理
        return RagAnswer(
            content=gen.content,
            citations=prep.citations,
            decision=prep.decision,
            judged=prep.judged,
            model=gen.model,
            usage={"promptTokens": gen.usage.get("prompt_tokens", 0),
                   "completionTokens": gen.usage.get("completion_tokens", 0)},
            rag_rewrite=prep.rewrite_source,
        )


rag_answer_service = RagAnswerService()

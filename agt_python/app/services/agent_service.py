"""Agent 业务编排层。

约定：路由层(endpoints)只做参数校验与响应包装，
真正的编排逻辑一律写在这里，方便单测与后续替换实现。

M1：接入 DeepSeek（OpenAI 兼容），闲聊能力 + prompts 集中管理。
M2：IntentRouter 意图路由（chitchat / rag / business），business 返回占位话术。
M3：服务端会话记忆（SessionMemory）：历史以 Python 侧为准，
    Java 传来的 req.history 仅在该会话无记忆时兜底使用。
M6：rag 意图接入真实检索链路——检索证据 → LLM judge → 带证据生成；
    跨域/检索故障降级闲聊，证据不足如实拒答（rag_service）。
"""

import logging
from typing import AsyncIterator

from app.core.config import settings
from app.core.exception import ServiceException
from app.core.llm import LLMError, llm_client
from app.core.response import StatusCode
from app.core.trace import ensure_trace_id
from app.prompts import load_prompt
from app.schemas.agent import ChatRequest, ChatResponse
from app.services.agent_loop import BizAuthError as LoopAuthError
from app.services.agent_loop import BizError as LoopBizError
from app.services.agent_loop import agent_loop
from app.services.biz import BizAuthError, BizError
from app.services.delegation_router import pick_agent
from app.services.fault import is_persistent_reply
from app.services.governance_service import (
    GovAction,
    TARGET_TOOL,
    governance_service,
)
from app.services.guard import (
    ensure_can_write,
    is_confirmation,
    is_rejection,
    pending_store,
)
from app.rag.history_filter import filter_history
from app.services.intent import IntentResult, business_reply, intent_router
from app.services.memory import session_memory
from app.services.memory_token import TokenEstimator
from app.services.memory_trim import trim_history
from app.services.planning import complexity_router, planning_agent
from app.services.rag_service import RagAnswer, rag_answer_service
from app.services.reply_clean import LeadThoughtStreamGuard, clean_reply
from app.services.telemetry import (
    PATH_MEMORY_FILTERED,
    PATH_MEMORY_SUMMARY_FAILED,
    PATH_MEMORY_TRIMMED,
    PATH_REPLY_CLEANED,
    set_trace_id,
    telemetry,
)
from app.services.tools import registry
from app.services.user_profile import profile_store, schedule_profile_extraction

logger = logging.getLogger(__name__)

# 未登录时业务意图的固定话术（查询类工具需登录后才能以用户身份执行）
_LOGIN_REQUIRED_REPLY = (
    "业务功能需要登录后使用。请先在系统登录，登录后我就能以你的身份查询设备、任务和告警了。"
)


def _audit_confirm(action: str, pending, identity: dict | None, *,
                   result: str = "SUCCESS", reason: str | None = None,
                   detail: str | None = None) -> None:
    """确认门放行/拒绝留痕（E-8）。

    危险写操作的"用户是否点了确认"是审计里最该回答的问题之一，但它此前只打日志。
    这里统一收敛到一个函数：**参数拼装只有一处**，避免放行/拒绝两条分支各写一遍
    而漏掉 target 或 operator。

    - `target` 恒为 `tool:<工具名>`，便于"某个工具被确认过几次"这类排查
    - `detail` 只记原因与业务提示语，**不含工具参数**（参数可能含业务数据）
    - 审计失败绝不影响对话：`governance_service.audit` 内部吞异常
    """
    try:
        actor = (identity or {}).get("username") or "anonymous"
        payload: dict[str, object] = {}
        if reason:
            payload["reason"] = reason
        if detail:
            payload["detail"] = detail[:300]
        payload["confirmed"] = action == GovAction.TOOL_CONFIRM_ALLOW
        governance_service.audit(
            action, operator=actor, target=f"{TARGET_TOOL}{pending.tool}",
            result=result, detail=payload,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("确认门审计写入失败（已忽略）: %s", e)


async def _iter_cleaned_deltas(
    messages: list[dict[str, str]], holder: dict, usage_out: dict | None = None, **llm_kwargs
) -> AsyncIterator[dict]:
    """逐字流式 + 首段净化（DEV_ISSUES §6.57），产出 delta 事件。

    为什么需要这一层：RAG / 闲聊的流式分支是真逐字 yield，出口拿不到完整文本，
    `_clean_reply` 对它们无效——泄漏会直达用户，且未净化的整段还会写回会话记忆
    污染后续轮次。这里把每个 delta 先过 `LeadThoughtStreamGuard`（只在开头疑似
    英文思考句时才缓冲，中文起头零延迟），只有净化后的文本才发给用户。

    流末的净化结果与剥离数写在 `holder`：`reply`（净化后全文）/ `removed` / `guard`。
    调用方在异常分支须调 `_flush_guard(holder)` 取回残留缓冲，否则会丢字。

    usage_out（E-13）：透传给 `llm_client.chat_stream` 的用量收集器——上游带
    `include_usage` 返回真实 token 时写进这里；调用方在流末检查有没有采到，
    没采到就本地估算（token 数不再记 0，见 chat_stream 收尾的计量段）。
    """
    guard = LeadThoughtStreamGuard()
    holder["guard"] = guard
    holder["reply"] = ""
    holder["removed"] = 0
    async for delta in llm_client.chat_stream(messages, usage_out=usage_out, **llm_kwargs):
        piece = guard.feed(delta)
        if piece:
            holder["reply"] += piece
            yield {"type": "delta", "content": piece}
    tail = guard.flush()
    if tail:
        holder["reply"] += tail
        yield {"type": "delta", "content": tail}
    holder["removed"] = guard.removed


def _flush_guard(holder: dict) -> str:
    """流被中断时的收尾：取回守卫残留缓冲并并回 `holder["reply"]`。

    返回本次新增的文本（调用方负责把它 yield 给用户，避免被缓冲的正文丢字）。
    """
    guard = holder.get("guard")
    if guard is None:
        return ""
    try:
        tail = guard.flush()
    except Exception as exc:  # noqa: BLE001 —— 收尾逻辑不得再抛
        logger.warning("流式净化收尾异常，放弃残留缓冲: %s", exc)
        return ""
    holder["removed"] = guard.removed
    if tail:
        holder["reply"] = holder.get("reply", "") + tail
    return tail


class AgentService:
    def __init__(self) -> None:
        self.model = settings.llm_model

    async def chat(self, req: ChatRequest) -> ChatResponse:
        """处理一轮对话：取记忆 -> 意图路由 -> 分派处理 -> 写回记忆。"""
        import time as _time

        trace_id = ensure_trace_id()   # 采纳 Java 传来的 X-Trace-Id（E-6），没有才新建
        set_trace_id(trace_id)  # 埋点贯穿：深层模块按 ContextVar 取当前 trace
        t0 = _time.perf_counter()
        logger.info(
            "[trace=%s] chat 收到请求 session=%s user=%s len=%d",
            trace_id,
            req.session_id,
            req.user_id,
            len(req.message),
        )

        intent_result = await self._route(req)
        logger.info(
            "[trace=%s] 意图=%s 来源=%s 置信度=%.2f",
            trace_id,
            intent_result.intent,
            intent_result.source,
            intent_result.confidence,
        )

        try:
            if intent_result.intent == "business":
                resp = await self._handle_business(req, intent_result)
            elif intent_result.intent == "rag":
                resp = await self._handle_rag(req, intent_result)
            else:
                resp = await self._handle_chitchat(req, intent_result)

            # 一轮对话结束，成对写回记忆（业务回合也记，跨意图上下文才连续）
            session_memory.record_turn(req.session_id, req.message, self._memory_reply(req, resp.content))
            # M-5：异步抽取用户偏好（fire-and-forget，不阻塞不抛——见 user_profile.py）
            schedule_profile_extraction(req.user_id, req.message, resp.content)
            logger.info(
                "[trace=%s] 记忆已写回 session=%s %s",
                trace_id,
                req.session_id,
                session_memory.stats(),
            )

            resp.trace_id = trace_id
            # 采集点②：请求级埋点（业务规约 §10）+ 采集点①工具级（tool_trace）
            self._record_metrics(req, intent_result, t0, resp=resp)
            # E-13：配额检查在 usage 已记录之后（used 含本轮）；超限只提示不拒绝（规约原文）
            quota = telemetry.quota_check(req.user_id, req.session_id)
            if quota:
                resp.quota = quota
            return resp
        except ServiceException as exc:
            self._record_metrics(req, intent_result, t0, error=str(exc))
            raise

    # ---- chitchat：DeepSeek 真实回复 ----

    async def _handle_chitchat(self, req: ChatRequest, intent: IntentResult) -> ChatResponse:
        try:
            content, model, usage = await self._generate(req)
        except LLMError as exc:
            raise ServiceException(StatusCode.AGENT_CALL_ERROR, str(exc)) from exc

        return ChatResponse(
            session_id=req.session_id,
            content=self._clean_reply(content, self._session_key(req)),
            intent=intent.intent,
            intent_source=intent.source,
            model=model,
            usage={
                "promptTokens": usage.get("prompt_tokens", 0),
                "completionTokens": usage.get("completion_tokens", 0),
            },
        )

    # ---- business：登录走 AgentLoop 调业务工具；未登录走确认话术/登录引导 ----

    def _identity(self, req: ChatRequest) -> dict | None:
        """Java 层注入的用户身份（含 droneToken）；未登录为 None。"""
        return (req.metadata or {}).get("identity") or None

    async def _handle_business(self, req: ChatRequest, intent: IntentResult) -> ChatResponse:
        identity = self._identity(req)
        session_key = self._session_key(req)

        # ---- 待确认写操作优先：只认用户字面「确认/取消」，其余内容视为放弃 ----
        pending = pending_store.get(session_key)
        if pending:
            if is_confirmation(req.message):
                return await self._execute_pending(req, intent, identity, pending, session_key)
            if is_rejection(req.message):
                pending_store.clear(session_key)
                logger.info("用户取消待确认写操作 session=%s tool=%s", session_key, pending.tool)
                _audit_confirm(GovAction.TOOL_CONFIRM_DENY, pending, identity,
                               reason="user_rejected")
                return ChatResponse(
                    session_id=req.session_id,
                    content=f"好的，已取消「{pending.description}」，业务系统没有任何改动。",
                    intent=intent.intent,
                    intent_source=intent.source,
                )
            # 用户改问别的：作废该动作，按新请求正常处理（避免陈旧动作被误确认）
            pending_store.clear(session_key)
            logger.info("待确认写操作因用户改问而作废 session=%s tool=%s", session_key, pending.tool)

        if not identity:
            # 未登录：维持安全话术（控制指令确认），查询类引导登录
            content = business_reply(req.message)
            return ChatResponse(
                session_id=req.session_id,
                content=content,
                intent=intent.intent,
                intent_source=intent.source,
            )

        # 登录用户：复杂度路由（PLAN/REACT，规约 §7）→ 执行引擎以用户身份调真实业务工具
        history = self._build_history(req)
        try:
            mode = "REACT"
            if settings.agent_planning_enabled:
                mode = await complexity_router.classify(req.message)
            # 确定性委派（DEV_ISSUES §6.55）：规则命中的设计用例钉死专家，消除 LLM 抖动
            forced = pick_agent(req.message)
            logger.info(
                "[trace] 执行模式=%s 确定性委派=%s session=%s",
                mode, forced or "无", req.session_id,
            )
            if mode == "PLAN":
                loop_result = await planning_agent.run(
                    req.message, history, identity, session_id=session_key,
                    forced_delegation=forced,
                )
            else:
                loop_result = await agent_loop.run(
                    req.message, history, identity, session_id=session_key,
                    forced_delegation=forced,
                )
        except LoopAuthError as exc:
            logger.info("业务调用登录过期: session=%s %s", req.session_id, exc)
            return ChatResponse(
                session_id=req.session_id,
                content=str(exc),
                intent=intent.intent,
                intent_source=intent.source,
            )
        except LoopBizError as exc:
            return ChatResponse(
                session_id=req.session_id,
                content=f"业务系统返回错误：{exc}",
                intent=intent.intent,
                intent_source=intent.source,
            )
        except LLMError as exc:
            raise ServiceException(StatusCode.AGENT_CALL_ERROR, str(exc)) from exc

        logger.info(
            "AgentLoop 完成 session=%s steps=%d tools=%s",
            req.session_id,
            len(loop_result.tool_trace),
            [t["tool"] for t in loop_result.tool_trace],
        )
        return ChatResponse(
            session_id=req.session_id,
            content=self._clean_reply(loop_result.content, session_key),
            intent=intent.intent,
            intent_source=intent.source,
            model=loop_result.model,
            usage={
                "promptTokens": loop_result.usage.get("prompt_tokens", 0),
                "completionTokens": loop_result.usage.get("completion_tokens", 0),
            },
            tool_trace=loop_result.tool_trace,
            mode=loop_result.mode,
            plan=loop_result.plan,
        )

    def _clean_reply(self, text: str, session_key: str) -> str:
        """正文净化（DEV_ISSUES §6.56）：剥离模型开头漏出的英文思考句，并留痕。

        提示词已要求「正文直接输出中文、不输出思考过程」，但那是**软约束**——
        实测同一问题时而泄漏、时而不泄漏；本方法做确定性兜底（规则见 reply_clean）。
        净化是兜底手段，**异常时按原文返回**，绝不反过来弄挂主链路。
        """
        try:
            cleaned = clean_reply(text)
        except Exception as exc:  # noqa: BLE001 —— 兜底逻辑不得影响回答
            logger.warning("正文净化异常，按原文返回: %s", exc)
            return text
        if cleaned != text:
            self._note_reply_cleaned(len(text) - len(cleaned), session_key)
        return cleaned

    def _note_reply_cleaned(self, removed: int, session_key: str) -> None:
        """留痕：开头漏出思考过程、被确定性兜底剥掉多少字符。

        同步出口与流式出口共用（§6.56 / §6.57）。这个计数是"提示词没约束住、
        兜底接住了"的频次，持续增长说明该继续收紧提示词——兜底是网，不是解药。
        """
        telemetry.record_fault(
            PATH_REPLY_CLEANED, session_key,
            detail=f"本应正文直接输出中文，实际开头漏出思考过程（已剥离 {removed} 字符）",
        )

    def _memory_reply(self, req: ChatRequest, reply: str) -> str:
        """写回记忆的助手侧文本：写操作确认话术换成**极短系统标记**；
        兜底/熔断类文案返回空串（memory.append 忽略空内容 → 只有用户问题入历史）。

        原因（实测踩坑两轮）：
        1. 确认话术原文入历史 → 模型下一轮会模仿它，甚至把系统话术当自己台词说出去；
        2. 完全不记助手回复 → 历史里留下"没人回应的写请求"，用户改问别的内容时，
           模型会反射性地去补做那条旧请求。
        折中：记一句不含话术模板的短标记，既让上一轮请求看起来"已被接住"，
        又没多少可模仿的文本。标记若被模型抄出来，AgentLoop 会做一次自愈重试。

        容错守卫（docs/specs/全链路容错规约.md §6.8，G5）：LLM 兜底话术、
        4010 重登录引导、熔断最终文案按 fault 哨兵常量全等比对，一律不入历史
        ——兜底文案进历史=慢性中毒，模型会基于它继续道歉。
        """
        if pending_store.get(self._session_key(req)):
            logger.info("写操作确认话术以短标记入历史 session=%s", req.session_id)
            return "（等待用户确认是否执行）"
        if not is_persistent_reply(reply):
            logger.info("兜底/熔断文案不入会话历史 session=%s", req.session_id)
            return ""
        return reply

    def _session_key(self, req: ChatRequest) -> str:
        """待确认动作的存储键：优先会话 id，缺省退化到用户维度。"""
        return req.session_id or f"user-{req.user_id}"

    def _record_metrics(
        self,
        req: ChatRequest,
        intent: IntentResult,
        t0: float,
        *,
        resp: ChatResponse | None = None,
        reply: str = "",
        mode: str = "",
        tool_trace: list[dict] | None = None,
        error: str = "",
        rag_decision: str = "",
        rag_docs: list | None = None,
        rag_rewrite: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        usage_source: str = "",
    ) -> None:
        """请求级 + 工具级埋点（业务规约 §10 采集点①②）。

        埋点任何异常都不能影响主链路（规约 §10 写入方式），整体 try 兜底。

        rag_decision/rag_docs 显式传参是给**流式分支**用的：流式不构造 ChatResponse，
        判定结果只在 prepare() 的返回值里（规约 §5 知识缺口 = 必须涵盖流式链路，
        否则线上以流式为主时缺口视图永远为空）。
        rag_rewrite 同理：记录本轮改写来源（DEV_ISSUES 6.37），供执行流程弹窗着色。
        """
        import time as _time

        try:
            identity = self._identity(req) or {}
            content = (resp.content if resp else reply) or ""
            m_mode = mode or (resp.mode if resp else "") or (intent.intent if intent else "")
            # v4（数据中台规约 §5 知识缺口）：RAG 链路判定与命中文档落库。
            # 只对**知识问答链路**记录：显式传参优先，否则取 resp.rag_decision；
            # degraded 归一为空串——检索链路故障属故障域，混进「缺口」会把系统故障
            # 算成知识空白（两件事的处置动作完全不同）。
            decision = str(rag_decision or (resp.rag_decision if resp else "") or "")
            if decision == "degraded":
                decision = ""
            docs = rag_docs if rag_docs is not None else (
                resp.citations if resp and resp.citations else [])
            telemetry.record_chat(
                session_id=req.session_id,
                user_id=req.user_id or 0,
                permission=str(identity.get("permission") or ""),
                intent=intent.intent if intent else "",
                intent_source=intent.source if intent else "",
                mode=m_mode,
                model=(resp.model if resp else "") or settings.llm_model,
                # E-13：流式分支的 token 显式传参（resp 为 None 时原来恒记 0——计量主洞）；
                # 非流式分支不传参，仍从 resp.usage 取
                prompt_tokens=(prompt_tokens if prompt_tokens is not None
                               else ((resp.usage or {}).get("promptTokens", 0) if resp else 0)),
                completion_tokens=(completion_tokens if completion_tokens is not None
                                   else ((resp.usage or {}).get("completionTokens", 0) if resp else 0)),
                duration_ms=(_time.perf_counter() - t0) * 1000,
                ok=resp is not None or bool(reply),
                degraded=bool(
                    (getattr(resp, "rag_decision", "") if resp else "") == "degraded"
                ),
                content_chars=len(content),
                error=error,
                rag_decision=decision,
                rag_docs=[c.get("docId") or c.get("doc_id") for c in docs],
                rag_rewrite=str((resp.rag_rewrite if resp else "") or ""),
                usage_source=usage_source,
            )
            for entry in (tool_trace if tool_trace is not None else (resp.tool_trace if resp and resp.tool_trace else [])):
                telemetry.record_tool_call(
                    session_id=req.session_id,
                    step=int(entry.get("step") or 0),
                    tool=str(entry.get("tool") or "-"),
                    ok=bool(entry.get("ok")),
                    blocked=str(entry.get("blocked") or ""),
                    error=str(entry.get("error") or ""),
                    duration_ms=entry.get("durationMs") or 0,
                    arguments=entry.get("arguments"),
                )
        except Exception as exc:  # 埋点绝不阻塞主链路
            logger.warning("埋点记录异常（已忽略）: %s", exc)

    async def _route(self, req: ChatRequest) -> IntentResult:
        """意图路由（含待确认动作仲裁）。

        有挂起的写操作时，用户的「确认/取消」必须回到业务处理链路——
        否则「确认」这种超短消息会被 LLM 兜底判成闲聊，模型甚至可能
        凭空宣称"已创建成功"（幻觉执行）。所以这里优先于意图分类：
        - 是确认/取消 → 强制 business，交由 _handle_business 消费挂起动作；
        - 是别的内容 → 作废挂起动作（避免陈旧动作被后一句"确认"误触发），
          再按正常意图分类走。
        """
        session_key = self._session_key(req)
        pending = pending_store.get(session_key)
        if pending:
            if is_confirmation(req.message) or is_rejection(req.message):
                logger.info(
                    "待确认动作命中用户回复 session=%s tool=%s msg=%s",
                    session_key, pending.tool, req.message.strip()[:10],
                )
                return IntentResult("business", 1.0, "pending")
            pending_store.clear(session_key)
            logger.info(
                "待确认动作因用户改问作废 session=%s tool=%s", session_key, pending.tool
            )
        return await intent_router.classify(req.message, self._identity(req))

    async def _execute_pending(
        self,
        req: ChatRequest,
        intent: IntentResult,
        identity: dict | None,
        pending,
        session_key: str,
    ) -> ChatResponse:
        """用户确认后真正执行写操作（执行前再校验一次身份与角色）。"""
        pending_store.clear(session_key)

        def reply(text: str) -> ChatResponse:
            return ChatResponse(
                session_id=req.session_id,
                content=text,
                intent=intent.intent,
                intent_source=intent.source,
            )

        if not identity:
            _audit_confirm(GovAction.TOOL_CONFIRM_DENY, pending, identity,
                           reason="session_expired")
            return reply("登录状态已失效，请重新登录后再确认执行。")
        try:
            ensure_can_write(identity)
        except BizError as exc:
            logger.info("写操作角色校验未通过 session=%s %s", session_key, exc)
            _audit_confirm(GovAction.TOOL_CONFIRM_DENY, pending, identity,
                           reason="role_denied", detail=str(exc))
            return reply(f"❌ 未执行：{exc}")

        try:
            output = await registry.execute(pending.tool, identity, pending.arguments)
        except BizAuthError as exc:
            _audit_confirm(GovAction.TOOL_CONFIRM_ALLOW, pending, identity,
                           result="FAILED", detail=str(exc))
            return reply(str(exc))
        except BizError as exc:
            logger.info("写操作执行失败 session=%s tool=%s: %s", session_key, pending.tool, exc)
            _audit_confirm(GovAction.TOOL_CONFIRM_ALLOW, pending, identity,
                           result="FAILED", detail=str(exc))
            return reply(f"❌ 执行失败：{exc}\n\n（{pending.description} 未生效，数据没有被改动）")

        detail = output.get("message") if isinstance(output, dict) else output
        logger.info("写操作已执行 session=%s tool=%s", session_key, pending.tool)
        _audit_confirm(GovAction.TOOL_CONFIRM_ALLOW, pending, identity)
        return reply(f"✅ 已执行：{pending.description}\n\n{detail or '业务系统已受理'}")

    # ---- rag：检索证据 → judge → 生成（M6） ----

    async def _handle_rag(self, req: ChatRequest, intent: IntentResult) -> ChatResponse:
        # 用**未裁剪**历史：RAG 链路里它同时喂给改写层（需紧邻前一轮做指代消解）
        # 与 prepare 的上下文装配，裁剪会掐掉指代消解需要的锚点（规约 §8 开放问题 4）
        history = self._raw_history(req)
        try:
            rag: RagAnswer = await rag_answer_service.answer(req.message, history)
        except LLMError as exc:
            # 生成阶段挂了：降级为闲聊式回答，绝不让知识问答整单失败
            logger.warning("RAG 生成失败，降级闲聊: %s", exc)
            return await self._handle_chitchat(req, intent)

        if rag.decision == "degraded" or not rag.content:
            # 只剩 degraded（检索链路故障）走这里：降级闲聊自然回答。
            # cross_domain/no_hit 自 v0.2 起在 rag_service 内给出固定拒答话术，
            # rag.content 非空，走下方正常返回路径（带 rag_decision 透传）
            resp = await self._handle_chitchat(req, intent)
            resp.rag_decision = rag.decision or "degraded"
            resp.rag_rewrite = rag.rag_rewrite
            return resp

        return ChatResponse(
            session_id=req.session_id,
            content=self._clean_reply(rag.content, self._session_key(req)),
            intent=intent.intent,
            intent_source=intent.source,
            model=rag.model,
            usage=rag.usage,
            citations=rag.citations,
            rag_decision=rag.decision,
            rag_judged=rag.judged,
            rag_rewrite=rag.rag_rewrite,
        )

    # ---- 流式（M4）：事件协议 meta -> delta* -> done | error ----

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[dict]:
        """流式处理一轮对话，产出 SSE 事件 dict。

        meta 先行（前端先拿到 sessionId/intent 再开始渲染气泡），
        delta 携带增量文本，done 收尾；LLM 失败发 error（HTTP 层仍是 200）。
        """
        trace_id = ensure_trace_id()   # 采纳 Java 传来的 X-Trace-Id（E-6），没有才新建
        set_trace_id(trace_id)  # 埋点贯穿
        import time as _time

        t0 = _time.perf_counter()
        stream_mode = ""               # business 分支的执行模式（REACT/PLAN）
        stream_trace: list[dict] = []  # business 分支的工具轨迹（采集点①）
        stream_rag_decision = ""       # rag 分支的链路判定（v4 知识缺口埋点）
        stream_rag_docs: list = []     # rag 分支命中文档 docId（v4）
        stream_rag_rewrite = ""        # rag 分支的查询改写来源（v5，DEV_ISSUES 6.37）
        holder: dict = {}              # 逐字流式净化守卫的载体（reply / removed / guard）
        # E-13：流式 token 计量载体。E-13 之前流式链路 usage 恒记 0（done.usage={}），
        # 而流式是主用链路——成本看板会系统性漏记。三个来源优先级：
        # business=LoopResult 的真实 usage（api）→ LLM include_usage 收尾 chunk（api）
        # → 都没有则 TokenEstimator 本地估算（estimate，看板如实标注）。
        stream_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "estimated": 0}
        stream_usage_source = ""       # 'api' / 'estimate' / ''（本轮没调 LLM，如 refusal）
        stream_messages: list | None = None  # 估算 fallback 需要本轮的 prompt messages
        logger.info(
            "[trace=%s] chat_stream 收到请求 session=%s len=%d",
            trace_id,
            req.session_id,
            len(req.message),
        )

        intent_result = await self._route(req)
        yield {
            "type": "meta",
            "sessionId": req.session_id,
            "traceId": trace_id,
            "intent": intent_result.intent,
            "intentSource": intent_result.source,
        }

        if intent_result.intent == "business":
            # AgentLoop 是多步工具循环，中间过程不适合直接流式吐给前端；
            # 跑完后整段产出（后续可优化为工具事件 + 分段流式）
            try:
                biz_resp = await self._handle_business(req, intent_result)
            except ServiceException as exc:
                yield {"type": "error", "code": int(exc.status_code), "message": str(exc)}
                return
            # 过程可观测（规约 §6/§9）：正文前先发 biz 事件，携带工具轨迹/执行模式/计划
            yield {
                "type": "biz",
                "toolTrace": biz_resp.tool_trace,
                "mode": biz_resp.mode,
                "plan": biz_resp.plan,
            }
            yield {"type": "delta", "content": biz_resp.content}
            reply = biz_resp.content
            stream_mode = biz_resp.mode
            stream_trace = biz_resp.tool_trace or []
            # E-13：business 流式的 usage 在 LoopResult 里（多步工具循环逐次累加），直接取
            if biz_resp.usage:
                stream_usage["prompt_tokens"] = int(biz_resp.usage.get("promptTokens") or 0)
                stream_usage["completion_tokens"] = int(biz_resp.usage.get("completionTokens") or 0)
                stream_usage_source = "api"
        elif intent_result.intent == "rag":
            # M6：rag 意图——先完成检索+judge（meta 之后、delta 之前），
            # 前端会多等约 2~3s 才见到首字，用 rag 事件告知链路判定结果
            # 用**未裁剪**历史：同非流式分支，改写层需要紧邻前一轮（规约 §8 开放问题 4）
            history = self._raw_history(req)
            prep = await rag_answer_service.prepare(req.message, history)
            # v4：链路判定与命中文档留给收尾埋点用（degraded 归一为空，故障域不算缺口）
            stream_rag_decision = "" if prep.decision == "degraded" else (prep.decision or "")
            stream_rag_docs = [c.get("docId") or c.get("doc_id") for c in (prep.citations or [])]
            stream_rag_rewrite = prep.rewrite_source or ""
            yield {
                "type": "rag",
                "decision": prep.decision,
                "judged": prep.judged,
                "citations": prep.citations,
            }

            if prep.mode == "refusal":
                # 领域内无答案：如实拒答（固定话术，无需再调模型）
                reply = prep.refusal_content
                yield {"type": "delta", "content": reply}
            elif prep.mode == "fallback":
                # 仅剩 degraded（检索链路故障）走这里：降级闲聊链路。
                # cross_domain/no_hit 已在 rag_service 内改为固定拒答话术（mode=refusal）
                messages: list[dict[str, str]] = [
                    {"role": "system", "content": load_prompt("chitchat_system")}
                ]
                messages.extend(history)
                messages.append({"role": "user", "content": req.message})
                stream_messages = messages
                try:
                    async for evt in _iter_cleaned_deltas(messages, holder, usage_out=stream_usage):
                        yield evt
                except LLMError as exc:
                    tail = _flush_guard(holder)
                    if tail:
                        yield {"type": "delta", "content": tail}
                    if holder["reply"]:
                        yield {"type": "delta", "content": "\n\n（回复中断了，你可以重试）"}
                    else:
                        yield {"type": "error", "code": int(StatusCode.AGENT_CALL_ERROR), "message": str(exc)}
                        return
                reply = holder["reply"]
            else:
                # 正常生成：rag_system 提示词已带证据
                messages = rag_answer_service.build_generate_messages(prep)
                stream_messages = messages
                try:
                    async for evt in _iter_cleaned_deltas(messages, holder, usage_out=stream_usage, temperature=0.3):
                        yield evt
                except LLMError as exc:
                    tail = _flush_guard(holder)
                    if tail:
                        yield {"type": "delta", "content": tail}
                    if holder["reply"]:
                        yield {"type": "delta", "content": "\n\n（回复中断了，你可以重试）"}
                    else:
                        yield {"type": "error", "code": int(StatusCode.AGENT_CALL_ERROR), "message": str(exc)}
                        return
                reply = holder["reply"]
        else:
            messages: list[dict[str, str]] = [
                {"role": "system", "content": load_prompt("chitchat_system")}
            ]
            messages.extend(self._build_history(req))
            messages.append({"role": "user", "content": req.message})
            stream_messages = messages
            try:
                async for evt in _iter_cleaned_deltas(messages, holder, usage_out=stream_usage):
                    yield evt
            except LLMError as exc:
                tail = _flush_guard(holder)
                if tail:
                    yield {"type": "delta", "content": tail}
                if holder["reply"]:
                    # 已经吐了一半：把半截话收尾并落记忆，然后提示中断
                    yield {"type": "delta", "content": "\n\n（回复中断了，你可以重试）"}
                else:
                    yield {"type": "error", "code": int(StatusCode.AGENT_CALL_ERROR), "message": str(exc)}
                    return
            reply = holder["reply"]

        stream_removed = holder.get("removed", 0)
        if stream_removed:
            self._note_reply_cleaned(stream_removed, self._session_key(req))

        session_memory.record_turn(req.session_id, req.message, self._memory_reply(req, reply))
        # M-5：流式分支同样挂画像抽取（fire-and-forget，见非流式分支注释）
        schedule_profile_extraction(req.user_id, req.message, reply)
        # E-13：流式 token 计量。上游 usage 已采到（api）→ 直接用；没采到且确实调过
        # LLM（stream_messages 非空）→ TokenEstimator 本地估算（estimate）。估算只可
        # 看趋势、不可拿去对账——usage_source 列就是干这个的（如实标注口径）。
        if not stream_usage_source:
            if stream_usage.get("prompt_tokens") or stream_usage.get("completion_tokens"):
                stream_usage_source = "api"
            elif stream_messages is not None:
                try:
                    est = TokenEstimator()
                    stream_usage["prompt_tokens"] = est.estimate_messages(stream_messages)
                    stream_usage["completion_tokens"] = est.estimate(reply or "")
                    stream_usage["estimated"] = 1
                except Exception:  # noqa: BLE001 —— 估算失败按 0 记，不影响主链路
                    pass
                stream_usage_source = "estimate"
        self._record_metrics(
            req, intent_result, t0, reply=reply, mode=stream_mode, tool_trace=stream_trace,
            rag_decision=stream_rag_decision, rag_docs=stream_rag_docs,
            rag_rewrite=stream_rag_rewrite,
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            usage_source=stream_usage_source,
        )
        # E-13：配额检查放在 usage 已记录之后（used 含本轮）；超限只提示不拒绝（规约原文）。
        # SSE 事件走独立的 quota 类型：Java 原样转发 SSE，前端按事件渲染黄条，零 Java 改动。
        quota = telemetry.quota_check(req.user_id, req.session_id)
        if quota:
            yield {"type": "quota", **quota}
        yield {
            "type": "done", "model": settings.llm_model,
            "usage": {
                "promptTokens": stream_usage.get("prompt_tokens", 0),
                "completionTokens": stream_usage.get("completion_tokens", 0),
                "source": stream_usage_source,
            },
        }

    def _raw_history(self, req: ChatRequest) -> list[dict[str, str]]:
        """原始历史（未裁剪）。来源优先级：服务端记忆 > Java 透传的 req.history。

        裁剪是**调用方**的事（`_build_history` 做），这里保持"取全量"语义，
        供改写层（需要紧邻前一轮）等对裁剪不敏感的消费者使用。
        """
        if session_memory.has(req.session_id):
            return session_memory.get(req.session_id)
        return [{"role": m.role, "content": m.content} for m in req.history]

    def _build_history(self, req: ChatRequest) -> list[dict[str, str]]:
        """装配给生成用的历史：相关性过滤（规约 §6 M-3）→ token 预算 + 双锚点裁剪（M-2）。

        为什么改写层不共用这个方法：改写需要**紧邻前一轮**做指代消解，
        而裁剪的双锚点与相关性过滤都可能把中间轮次丢掉——两者用途不同
        （规约 §8 开放问题 4）。`_raw_history` 保持全量，改写层与排查口径不受影响。
        """
        raw = self._raw_history(req)
        if not raw:
            # M-4：空历史也登记 0——防御同一任务上下文里的陈旧值串到本轮落库
            try:
                telemetry.note_memory_stats(total=0, used=0, trimmed=0)
            except Exception:  # noqa: BLE001 —— 埋点绝不影响主链路
                pass
            # M-5：新会话第一条消息没有历史可带，画像恰恰是此刻唯一的背景来源
            return self._with_profile(req, raw)
        session_id = req.session_id or ""
        prev_summary = ""
        getter = getattr(session_memory, "get_summary", None)
        if getter is not None:
            try:
                prev_summary = getter(session_id) or ""
            except Exception:  # noqa: BLE001 —— 摘要读取失败不影响裁剪
                prev_summary = ""

        # M-3：相关性过滤（锚点保护在过滤层内部；短会话/关闭/异常时原样返回）。
        # filter_history 自身已兜底不抛，这里再包一层双保险。
        fres = None  # M-4：异常路径下供下方守卫判断（过滤没跑成 = 没有淘汰数）
        try:
            fres = filter_history(raw, req.message or "")
            if fres.was_filtered:
                logger.info(
                    "记忆相关性过滤 session=%s：打分 %d 轮、淘汰 %d 轮",
                    session_id, fres.scored_turns, fres.dropped_turns,
                )
            history_input = fres.messages
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆相关性过滤调用异常，退回全量历史: %s", exc)
            history_input = raw

        result = trim_history(history_input, prev_summary=prev_summary)
        if result.was_trimmed:
            logger.info(
                "记忆裁剪 session=%s %d 条 -> %d 条（丢弃 %d，摘要=%s，失败=%s）",
                session_id, len(history_input), len(result.messages),
                result.trimmed_count, bool(result.summary), result.summary_failed,
            )
            # 摘要成功则持久化（递归压缩的载体）；失败不写，避免覆盖旧的可用摘要。
            # covered_turn 按**原始全量**轮数记（raw 未过滤未裁剪）——它是"摘要覆盖边界"，
            # 用过滤后的轮数会把边界记小，后续递归摘要会漏内容。
            saver = getattr(session_memory, "save_summary", None)
            if result.summary and saver is not None:
                try:
                    saver(session_id, result.summary, len(raw) // 2)
                except Exception:  # noqa: BLE001
                    pass

        # M-4（会话记忆规约 §6 G14）：记忆数字 + 路径事件落埋点。
        # 数字经 note_memory_stats 传给 record_chat 落 agent_chat_log 三列
        # （同一请求任务上下文内，_record_metrics 稍后消费）；路径事件进
        # 计数器与 fault_event，供弹窗记忆段着色与 M-3 阈值调参攒分布。
        # 整体 try 兜底——任何埋点异常都不影响历史装配结果。
        messages = result.messages
        try:
            telemetry.note_memory_stats(
                total=len(raw), used=len(messages), trimmed=result.trimmed_count)
            if fres is not None and fres.dropped_turns > 0:
                telemetry.record_fault(
                    PATH_MEMORY_FILTERED, session_id, level=logging.INFO,
                    detail=f"相关性过滤淘汰 {fres.dropped_turns} 轮（候选 {fres.scored_turns} 轮）")
            if result.was_trimmed:
                telemetry.record_fault(
                    PATH_MEMORY_TRIMMED, session_id, level=logging.INFO,
                    detail=f"超 token 预算裁剪 {result.trimmed_count} 条，"
                           f"摘要={'已生成' if result.summary else '未生成'}")
            if result.summary_failed:
                telemetry.record_fault(
                    PATH_MEMORY_SUMMARY_FAILED, session_id,
                    detail="裁剪后摘要生成失败，被裁内容没有摘要补偿")
        except Exception:  # noqa: BLE001 —— 埋点绝不影响主链路
            pass
        return self._with_profile(req, messages)

    def _with_profile(self, req: ChatRequest,
                      messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """M-5：把用户画像插到历史最前（system 语义的背景设定）。

        只作用于 chitchat / business 两条链路（rag 分支走 _raw_history，
        按 §8 开放问题 4 不做记忆加工）。画像读取失败=无画像行，零影响。
        """
        if not settings.profile_enabled:
            return messages
        try:
            profile_line = profile_store.as_prompt(req.user_id)
        except Exception:  # noqa: BLE001 —— 画像异常绝不影响历史装配
            return messages
        if not profile_line:
            return messages
        return [{"role": "system", "content": f"[用户画像] {profile_line}"},
                *messages]

    async def _generate(self, req: ChatRequest) -> tuple[str, str, dict]:
        """装配上下文并调用大模型，返回 (回复, 模型名, 用量)。"""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": load_prompt("chitchat_system")}
        ]
        messages.extend(self._build_history(req))
        messages.append({"role": "user", "content": req.message})

        result = await llm_client.chat(messages)
        logger.info(
            "LLM 回复完成 model=%s prompt=%s completion=%s",
            result.model,
            result.usage.get("prompt_tokens", 0),
            result.usage.get("completion_tokens", 0),
        )
        return result.content, result.model, result.usage


agent_service = AgentService()

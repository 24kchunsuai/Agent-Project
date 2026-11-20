
"""业务 AgentLoop：ReAct 风格的工具调用循环。

参考 open-agent-master 的 AgentLoop 思想，Python 侧最小实现：
LLM 声明要调的工具 → registry 执行 → 结果以 role=tool 回填 → 再生成，
循环直到模型给出最终回答，或触及熔断。

熔断体系（docs/specs/全链路容错规约.md §6.1）：
- max_steps：最大工具调用步数（超限无工具 finalize 收尾）；
- 循环指纹：同工具+同参数，第 2 次执行但提醒、第 3 次拒绝执行并触发
  无工具纯生成收尾（熔断恢复，交付阶段性结论而非抛异常）；
- 单工具连续失败：同一工具连续失败 ≥ agent_tool_fail_max 次后本轮拒绝再调；
- 工具执行 wait_for 超时兜底（media_upload 覆盖独立长超时）。

安全边界：
- identity 由 Java 层鉴权后注入，本循环只透传——所有工具调用都
  以当前用户身份执行，不做任何提权；
- 4010（登录过期）直接终止循环，转成引导重新登录的固定话术（不入会话历史）；
- 确认门拦截（写操作待确认）不属于失败，不进任何熔断计数（规约 §6.6）。
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.prompts import load_prompt
from app.services import delegation_router
from app.services.biz import BizAuthError, BizError
from app.services.fault import BREAK_FALLBACK_REPLY, LLM_FALLBACK_REPLY
from app.services.guard import build_action, build_confirm_reply, ensure_can_write, pending_store
from app.services.subagent import SUBAGENTS, runner as subagent_runner
from app.services.telemetry import (
    PATH_FP_BREAK,
    PATH_FP_WARN,
    PATH_LLM_FALLBACK,
    PATH_TOOL_FAIL_BREAK,
    PATH_TOOL_TIMEOUT,
    telemetry,
)
from app.services.tools import registry

logger = logging.getLogger(__name__)

# role=tool 消息需要 tool_call_id 对齐 OpenAI 协议
_MAX_TOOL_OUTPUT_CHARS = 3000
# 工具级超时覆盖表：媒体上传是大文件传输，不能套查询类的 15s
# delegate_subagent：委派的内部预算由子智能体自身 timeout 决定（45~90s），
# 套 15s 通用超时会把它提前掐断（实测委派 5~7s 侥幸没踩，慢模型下必炸）——
# 按最大子智能体预算 + 5s 余量覆盖（DEV_ISSUES §6.55）。
_TOOL_TIMEOUT_OVERRIDES = {
    "media_upload": settings.jsdstu_upload_timeout,
    "delegate_subagent": max((d.timeout_seconds for d in SUBAGENTS), default=60) + 5,
}
# 无工具 finalize 收尾的统一 system 注入（max_steps / 指纹熔断共用）
_FINALIZE_SYSTEM = (
    "已达到工具调用步数上限，禁止再调用任何工具。"
    "请基于以上已获取的工具结果直接回答用户最初的问题："
    "查到了就总结作答；信息不足就如实说明已查到什么、还缺什么，"
    "并建议用户把问题拆小一点。不要编造没有查到的数据。"
)
# 历史里由系统写入的短标记；模型抄出它=该调工具没调，需自愈重试
_MARKER_LEAK = "等待用户确认"
_NUDGE = (
    "你刚才输出的是系统内部标记，不是给用户的回复。若用户本轮要求执行写操作，"
    "请直接调用对应的写工具（系统会代为征求用户确认）；若只是查询或闲聊，"
    "请正常回答问题。不要再输出任何带括号的系统标记。"
)

# ---- 未尽力即弃答的自愈重试（DEV_ISSUES §6.59）----
# 现象：模型查了一个工具（常是 dashboard_query）发现是汇总口径、没有用户要的粒度，
# 于是直接回"查不到/系统不提供该数据"，**却没有去调用明显更细粒度的工具**
# （drone_query / task_query 等）。biz-006（"哪台无人机飞行架次最多"）即此形态：
# 数据明明在 drone_query 的 flightCount 里，模型只看了总览就收工。
#
# 为什么不只靠提示词：提示词的"数据不足要继续查"约束只能提高概率（实测 1/3→2/3），
# 不能兜住。此处按既有 `_MARKER_LEAK` 自愈的同一范式加**第二类触发器**：
# 检出"弃答形态"就推模型一次，逼它把还没用过的查询工具用上。
# 与 `_MARKER_LEAK` 一样**只重试一次**，避免和模型的固执拉锯。
#
# 判定从严（宁可漏推不可错推）：只认"明确的无法回答措辞"，且必须**确实存在
# 尚未调用过的查询工具**——后者是硬证据：有工具没用过就说"查不到"，一定是弃答。
_REFUSAL_MARKERS = (
    "无法回答", "无法直接", "无法判断", "无法确定", "无法给出",
    "查不到", "查询不到", "没有该数据", "没有这个数据", "未提供该数据",
    "不提供该数据", "系统不提供", "没有按", "无法获取到",
)
_REFUSAL_NUDGE = (
    "停下——你还没把可用的工具查完，不要现在就下「查不到」的结论。"
    "你目前只看了汇总口径的数据，而用户问的可能是更细的粒度（单台设备/单个任务/单条告警）。"
    "请立刻调用下面这些**你还没用过**的查询工具去拿明细，再基于真实结果回答：\n{tools}\n"
    "只有在所有可能含该数据的工具都查过且确实为空时，才可以说「查不到」。"
)


@dataclass
class LoopResult:
    content: str                    # 最终回复正文
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)  # 调用轨迹（观测用）
    mode: str = "REACT"             # 执行模式：REACT / PLAN（PlanningAgent 置 PLAN）
    plan: list[str] = field(default_factory=list)  # PLAN 模式的步骤清单（含状态标注）


class AgentLoop:
    def __init__(self) -> None:
        self.max_steps = settings.agent_loop_max_steps
        # ---- 容错参数（实例属性：e2e 可按用例覆盖）----
        self.same_call_max = settings.agent_loop_same_call_max
        self.tool_fail_max = settings.agent_tool_fail_max
        self.tool_timeout = settings.agent_tool_timeout
        # ---- 会话级熔断状态（规约 §6.1：跨消息生效）----
        self._fp_counts: dict[str, dict[str, int]] = {}   # session -> 指纹 -> 次数（window=0）
        self._fp_windows: dict[str, deque] = {}           # session -> 滚动窗口指纹（window>0）
        self._tool_fails: dict[str, dict[str, int]] = {}  # session -> 工具 -> 连续失败次数

    # ---- 未尽力即弃答的检出（DEV_ISSUES §6.59）----

    @staticmethod
    def _unused_query_tools(used: set[str]) -> list[str]:
        """本次会话**还没调用过**的查询类工具（只读、非危险）。

        这是"弃答"判定的硬证据：只要还有查询工具没用过，模型说"查不到"就是没查完。
        筛选口径——名字以 `_query` 结尾的取明细工具，外加 `dashboard_query` 这类
        `_query` 尾缀已覆盖的汇总工具；`*_detail` / `*_stats` 属深挖同一对象，
        一并算作可查项。危险工具（写操作）不在列——不能逼模型去调写工具。
        """
        candidates = (
            name for name, tool in registry.tools.items()
            if not tool.danger and name.endswith(("_query", "_detail", "_stats"))
        )
        return sorted(n for n in candidates if n not in used)

    @classmethod
    def _looks_like_refusal(cls, text: str) -> bool:
        """回复是否在"明确表示无法回答"（从宽收录措辞，从严交给调用点判定）。"""
        return bool(text) and any(m in text for m in _REFUSAL_MARKERS)

    # ---- 循环指纹（同工具+同参数，规约 §6.1-1）----

    @staticmethod
    def _fingerprint(tool: str, args: dict[str, Any]) -> str:
        raw = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _fp_seen(self, session_id: str, fp: str) -> int:
        """该指纹此前已出现（执行）过的次数。"""
        window = self._fp_windows.get(session_id)
        if window is not None:
            return window.count(fp)
        return self._fp_counts.setdefault(session_id, {}).get(fp, 0)

    def _fp_record(self, session_id: str, fp: str) -> None:
        """记录一次实际执行（拒绝/拦截不记录）。"""
        if settings.agent_fingerprint_window > 0:
            self._fp_windows.setdefault(
                session_id, deque(maxlen=settings.agent_fingerprint_window)
            ).append(fp)
            return
        counts = self._fp_counts.setdefault(session_id, {})
        counts[fp] = counts.get(fp, 0) + 1
        # 会话级状态防膨胀：会话数超限时丢弃最旧（memory 同策略）
        if len(self._fp_counts) > 2000:
            self._fp_counts.pop(next(iter(self._fp_counts)), None)

    # ---- 单工具连续失败（规约 §6.1-3）----

    def _tool_failed(self, session_id: str, tool: str) -> int:
        per = self._tool_fails.setdefault(session_id, {})
        per[tool] = per.get(tool, 0) + 1
        return per[tool]

    def _tool_ok(self, session_id: str, tool: str) -> None:
        self._tool_fails.setdefault(session_id, {})[tool] = 0

    def _tool_is_broken(self, session_id: str, tool: str) -> bool:
        return self._tool_fails.get(session_id, {}).get(tool, 0) >= self.tool_fail_max

    async def _force_delegate(
        self, agent: str, question: str,
        identity: dict[str, Any], session_id: str,
    ) -> tuple[dict[str, Any], str]:
        """确定性委派一次（DEV_ISSUES §6.55）。返回 (轨迹条目, 注入模型的观测)。

        失败不上抛（登录过期除外）：委派失败只记轨迹 + 如实告知模型，让它回退到
        自主查询继续作答——"设计用例必须委派且成功"由评测断言把关，
        主链路不能因为一次委派失败就中断。
        """
        entry: dict[str, Any] = {
            "step": 0, "tool": "delegate_subagent",
            "arguments": {"name": agent, "task": question}, "forced": True,
        }
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                registry.execute(
                    "delegate_subagent", identity,
                    {"name": agent, "task": delegation_router.task_brief(question, agent)},
                ),
                timeout=_TOOL_TIMEOUT_OVERRIDES["delegate_subagent"],
            )
        except BizAuthError:
            raise  # 登录过期：整轮终止，交上层转固定话术
        except asyncio.TimeoutError:
            entry["durationMs"] = round((time.perf_counter() - t0) * 1000, 1)
            entry["ok"] = False
            entry["error"] = "确定性委派超时"
            logger.warning("确定性委派超时 session=%s agent=%s", session_id, agent)
            return entry, (
                f"（系统已按规则委派专家 {agent}，但该专家超时未完成。"
                "请基于已有信息作答，不要编造该专家的结论。）"
            )
        except Exception as exc:  # noqa: BLE001 —— 委派失败不阻断主链路
            entry["durationMs"] = round((time.perf_counter() - t0) * 1000, 1)
            entry["ok"] = False
            entry["error"] = f"确定性委派失败: {exc}"
            logger.warning("确定性委派失败 session=%s agent=%s: %s", session_id, agent, exc)
            return entry, (
                f"（系统已按规则委派专家 {agent}，但委派未成功：{exc}。"
                "请自行查询后作答，并如实说明。）"
            )

        entry["durationMs"] = round((time.perf_counter() - t0) * 1000, 1)
        payload = result if isinstance(result, dict) else {}
        status = payload.get("status") or ""
        conclusion = payload.get("conclusion") or ""
        entry["ok"] = status == "DONE"
        if not entry["ok"]:
            entry["error"] = f"专家返回状态 {status or '未知'}"
        logger.info(
            "确定性委派结束 session=%s agent=%s status=%s duration=%sms",
            session_id, agent, status, entry["durationMs"],
        )
        if not entry["ok"] or not conclusion:
            return entry, (
                f"（系统已按规则委派专家 {agent}，该专家未能给出结论"
                f"（状态 {status or '未知'}）。请自行补查后如实作答。）"
            )
        return entry, (
            f"系统已按规则委派专家「{agent}」完成协查，结论如下。"
            "请直接采信并整合进最终回答，**不要重复委派同一任务**；"
            "如仍缺其他维度的信息，可自行调用查询工具补齐。\n"
            f"—— {agent} 的结论 ——\n{conclusion}"
        )

    async def run(
        self,
        question: str,
        history: list[dict[str, str]],
        identity: dict[str, Any],
        session_id: str = "default",
        plan: list[str] | None = None,
        replanned: bool = False,
        forced_delegation: str = "",
    ) -> LoopResult:
        """跑一轮业务问答。identity 为 Java 注入的用户身份（含 droneToken）。

        危险工具（写操作）不当场执行：拦下并登记待确认动作，本轮提前收尾，
        由下一条用户消息的「确认」触发执行（见 guard 模块）。
        plan 非空时为 PLAN 模式：把步骤清单作为 system 消息注入（规约 §7.2），
        执行仍复用本循环；replanned=True 表示这是重排后的第二次执行。
        forced_delegation 非空时：进循环前先确定性委派该专家（DEV_ISSUES §6.55），
        把结论作为 system 观测注入，消除"委派与否由 LLM 决定"的抖动。"""
        # 会话 ID 注入 identity：delegate_subagent 的预算与黑板按会话记账用
        identity = {**identity, "_session_id": session_id}
        tools_schema = registry.to_openai_schema()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_prompt("biz_system")},
            *history,
        ]
        # 黑板注入（规约 §8.3）：本会话此前子智能体的结论，供主循环汇总
        if settings.agent_subagent_enabled:
            blackboard = subagent_runner.render_blackboard(session_id)
            if blackboard:
                messages.append({
                    "role": "system",
                    "content": "以下是本会话中专家子智能体此前的协查结论（供参考与汇总，"
                               "不要重复委派已完成的相同任务）：\n" + blackboard,
                })
        if plan:
            plan_note = (
                "本次请求较复杂，已生成执行计划。请逐项完成（可根据实际情况"
                "调整顺序或跳过，但不要遗漏目标；每步用对应的查询/操作工具实现）：\n"
                + "\n".join(plan)
                + ("\n（注意：上一次执行中断，失败原因已由规划器重新规划，"
                   "已成功获取过的数据可直接复用结论，不必机械重复）" if replanned else "")
            )
            messages.append({"role": "system", "content": plan_note})
        messages.append({"role": "user", "content": question})

        usage_total: dict[str, int] = {}
        tool_trace: list[dict[str, Any]] = []
        nudged = False   # 系统标记泄漏的自愈重试只做一次
        refusal_nudged = False   # 未尽力即弃答的自愈重试只做一次（DEV_ISSUES §6.59）

        # 确定性委派（DEV_ISSUES §6.55）：规则命中的设计用例不再等 LLM 自发调用，
        # 进循环前先委派一次，把专家结论作为 system 观测注入，
        # 再由本循环汇总结论、补查其余维度（结论与轨迹都进埋点/前端）。
        if forced_delegation and settings.agent_subagent_enabled \
                and "delegate_subagent" in registry.tools:
            entry, note = await self._force_delegate(
                forced_delegation, question, identity, session_id
            )
            tool_trace.append(entry)
            if note:
                messages.append({"role": "system", "content": note})

        def _fallback(reason: str) -> LoopResult:
            """LLM 故障兜底：绝不上抛堆栈，给用户可执行的下一步。

            文案是 fault.LLM_FALLBACK_REPLY 哨兵常量——agent_service 写历史时
            按常量比对跳过（规约 §6.8，兜底文案不入会话历史）。"""
            logger.error("AgentLoop LLM 故障收尾: %s", reason)
            telemetry.record_fault(
                PATH_LLM_FALLBACK, session_id,
                detail=f"本应正常回答，实际转兜底话术（原因：{reason}）",
            )
            return LoopResult(
                content=LLM_FALLBACK_REPLY,
                usage=usage_total,
                tool_trace=tool_trace,
                mode="PLAN" if plan else "REACT",
                plan=list(plan or []),
            )

        async def _soft_land(reason: str, step_no: int) -> LoopResult:
            """熔断恢复：调一次不带 tools 的 LLM，基于已有工具结果汇总收尾。

            收尾成功=真实工作产出（可入历史）；也失败时按 reason 给兜底文案
            （哨兵常量，不入历史）。规约 §6.1-2：熔断必须带恢复路径。"""
            logger.warning("AgentLoop 触发收尾（%s），进入无工具 finalize", reason)
            messages.append({"role": "system", "content": _FINALIZE_SYSTEM})
            try:
                final = await llm_client.chat(messages, temperature=0.2, max_tokens=768)
                if final.content:
                    tool_trace.append(
                        {"step": step_no, "tool": "(finalize)", "ok": True, "reason": reason}
                    )
                    return LoopResult(
                        content=final.content,
                        model=final.model,
                        usage=usage_total,
                        tool_trace=tool_trace,
                        mode="PLAN" if plan else "REACT",
                        plan=list(plan or []),
                    )
            except LLMError as exc:
                logger.error("finalize 总结调用失败: %s", exc)
            if reason == "fingerprint_break":
                return LoopResult(
                    content=BREAK_FALLBACK_REPLY,
                    usage=usage_total,
                    tool_trace=tool_trace,
                    mode="PLAN" if plan else "REACT",
                    plan=list(plan or []),
                )
            return _fallback("finalize 失败且无工具结果可用")

        for step in range(1, self.max_steps + 1):
            try:
                result = await llm_client.chat(messages, temperature=0.3, tools=tools_schema)
            except LLMError as exc:
                # chat 内部已有网络重试；走到这里多为持续故障，兜底收尾不上抛
                return _fallback(str(exc))
            for key in ("prompt_tokens", "completion_tokens"):
                usage_total[key] = usage_total.get(key, 0) + result.usage.get(key, 0)

            if not result.tool_calls:
                # 自愈：模型把历史里的系统标记（"等待用户确认"）当答案抄了出来，
                # 说明它本该调写工具却没调——推它一次，别把标记当回复发给用户。
                if not nudged and _MARKER_LEAK in (result.content or ""):
                    nudged = True
                    logger.warning("模型复述系统标记，触发一次自愈重试")
                    tool_trace.append({"step": step, "tool": "-", "ok": False,
                                       "error": "marker_leak_retry"})
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({"role": "system", "content": _NUDGE})
                    continue
                # 自愈（DEV_ISSUES §6.59）：未尽力即弃答——回复里说"查不到/无法回答"，
                # 但**还有查询工具压根没调用过**。这是硬证据：没查完就不能下结论。
                # 推模型一次，把它还没用过的查询工具点名列给它。只推一次。
                unused = self._unused_query_tools(
                    {t.get("tool") for t in tool_trace if t.get("tool")}
                )
                if (not refusal_nudged and unused
                        and self._looks_like_refusal(result.content or "")):
                    refusal_nudged = True
                    picked = "、".join(f"`{n}`" for n in unused[:8])
                    logger.warning(
                        "模型未尽力即弃答（未用工具=%s），触发一次自愈重试", unused[:8])
                    tool_trace.append({"step": step, "tool": "-", "ok": False,
                                       "error": "refusal_retry"})
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({"role": "system",
                                     "content": _REFUSAL_NUDGE.format(tools=picked)})
                    continue
                # 模型给出最终回答，循环结束
                return LoopResult(
                    content=result.content,
                    model=result.model,
                    usage=usage_total,
                    tool_trace=tool_trace,
                    mode="PLAN" if plan else "REACT",
                    plan=list(plan or []),
                )

            # 回填 assistant 消息（含 tool_calls），再逐个执行工具
            messages.append({
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in result.tool_calls
                ],
            })

            # 工具执行：连续的安全（查询类）调用并发执行（规约 §6 单步并行，
            # 上限 5）；danger（写操作）永远单独处理——遇拦截即整轮收尾。
            def _parse_args(tc) -> dict[str, Any]:
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                return args if isinstance(args, dict) else {}

            async def _exec_safe(tc) -> tuple[dict[str, Any], str, bool]:
                """执行一个安全工具，返回 (trace_entry, payload, fingerprint_break)。

                BizAuthError 上抛（登录过期整轮终止）；
                fingerprint_break=True 表示该调用因同指纹熔断被拒绝，
                调用方必须立即转入 _soft_land 收尾（规约 §6.1-2）。"""
                trace_entry: dict[str, Any] = {"step": step, "tool": tc.name}
                args = _parse_args(tc)
                trace_entry["arguments"] = args
                logger.info("AgentLoop 工具调用 step=%d tool=%s args=%s", step, tc.name, args)

                # ---- 循环指纹分级熔断（规约 §6.1-1）----
                # 第 1 次正常执行；第 2 次执行但提醒；第 3 次拒绝并触发收尾
                fp = self._fingerprint(tc.name, args)
                seen = self._fp_seen(session_id, fp)
                if seen >= self.same_call_max - 1:
                    trace_entry["ok"] = False
                    trace_entry["blocked"] = "fingerprint_break"
                    trace_entry["error"] = f"同参数重复调用已被熔断（第 {seen + 1} 次）"
                    logger.warning(
                        "循环指纹熔断 session=%s tool=%s 出现第 %d 次",
                        session_id, tc.name, seen + 1,
                    )
                    telemetry.record_fault(
                        PATH_FP_BREAK, session_id,
                        detail=f"本应执行 {tc.name}，实际熔断拒绝"
                               f"（原因：同指纹第 {seen + 1} 次出现）",
                    )
                    payload = json.dumps({
                        "error": f"你已经用完全相同的参数调用过 {tc.name} 共 {seen} 次，"
                                 "本次调用被熔断。请基于已有结果直接作答。",
                    }, ensure_ascii=False)
                    return trace_entry, payload, True

                # ---- 单工具连续失败熔断（规约 §6.1-3）----
                if self._tool_is_broken(session_id, tc.name):
                    fails = self._tool_fails[session_id][tc.name]
                    trace_entry["ok"] = False
                    trace_entry["blocked"] = "tool_fail_break"
                    trace_entry["error"] = f"工具连续失败 {fails} 次，已熔断"
                    telemetry.record_fault(
                        PATH_TOOL_FAIL_BREAK, session_id,
                        detail=f"本应执行 {tc.name}，实际熔断拒绝"
                               f"（原因：连续失败 {fails} 次）",
                    )
                    payload = json.dumps({
                        "error": f"工具 {tc.name} 已连续失败 {fails} 次，疑似服务故障，"
                                 "本轮请停止调用它；如需继续请换用其他工具或直接作答。",
                    }, ensure_ascii=False)
                    return trace_entry, payload, False

                # 指纹只在实际执行时记录（拒绝/确认门拦截均不记录，规约 §6.6）
                self._fp_record(session_id, fp)

                timeout_s = _TOOL_TIMEOUT_OVERRIDES.get(tc.name, self.tool_timeout)
                _t0 = time.perf_counter()
                try:
                    output = await asyncio.wait_for(
                        registry.execute(tc.name, identity, args), timeout=timeout_s
                    )
                    self._tool_ok(session_id, tc.name)  # 成功清零连续失败计数
                    trace_entry["durationMs"] = round((time.perf_counter() - _t0) * 1000, 1)
                    trace_entry["ok"] = True
                    if seen == self.same_call_max - 2:
                        # 第 N-1 次同指纹：执行，但在结果里附提醒（规约 §6.1-1）
                        telemetry.record_fault(
                            PATH_FP_WARN, session_id,
                            detail=f"本应静默执行 {tc.name}，实际附加重复提醒"
                                   f"（原因：同指纹第 {seen + 1} 次执行）",
                        )
                        payload = json.dumps({
                            "reminder": (
                                f"⚠️ 你已经用完全相同的参数调用过 {tc.name}，"
                                "结果不会变化；请换参数、换工具，或直接汇总作答。"
                            ),
                            "result": output,
                        }, ensure_ascii=False)
                    else:
                        payload = json.dumps(output, ensure_ascii=False)
                except BizAuthError:
                    raise  # 登录过期：整轮终止，交上层转固定话术
                except BizError as exc:
                    fails = self._tool_failed(session_id, tc.name)
                    trace_entry["durationMs"] = round((time.perf_counter() - _t0) * 1000, 1)
                    # 业务错误（权限/数据不存在）：作为工具结果回填，让模型如实告知用户
                    trace_entry["ok"] = False
                    trace_entry["error"] = str(exc)
                    payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    logger.info("工具 %s 连续失败第 %d 次: %s", tc.name, fails, exc)
                except asyncio.TimeoutError:
                    fails = self._tool_failed(session_id, tc.name)
                    trace_entry["durationMs"] = round((time.perf_counter() - _t0) * 1000, 1)
                    # 超时按一次失败计入连续失败计数（规约 §6.1-4）
                    trace_entry["ok"] = False
                    trace_entry["error"] = f"工具执行超时({timeout_s:.0f}s)"
                    payload = json.dumps({"error": f"工具执行超时({timeout_s:.0f}s)"},
                                         ensure_ascii=False)
                    telemetry.record_fault(
                        PATH_TOOL_TIMEOUT, session_id,
                        detail=f"本应 {timeout_s:.0f}s 内完成 {tc.name}，"
                               f"实际超时（连续失败第 {fails} 次）",
                    )
                    logger.warning("工具 %s 执行超时（连续失败第 %d 次）", tc.name, fails)
                except TypeError as exc:
                    # 模型传参不合法：模型自愈范畴，不计入失败熔断计数（规约 §6.1-3）
                    logger.warning("工具 %s 参数错误: %s", tc.name, exc)
                    trace_entry["durationMs"] = round((time.perf_counter() - _t0) * 1000, 1)
                    trace_entry["ok"] = False
                    trace_entry["error"] = f"参数不合法: {exc}"
                    payload = json.dumps({"error": f"参数不合法: {exc}"}, ensure_ascii=False)
                return trace_entry, payload, False

            calls = list(result.tool_calls)
            idx = 0
            while idx < len(calls):
                # ---- 连续安全块：并发执行（上限 5） ----
                batch_start = idx
                while idx < len(calls) and not registry.is_dangerous(calls[idx].name):
                    idx += 1
                batch = calls[batch_start:idx]
                if batch:
                    semaphore = asyncio.Semaphore(5)

                    async def _run_one(tc):
                        async with semaphore:
                            return await _exec_safe(tc)

                    executed = await asyncio.gather(*(_run_one(tc) for tc in batch))
                    fp_break = False
                    for (trace_entry, payload, brk), tc in zip(executed, batch):
                        tool_trace.append(trace_entry)
                        if brk:
                            fp_break = True
                        if len(payload) > _MAX_TOOL_OUTPUT_CHARS:
                            payload = payload[:_MAX_TOOL_OUTPUT_CHARS] + "...(截断)"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": payload,
                        })
                    if fp_break:
                        # 熔断恢复：不再给模型继续调工具的机会，立即软着陆（规约 §6.1-2）
                        return await _soft_land("fingerprint_break", step)
                    continue

                # ---- 危险白名单拦截：写操作一律转二次确认，不当场执行 ----
                tc = calls[idx]
                idx += 1
                args = _parse_args(tc)
                trace_entry = {"step": step, "tool": tc.name, "arguments": args}
                trace_entry["ok"] = False
                trace_entry["blocked"] = "pending_confirmation"
                tool_trace.append(trace_entry)
                # 角色不符：不进确认流程，直接把错误回填让模型如实转述
                try:
                    ensure_can_write(identity)
                except BizError as exc:
                    trace_entry["error"] = str(exc)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"error": str(exc)}, ensure_ascii=False),
                    })
                    continue
                action = build_action(session_id, tc.name, args, identity)
                pending_store.put(action)
                logger.info(
                    "写操作已拦截待确认 session=%s tool=%s desc=%s",
                    session_id, tc.name, action.description,
                )
                return LoopResult(
                    content=build_confirm_reply(action),
                    model=result.model,
                    usage=usage_total,
                    tool_trace=tool_trace,
                    mode="PLAN" if plan else "REACT",
                    plan=list(plan or []),
                )

        # 熔断：达到最大步数模型还在调工具——软着陆收尾（规约 §6.1-5）
        return await _soft_land("max_steps", self.max_steps)


agent_loop = AgentLoop()

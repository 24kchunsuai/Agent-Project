"""Plan-and-Execute 执行引擎 + 复杂度路由（规约 §7）。

设计要点（照搬规约，克制实现）：
- ComplexityRouter：规则优先、LLM 兜底、失败降级 REACT——主链路永不因
  路由失败阻塞（判 PLAN：命中意图词 / 长度>80 / 跨 ≥2 资源域）；
- PlanningAgent：一次 LLM 调用生成步骤清单（JSON，≤max_plan_steps 步，
  工具提示只写"哪类工具"不绑定具体工具名），然后**复用同一个 AgentLoop**
  执行（不维护第二套循环）；某步连续失败 2 次时给一次重排机会，
  再失败按已获数据收尾（AgentLoop 自带 finalize）；
- 所有失败路径都降级为 REACT 或直接返回，绝不上抛新异常类型。
"""

import json
import logging
import re
from typing import Any

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.prompts import load_prompt
from app.services.agent_loop import LoopResult, agent_loop
from app.services.telemetry import PATH_PLAN_DEGRADED, PATH_PLAN_REPLAN, telemetry

logger = logging.getLogger(__name__)

# 资源域关键词 → 域名（跨 ≥2 域判 PLAN，见规约 §7.1）
_DOMAIN_WORDS: list[tuple[str, str]] = [
    ("无人机", "device"), ("设备", "device"), ("维保", "device"), ("维修", "device"),
    ("任务", "task"), ("工单", "task"),
    ("告警", "alarm"), ("报警", "alarm"),
    ("飞手", "user"), ("用户", "user"), ("账号", "user"),
    ("围栏", "fence"), ("禁飞", "fence"),
    ("媒体", "media"), ("影像", "media"), ("照片", "media"), ("视频", "media"),
]

_PLAN_JSON_RE = re.compile(r"\{[\s\S]*\}")


class ComplexityRouter:
    """复杂度路由：REACT / PLAN。规则优先，LLM 只兜边界情况。"""

    def __init__(self) -> None:
        self._keywords = [
            k.strip() for k in settings.agent_router_intent_keywords.split(",") if k.strip()
        ]

    def _rule(self, text: str) -> str | None:
        """规则判定；返回 None 表示交给 LLM 兜底。"""
        if any(k in text for k in self._keywords):
            return "PLAN"
        domains = {domain for word, domain in _DOMAIN_WORDS if word in text}
        if len(domains) >= 2:
            return "PLAN"
        if len(text.strip()) > 80:
            return "PLAN"
        if len(text.strip()) < 30:
            return "REACT"
        return None

    async def _llm_adjudicate(self, text: str) -> str:
        """LLM 一次轻量裁决；失败降级 REACT（规约 §7.1）。"""
        try:
            result = await llm_client.chat(
                [
                    {"role": "system", "content": load_prompt("router_classify")},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=8,
            )
            answer = (result.content or "").strip().upper()
            return "PLAN" if "PLAN" in answer else "REACT"
        except LLMError as exc:
            logger.warning("复杂度 LLM 裁决失败，降级 REACT: %s", exc)
            return "REACT"

    async def classify(self, text: str) -> str:
        """返回 'REACT' 或 'PLAN'；永不抛异常。"""
        mode = settings.agent_router_mode
        try:
            if mode == "llm_only":
                return await self._llm_adjudicate(text)
            rule = self._rule(text)
            if mode == "rule_only":
                return rule or "REACT"
            if rule is not None:
                return rule
            return await self._llm_adjudicate(text)
        except Exception as exc:  # 路由绝不阻塞主链路
            logger.warning("复杂度路由异常，降级 REACT: %s", exc)
            return "REACT"


class PlanningAgent:
    """先生成计划，再复用 AgentLoop 执行。"""

    async def _make_plan(
        self, question: str, history: list[dict[str, str]],
        failure_summary: str = "",
    ) -> list[dict[str, Any]] | None:
        """生成步骤清单；任何失败返回 None（调用方降级 REACT）。"""
        system = (
            load_prompt("planning_system")
            .replace("{max_steps}", str(settings.agent_planning_max_plan_steps))
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if failure_summary:
            messages.append({
                "role": "system",
                "content": f"上一次执行中断，失败情况：{failure_summary}\n"
                           "请重新规划剩余工作，已成功获取的数据不要重复获取。",
            })
        messages.extend(history)
        messages.append({"role": "user", "content": question})
        try:
            result = await llm_client.chat(messages, temperature=0.1, max_tokens=512)
            match = _PLAN_JSON_RE.search(result.content or "")
            if not match:
                return None
            data = json.loads(match.group(0))
            steps = data.get("steps")
            if not isinstance(steps, list) or not steps:
                return None
            plan = []
            for s in steps[: settings.agent_planning_max_plan_steps]:
                if not isinstance(s, dict) or not s.get("action"):
                    continue
                plan.append({
                    "action": str(s["action"]),
                    "tool_hint": str(s.get("tool_hint") or ""),
                    "expect": str(s.get("expect") or ""),
                    "status": "pending",
                })
            return plan or None
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("计划生成失败: %s", exc)
            return None

    @staticmethod
    def _plan_labels(plan: list[dict[str, Any]]) -> list[str]:
        return [f"{i + 1}. {s['action']}" for i, s in enumerate(plan)]

    @staticmethod
    def _consecutive_failures(tool_trace: list[dict[str, Any]]) -> int:
        """末尾连续失败的工具执行次数（不含拦截/标记类条目）。"""
        count = 0
        for entry in reversed(tool_trace):
            if "ok" not in entry:
                continue
            if entry.get("ok"):
                break
            count += 1
        return count

    async def run(
        self,
        question: str,
        history: list[dict[str, str]],
        identity: dict[str, Any],
        session_id: str = "default",
        forced_delegation: str = "",
    ) -> LoopResult:
        """PLAN 模式入口：规划 → 执行 →（必要时）一次重排 → 收尾。

        forced_delegation 非空时透传给循环（确定性委派，DEV_ISSUES §6.55）——
        重排的第二次执行也要透传，否则"重排把委派丢了"。
        """
        plan = await self._make_plan(question, history)
        if plan is None:
            logger.warning("PLAN 生成失败，降级 REACT session=%s", session_id)
            telemetry.record_fault(
                PATH_PLAN_DEGRADED, session_id,
                detail="本应 PLAN 执行，实际降级 REACT（原因：计划生成/解析失败）",
            )
            return await agent_loop.run(
                question, history, identity, session_id=session_id,
                forced_delegation=forced_delegation,
            )

        logger.info("PLAN 生成 %d 步 session=%s: %s", len(plan), session_id, self._plan_labels(plan))
        result = await agent_loop.run(
            question, history, identity, session_id=session_id,
            plan=self._plan_labels(plan),
            forced_delegation=forced_delegation,
        )
        # 步骤状态标注：循环内未做单步追踪，按"轨迹里有失败"粗粒度标注（P1 前端逐项打勾时细化）
        has_failure = any(not e.get("ok") for e in result.tool_trace if "ok" in e)

        # re-plan：连续失败 ≥2 步 → 一次重排机会（规约 §7.2-3）
        if self._consecutive_failures(result.tool_trace) >= 2:
            failures = [
                f"{e.get('tool')} 失败: {e.get('error')}"
                for e in result.tool_trace[-2:] if not e.get("ok")
            ]
            logger.warning("PLAN 执行连续失败，触发一次重排 session=%s", session_id)
            telemetry.record_fault(
                PATH_PLAN_REPLAN, session_id,
                detail="本应一次执行完成，实际触发重排（原因：末尾连续失败 ≥2 步）",
            )
            plan2 = await self._make_plan(question, history, failure_summary="；".join(failures))
            if plan2:
                second = await agent_loop.run(
                    question, history, identity, session_id=session_id,
                    plan=self._plan_labels(plan2), replanned=True,
                    forced_delegation=forced_delegation,
                )
                # 合并观测数据：内容以第二次执行为准，轨迹/用量累加
                second.tool_trace = [*result.tool_trace, *second.tool_trace]
                for key in ("prompt_tokens", "completion_tokens"):
                    second.usage[key] = (
                        result.usage.get(key, 0) + second.usage.get(key, 0)
                    )
                return second
            # 重排也失败：按已获数据收尾（result 已含 finalize 内容）
            result.plan = [f"{label}（未完成，已按已获数据总结）"
                           for label in self._plan_labels(plan)] if has_failure \
                          else self._plan_labels(plan)
            return result

        result.plan = self._plan_labels(plan)
        return result


planning_agent = PlanningAgent()
complexity_router = ComplexityRouter()

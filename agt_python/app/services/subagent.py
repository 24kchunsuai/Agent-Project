"""多智能体编排：Supervisor-Worker 一层（规约 §8）。

设计要点（克制版）：
- 3 个专家子智能体，定义文件化（app/prompts/agents/AGENT.md，frontmatter 含
  name/description/prompt/tools/max-turns/timeout-seconds），照搬 open-agent-master 模式；
- 委派工具 delegate_subagent(name, task) 注册进主 registry，描述里动态列出
  可用子智能体——LLM 自主选择，不做独立路由节点；
- 子智能体工具白名单 = AGENT.md 声明 ∩ 主注册表，且**一律剔除 danger 工具**
  （子智能体只做只读分析，写操作必须回到主循环过确认门）；
- 黑板模式：子智能体结果按名字追加进会话级 TaskContext（同键上限 5 条），
  主循环下一轮注入 system 统一汇总，子智能体之间不直接对话；
- 预算：会话级委派上限（settings.agent_subagent_max_calls_per_session），
  超限返回明确错误而不是静默失败；
- 超时：asyncio.wait_for，超时返 status=TIMEOUT 并写黑板"该专家未完成"，
  主循环基于已有结果收尾，绝不空转等待。
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.services.biz import BizError
from app.services.telemetry import PATH_SUBAGENT_BUDGET, PATH_SUBAGENT_TIMEOUT, telemetry
from app.services.tools import Tool, registry

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "agents"

# 黑板：同一子智能体名下最多保留的结论条数（旧的淘汰）
_BLACKBOARD_KEY_CAP = 5
# 单条黑板结论的摘要长度上限（防超长输出撑爆主循环上下文）
_BLACKBOARD_ENTRY_CHARS = 600


@dataclass
class SubagentDef:
    """一个专家子智能体的定义（来自 AGENT.md frontmatter）。"""

    name: str
    description: str
    prompt: str                      # 含 {task} 占位
    tools: list[str]
    max_turns: int
    timeout_seconds: int


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """极简 frontmatter 解析：只支持本项目用到的三种形态——
    key: 单行值 / key: a, b, c（列表）/ key: | 块文本（后续缩进行）。
    不引 PyYAML，避免只为几个标量拖一个依赖。
    """
    meta: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            m = re.match(r"^(\w[\w-]*):\s*(.*)$", lines[i])
            if not m:
                i += 1
                continue
            key, val = m.group(1), m.group(2).strip()
            if val == "|":
                block: list[str] = []
                i += 1
                while i < len(lines) and (lines[i].startswith(("  ", "\t")) or not lines[i].strip()):
                    block.append(lines[i].strip())
                    i += 1
                meta[key] = "\n".join(block).strip()
                continue
            if "," in val:
                meta[key] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                meta[key] = val
            i += 1
    return meta


def load_subagents() -> list[SubagentDef]:
    """加载 prompts/agents/ 下全部 AGENT.md，按文件名排序保证稳定。"""
    defs: list[SubagentDef] = []
    if not AGENTS_DIR.is_dir():
        return defs
    for f in sorted(AGENTS_DIR.glob("*.md")):
        meta = _parse_frontmatter(f.read_text(encoding="utf-8"))
        try:
            defs.append(SubagentDef(
                name=str(meta["name"]),
                description=str(meta.get("description", "")),
                prompt=str(meta.get("prompt", "你的任务是：{task}")),
                tools=[str(t) for t in meta.get("tools", [])],
                max_turns=int(meta.get("max-turns", 6)),
                timeout_seconds=int(meta.get("timeout-seconds", 60)),
            ))
        except (KeyError, ValueError) as exc:
            logger.error("子智能体定义不完整，已跳过 %s: %s", f.name, exc)
    return defs


SUBAGENTS: list[SubagentDef] = load_subagents()
SUBAGENT_MAP: dict[str, SubagentDef] = {d.name: d for d in SUBAGENTS}


def _whitelist_tools(defn: SubagentDef) -> list[str]:
    """白名单 = 声明 ∩ 主注册表，且一律剔除 danger 工具（只读原则）。"""
    allowed: list[str] = []
    for name in defn.tools:
        tool = registry.get(name)
        if tool and not tool.danger:
            allowed.append(name)
    return allowed


class SubagentRunner:
    """子智能体执行器：受限工具循环 + 预算 + 黑板。"""

    def __init__(self) -> None:
        self._parallel = asyncio.Semaphore(settings.agent_subagent_parallelism)
        self._budget: dict[str, int] = {}          # session_id -> 已委派次数
        self._blackboard: dict[str, dict[str, list[str]]] = {}  # session -> agent -> [结论]

    # ---- 黑板（规约 §8.3）----

    def blackboard_append(self, session_id: str, agent: str, summary: str) -> None:
        board = self._blackboard.setdefault(session_id, {})
        entries = board.setdefault(agent, [])
        entries.append(summary[:_BLACKBOARD_ENTRY_CHARS])
        if len(entries) > _BLACKBOARD_KEY_CAP:
            del entries[: len(entries) - _BLACKBOARD_KEY_CAP]

    def blackboard_get(self, session_id: str) -> dict[str, list[str]]:
        return self._blackboard.get(session_id, {})

    def blackboard_note_incomplete(self, session_id: str, agent: str) -> None:
        self.blackboard_append(session_id, agent, "（该专家未在时限内完成，以上无有效结论）")

    def render_blackboard(self, session_id: str) -> str:
        """把黑板渲染成可注入主循环 system 的文本（空则返回空串）。"""
        board = self.blackboard_get(session_id)
        if not board:
            return ""
        parts: list[str] = []
        for agent, entries in board.items():
            parts.append(f"【{agent}】\n" + "\n".join(f"- {e}" for e in entries))
        return "\n".join(parts)

    # ---- 预算 ----

    def _spend_budget(self, session_id: str) -> int:
        """花费一次委派名额，返回剩余次数；超限抛 BizError。"""
        cap = settings.agent_subagent_max_calls_per_session
        used = self._budget.get(session_id, 0)
        if used >= cap:
            telemetry.record_fault(
                PATH_SUBAGENT_BUDGET, session_id,
                detail=f"本应继续委派，实际拒绝（原因：会话委派已达上限 {cap} 次）",
            )
            raise BizError(
                f"本会话的专家委派次数已用完（上限 {cap} 次）。"
                "请直接提问，我会用已有信息回答。"
            )
        self._budget[session_id] = used + 1
        return cap - used - 1

    # ---- 执行 ----

    async def _run_inner_loop(self, defn: SubagentDef, task: str,
                              identity: dict[str, Any]) -> str:
        """子智能体的受限 ReAct 循环：白名单工具 + max_turns 熔断。"""
        allowed = _whitelist_tools(defn)
        if not allowed:
            raise BizError(f"子智能体 {defn.name} 没有可用工具（白名单与注册表无交集）")
        tools_schema = [
            {"type": "function", "function": {
                "name": registry.get(n).name,
                "description": registry.get(n).description,
                "parameters": registry.get(n).parameters,
            }}
            for n in allowed
        ]
        system = defn.prompt.replace("{task}", task)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        for _turn in range(defn.max_turns):
            result = await llm_client.chat(messages, temperature=0.3, tools=tools_schema)
            if not result.tool_calls:
                return result.content or ""
            messages.append({
                "role": "assistant",
                "content": result.content or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in result.tool_calls
                ],
            })
            for tc in result.tool_calls:
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                # 白名单二次校验：模型点名白名单外的工具一律拒绝
                if tc.name not in allowed:
                    payload = json.dumps(
                        {"error": f"工具 {tc.name} 不在 {defn.name} 的授权范围内"},
                        ensure_ascii=False,
                    )
                else:
                    try:
                        # 容错继承（规约 §6.4/原则9）：子循环工具执行同样套
                        # wait_for 超时兜底，防单个工具卡死拖挂整个子智能体。
                        # 延迟导入 agent_loop：模块级会循环引用（agent_loop → 本模块）
                        from app.services.agent_loop import agent_loop

                        output = await asyncio.wait_for(
                            registry.execute(
                                tc.name, identity, args if isinstance(args, dict) else {}
                            ),
                            timeout=agent_loop.tool_timeout,
                        )
                        payload = json.dumps(output, ensure_ascii=False)
                    except asyncio.TimeoutError:
                        payload = json.dumps(
                            {"error": f"工具执行超时({agent_loop.tool_timeout:.0f}s)"},
                            ensure_ascii=False,
                        )
                    except Exception as exc:  # 业务错误回填，子循环内自愈，不上抛
                        payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                if len(payload) > 2000:
                    payload = payload[:2000] + "...(截断)"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
        # max_turns 耗尽：要求只总结不再调工具
        messages.append({"role": "system", "content": (
            "已达到你的工具调用轮次上限，禁止再调用工具。"
            "请基于已获取的信息直接输出结论；信息不足就如实说明。"
        )})
        final = await llm_client.chat(messages, temperature=0.2, max_tokens=768)
        return final.content or ""

    async def delegate(self, session_id: str, name: str, task: str,
                       identity: dict[str, Any]) -> dict[str, Any]:
        """委派入口（也是 delegate_subagent 工具的 handler 核心）。"""
        defn = SUBAGENT_MAP.get(name)
        if defn is None:
            available = ", ".join(d.name for d in SUBAGENTS) or "（无）"
            raise BizError(f"未知子智能体「{name}」，可选：{available}")
        remaining = self._spend_budget(session_id)
        t0 = time.perf_counter()
        logger.info("子智能体委派 session=%s agent=%s task=%s", session_id, name, task[:80])
        try:
            async with self._parallel:
                content = await asyncio.wait_for(
                    self._run_inner_loop(defn, task, identity),
                    timeout=defn.timeout_seconds,
                )
        except asyncio.TimeoutError:
            self.blackboard_note_incomplete(session_id, name)
            logger.warning("子智能体超时 session=%s agent=%s timeout=%ds",
                           session_id, name, defn.timeout_seconds)
            telemetry.record_fault(
                PATH_SUBAGENT_TIMEOUT, session_id,
                detail=f"本应 {defn.timeout_seconds}s 内完成，实际超时（专家：{name}）",
            )
            telemetry.record_delegation(
                session_id=session_id, agent=name, status="TIMEOUT",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
            return {
                "status": "TIMEOUT",
                "agent": name,
                "message": (f"{name} 在 {defn.timeout_seconds} 秒内未完成，"
                            "已记录「该专家未完成」。可基于已有信息继续，或稍后重试。"),
            }
        except LLMError as exc:
            logger.error("子智能体 LLM 故障 session=%s agent=%s: %s", session_id, name, exc)
            self.blackboard_note_incomplete(session_id, name)
            telemetry.record_delegation(
                session_id=session_id, agent=name, status="FAILED",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
            return {"status": "FAILED", "agent": name,
                    "message": f"{name} 因大模型服务故障未能完成，请稍后重试。"}
        duration = round((time.perf_counter() - t0) * 1000, 1)
        summary = f"[{duration}ms] {content}"
        self.blackboard_append(session_id, name, summary)
        logger.info("子智能体完成 session=%s agent=%s duration=%sms", session_id, name, duration)
        telemetry.record_delegation(
            session_id=session_id, agent=name, status="DONE", duration_ms=duration,
        )
        return {
            "status": "DONE",
            "agent": name,
            "durationMs": duration,
            "remainingDelegations": remaining,
            "conclusion": content,
        }


runner = SubagentRunner()


async def _delegate_subagent_async(
    identity: dict[str, Any], name: str, task: str
) -> dict[str, Any]:
    session_id = str(identity.get("_session_id") or "default")
    return await runner.delegate(session_id, name, task, identity)


def register_delegate_tool() -> None:
    """把 delegate_subagent 注册进主 registry（子智能体启用时调用）。"""
    if not SUBAGENTS:
        logger.warning("未找到任何子智能体定义，delegate_subagent 不注册")
        return
    listing = "；".join(f"{d.name}（{d.description}）" for d in SUBAGENTS)
    registry.register(Tool(
        name="delegate_subagent",
        description=(
            "把一个只读分析类子任务委派给专家子智能体，拿到它的结论后由你汇总回答。"
            "可用专家：" + listing + "。"
            "适合跨域组合场景（如应急响应=告警排查+设备定位；运营报告=统计成文）。"
            "注意：子智能体只有查询类工具，不能执行任何写操作；"
            "委派时 task 要写清楚目标与需要的输出形式。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "子智能体名", "enum": [d.name for d in SUBAGENTS]},
                "task": {"type": "string", "description": "交给专家的具体任务描述（一到两句，说清要什么结论）"},
            },
            "required": ["name", "task"],
        },
        handler=_delegate_subagent_async,
    ))
    logger.info("delegate_subagent 已注册（%d 个子智能体）", len(SUBAGENTS))


# 按配置开关注册（规约 §12：agent.subagent.enabled，P1 打开）
if settings.agent_subagent_enabled:
    register_delegate_tool()

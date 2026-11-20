"""确定性委派路由（DEV_ISSUES §6.55）。

背景：委派原本完全由 LLM 自主决定 → 同一问题时而委派、时而不委派
（实测 biz-057：上一会话未委派，下一会话连续 6 次全中）。评测把它写成硬断言后，
"抖动"会伪装成产品回归，多智能体设计用例的覆盖率也变成"看运气"。

做法（克制、可关）：**只对规则命中的设计用例**在进循环前确定性委派一次，
其余请求保持 LLM 自主，不越权扩大委派面。规则保守、纯函数、永不抛异常；
开关 `agent_delegation_rule_enabled=false` 时行为与改造前完全一致（可回退验证）。
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# 点名类：出现这些词即指定专家（按顺序匹配，先到先得）
_NAMED_RULES: list[tuple[tuple[str, ...], str]] = [
    (("运营专家", "运营简报", "运营报告", "运营周报"), "ops-reporter"),
    (("告警分析专家", "告警专家"), "alarm-analyst"),
    (("定位专家", "设备定位专家"), "device-locator"),
]

# 严重级 + 分析诉求 → 告警分析专家
# 典型："D007 电子围栏越界这条 P0 告警挺严重的，帮我深入分析一下原因和处置建议"
_SEVERE_WORDS = ("P0", "p0", "严重", "紧急")
_ANALYSIS_WORDS = (
    "深入分析", "深度分析", "研判", "处置建议", "原因分析", "分析原因",
    "根因", "分析一下原因", "分析下原因",
)

# 各专家的任务说明尾巴（委派任务要写清目标与产出形式，不然专家只能泛泛而谈）
_AGENT_TASK_HINT = {
    "alarm-analyst": "请基于只读查询结论，分析该告警的可能原因、影响范围与处置优先级，给出可执行建议。",
    "device-locator": "请定位相关设备/区域的位置与当前状态，给出结论及数据依据。",
    "ops-reporter": "请汇总相关统计指标，输出一份结构化运营简报（含关键数字与结论）。",
}


def pick_agent(text: str) -> str:
    """返回应确定性委派的专家名；无匹配返回 ""（交回 LLM 自主）。

    纯规则、无副作用、永不抛异常——主链路不得因规则失败而阻塞。
    """
    if not settings.agent_delegation_rule_enabled:
        return ""
    try:
        t = (text or "").strip()
        if not t:
            return ""
        for words, agent in _NAMED_RULES:
            if any(w in t for w in words):
                return agent
        if any(w in t for w in _SEVERE_WORDS) and any(w in t for w in _ANALYSIS_WORDS):
            return "alarm-analyst"
    except Exception as exc:  # noqa: BLE001 —— 规则永不阻塞主链路
        logger.warning("确定性委派规则异常，退回 LLM 自主: %s", exc)
    return ""


def task_brief(text: str, agent: str) -> str:
    """拼给专家的任务描述：用户原话 + 明确的输出要求。"""
    hint = _AGENT_TASK_HINT.get(agent, "请给出结论。")
    return f"用户诉求：{text}\n\n{hint}"

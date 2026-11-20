"""全链路容错共享常量（docs/specs/全链路容错规约.md §6.8）。

兜底/降级类固定话术统一在此定义为 sentinel 常量：
- 各产生方（agent_loop / biz）引用常量生成回复；
- 会话历史写入方（agent_service._memory_reply）比对常量决定是否入历史；
- 只做全等比对，不做模糊匹配——避免误伤正常回答（铁律：判定宁缺毋滥）。

原则（规约 §9-4）：兜底文案不入会话历史——致歉/熔断/失败话术写入历史
= 慢性中毒，模型会基于它们继续道歉。
"""

# 4010 鉴权失败统一话术（biz._request 归一化时使用）
AUTH_EXPIRED_REPLY = "登录已过期，请重新登录后再使用业务功能"

# LLM 故障兜底（agent_loop._fallback）
LLM_FALLBACK_REPLY = (
    "智能体服务这会儿不太稳定，没有完成这次查询。请稍后重试；"
    "如果持续失败，请联系管理员检查大模型服务状态。"
)

# 熔断恢复（无工具 finalize）也失败的最终文案
BREAK_FALLBACK_REPLY = (
    "检测到重复操作已被安全熔断，且自动总结也未能完成。"
    "请把问题换一种问法，或稍后重试。"
)

_NON_PERSISTENT_REPLIES = frozenset({
    AUTH_EXPIRED_REPLY,
    LLM_FALLBACK_REPLY,
    BREAK_FALLBACK_REPLY,
})


def is_persistent_reply(text: str) -> bool:
    """True=可写入会话历史；False=兜底/熔断类文案，绝不能进历史（规约 §6.8）。"""
    return text not in _NON_PERSISTENT_REPLIES

"""Agent 接口的数据契约。

字段统一用 camelCase 对外（与 Java / 前端保持一致），
内部代码用 snake_case 访问，靠 alias + populate_by_name 自动转换。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatMessage(BaseModel):
    """一条对话消息。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatRequest(BaseModel):
    """Java 后端 -> Python agent 的对话请求。"""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId", description="会话 ID，由 Java 后端生成并透传")
    user_id: int = Field(alias="userId", description="用户 ID，用于后续做记忆隔离")
    message: str = Field(min_length=1, description="本轮用户输入")
    history: list[ChatMessage] = Field(default_factory=list, description="历史消息，由 Java 侧裁剪后传入")
    metadata: dict[str, Any] = Field(default_factory=dict, description="预留扩展位，如工具开关、知识库范围")

    # Java 侧若把 null 直接序列化过来（history: null），这里兜底成空集合，
    # 避免因为一个可选字段就整单拒绝。
    @field_validator("history", mode="before")
    @classmethod
    def _none_to_empty_list(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("metadata", mode="before")
    @classmethod
    def _none_to_empty_dict(cls, value: Any) -> Any:
        return {} if value is None else value


class FeedbackRequest(BaseModel):
    """数据中台 P2 反馈提交（数据中台规约 §6；二值评分，开放问题 2 拍板）。"""

    model_config = ConfigDict(populate_by_name=True)

    trace_id: str = Field(alias="traceId", description="要评价的会话轨迹 ID")
    rating: str = Field(description="二值评分：up（赞）/ down（踩）")
    session_id: str = Field(default="", alias="sessionId", description="会话 ID，可选（缺省从 trace 反查）")
    comment: str = Field(default="", description="文字备注，可选（只存摘要 ≤200 字符）")


class ChatResponse(BaseModel):
    """Python agent -> Java 后端的对话响应。"""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    content: str = Field(description="模型回复正文")
    intent: str = Field(default="chitchat", description="识别出的意图：chitchat / rag / business")
    intent_source: str = Field(
        default="", alias="intentSource", description="意图判定来源：rule / llm / fallback"
    )
    model: str = Field(default="", description="实际使用的模型标识")
    usage: dict[str, int] = Field(default_factory=dict, description="token 用量统计")
    trace_id: str = Field(default="", alias="traceId", description="链路追踪 ID，便于排查")
    citations: list[dict[str, Any]] = Field(
        default_factory=list, description="RAG 引用来源（docId/docTitle/score/category），非 rag 意图为空"
    )
    rag_decision: str = Field(
        default="", alias="ragDecision",
        description="RAG 链路判定：kept / cross_domain / no_hit / insufficient_evidence / degraded",
    )
    rag_judged: str = Field(
        default="", alias="ragJudged",
        description="LLM judge 评审结果：sufficient / insufficient / skipped",
    )
    rag_rewrite: str = Field(
        default="", alias="ragRewrite",
        description="查询改写来源：dict / dict_suffix / llm / llm+dict / none（DEV_ISSUES 6.37）",
    )
    tool_trace: list[dict[str, Any]] = Field(
        default_factory=list, alias="toolTrace",
        description="业务工具调用轨迹（step/tool/ok/durationMs/error），非 business 意图为空；埋点与前端过程展示共用",
    )
    mode: str = Field(
        default="REACT", description="执行模式：REACT / PLAN（P0 起 ComplexityRouter 判定）",
    )
    plan: list[str] = Field(
        default_factory=list, description="PLAN 模式生成的步骤清单（含执行状态标注），REACT 模式为空",
    )
    quota: dict[str, Any] | None = Field(
        default=None, alias="quota",
        description=(
            "E-13 配额提示：仅当该用户今日 token 用量触顶时非空"
            "（{exceeded, usedTokens, capTokens, message}）。"
            "超限**不拒绝服务**（降级为提示，规约原文）——前端把 message 渲染成黄条即可，"
            "没有这个字段 = 一切正常，不要造一个默认提示出来"
        ),
    )

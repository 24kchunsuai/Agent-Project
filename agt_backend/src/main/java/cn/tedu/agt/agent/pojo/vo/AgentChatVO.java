package cn.tedu.agt.agent.pojo.vo;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Getter;
import lombok.Setter;

import java.util.List;
import java.util.Map;

/**
 * 对话响应结果，返回给前端。
 */
@Setter
@Getter
@Schema(description = "智能体对话响应")
public class AgentChatVO {

    @Schema(description = "会话 ID")
    private String sessionId;

    @Schema(description = "模型回复正文")
    private String content;

    @Schema(description = "识别出的意图：chitchat / business")
    private String intent;

    @Schema(description = "意图判定来源：rule / llm / fallback")
    private String intentSource;

    @Schema(description = "实际使用的模型标识")
    private String model;

    @Schema(description = "token 用量")
    private Map<String, Integer> usage;

    @Schema(description = "链路追踪 ID，排查问题时使用")
    private String traceId;

    // ---- RAG 知识问答分支（intent=rag 时有值，其余意图为空）----

    @Schema(description = "RAG 检索决策：kept / insufficient_evidence / cross_domain / no_hit / degraded")
    private String ragDecision;

    @Schema(description = "LLM judge 结果：sufficient / insufficient / skipped（未走 RAG 为空串）")
    private String ragJudged;

    @Schema(description = "查询改写来源：dict / dict_suffix / llm / llm+dict / none（未走 RAG 为空串）")
    private String ragRewrite;

    @Schema(description = "引用来源列表（文档级去重）：docId / docTitle / score / category")
    private List<Map<String, Object>> citations;

    // ---- 业务执行引擎（规约 §6/§7：过程可观测，前端过程展示与埋点共用）----

    @Schema(description = "业务工具调用轨迹：step / tool / ok / durationMs / error / blocked，非 business 意图为空")
    private List<Map<String, Object>> toolTrace;

    @Schema(description = "执行模式：REACT / PLAN")
    private String mode;

    @Schema(description = "PLAN 模式的步骤清单（含状态标注），REACT 模式为空")
    private List<String> plan;

    // ---- E-13 成本计量与配额（判据 C7）----

    /**
     * 配额提示（Python 侧 quota_check 的产物）。
     *
     * <p><b>为什么必须声明这个字段</b>：本 VO 由 Jackson 直接反序列化 Python 的
     * ChatResponse（{@code AgentApiResponse<AgentChatVO>}）。Python 新增字段而这里
     * 不声明的话，Jackson 会<b>静默丢弃</b>——现象就是"直连 8000 有 quota、走 8080
     * 没了"，用户永远看不到超限提示（AgentHealthVO 踩过同一个坑）。
     *
     * <p>结构：{exceeded, usedTokens, capTokens, message}；仅当日用量触顶时非空。
     * 超限不拒绝服务（规约原文：降级为提示）——前端把 message 渲染成黄条即可。
     */
    @Schema(description = "E-13 配额提示：仅当日 token 用量触顶时非空，超限不拒绝服务（降级为提示）")
    private Map<String, Object> quota;
}

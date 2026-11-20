package cn.tedu.agt.agent.pojo.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Getter;
import lombok.Setter;

import java.util.List;
import java.util.Map;

/**
 * 对话请求参数：前端 -> Java 后端 -> Python 智能体，字段名与 Python 侧契约一致。
 *
 * 注意 NON_NULL：不要把 null 字段（如未传的 history）序列化过去，
 * 否则 Python 侧的类型校验会因为显式 null 而拒绝整个请求。
 */
@Setter
@Getter
@JsonInclude(JsonInclude.Include.NON_NULL)
@Schema(description = "智能体对话请求参数")
public class AgentChatParam {

    @Schema(description = "本轮用户输入", example = "帮我规划今天上午的巡检航线")
    @NotBlank(message = "消息内容不能为空")
    @Size(max = 4000, message = "单次输入不能超过 4000 字")
    private String message;

    @Schema(description = "会话 ID，为空时由后端生成")
    private String sessionId;

    @Schema(description = "用户 ID，用于记忆隔离")
    private Long userId;

    @Schema(description = "历史消息，由前端或后端裁剪后传入")
    private List<AgentChatMessage> history;

    @Schema(description = "扩展位：工具开关、知识库范围等")
    private Map<String, Object> metadata;
}

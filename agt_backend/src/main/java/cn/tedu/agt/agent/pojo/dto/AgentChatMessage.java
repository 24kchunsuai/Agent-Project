package cn.tedu.agt.agent.pojo.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import lombok.Getter;
import lombok.Setter;

/**
 * 一条历史消息。
 */
@Setter
@Getter
@Schema(description = "对话历史中的单条消息")
public class AgentChatMessage {

    @Schema(description = "角色：system / user / assistant / tool", example = "user")
    @NotBlank(message = "消息角色不能为空")
    private String role;

    @Schema(description = "消息内容")
    @NotBlank(message = "消息内容不能为空")
    private String content;
}

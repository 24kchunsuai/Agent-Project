package cn.tedu.agt.agent.pojo.vo;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Getter;
import lombok.Setter;

/**
 * 智能体服务健康状态。
 *
 * <p>字段与 Python 侧 {@code /v1/health} 的 data 一一对应（企业级落地规约 E-1）。
 * <b>加字段时要同步这里</b>：Jackson 默认忽略未知属性，VO 里没声明的字段
 * 会被静默丢弃——表现为"直连 8000 有、走后端 8080 就没了"，
 * 排查时极易误判成服务版本不一致。
 */
@Setter
@Getter
@Schema(description = "智能体服务健康状态")
public class AgentHealthVO {

    @Schema(description = "服务状态")
    private String status;

    @Schema(description = "服务名")
    private String app;

    @Schema(description = "服务版本")
    private String version;

    @Schema(description = "构建时所在的 git 提交：短 hash，或 unborn@分支 / no-git")
    private String gitCommit;

    @Schema(description = "构建时的 git 分支")
    private String gitBranch;

    @Schema(description = "构建时工作区是否有未提交改动（true 表示该构建不可复现）")
    private Boolean gitDirty;

    @Schema(description = "构建时间（ISO8601 带时区）")
    private String buildTime;

    /** 取值来源：stamp（打戳产物）/ runtime（实时求值） */
    @Schema(description = "版本信息取值来源")
    private String versionSource;

    @Schema(description = "大模型是否已配置")
    private Boolean llmConfigured;

    @Schema(description = "Python 运行时版本，如 3.11.15")
    private String python;

    @Schema(description = "Python 运行环境名，如 python311")
    private String pythonEnv;
}

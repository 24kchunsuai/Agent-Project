package cn.tedu.agt.base.response;

import lombok.AllArgsConstructor;
import lombok.Getter;
import lombok.NoArgsConstructor;

/**
 * 业务状态码。沿用 drone 项目的编码段：
 * 2000 成功 / 3000 段为各类业务失败。
 */
@Getter
@AllArgsConstructor
@NoArgsConstructor
public enum StatusCode {
    OPERATION_SUCCESS(2000, "操作成功"),
    OPERATION_FAILED(3000, "操作失败"),
    VALIDATE_ERROR(3001, "参数校验失败"),
    DATA_UNEXISTS(3002, "请求数据不存在"),
    USERNAME_ALREADY_EXISTS(3003, "用户名被占用"),
    USERNAME_ERROR(3004, "用户名或密码错误"),
    PASSWORD_ERROR(3005, "用户名或密码错误"),
    TASK_STATUS_ERROR(3006, "任务状态不允许此操作"),

    // ===== 认证（与 jsdstu-3 的 4010 编码对齐，前端一套处理逻辑） =====
    UNAUTHORIZED(4010, "未登录或登录已过期，请重新登录"),
    PERMISSION_DENIED(4011, "没有执行此操作的权限"),

    // ===== 智能体（Python 服务）相关 =====
    AGENT_UNAVAILABLE(3010, "智能体服务不可用，请稍后重试"),
    AGENT_CALL_ERROR(3011, "智能体调用失败");

    private Integer code;
    private String message;
}

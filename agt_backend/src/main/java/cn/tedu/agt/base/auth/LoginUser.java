package cn.tedu.agt.base.auth;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 登录用户信息（不含任何凭据），可安全返回给前端 / 透传给智能体。
 *
 * <p>字段取自 jsdstu-3 的用户体系，本系统不维护第二套账号。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class LoginUser {

    /** 用户 ID（与 jsdstu-3 用户表一致），用于数据范围与记忆隔离 */
    private Long userId;

    /** 用户名 */
    private String username;

    /** 角色：admin / pilot / repair（与 jsdstu-3 RoleEnum 对应） */
    private String permission;
}

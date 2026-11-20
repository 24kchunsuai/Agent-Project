package cn.tedu.agt.base.auth;

import lombok.Data;

/**
 * 一个登录会话：agt_backend 自己的会话信息 + 该用户在 jsdstu-3 侧的凭据。
 *
 * <p>设计要点：</p>
 * <ul>
 *   <li>用户身份"用户优先"：智能体调用业务工具时使用<strong>当前用户自己</strong>的
 *       jsdstu-3 token，而不是系统账号全权令牌——数据范围与写权限天然由 jsdstu-3
 *       的角色体系收敛（pilot 查不到别人的任务，写操作被 ADMIN 校验拦截）。</li>
 *   <li>凭据仅在内存中保存，用于 token 过期时静默重登；禁止写入日志、禁止序列化给前端。</li>
 *   <li>单实例内存态，与 jsdstu-3 的 TokenStore 同构；多实例部署时再升级 Redis。</li>
 * </ul>
 */
@Data
public class DroneSession {

    /** 展示用身份信息 */
    private LoginUser user;

    // ---- jsdstu-3 侧凭据（敏感，勿外泄/勿打日志） ----

    /** jsdstu-3 登录用户名 */
    private String droneUsername;

    /** jsdstu-3 登录密码（仅内存保存，供过期静默重登） */
    private String dronePassword;

    /** jsdstu-3 的 Bearer token，智能体业务工具调用时使用 */
    private String droneToken;

    /** jsdstu-3 token 过期时间（epoch 毫秒） */
    private long droneTokenExpireAt;

    /** agt_backend 会话过期时间（epoch 毫秒），过期后须重新登录 */
    private long sessionExpireAt;
}

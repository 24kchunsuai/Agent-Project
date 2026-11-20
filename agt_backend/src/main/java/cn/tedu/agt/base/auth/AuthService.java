package cn.tedu.agt.base.auth;

import cn.tedu.agt.base.audit.AuditReporter;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.response.StatusCode;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.util.HashMap;
import java.util.Map;

/**
 * 认证服务：登录代理 + 会话管理 + 智能体身份解析。
 *
 * <p>登录是<strong>代理</strong>而不是复制：账号体系完全来自 jsdstu-3，
 * 本系统只负责签发自己的会话令牌、托管用户的 jsdstu-3 凭据。</p>
 */
@Slf4j
@Service
public class AuthService {

    /** 会话最长有效期：跟随 jsdstu-3 的 8h token，两把锁同寿命最省心 */
    private static final long SESSION_TTL_MS = 8 * 3600 * 1000L;

    private final TokenStore tokenStore;
    private final DroneAuthClient droneAuthClient;
    private final AuditReporter auditReporter;

    public AuthService(TokenStore tokenStore, DroneAuthClient droneAuthClient,
                       AuditReporter auditReporter) {
        this.tokenStore = tokenStore;
        this.droneAuthClient = droneAuthClient;
        this.auditReporter = auditReporter;
    }

    /**
     * 登录：转发 jsdstu-3 校验，成功后建立本地会话。
     *
     * @return JsonResult.data：{token, expiresIn, user}
     */
    public Map<String, Object> login(String username, String password) {
        return login(username, password, null);
    }

    /**
     * 登录（带来源 IP，用于操作审计留痕）。
     *
     * <p>登录成功与失败都留痕：失败尤其重要——撞库的痕迹只能从这里看出来。</p>
     */
    public Map<String, Object> login(String username, String password, String ip) {
        Map<String, Object> result;
        try {
            result = droneAuthClient.login(username, password);
        } catch (RuntimeException e) {
            // 上游（jsdstu-3）不可达也留痕：否则"认证服务挂了"在审计里表现为
            // "压根没人尝试登录"，那是两种完全不同的故障，必须分得开
            auditReporter.loginFail(username, "认证服务不可用：" + e.getClass().getSimpleName(), ip);
            throw e;
        }
        int code = ((Number) result.getOrDefault("code", -1)).intValue();
        if (code != StatusCode.OPERATION_SUCCESS.getCode()) {
            // 登录失败：3004/3005 归一为"用户名或密码错误"，其余原样透传文案
            log.info("jsdstu-3 登录失败:username={},code={}", username, code);
            StatusCode status = (code == StatusCode.USERNAME_ERROR.getCode()
                    || code == StatusCode.PASSWORD_ERROR.getCode())
                    ? StatusCode.USERNAME_ERROR : StatusCode.OPERATION_FAILED;
            String message = String.valueOf(result.getOrDefault("message", "登录失败"));
            // 审计只记"为什么失败"，**绝不记密码**
            auditReporter.loginFail(username, message, ip);
            throw new ServiceException(status, message);
        }

        @SuppressWarnings("unchecked")
        Map<String, Object> data = (Map<String, Object>) result.get("data");
        String droneToken = (String) data.get("token");
        long expiresIn = ((Number) data.getOrDefault("expiresIn", 28800)).longValue();
        @SuppressWarnings("unchecked")
        Map<String, Object> userMap = (Map<String, Object>) data.get("user");

        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder()
                .userId(((Number) userMap.get("id")).longValue())
                .username((String) userMap.get("username"))
                .permission((String) userMap.get("permission"))
                .build());
        session.setDroneUsername(username);
        session.setDronePassword(password);
        session.setDroneToken(droneToken);
        session.setDroneTokenExpireAt(System.currentTimeMillis() + expiresIn * 1000);
        session.setSessionExpireAt(System.currentTimeMillis() + SESSION_TTL_MS);

        String agtToken = tokenStore.create(session);
        log.info("登录成功:userId={},username={},permission={}",
                session.getUser().getUserId(), session.getUser().getUsername(), session.getUser().getPermission());
        // 审计用**上游返回的用户名**而不是入参：两者不一致时（大小写/别名），
        // 审计要跟业务系统的事实一致，而不是跟用户敲进去的字符串一致
        auditReporter.loginOk(session.getUser().getUsername(), ip);

        Map<String, Object> body = new HashMap<>();
        body.put("token", agtToken);
        body.put("expiresIn", SESSION_TTL_MS / 1000);
        body.put("user", userMap);
        return body;
    }

    /** 当前登录人信息；未登录抛 4010 */
    public LoginUser me(String agtToken) {
        DroneSession session = require(agtToken);
        return session.getUser();
    }

    /** 登出：本地会话必删，jsdstu-3 侧尽力而为 */
    public void logout(String agtToken) {
        logout(agtToken, null);
    }

    /** 登出（带来源 IP，用于操作审计留痕）。 */
    public void logout(String agtToken, String ip) {
        DroneSession session = tokenStore.get(agtToken);
        if (session != null) {
            droneAuthClient.logout(session.getDroneToken());
            tokenStore.remove(agtToken);
            log.info("登出成功:userId={}", session.getUser().getUserId());
            auditReporter.logout(session.getUser().getUsername(), ip);
        }
    }

    /**
     * 为智能体调用解析业务身份：返回注入 metadata.identity 的结构，
     * 含该用户<strong>自己的</strong> jsdstu-3 token。
     *
     * <p>jsdstu-3 token 过期时用内存凭据静默重登一次；重登失败则降级为
     * 匿名（返回 null）——知识问答不受影响，业务工具由 Python 侧提示重新登录。</p>
     */
    public Map<String, Object> resolveIdentity(DroneSession session) {
        if (session == null) {
            return null;
        }
        if (System.currentTimeMillis() > session.getDroneTokenExpireAt()) {
            try {
                Map<String, Object> result = droneAuthClient.login(session.getDroneUsername(), session.getDronePassword());
                if (StatusCode.OPERATION_SUCCESS.getCode() == ((Number) result.getOrDefault("code", -1)).intValue()) {
                    @SuppressWarnings("unchecked")
                    Map<String, Object> data = (Map<String, Object>) result.get("data");
                    session.setDroneToken((String) data.get("token"));
                    long expiresIn = ((Number) data.getOrDefault("expiresIn", 28800)).longValue();
                    session.setDroneTokenExpireAt(System.currentTimeMillis() + expiresIn * 1000);
                    log.info("jsdstu-3 token 已静默续期:userId={}", session.getUser().getUserId());
                }
            } catch (Exception e) {
                log.warn("jsdstu-3 token 续期失败，本次降级为匿名:{}", e.getMessage());
                return null;
            }
        }
        LoginUser user = session.getUser();
        Map<String, Object> identity = new HashMap<>();
        identity.put("userId", user.getUserId());
        identity.put("username", user.getUsername());
        identity.put("permission", user.getPermission());
        identity.put("droneToken", session.getDroneToken());
        return identity;
    }

    private DroneSession require(String agtToken) {
        DroneSession session = tokenStore.get(agtToken);
        if (session == null) {
            throw new ServiceException(StatusCode.UNAUTHORIZED);
        }
        return session;
    }
}

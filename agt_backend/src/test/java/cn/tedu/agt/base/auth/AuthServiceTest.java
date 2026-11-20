package cn.tedu.agt.base.auth;

import cn.tedu.agt.base.audit.AuditReporter;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.response.StatusCode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 认证服务（E-5 鉴权组）。
 *
 * <p>本系统的账号体系是**代理** jsdstu-3，自己不存用户表。所以这里要钉住两件事：
 * 登录失败必须**归一**成 3004（不能让 3006 之类的内部码漏到前端），
 * 以及 jsdstu-3 token 过期时的**静默续期 / 降级匿名**——后者是"知识问答不受影响、
 * 只有业务派活要求重新登录"这条体验承诺的实现处。</p>
 *
 * <p>全程 mock {@link DroneAuthClient}，不发任何真实请求。</p>
 */
@ExtendWith(MockitoExtension.class)
class AuthServiceTest {

    private static final String USERNAME = "admin";
    private static final String PASSWORD = "123456";

    @Mock
    private DroneAuthClient droneAuthClient;

    @Mock
    private AuditReporter auditReporter;

    private TokenStore tokenStore;
    private AuthService authService;

    @BeforeEach
    void setUp() {
        tokenStore = new TokenStore();
        authService = new AuthService(tokenStore, droneAuthClient, auditReporter);
    }

    /** jsdstu-3 登录成功的响应体（结构与真实 JsonResult 一致）。 */
    private Map<String, Object> droneLoginOk(String token, long expiresIn) {
        Map<String, Object> user = new HashMap<>();
        user.put("id", 1);
        user.put("username", USERNAME);
        user.put("permission", "admin");

        Map<String, Object> data = new HashMap<>();
        data.put("token", token);
        data.put("expiresIn", expiresIn);
        data.put("user", user);

        Map<String, Object> body = new HashMap<>();
        body.put("code", 2000);
        body.put("message", "操作成功");
        body.put("data", data);
        return body;
    }

    private Map<String, Object> droneLoginFail(int code, String message) {
        Map<String, Object> body = new HashMap<>();
        body.put("code", code);
        body.put("message", message);
        body.put("data", null);
        return body;
    }

    /** 造一个已过期 jsdstu-3 token 的会话，用来触发续期分支。 */
    private DroneSession sessionWithExpiredDroneToken(long ageMs) {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(1L).username(USERNAME).permission("admin").build());
        session.setDroneUsername(USERNAME);
        session.setDronePassword(PASSWORD);
        session.setDroneToken("old-drone-token");
        session.setDroneTokenExpireAt(System.currentTimeMillis() - ageMs);
        session.setSessionExpireAt(System.currentTimeMillis() + 60_000);
        return session;
    }

    // ------------------------------------------------------------ 登录

    @Test
    @DisplayName("登录成功：签发本系统令牌，会话里托管用户自己的 jsdstu-3 凭据")
    void loginSuccessIssuesOwnToken() {
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("drone-token-1", 28800));

        Map<String, Object> body = authService.login(USERNAME, PASSWORD);

        String agtToken = (String) body.get("token");
        assertThat(agtToken).matches("[0-9a-f]{32}");
        assertThat(body.get("expiresIn")).isEqualTo(8 * 3600L);
        assertThat(body.get("user")).isInstanceOf(Map.class);

        // 关键：会话里存着**用户自己**的 drone token，而不是系统账号令牌
        DroneSession session = tokenStore.get(agtToken);
        assertThat(session).isNotNull();
        assertThat(session.getDroneToken()).isEqualTo("drone-token-1");
        assertThat(session.getDroneUsername()).isEqualTo(USERNAME);
    }

    @Test
    @DisplayName("登录失败：3004/3005 归一为 3004（不向前端暴露内部码差异）")
    void loginFailureNormalizesCredentialErrors() {
        for (int code : new int[]{3004, 3005}) {
            when(droneAuthClient.login(USERNAME, PASSWORD))
                    .thenReturn(droneLoginFail(code, "用户名或密码错误"));

            assertThatThrownBy(() -> authService.login(USERNAME, PASSWORD))
                    .isInstanceOf(ServiceException.class)
                    .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                            .isEqualTo(StatusCode.USERNAME_ERROR))
                    .hasMessage("用户名或密码错误");
        }
    }

    @Test
    @DisplayName("登录失败：其它码（如 3006）→ 3000 且**原样透传下游文案**")
    void loginFailurePassesThroughOtherMessages() {
        when(droneAuthClient.login(USERNAME, PASSWORD))
                .thenReturn(droneLoginFail(3006, "账号已被锁定，请联系管理员"));

        assertThatThrownBy(() -> authService.login(USERNAME, PASSWORD))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.OPERATION_FAILED))
                .hasMessage("账号已被锁定，请联系管理员");
    }

    // ------------------------------------------------------------ 当前用户 / 登出

    @Test
    @DisplayName("me：未登录或令牌无效 → 4010（而不是返回匿名信息）")
    void meRequiresSession() {
        assertThatThrownBy(() -> authService.me(null))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));
        assertThatThrownBy(() -> authService.me("not-a-real-token"))
                .isInstanceOf(ServiceException.class)
                .satisfies(e -> assertThat(((ServiceException) e).getStatusCode())
                        .isEqualTo(StatusCode.UNAUTHORIZED));
    }

    @Test
    @DisplayName("me：已登录 → 返回不含凭据的 LoginUser")
    void meReturnsLoginUser() {
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("dt", 28800));
        String token = (String) authService.login(USERNAME, PASSWORD).get("token");

        LoginUser user = authService.me(token);

        assertThat(user.getUserId()).isEqualTo(1L);
        assertThat(user.getUsername()).isEqualTo(USERNAME);
        assertThat(user.getPermission()).isEqualTo("admin");
    }

    @Test
    @DisplayName("登出：本地会话必删，jsdstu-3 侧尽力而为")
    void logoutRevokesLocalSession() {
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("drone-token-9", 28800));
        String token = (String) authService.login(USERNAME, PASSWORD).get("token");

        authService.logout(token);

        assertThat(tokenStore.get(token)).isNull();
        verify(droneAuthClient).logout("drone-token-9");
    }

    @Test
    @DisplayName("登出未知令牌：不抛异常（登出必须幂等，重复点不该报错）")
    void logoutUnknownTokenIsIdempotent() {
        authService.logout(null);
        authService.logout("never-existed");
    }

    // ------------------------------------------------------------ 身份解析（业务派活）

    @Test
    @DisplayName("resolveIdentity：匿名会话 → null（知识问答照常，业务工具由 Python 提示登录）")
    void resolveIdentityForAnonymousIsNull() {
        assertThat(authService.resolveIdentity(null)).isNull();
    }

    @Test
    @DisplayName("resolveIdentity：drone token 未过期 → 直接带上，不重登")
    void resolveIdentityUsesLiveToken() {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(3L).username("pilot01").permission("pilot").build());
        session.setDroneToken("live-token");
        session.setDroneTokenExpireAt(System.currentTimeMillis() + 60_000);

        Map<String, Object> identity = authService.resolveIdentity(session);

        assertThat(identity).containsEntry("droneToken", "live-token");
        assertThat(identity).containsEntry("userId", 3L);
        assertThat(identity).containsEntry("username", "pilot01");
        assertThat(identity).containsEntry("permission", "pilot");
    }

    @Test
    @DisplayName("resolveIdentity：drone token 过期 → 用内存凭据静默续期一次")
    void resolveIdentitySilentlyRenewsExpiredToken() {
        DroneSession session = sessionWithExpiredDroneToken(1_000);
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("fresh-token", 28800));

        Map<String, Object> identity = authService.resolveIdentity(session);

        assertThat(identity).containsEntry("droneToken", "fresh-token");
        // 会话里的 token 也被更新了，后续调用不必再次重登
        assertThat(session.getDroneToken()).isEqualTo("fresh-token");
        assertThat(session.getDroneTokenExpireAt()).isGreaterThan(System.currentTimeMillis());
    }

    @Test
    @DisplayName("resolveIdentity：续期抛异常 → 降级为匿名（返回 null，不影响本轮知识问答）")
    void resolveIdentityDegradesToAnonymousOnRenewFailure() {
        DroneSession session = sessionWithExpiredDroneToken(1_000);
        when(droneAuthClient.login(USERNAME, PASSWORD))
                .thenThrow(new org.springframework.web.client.ResourceAccessException("jsdstu-3 挂了"));

        assertThat(authService.resolveIdentity(session)).isNull();
    }

    @Test
    @DisplayName("resolveIdentity：续期返回非 2000 → 保留旧 token 继续（不误判成匿名）")
    void resolveIdentityKeepsOldTokenWhenRenewRejected() {
        DroneSession session = sessionWithExpiredDroneToken(1_000);
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginFail(3004, "密码已变更"));

        Map<String, Object> identity = authService.resolveIdentity(session);

        // 下游会被旧 token 拒绝并返回 4010，由 Python 侧提示重新登录——
        // 这条路径与"续期抛异常"不同，刻意区分开。
        assertThat(identity).containsEntry("droneToken", "old-drone-token");
    }

    // ------------------------------------------------------------ 操作审计（E-8）

    @Test
    @DisplayName("审计：登录成功留痕，actor 取**上游返回**的用户名且带上来源 IP")
    void loginSuccessIsAudited() {
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("dt", 28800));

        authService.login(USERNAME, PASSWORD, "10.1.2.3");

        verify(auditReporter).loginOk(USERNAME, "10.1.2.3");
    }

    @Test
    @DisplayName("审计：登录失败也留痕（撞库痕迹只能从这里看出来），且**不记密码**")
    void loginFailureIsAudited() {
        when(droneAuthClient.login(USERNAME, PASSWORD))
                .thenReturn(droneLoginFail(3004, "用户名或密码错误"));

        assertThatThrownBy(() -> authService.login(USERNAME, PASSWORD, "10.1.2.3"))
                .isInstanceOf(ServiceException.class);

        verify(auditReporter).loginFail(USERNAME, "用户名或密码错误", "10.1.2.3");
        // 密码绝不能出现在审计里：任何一次调用都不该带 PASSWORD
        verify(auditReporter, never()).loginFail(eq(USERNAME), contains(PASSWORD), any());
    }

    @Test
    @DisplayName("审计：上游不可达也要留痕（否则表现成'压根没人登录'，与'登录被拒'分不开）")
    void upstreamFailureIsAudited() {
        when(droneAuthClient.login(USERNAME, PASSWORD))
                .thenThrow(new org.springframework.web.client.ResourceAccessException("连不上"));

        assertThatThrownBy(() -> authService.login(USERNAME, PASSWORD, null))
                .isInstanceOf(RuntimeException.class);

        verify(auditReporter).loginFail(eq(USERNAME), contains("认证服务不可用"), isNull());
    }

    @Test
    @DisplayName("审计：登出留痕（用会话里的用户名，不是令牌）")
    void logoutIsAudited() {
        when(droneAuthClient.login(USERNAME, PASSWORD)).thenReturn(droneLoginOk("dt", 28800));
        String token = (String) authService.login(USERNAME, PASSWORD, null).get("token");

        authService.logout(token, "10.9.9.9");

        verify(auditReporter).logout(USERNAME, "10.9.9.9");
    }

    @Test
    @DisplayName("审计：登出未知令牌不写审计（没有会话就没有事实可记，别造匿名噪声）")
    void logoutUnknownTokenIsNotAudited() {
        authService.logout("never-existed", "10.0.0.1");

        verify(auditReporter, never()).logout(any(), any());
    }
}

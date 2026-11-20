package cn.tedu.agt.base.auth;

import cn.tedu.agt.base.log.TraceHeaders;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 认证拦截器（E-5 鉴权组）。
 *
 * <p>本拦截器是**可选认证**：有令牌就解析身份，没有也放行（匿名）。
 * 所以它唯一会出错的方式不是"拦错"，而是**认错令牌头**——
 * 这里最有价值的一条测试是 {@link #onlyBearerIsAccepted()}：
 * 本系统只认 {@code Authorization: Bearer}，传 {@code X-Token} 会被静默忽略
 * （表现为"已登录却提示请先登录"，极易误判成鉴权把服务改坏了）。</p>
 */
class AuthInterceptorTest {

    private TokenStore tokenStore;
    private AuthInterceptor interceptor;
    private MockHttpServletResponse response;

    @BeforeEach
    void setUp() {
        tokenStore = new TokenStore();
        interceptor = new AuthInterceptor(tokenStore);
        response = new MockHttpServletResponse();
    }

    /** 造一个已入库的会话，返回其令牌。 */
    private String validToken() {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(7L).username("pilot01").permission("pilot").build());
        session.setDroneToken("drone-token");
        session.setSessionExpireAt(System.currentTimeMillis() + 60_000);
        return tokenStore.create(session);
    }

    private DroneSession sessionOf(MockHttpServletRequest request) {
        return (DroneSession) request.getAttribute(AuthInterceptor.ATTR_SESSION);
    }

    @Test
    @DisplayName("无 Authorization 头 → 放行且不挂会话（匿名访问是设计内行为）")
    void anonymousIsAllowed() {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/agent/chat");

        assertThat(interceptor.preHandle(request, response, new Object())).isTrue();
        assertThat(sessionOf(request)).isNull();
    }

    @Test
    @DisplayName("合法 Bearer 令牌 → 放行并把会话挂到请求属性上")
    void bearerTokenAttachesSession() {
        String token = validToken();
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/auth/me");
        request.addHeader("Authorization", "Bearer " + token);

        assertThat(interceptor.preHandle(request, response, new Object())).isTrue();
        assertThat(sessionOf(request)).isNotNull();
        assertThat(sessionOf(request).getUser().getUsername()).isEqualTo("pilot01");
    }

    @Test
    @DisplayName("只认 Authorization: Bearer —— 传 X-Token 时放行但会话为 null（本系统与 8060 的头不同）")
    void onlyBearerIsAccepted() {
        String token = validToken();

        // 8060（jsdstu-3）认 X-Token，本服务不认。传错头不会有任何报错，
        // 只会表现为"明明登录了却要重新登录"——这条测试就是这个坑的告警器。
        MockHttpServletRequest wrongHeader = new MockHttpServletRequest("GET", "/v1/auth/me");
        wrongHeader.addHeader("X-Token", token);
        assertThat(interceptor.preHandle(wrongHeader, response, new Object())).isTrue();
        assertThat(sessionOf(wrongHeader))
                .as("X-Token 不应被当作登录凭据")
                .isNull();

        // 裸令牌（没有 Bearer 前缀）同样不被接受
        MockHttpServletRequest nakedToken = new MockHttpServletRequest("GET", "/v1/auth/me");
        nakedToken.addHeader("Authorization", token);
        assertThat(interceptor.preHandle(nakedToken, response, new Object())).isTrue();
        assertThat(sessionOf(nakedToken)).isNull();
    }

    @Test
    @DisplayName("无效/已过期令牌 → 放行但会话为 null（由 Controller 层转 4010）")
    void invalidTokenLeavesSessionNull() {
        MockHttpServletRequest unknown = new MockHttpServletRequest("GET", "/v1/auth/me");
        unknown.addHeader("Authorization", "Bearer not-a-real-token");
        assertThat(interceptor.preHandle(unknown, response, new Object())).isTrue();
        assertThat(sessionOf(unknown)).isNull();

        // 过期令牌：先入库再让它过期，验证拦截器不会把过期会话挂上去
        DroneSession expired = new DroneSession();
        expired.setUser(LoginUser.builder().userId(8L).username("pilot02").build());
        expired.setSessionExpireAt(System.currentTimeMillis() - 1);
        String expiredToken = tokenStore.create(expired);

        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/auth/me");
        request.addHeader("Authorization", "Bearer " + expiredToken);
        assertThat(interceptor.preHandle(request, response, new Object())).isTrue();
        assertThat(sessionOf(request)).isNull();
    }

    @Test
    @DisplayName("Bearer 后带多余空格 → 仍能正确解析（前端拼头时容易多一个空格）")
    void toleratesWhitespaceAfterBearer() {
        String token = validToken();
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/auth/me");
        request.addHeader("Authorization", "Bearer   " + token);

        interceptor.preHandle(request, response, new Object());
        assertThat(sessionOf(request)).isNotNull();
    }

    @Test
    @DisplayName("OPTIONS 预检 → 直接放行且不做令牌解析（预检请求不带自定义头）")
    void optionsIsPassedThrough() {
        // 即使带了合法令牌，预检分支也不解析：它只需要拿到 CORS 头就够
        MockHttpServletRequest request = new MockHttpServletRequest("OPTIONS", "/v1/agent/chat");
        request.addHeader("Authorization", "Bearer " + validToken());

        assertThat(interceptor.preHandle(request, response, new Object())).isTrue();
        assertThat(sessionOf(request)).as("预检请求不应挂会话").isNull();
    }

    // ------------------------------------------------------------ E-6：日志的 userId 字段由这里补写

    @Test
    @DisplayName("E-6：解析出身份后把 userId 写进 MDC（Filter 层还不知道是谁）")
    void writesUserIdToMdc() {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/auth/me");
        request.addHeader("Authorization", "Bearer " + validToken());

        interceptor.preHandle(request, response, new Object());

        assertThat(MDC.get(TraceHeaders.MDC_USER_ID)).isEqualTo("7");
    }

    @Test
    @DisplayName("E-6：匿名请求必须**清掉** MDC.userId（线程复用会把上个请求的身份带过来）")
    void clearsUserIdForAnonymous() {
        // 模拟"同一个 Tomcat 线程先服务了已登录用户，再服务匿名请求"
        MDC.put(TraceHeaders.MDC_USER_ID, "999");
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/agent/chat");

        interceptor.preHandle(request, response, new Object());

        assertThat(MDC.get(TraceHeaders.MDC_USER_ID)).isNull();
        MDC.clear();
    }

    @Test
    @DisplayName("E-6：令牌无效（会话不存在）时也不该留下 userId")
    void clearsUserIdWhenSessionMissing() {
        MDC.put(TraceHeaders.MDC_USER_ID, "999");
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/auth/me");
        request.addHeader("Authorization", "Bearer not-a-real-token");

        interceptor.preHandle(request, response, new Object());

        assertThat(MDC.get(TraceHeaders.MDC_USER_ID)).isNull();
        MDC.clear();
    }
}

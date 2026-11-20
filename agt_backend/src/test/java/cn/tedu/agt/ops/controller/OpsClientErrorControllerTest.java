package cn.tedu.agt.ops.controller;

import cn.tedu.agt.base.auth.AuthInterceptor;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.ops.service.OpsService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

/**
 * {@code POST /v1/ops/client-error} 的 HTTP 层契约（企业级落地规约 E-12）。
 *
 * <p>这一层要守三件事，它们都是"看起来能跑、其实已经把功能做废"的类型：</p>
 * <ol>
 *   <li><b>匿名要能上报</b>：登录页自己崩了时用户还没有令牌。若这里要求登录，
 *       恰好把最该记的场景挡在门外；</li>
 *   <li><b>永远返回 2000</b>：调用方是一个已经出错的页面。给它一个失败码，
 *       它既不会重试也无法展示——"上报失败"只会变成一个没人消费的噪音，
 *       或者更糟：被前端的错误处理当成新错误再报一次（自我放大）；</li>
 *   <li><b>体积闸门真的拦在转发之前</b>：拦住不等于"返回个错"，而是
 *       <b>不往下游转发</b>（下游会截断、入库、占空间）。用"服务是否被调用过"
 *       来断言这一点，而不是看响应文案——文案可以写对而逻辑没做。</li>
 * </ol>
 */
class OpsClientErrorControllerTest {

    private MockMvc mvc;
    private final ObjectMapper json = new ObjectMapper();
    private final AtomicInteger serviceCalls = new AtomicInteger();
    private final AtomicReference<DroneSession> seenSession = new AtomicReference<>();
    private final AtomicReference<Map<String, Object>> seenBody = new AtomicReference<>();

    @BeforeEach
    void setUp() {
        OpsService stub = new OpsService() {
            @Override
            public Map<String, Object> health(boolean fresh) {
                return Map.of("status", "up");
            }

            @Override
            public String metrics(int windowHours, boolean fresh) {
                return "# HELP agt_java_up x\n# TYPE agt_java_up gauge\nagt_java_up 1\n";
            }

            @Override
            public Map<String, Object> clientError(DroneSession session, Map<String, Object> body) {
                serviceCalls.incrementAndGet();
                seenSession.set(session);
                seenBody.set(body);
                Map<String, Object> out = new LinkedHashMap<>();
                out.put("accepted", true);
                out.put("stored", true);
                out.put("reason", "");
                return out;
            }
        };
        OpsController controller = new OpsController();
        ReflectionTestUtils.setField(controller, "opsService", stub);
        mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    private static Map<String, Object> payload() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("page", "/dashboard/faults");
        body.put("source", "vue");
        body.put("message", "TypeError: a is not a function");
        body.put("stack", "at foo (main.js:1)");
        body.put("appVersion", "1.0.0");
        body.put("userAgent", "Mozilla/5.0");
        return body;
    }

    @Test
    @DisplayName("匿名（无登录态）也能上报：拿到 code=2000 且服务被调用")
    void anonymousReportIsAccepted() throws Exception {
        String resp = mvc.perform(post("/v1/ops/client-error")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(payload())))
                .andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);

        assertThat(resp).contains("\"code\":2000");
        assertThat(serviceCalls.get()).isEqualTo(1);
        // 无会话 → 服务层收到的 session 必须是 null（而不是"某个前一个请求留下的"）
        assertThat(seenSession.get()).isNull();
    }

    @Test
    @DisplayName("有登录态时把会话原样交给服务层（userId 由服务层从会话注入）")
    void sessionIsHandedToService() throws Exception {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(1L).username("admin").permission("admin").build());

        mvc.perform(post("/v1/ops/client-error")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(payload()))
                        .requestAttr(AuthInterceptor.ATTR_SESSION, session));

        assertThat(seenSession.get()).isSameAs(session);
    }

    @Test
    @DisplayName("上报体超限：不往下游转发，但依然返回 code=2000 且如实说明原因")
    void oversizedBodyIsRejectedBeforeForwarding() throws Exception {
        Map<String, Object> big = payload();
        big.put("stack", "x".repeat(9 * 1024)); // > MAX_REPORT_BYTES(8KB)

        // ⚠️ 必须显式按 UTF-8 读：Java 的 `getContentAsString()` 在响应头没带 charset 时
        // 按 ISO-8859-1 解码，中文会变成"?????"——于是断言中文文案会莫名失败，
        // 而人去看日志只看到一堆问号，很容易误判成"功能没做"。
        String resp = mvc.perform(post("/v1/ops/client-error")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(big)))
                .andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);

        assertThat(resp).contains("\"code\":2000");
        assertThat(resp).contains("上报体过大");
        assertThat(resp).contains("\"stored\":false");
        // 关键断言：拦在**转发之前** —— 服务层一次都不该被调用
        assertThat(serviceCalls.get()).isZero();
    }

    @Test
    @DisplayName("服务层返回的 accepted/stored/reason 原样透出（不二次加工）")
    void serviceResultIsPassedThrough() throws Exception {
        String resp = mvc.perform(post("/v1/ops/client-error")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(payload())))
                .andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);

        assertThat(resp).contains("\"accepted\":true");
        assertThat(resp).contains("\"stored\":true");
    }

    @Test
    @DisplayName("上报体里的 userId 不会被转成服务层入参（身份只由 Java 注入）")
    void clientSuppliedUserIdStaysInBodyOnly() throws Exception {
        Map<String, Object> spoofed = payload();
        spoofed.put("userId", 999);

        mvc.perform(post("/v1/ops/client-error")
                .contentType(MediaType.APPLICATION_JSON)
                .content(json.writeValueAsString(spoofed)));

        // 会话为 null → 服务层会把 userId 解析成 0（见 OpsClientErrorServiceTest）。
        // 这里只证明一件事：**控制器不会把 body 里的 userId 提升成任何东西**。
        assertThat(seenSession.get()).isNull();
        assertThat(seenBody.get()).containsEntry("userId", 999); // 仍在 body 里，但无意义
    }

    @Test
    @DisplayName("无请求体也不 500（前端可能在极端情况下发出空体）")
    void emptyBodyDoesNotBreak() throws Exception {
        String resp = mvc.perform(post("/v1/ops/client-error")
                        .contentType(MediaType.APPLICATION_JSON))
                .andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);

        assertThat(resp).contains("\"code\":2000");
        assertThat(serviceCalls.get()).isEqualTo(1);
    }
}

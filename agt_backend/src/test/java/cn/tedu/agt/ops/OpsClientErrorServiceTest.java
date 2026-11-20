package cn.tedu.agt.ops;

import cn.tedu.agt.agent.client.AgentClient;
import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.ops.service.impl.OpsServiceImpl;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 前端报错上报的服务层契约（企业级落地规约 E-12）。
 *
 * <p>与 {@code OpsMetricsServiceTest} 同一条取向：<b>不起 mock，起一个真实的本地
 * HTTP 服务读原始请求</b>（{@code com.sun.net.httpserver} 是 JDK 自带，零新依赖，
 * 全程 127.0.0.1、断网可跑）。理由也一样——本层要证明的是
 * "<b>线路上到底带了什么</b>"：</p>
 * <ul>
 *   <li>{@code user_id} 真的以<b>查询参数</b>发出去了（身份注入的载体）；</li>
 *   <li>上报体里<b>没有</b> userId（"前端自报身份"在链路上无处可传）；</li>
 *   <li>{@code X-Agent-Key} 真的带上了（S-1：漏掉这个头会让整个域 4010，
 *       而且不会有编译错误）；</li>
 *   <li>Python 不可达时<b>不抛异常</b>、也不返回"成功"——而是如实说 stored=false
 *       并给出原因（宁可少一条数据，也不能让上报接口自己变成新的故障源）。</li>
 * </ul>
 */
class OpsClientErrorServiceTest {

    private static final String EXPECTED_KEY = "client-error-contract-key";

    private record Recorded(String method, String path, String query,
                            Map<String, String> headers, String body) {
        String header(String name) {
            return headers.get(name.toLowerCase(Locale.ROOT));
        }
    }

    private HttpServer server;
    private final List<Recorded> received = Collections.synchronizedList(new ArrayList<>());
    private OpsServiceImpl service;

    /** stub 的应答体（可被单个测试改写）。 */
    private volatile String replyBody = "{\"code\":2000,\"message\":\"操作成功\","
            + "\"data\":{\"accepted\":true,\"stored\":true,\"reason\":\"\","
            + "\"fingerprint\":\"abc123\",\"bucket\":\"2026-09-24T16:40:00\"}}";

    @BeforeEach
    void startStubServer() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", this::recordAndReply);
        server.start();

        AgentProperties properties = new AgentProperties();
        properties.setBaseUrl("http://127.0.0.1:" + server.getAddress().getPort());
        properties.setSharedKey(EXPECTED_KEY);
        properties.setConnectTimeout(2_000);
        properties.setReadTimeout(5_000);
        properties.setOpsReadTimeout(2_000);

        service = new OpsServiceImpl();
        ReflectionTestUtils.setField(service, "agentClient",
                new AgentClient(properties, new ObjectMapper()));
    }

    @AfterEach
    void stopStubServer() {
        server.stop(0);
    }

    private void recordAndReply(HttpExchange exchange) throws IOException {
        Map<String, String> headers = new LinkedHashMap<>();
        exchange.getRequestHeaders().forEach(
                (k, v) -> headers.put(k.toLowerCase(Locale.ROOT), String.join(",", v)));
        String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        received.add(new Recorded(exchange.getRequestMethod(),
                exchange.getRequestURI().getPath(), exchange.getRequestURI().getQuery(),
                headers, body));

        byte[] payload = replyBody.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(200, payload.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(payload);
        }
    }

    private static Map<String, Object> payload() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("page", "/dashboard/faults");
        body.put("source", "vue");
        body.put("message", "TypeError: a is not a function");
        body.put("appVersion", "1.0.0");
        return body;
    }

    private static DroneSession sessionOf(long userId) {
        DroneSession session = new DroneSession();
        session.setUser(LoginUser.builder().userId(userId).username("admin")
                .permission("admin").build());
        return session;
    }

    @Test
    @DisplayName("登录态：userId 以查询参数注入，且上报体里没有 userId")
    void injectsUserIdFromSessionAsQueryParam() {
        service.clientError(sessionOf(1L), payload());

        assertThat(received).hasSize(1);
        Recorded r = received.get(0);
        assertThat(r.method()).isEqualTo("POST");
        assertThat(r.path()).isEqualTo("/v1/ops/client-error");
        assertThat(r.query()).isEqualTo("user_id=1");
        assertThat(r.body()).doesNotContain("userId");
        assertThat(r.body()).doesNotContain("user_id");
    }

    @Test
    @DisplayName("匿名（登录页报错 / 令牌刚过期）：userId=0，不阻断上报")
    void anonymousBecomesZero() {
        service.clientError(null, payload());

        assertThat(received).hasSize(1);
        assertThat(received.get(0).query()).isEqualTo("user_id=0");
    }

    @Test
    @DisplayName("出站带 X-Agent-Key（S-1：漏了不会有编译错误，只会整域 4010）")
    void carriesSharedKeyHeader() {
        service.clientError(sessionOf(7L), payload());

        assertThat(received.get(0).header("x-agent-key")).isEqualTo(EXPECTED_KEY);
    }

    @Test
    @DisplayName("Python 的结果（accepted/stored/reason/fingerprint）原样透出")
    void passesThroughPythonResult() {
        Map<String, Object> out = service.clientError(sessionOf(1L), payload());

        assertThat(out).containsEntry("accepted", true);
        assertThat(out).containsEntry("stored", true);
        assertThat(out).containsEntry("fingerprint", "abc123");
        assertThat(out).containsEntry("bucket", "2026-09-24T16:40:00");
    }

    @Test
    @DisplayName("Python 返回业务失败码：不抛异常，reason 带上 code")
    void businessFailureIsReportedNotThrown() {
        replyBody = "{\"code\":4010,\"message\":\"未授权\",\"data\":null}";

        Map<String, Object> out = service.clientError(sessionOf(1L), payload());

        assertThat(out).containsEntry("accepted", false);
        assertThat(String.valueOf(out.get("reason"))).contains("4010");
    }

    @Test
    @DisplayName("Python 完全不可达：不抛异常、不谎报成功（stored=false + 原因）")
    void unreachablePythonIsNotFatal() {
        // 直接把下游掐掉：关掉 stub 服务，端口随即不可连
        server.stop(0);

        Map<String, Object> out = service.clientError(sessionOf(1L), payload());

        assertThat(out).containsEntry("accepted", false);
        assertThat(out).containsEntry("stored", false);
        assertThat(String.valueOf(out.get("reason"))).contains("不可达");
    }

    @Test
    @DisplayName("空 body 也照常转发（服务端会当空对象处理，不 NPE）")
    void nullBodyBecomesEmptyObject() {
        Map<String, Object> out = service.clientError(sessionOf(1L), null);

        assertThat(received).hasSize(1);
        assertThat(received.get(0).body()).isEqualTo("{}");
        assertThat(out).containsEntry("accepted", true);
    }
}

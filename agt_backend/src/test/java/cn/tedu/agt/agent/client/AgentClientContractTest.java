package cn.tedu.agt.agent.client;

import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.agent.pojo.dto.AgentChatParam;
import cn.tedu.agt.base.log.TraceHeaders;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;

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
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Java → Python 出站请求契约（E-5 透传契约组，**本组是整个 E-5 的重点**）。
 *
 * <p>它守的是 S-1 服务间信任边界：Java 侧有三个出口
 * （{@code AgentClient} / {@code KbClient} / {@code GovernanceClient}），
 * 任何一个出口漏掉 {@code X-Agent-Key}，表现都是"那个业务域整体 4010"——
 * 前端弹"登录已过期"，排查方向会被带到会话/登录上去。而漏加一行头
 * <b>不会有任何编译错误</b>，只能靠测试挡住。</p>
 *
 * <h3>为什么用 JDK 自带 HttpServer 而不是 mock</h3>
 * <p>Mock 掉 HTTP 层就等于把这个测试要证明的东西（"请求里到底有没有那个头"）
 * 一起 mock 掉了：断言的是"我调用了 mock 的方法"，而不是"线路上真的带了这个头"。
 * 这里起一个真实的本地 HTTP 服务，让它<b>读原始请求</b>再断言，
 * 才真正证得住。零新依赖（{@code com.sun.net.httpserver} 是 JDK 自带），
 * 且全程 127.0.0.1，断网可跑。</p>
 *
 * <p>验证"门禁真的生效"的方式：把 {@code AgentAuth.withKey(...)} 换成
 * 裸 {@code RestClient.builder()}，本类会立刻大面积变红。</p>
 */
class AgentClientContractTest {

    private static final String EXPECTED_KEY = "contract-test-shared-key";
    private static final String JSON_BODY = "{\"code\":2000,\"message\":\"ok\",\"data\":{}}";
    private static final String SSE_BODY =
            "data: {\"code\":2000,\"data\":{\"reply\":\"hi\"}}\n\n"
                    + "data: [DONE]\n\n";

    /** 一次被收到的出站请求（原始形态）。 */
    private record Recorded(String method, String path, String query,
                            Map<String, String> headers, String body) {
        String header(String name) {
            return headers.get(name.toLowerCase(Locale.ROOT));
        }
    }

    private HttpServer server;
    /**
     * stub 的 HTTP 处理器跑在 HttpServer 自己的线程上，主线程随后读取，
     * 故用同步包装保证可见性（否则偶发"请求明明发了但断言说没收到"）。
     */
    private final List<Recorded> received = Collections.synchronizedList(new ArrayList<>());
    private AgentClient agentClient;

    @BeforeEach
    void startStubServer() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", this::recordAndReply);
        server.start();

        AgentProperties properties = new AgentProperties();
        // 端口 0 = 让系统分配空闲端口，避免与在跑的服务撞车
        properties.setBaseUrl("http://127.0.0.1:" + server.getAddress().getPort());
        properties.setSharedKey(EXPECTED_KEY);
        properties.setConnectTimeout(2_000);
        properties.setReadTimeout(5_000);
        properties.setOpsReadTimeout(2_000);

        agentClient = new AgentClient(properties, new ObjectMapper());
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

        String path = exchange.getRequestURI().getPath();
        received.add(new Recorded(exchange.getRequestMethod(), path,
                exchange.getRequestURI().getQuery(), headers, body));

        boolean sse = path.endsWith("/chat/stream");
        byte[] payload = (sse ? SSE_BODY : JSON_BODY).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type",
                sse ? "text/event-stream; charset=utf-8" : "application/json; charset=utf-8");
        exchange.sendResponseHeaders(200, payload.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(payload);
        }
    }

    private Recorded only() {
        assertThat(received).as("应当恰好收到一个请求").hasSize(1);
        return received.get(0);
    }

    private AgentChatParam chatParam() {
        AgentChatParam param = new AgentChatParam();
        param.setMessage("plan-the-flight");
        param.setSessionId("s-1");
        param.setUserId(1L);
        return param;
    }

    // ------------------------------------------------------------ 核心：每个出口都必须带 X-Agent-Key

    @Test
    @DisplayName("health：GET /v1/health + X-Agent-Key")
    void healthCarriesKey() {
        agentClient.health();

        Recorded r = only();
        assertThat(r.method()).isEqualTo("GET");
        assertThat(r.path()).isEqualTo("/v1/health");
        assertThat(r.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
    }

    @Test
    @DisplayName("chat：POST /v1/agent/chat + X-Agent-Key + JSON 请求体")
    void chatCarriesKey() {
        agentClient.chat(chatParam());

        Recorded r = only();
        assertThat(r.method()).isEqualTo("POST");
        assertThat(r.path()).isEqualTo("/v1/agent/chat");
        assertThat(r.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
        assertThat(r.header("Content-Type")).contains("application/json");
        assertThat(r.body()).contains("\"message\":\"plan-the-flight\"");
        assertThat(r.body()).contains("\"sessionId\":\"s-1\"");
    }

    @Test
    @DisplayName("chatStream：流式走 JDK HttpClient，**不共享 RestClient 的默认头**，必须单独带 key")
    void chatStreamCarriesKeyOnItsOwnClient() {
        List<String> events = new ArrayList<>();

        agentClient.chatStream(chatParam(), events::add);

        Recorded r = only();
        assertThat(r.method()).isEqualTo("POST");
        assertThat(r.path()).isEqualTo("/v1/agent/chat/stream");
        // 这一行是本类最值钱的断言：流式用的是另一个 HTTP 客户端，
        // RestClient 的 defaultHeader 在这里完全不生效。
        assertThat(r.header("X-Agent-Key"))
                .as("流式出口漏了 X-Agent-Key 会导致聊天流整体 4010")
                .isEqualTo(EXPECTED_KEY);
        // SSE 解析本身也要对：两条 data 都要回调出去（含结束标记）
        assertThat(events).hasSize(2);
        assertThat(events.get(0)).contains("\"reply\":\"hi\"");
        assertThat(events.get(1)).isEqualTo("[DONE]");
    }

    @Test
    @DisplayName("opsHealth：GET /v1/ops/health + X-Agent-Key（独立短超时的客户端也必须带头）")
    void opsHealthCarriesKey() {
        agentClient.opsHealth(false);

        Recorded r = only();
        assertThat(r.path()).isEqualTo("/v1/ops/health");
        assertThat(r.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
        assertThat(r.query()).isNull();
    }

    @Test
    @DisplayName("opsHealth(fresh)：路径带 ?fresh=true（绕过聚合缓存）")
    void opsHealthFreshAddsQuery() {
        agentClient.opsHealth(true);

        assertThat(only().query()).isEqualTo("fresh=true");
    }

    @Test
    @DisplayName("metrics 透传：GET 带 key；带身份时额外加 Authorization: Bearer")
    void metricsCarriesKeyAndOptionalBearer() {
        agentClient.metricsGet("/v1/agent/metrics/overview");

        Recorded plain = only();
        assertThat(plain.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
        assertThat(plain.header("Authorization")).isNull();

        received.clear();

        agentClient.metricsGet("/v1/agent/metrics/cost", "user-drone-token");

        Recorded withIdentity = only();
        assertThat(withIdentity.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
        // 看板按访问者身份拉告警：用的是**用户自己的** drone token，不是系统账号
        assertThat(withIdentity.header("Authorization")).isEqualTo("Bearer user-drone-token");
    }

    @Test
    @DisplayName("profile 透传：GET /v1/profile/{userId} 与 DELETE 都带 key")
    void profileCarriesKey() {
        agentClient.profileGet(7L);

        Recorded get = only();
        assertThat(get.method()).isEqualTo("GET");
        assertThat(get.path()).isEqualTo("/v1/profile/7");
        assertThat(get.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);

        received.clear();

        agentClient.profileDelete(7L, "favorite-color");

        Recorded delete = only();
        assertThat(delete.method()).isEqualTo("DELETE");
        assertThat(delete.path()).isEqualTo("/v1/profile/7/favorite-color");
        assertThat(delete.header("X-Agent-Key")).isEqualTo(EXPECTED_KEY);
    }

    // ------------------------------------------------------------ 路径白名单：非法路径不发请求

    @Test
    @DisplayName("metrics 透传路径必须限定 /v1/agent/metrics/ 前缀，否则直接拒绝且**不发请求**")
    void metricsRejectsForeignPath() {
        for (String bad : new String[]{null, "", "/v1/kb/documents", "/v1/agent/chat", "../admin"}) {
            assertThatThrownBy(() -> agentClient.metricsGet(bad))
                    .as("路径 [%s] 应被拒绝", bad)
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> agentClient.metricsPost(bad, Map.of()))
                    .as("路径 [%s] 应被拒绝", bad)
                    .isInstanceOf(IllegalArgumentException.class);
        }
        // 一个请求都不该出去：被拒绝的越权尝试不该在下游留痕
        assertThat(received).isEmpty();
    }

    @Test
    @DisplayName("opsHealth 的路径是常量，不接受调用方拼装（少一个口子少一类越权）")
    void opsHealthHasNoPathParameter() {
        // 这不是运行时断言，而是一条防回归的"接口形态"检查：
        // 若有人给 opsHealth 加上了路径参数，本测试会因编译失败而拦下这次改动。
        agentClient.opsHealth(true);
        assertThat(only().path()).isEqualTo("/v1/ops/health");
    }

    // ------------------------------------------------------------ E-6：链路头透传（X-Trace-Id / X-User-Id）

    @Test
    @DisplayName("E-6：RestClient 出口自动带 X-Trace-Id / X-User-Id（值每请求不同，不能用 defaultHeader）")
    void restClientCarriesTraceHeaders() {
        MDC.put(TraceHeaders.MDC_TRACE_ID, "8f3c1a92d47b05e6");
        MDC.put(TraceHeaders.MDC_USER_ID, "7");
        try {
            agentClient.health();
        } finally {
            MDC.clear();
        }

        Recorded r = only();
        assertThat(r.header("X-Trace-Id")).isEqualTo("8f3c1a92d47b05e6");
        assertThat(r.header("X-User-Id")).isEqualTo("7");
    }

    @Test
    @DisplayName("E-6：chatStream 走 JDK HttpClient，**不共享拦截器**，必须自己带链路头")
    void chatStreamCarriesTraceHeadersOnItsOwnClient() {
        // 与 S-1 的 X-Agent-Key 是同一条教训：流式那条路径什么默认头都不继承。
        // 漏了的表现是"聊天流那一段的日志没有 traceId"，不报错，只是查不到。
        MDC.put(TraceHeaders.MDC_TRACE_ID, "aaaabbbbccccdddd");
        MDC.put(TraceHeaders.MDC_USER_ID, "7");
        try {
            agentClient.chatStream(chatParam(), payload -> {
            });
        } finally {
            MDC.clear();
        }

        Recorded r = only();
        assertThat(r.header("X-Trace-Id")).isEqualTo("aaaabbbbccccdddd");
        assertThat(r.header("X-User-Id")).isEqualTo("7");
    }

    @Test
    @DisplayName("E-6：不在请求上下文时不发链路头（而不是发一个字面量的 null）")
    void noTraceHeadersOutsideRequest() {
        agentClient.health();

        Recorded r = only();
        assertThat(r.header("X-Trace-Id")).isNull();
        assertThat(r.header("X-User-Id")).isNull();
    }

    @Test
    @DisplayName("E-6：opsHealth 的独立客户端也带链路头（同一个 AgentAuth 出口）")
    void opsHealthCarriesTraceHeaders() {
        MDC.put(TraceHeaders.MDC_TRACE_ID, "0f0f0f0f0f0f0f0f");
        try {
            agentClient.opsHealth(false);
        } finally {
            MDC.clear();
        }

        assertThat(only().header("X-Trace-Id")).isEqualTo("0f0f0f0f0f0f0f0f");
    }
}

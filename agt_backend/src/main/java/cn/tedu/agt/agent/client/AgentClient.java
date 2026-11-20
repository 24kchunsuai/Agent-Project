package cn.tedu.agt.agent.client;

import cn.tedu.agt.agent.config.AgentAuth;
import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.agent.pojo.dto.AgentChatParam;
import cn.tedu.agt.agent.pojo.vo.AgentChatVO;
import cn.tedu.agt.agent.pojo.vo.AgentHealthVO;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.log.StructuredLog;
import cn.tedu.agt.base.log.TraceHeaders;
import cn.tedu.agt.base.response.StatusCode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.function.Consumer;

/**
 * 访问 Python 智能体服务的唯一出口。
 *
 * 约定：Java 侧只通过本类与 Python 通信，
 * 其它地方不得直接拼 URL，方便后续统一加链路追踪、重试与降级策略。
 */
@Slf4j
@Component
public class AgentClient {

    private static final String CHAT_PATH = "/v1/agent/chat";
    private static final String STREAM_PATH = "/v1/agent/chat/stream";
    private static final String HEALTH_PATH = "/v1/health";
    private static final String METRICS_PATH = "/v1/agent/metrics/overview";
    private static final String PROFILE_BASE = "/v1/profile/";
    /** 运维健康聚合（E-3）：路径是常量，不接受调用方拼装 */
    private static final String OPS_HEALTH_PATH = "/v1/ops/health";
    private static final String OPS_METRICS_PATH = "/v1/ops/metrics";
    private static final String OPS_CLIENT_ERROR_PATH = "/v1/ops/client-error";

    private final RestClient restClient; //普通非流式接口
    /**
     * 运维聚合专用客户端：短读超时（见 {@link AgentProperties#getOpsReadTimeout()}）。
     * 其余配置与 {@link #restClient} 完全一致（同一个 AgentAuth 出口，带同一个 X-Agent-Key）。
     */
    private final RestClient opsRestClient;
    private final AgentProperties properties; //读取配置
    private final ObjectMapper objectMapper; //JSON工具，解析sse中的json字段

    /** 流式专用：JDK 自带 HttpClient，流式读 InputStream，无需引入 webflux。 */
    private final HttpClient streamHttpClient;

    public AgentClient(AgentProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;

        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.getConnectTimeout());
        factory.setReadTimeout(properties.getReadTimeout());

        // 服务间信任边界（企业级落地规约 S-1）：统一出口加鉴权头；密钥缺失由
        // AgentAuth 抛异常 → 应用拒绝启动（fail-closed）。
        // ⚠️ 不要把校验写在这里：Java 侧直连 Python 的出口有三个
        // （本类 / KbClient / GovernanceClient），漏一个就是"那个业务域整体 4010"。
        this.restClient = AgentAuth
                .withKey(RestClient.builder().baseUrl(properties.getBaseUrl()), properties)
                .requestFactory(factory)
                .build();

        // E-3 健康聚合：同出口、同密钥，只把读超时收短（理由见 AgentProperties）
        SimpleClientHttpRequestFactory opsFactory = new SimpleClientHttpRequestFactory();
        opsFactory.setConnectTimeout(properties.getConnectTimeout());
        opsFactory.setReadTimeout(properties.getOpsReadTimeout());
        this.opsRestClient = AgentAuth
                .withKey(RestClient.builder().baseUrl(properties.getBaseUrl()), properties)
                .requestFactory(opsFactory)
                .build();

        this.streamHttpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(properties.getConnectTimeout()))
                // 强制 HTTP/1.1：JDK 默认会对 HTTP 尝试 h2c 升级（Upgrade 头），
                // uvicorn 不支持会把请求体弄丢，导致 Python 侧报参数缺失
                .version(HttpClient.Version.HTTP_1_1)
                .build();

        log.info("智能体客户端已初始化：baseUrl={} connectTimeout={}ms readTimeout={}ms sharedKey={}",
                properties.getBaseUrl(), properties.getConnectTimeout(), properties.getReadTimeout(),
                AgentAuth.mask(AgentAuth.requireSharedKey(properties)));
    }

    /**
     * 调用智能体对话接口。
     */
    public AgentApiResponse<AgentChatVO> chat(AgentChatParam param) {
        long startedAt = System.nanoTime();
        StructuredLog.info(log, "agent.call", -1, "POST {} sessionId={}", CHAT_PATH, param.getSessionId());
        try {
            AgentApiResponse<AgentChatVO> result = restClient.post()
                    .uri(CHAT_PATH)
                    .body(param)
                    .retrieve()
                    .body(new ParameterizedTypeReference<AgentApiResponse<AgentChatVO>>() {
                    });
            StructuredLog.info(log, "agent.call", elapsedMs(startedAt), "POST {} 完成", CHAT_PATH);
            return result;
        } catch (RuntimeException e) {
            StructuredLog.error(log, "agent.call", elapsedMs(startedAt), e,
                    "POST {} 失败 sessionId={}", CHAT_PATH, param.getSessionId());
            throw e;
        }
    }

    /**
     * 流式对话：逐行读取 Python 的 SSE，把每个 data: 的 JSON 载荷回调给 onEvent。
     * 该方法阻塞直到流结束，调用方应放到独立线程执行。
     */
    public void chatStream(AgentChatParam param, Consumer<String> onEvent) {
        long startedAt = System.nanoTime();
        try {
            String body = objectMapper.writeValueAsString(param);
            HttpRequest request = TraceHeaders.apply(HttpRequest.newBuilder()
                    .uri(URI.create(properties.getBaseUrl() + STREAM_PATH))
                    // 流式总超时 300s（容错规约 §7）：略大于 SseEmitter 的 240s，
                    // 让超时统一由 emitter 层触发并给前端发 error 事件
                    .timeout(Duration.ofSeconds(300L))
                    .header("Content-Type", "application/json")
                    // 流式走的是 JDK HttpClient，不共享 RestClient 的 defaultHeader，
                    // 必须单独加（漏了会导致聊天流被 Python 拒绝：4010）
                    .header(AgentAuth.HEADER_NAME, AgentAuth.requireSharedKey(properties)))
                    // E-6：链路头也要单独加，理由同上一行（defaultHeader 与拦截器都不生效）
                    .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                    .build();

            HttpResponse<InputStream> response =
                    streamHttpClient.send(request, HttpResponse.BodyHandlers.ofInputStream());

            if (response.statusCode() != 200) {
                StructuredLog.error(log, "agent.call", elapsedMs(startedAt), null,
                        "智能体流式接口 HTTP 状态异常:{}", response.statusCode());
                throw new ServiceException(StatusCode.AGENT_UNAVAILABLE);
            }

            try (InputStream in = response.body();
                 BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    if (!line.startsWith("data:")) {
                        continue;
                    }
                    String payload = line.substring("data:".length()).trim();
                    if (!payload.isEmpty()) {
                        onEvent.accept(payload);
                    }
                }
            }
            // "Java 发出了、Python 收到了、各花了多久"——这条与 Python 侧
            // chat.stream 配对，是"按 traceId 串起两侧"的可见证据（E-6 验收）
            StructuredLog.info(log, "agent.call", elapsedMs(startedAt), "POST {} 流结束", STREAM_PATH);
        } catch (ServiceException e) {
            throw e;
        } catch (Exception e) {
            StructuredLog.error(log, "agent.call", elapsedMs(startedAt), e,
                    "智能体流式调用异常:sessionId={}", param.getSessionId());
            throw new ServiceException(StatusCode.AGENT_UNAVAILABLE);
        }
    }

    private static long elapsedMs(long startedAtNanos) {
        return (System.nanoTime() - startedAtNanos) / 1_000_000L;
    }

    /**
     * 探测智能体服务健康状态。
     */
    public AgentApiResponse<AgentHealthVO> health() {
        return restClient.get()
                .uri(HEALTH_PATH)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<AgentHealthVO>>() {
                });
    }

    /**
     * 数据中台可观测总览（降级计数 + 近 24h 聚合），原样透传给前端。
     */
    public AgentApiResponse<Map<String, Object>> metricsOverview() {
        return metricsGet(METRICS_PATH);
    }

    /**
     * 数据中台查询类接口通用透传（数据中台开发规约 §6：Java 纯透传不加工）。
     * pathWithQuery 需为 /v1/agent/metrics/ 前缀的完整路径（可带查询串）。
     */
    public AgentApiResponse<Map<String, Object>> metricsGet(String pathWithQuery) {
        return metricsGet(pathWithQuery, null);
    }

    /**
     * 带 jsdstu 身份的透传重载（P2 告警处置联动）：把看板访问者自己的 droneToken
     * 以 Bearer 头传给 Python，由 Python 调 jsdstu 拉告警列表——不引入系统账号。
     */
    public AgentApiResponse<Map<String, Object>> metricsGet(String pathWithQuery, String bearerToken) {
        if (pathWithQuery == null || !pathWithQuery.startsWith("/v1/agent/metrics/")) {
            throw new IllegalArgumentException("metrics 透传路径非法: " + pathWithQuery);
        }
        if (bearerToken == null || bearerToken.isBlank()) {
            return restClient.get()
                    .uri(pathWithQuery)
                    .retrieve()
                    .body(new ParameterizedTypeReference<AgentApiResponse<Map<String, Object>>>() {
                    });
        }
        return restClient.get()
                .uri(pathWithQuery)
                .header("Authorization", "Bearer " + bearerToken)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Map<String, Object>>>() {
                });
    }

    /**
     * 数据中台写类接口透传（P2 feedback）：同样限定 /v1/agent/metrics/ 前缀。
     */
    public AgentApiResponse<Map<String, Object>> metricsPost(String path, Map<String, Object> body) {
        if (path == null || !path.startsWith("/v1/agent/metrics/")) {
            throw new IllegalArgumentException("metrics 透传路径非法: " + path);
        }
        return restClient.post()
                .uri(path)
                .body(body)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Map<String, Object>>>() {
                });
    }

    /**
     * 运维健康聚合（企业级落地规约 E-3）：转发 Python 侧的组件明细。
     *
     * <p>与 {@code metricsGet} 那类"路径由调用方拼"的透传不同，本方法的路径是
     * <b>常量</b>——运维接口不需要开放式路径参数，少一个口子就少一类越权。
     *
     * <p>注意本方法<b>可能抛异常</b>（Python 不可达 / 超时）：调用方
     * {@code OpsServiceImpl} 必须接住并转成 "down" 组件，而不是让它冒成 500 ——
     * "连不上 Python"正是这个接口要报告的头号情报。
     */
    public AgentApiResponse<Map<String, Object>> opsHealth(boolean fresh) {
        return opsRestClient.get()
                .uri(fresh ? OPS_HEALTH_PATH + "?fresh=true" : OPS_HEALTH_PATH)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Map<String, Object>>>() {
                });
    }

    /**
     * 取 Python 侧的 Prometheus 文本指标（E-9），**原样字符串**，不做任何 JSON 解析。
     *
     * <p>为什么不能用 {@code AgentApiResponse<...>} 那套：这个响应体不是本项目的
     * JSON 信封（没有 {@code code/data}），而是 Prometheus 文本格式。用 JSON 反序列化会直接失败，
     * 而失败原因（"文本不是 JSON"）在日志里看起来像"智能体返回了坏数据"，误导排查方向。
     *
     * <p>注意本方法<b>可能抛异常</b>（Python 不可达 / 超时）：调用方
     * {@code OpsServiceImpl.metrics} 必须接住，退化成"只返回 Java 段并在注释里写明原因"，
     * 而不是让它冒成 500 —— <b>"Python 那一环不通"正是这套可观测体系要报告的情报</b>。
     */
    public String opsMetricsRaw(int windowHours, boolean fresh) {
        String query = "?window=" + windowHours + (fresh ? "&fresh=true" : "");
        return opsRestClient.get()
                .uri(OPS_METRICS_PATH + query)
                .accept(org.springframework.http.MediaType.TEXT_PLAIN)
                .retrieve()
                .body(String.class);
    }

    /**
     * 前端运行时报错上报（企业级落地规约 E-12）：转成 Python 的 `POST /v1/ops/client-error`。
     *
     * <p>{@code userId} 走<b>查询参数</b>而不是塞进 body——这是刻意的结构约束：
     * 上报体里根本没有 userId 字段，所以"前端自报的身份"在链路上无处可传，
     * 身份只能由 Java 从登录态注入（与 S-1「身份只由 Java 注入」同一条铁律）。</p>
     *
     * <p>路径是<b>常量</b>（不是调用方拼串）：运维类接口不需要开放式路径参数，
     * 少一个口子就少一类越权。</p>
     */
    public AgentApiResponse<Map<String, Object>> opsClientError(long userId, Map<String, Object> body) {
        return opsRestClient.post()
                .uri(OPS_CLIENT_ERROR_PATH + "?user_id=" + userId)
                .body(body)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Map<String, Object>>>() {
                });
    }

    /**
     * 用户画像查看透传（M-5）：GET /v1/profile/{userId}，data 为条目数组。
     * userId 必须来自登录会话（Controller 层收口），不允许前端指定任意人。
     */
    public AgentApiResponse<Object> profileGet(long userId) {
        return restClient.get()
                .uri(PROFILE_BASE + userId)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Object>>() {
                });
    }

    /**
     * 用户画像删除透传（M-5 知情权）：key 为空清空该用户全部，否则删单条。
     */
    public AgentApiResponse<Object> profileDelete(long userId, String key) {
        String path = (key == null || key.isBlank())
                ? PROFILE_BASE + userId
                : PROFILE_BASE + userId + "/" + key;
        return restClient.delete()
                .uri(path)
                .retrieve()
                .body(new ParameterizedTypeReference<AgentApiResponse<Object>>() {
                });
    }
}

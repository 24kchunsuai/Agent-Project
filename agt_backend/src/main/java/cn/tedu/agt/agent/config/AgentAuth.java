package cn.tedu.agt.agent.config;

import cn.tedu.agt.base.log.TraceHeaders;
import org.springframework.http.HttpRequest;
import org.springframework.http.client.ClientHttpRequestExecution;
import org.springframework.http.client.ClientHttpRequestInterceptor;
import org.springframework.http.client.ClientHttpResponse;
import org.springframework.web.client.RestClient;

import java.io.IOException;

/**
 * 服务间鉴权工具（企业级落地规约 S-1）。
 *
 * <p>Java 侧访问 Python 的<strong>每一个</strong> RestClient 出口都必须带
 * {@code X-Agent-Key}。出口目前有三个：{@code AgentClient}（对话/体检/画像）、
 * {@code KbClient}（知识库）、{@code GovernanceClient}（治理）。靠"记得加"迟早会漏，
 * 而漏掉的表现是<strong>那个业务域整体 4010</strong>（前端弹"登录已过期"），
 * 排查方向很容易被带到会话/登录上去。所以把"取密钥 + 空值拒绝 + 统一头名"
 * 收在这一个类里，新增出口时直接调。</p>
 *
 * <p>与 Python 侧 {@code app/core/auth.py} 的 {@code HEADER_NAME} 必须一致；
 * 密钥值两侧同源，Python 写在 {@code agt_python/.env}，Java 用环境变量
 * {@code AGENT_SHARED_KEY} 注入。</p>
 */
public final class AgentAuth {

    /** 服务间鉴权请求头名，对应 Python 侧 {@code auth.HEADER_NAME}。 */
    public static final String HEADER_NAME = "X-Agent-Key";

    /** 密钥在日志里的掩码形态（只留首尾，避免明文进日志文件）。 */
    public static String mask(String key) {
        if (key == null || key.length() <= 4) {
            return "****";
        }
        return key.substring(0, 2) + "***" + key.substring(key.length() - 2)
                + "(len=" + key.length() + ")";
    }

    private AgentAuth() {
    }

    /**
     * 取服务间共享密钥；为空直接抛异常（fail-closed）。
     *
     * <p>刻意不"降级为不带头启动"：那样每个请求都会拿到 Python 的 4010，
     * 把一次部署配置错误伪装成会话问题。</p>
     */
    public static String requireSharedKey(AgentProperties properties) {
        String key = properties.getSharedKey();
        if (key == null || key.isBlank()) {
            throw new IllegalStateException(
                    "AGENT_SHARED_KEY 未配置：拒绝启动（企业级落地规约 S-1，fail-closed）。"
                            + " 生成密钥：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
                            + "；Java 侧用环境变量 AGENT_SHARED_KEY 注入，"
                            + "必须与 agt_python/.env 的 AGENT_SHARED_KEY 同值。");
        }
        return key;
    }

    /**
     * 给 RestClient 构建器统一挂上鉴权头（所有出站客户端都从这里过），
     * 并注册<strong>链路头透传拦截器</strong>（E-6）。
     */
    public static RestClient.Builder withKey(RestClient.Builder builder, AgentProperties properties) {
        return builder
                .defaultHeader(HEADER_NAME, requireSharedKey(properties))
                .requestInterceptor(new TracePropagationInterceptor());
    }

    /**
     * 出站透传 {@code X-Trace-Id} / {@code X-User-Id}（企业级落地规约 E-6）。
     *
     * <p>为什么做成拦截器而不是 {@code defaultHeader}：默认头在构建期就固定了，
     * 而 traceId 是<strong>每个请求都不一样</strong>的。</p>
     *
     * <p>为什么放在这里：本方法是三个出站客户端（AgentClient / KbClient /
     * GovernanceClient）的共同入口，加一次就全覆盖。这与 S-1 把
     * {@code X-Agent-Key} 收在这里是同一个理由——靠"记得加"迟早会漏一个出口。</p>
     *
     * <p>⚠️ <strong>流式不经过这里</strong>：{@code AgentClient.chatStream} 走 JDK
     * {@code HttpClient}，必须自己调 {@code TraceHeaders.apply(...)}。</p>
     */
    static final class TracePropagationInterceptor implements ClientHttpRequestInterceptor {

        @Override
        public ClientHttpResponse intercept(HttpRequest request, byte[] body,
                                            ClientHttpRequestExecution execution) throws IOException {
            String traceId = TraceHeaders.currentTraceId();
            if (traceId != null) {
                request.getHeaders().set(TraceHeaders.TRACE, traceId);
            }
            String userId = TraceHeaders.currentUserId();
            if (userId != null) {
                request.getHeaders().set(TraceHeaders.USER, userId);
            }
            return execution.execute(request, body);
        }
    }
}

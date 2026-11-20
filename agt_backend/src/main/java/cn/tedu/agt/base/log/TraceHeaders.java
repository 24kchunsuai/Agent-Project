package cn.tedu.agt.base.log;

import org.slf4j.MDC;

import java.net.http.HttpRequest;

/**
 * 链路头的唯一常量与读写入口（企业级落地规约 E-6）。
 *
 * <p>把"头名 / MDC key / 当前值 / 怎么加到出站请求"收在一处，理由是
 * <strong>同一个值要在三个出口上出现</strong>：{@code TraceIdFilter} 写入、
 * RestClient 拦截器透传、SSE 的 JDK HttpClient 手动透传。三处各写一遍字符串，
 * 早晚会有一处写错，而写错的表现是"那一类请求没有 traceId"——不报错，只是查不到。</p>
 *
 * <p>命名与 Python 侧 {@code app/core/trace.py} 的常量逐字一致
 * （{@code TRACE_HEADER}/{@code USER_HEADER}）。</p>
 */
public final class TraceHeaders {

    /** 入站/出站链路 id 头（规范 §6） */
    public static final String TRACE = "X-Trace-Id";

    /** 用户 id 头：**只用于日志检索**，不做任何鉴权判定（身份以 Java 会话为准） */
    public static final String USER = "X-User-Id";

    /** MDC key：与日志字段名同名，便于对照（规范 §2） */
    public static final String MDC_TRACE_ID = "traceId";
    public static final String MDC_USER_ID = "userId";

    private TraceHeaders() {
    }

    /** 当前请求上下文的 traceId；不在请求里返回 {@code null}。 */
    public static String currentTraceId() {
        return blankToNull(MDC.get(MDC_TRACE_ID));
    }

    /** 当前请求上下文的 userId（十进制字符串）；未登录/不在请求里返回 {@code null}。 */
    public static String currentUserId() {
        return blankToNull(MDC.get(MDC_USER_ID));
    }

    /**
     * 给 JDK {@code HttpClient} 的请求加上链路头（SSE 流式那条独立路径专用）。
     *
     * <p>为什么必须单独调：{@code chatStream} 走的是 JDK {@code HttpClient}，
     * <strong>不共享</strong> RestClient 的拦截器与默认头。这与 S-1 的
     * {@code X-Agent-Key} 是同一条教训（{@code DEV_ISSUES.md} §6.71）——
     * 当年就是"以为默认头会带上"导致聊天流被 4010。</p>
     */
    public static HttpRequest.Builder apply(HttpRequest.Builder builder) {
        String traceId = currentTraceId();
        if (traceId != null) {
            builder.header(TRACE, traceId);
        }
        String userId = currentUserId();
        if (userId != null) {
            builder.header(USER, userId);
        }
        return builder;
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value;
    }
}

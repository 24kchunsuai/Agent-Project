package cn.tedu.agt.base.log;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * 链路 id 过滤器（企业级落地规约 <strong>E-6</strong>）。
 *
 * <p>本次请求的 {@code traceId} 在这里<strong>产生</strong>（或沿用上游传来的），
 * 放进 {@link MDC} 供日志 layout 取用，并<strong>回写响应头</strong>
 * {@code X-Trace-Id}。规则见 {@code docs/日志规范.md} §6。</p>
 *
 * <h3>为什么生成方只有 Java</h3>
 * <p>前端不生成：前端代码不可信（可以伪造），且多页签/重试会撞号。
 * 上游若已经带了合法的 {@code X-Trace-Id}（例如网关或压测工具），则沿用——
 * 这样"一次压测"也能被串成一条链。</p>
 *
 * <h3>为什么必须校验入站值</h3>
 * <p>{@code X-Trace-Id} 是<strong>外部输入</strong>。原样塞进日志会构成
 * <strong>日志伪造</strong>：一个引号就能破坏 JSON 行结构，一个换行就能凭空造出
 * 一条记录。所以只接受白名单字符集，不合法就"忽略该输入"并另生成一个，
 * 而不是"转义后使用"——后者仍会把垃圾带进日志。</p>
 *
 * <h3>顺序与清理</h3>
 * <ul>
 *   <li>{@link Order} 设为最高优先级：这样后续所有过滤器/拦截器的日志都带上 id。</li>
 *   <li>{@code finally} 里 {@code MDC.clear()}：Tomcat 线程是复用的，不清理会让
 *       下一个请求"继承"上一个请求的 id——表现为日志里两个请求串成一串，极难发现。</li>
 *   <li>异步（SSE）派发默认不重复执行（{@link OncePerRequestFilter} 的
 *       {@code shouldNotFilterAsyncDispatch} 默认 true），所以不会把 id 覆盖掉。</li>
 * </ul>
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceIdFilter extends OncePerRequestFilter {

    /** 入站/出站头名（常量统一定义在 {@link TraceHeaders}，避免三处各写一遍字符串） */
    public static final String TRACE_HEADER = TraceHeaders.TRACE;

    /** 用户 id 头：**只用于日志检索**，不参与任何鉴权判定（身份以 Java 会话为准） */
    public static final String USER_HEADER = TraceHeaders.USER;

    /** MDC key（与日志规范 §2 字段名同名，便于对照） */
    public static final String MDC_TRACE_ID = TraceHeaders.MDC_TRACE_ID;
    public static final String MDC_USER_ID = TraceHeaders.MDC_USER_ID;

    /** 白名单：十六进制或常见 id 字符，4~64 位。刻意放宽到字母数字与 {@code _-}，
     *  好让"上游已有 id"能被复用；但仍拒绝引号/换行/空格等可破坏日志结构的字符。 */
    private static final Pattern TRACE_PATTERN = Pattern.compile("^[0-9A-Za-z_-]{4,64}$");

    /** userId 只收十进制（{@code LoginUser.userId} 是 Long） */
    private static final Pattern USER_PATTERN = Pattern.compile("^[0-9]{1,20}$");

    /** 造一个新 traceId：16 位十六进制 = 64 bit，碰撞概率可忽略。 */
    public static String newTraceId() {
        return UUID.randomUUID().toString().replace("-", "").substring(0, 16);
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        String incoming = sanitize(request.getHeader(TRACE_HEADER), TRACE_PATTERN);
        String traceId = incoming != null ? incoming : newTraceId();
        MDC.put(MDC_TRACE_ID, traceId);
        // userId 通常此时还没有（认证拦截器在 Filter 之后才跑），由 AuthInterceptor 补写；
        // 这里先取一遍是为了"上游已经带了身份"的场景（如内部工具直连）。
        String userId = sanitize(request.getHeader(USER_HEADER), USER_PATTERN);
        if (userId != null) {
            MDC.put(MDC_USER_ID, userId);
        }

        // 回写响应头：前端弹错时带上它，用户报一句 traceId 就能定位到链路
        response.setHeader(TRACE_HEADER, traceId);
        long startedAt = System.nanoTime();
        try {
            chain.doFilter(request, response);
        } finally {
            // 一条 http.request（含耗时与状态码）。放在 finally 且**在 MDC.clear() 之前**，
            // 否则这条日志本身就没有 traceId —— 最容易漏的一处。
            recordRequest(request, response, startedAt);
            MDC.clear();
        }
    }

    private static final Logger ACCESS_LOG = LoggerFactory.getLogger("cn.tedu.agt.http");

    /**
     * 健康探针路径：成功时不写结构化日志。
     *
     * <p>理由与 Python 侧一致——前端每 30s 一次的三态灯、运维脚本的探活，
     * 全量记会把结构化日志冲成噪音，真正要看的业务请求反被淹没。
     * <strong>失败时仍会记</strong>：「探针挂了」必须看得见。</p>
     *
     * <p>⚠️ 只放<strong>探针</strong>，不放业务读接口（{@code /v1/kb/stats} 这类
     * 看板接口属于业务流量，要看得见）——这条边界写严一点，将来不会有人
     * 因为"这条也太频繁了吧"顺手加进来，最后把业务日志稀释掉。</p>
     *
     * <p>E-12 的 {@code /v1/ops/client-error} 是这条边界上的<b>一个特例，不是破例</b>：
     * 它既不是探针也不是业务读，而是<b>上报通道</b>——性质与探针同类（后台、周期性、
     * 无人值守，一次报错风暴能连来几十条），且它有自己的观测出口
     * （{@code client_error} 表 + 看板「前端错误」卡 + Python 侧同口径的 quiet path）。
     * 「前端报错」这件事要看，看的应该是那张表，而不是结构化日志里的一串
     * {@code POST /v1/ops/client-error -> 200}；把后者留着，正好把前者淹没。</p>
     */
    private static final Set<String> QUIET_PATHS =
            Set.of("/v1/version", "/v1/ops/health", "/v1/ops/metrics", "/v1/agent/health",
                    "/v1/ops/client-error");

    private static void recordRequest(HttpServletRequest request, HttpServletResponse response,
                                      long startedAt) {
        try {
            int status = response.getStatus();
            String path = request.getRequestURI();
            if (QUIET_PATHS.contains(path) && status >= 200 && status < 300) {
                return;
            }
            long costMs = (System.nanoTime() - startedAt) / 1_000_000L;
            StructuredLog.info(ACCESS_LOG, "http.request", costMs,
                    "{} {} -> {}", request.getMethod(), path, status);
        } catch (RuntimeException ignored) {
            // 埋点绝不能让一个已经成功的请求变成 500（埋点铁律）
        }
    }

    private static String sanitize(String value, Pattern pattern) {
        if (value == null) {
            return null;
        }
        String trimmed = value.trim();
        return pattern.matcher(trimmed).matches() ? trimmed : null;
    }
}

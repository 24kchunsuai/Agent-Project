package cn.tedu.agt.base.log;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import jakarta.servlet.FilterChain;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 链路 id 过滤器（E-6，`docs/日志规范.md` §6）。
 *
 * <p>三条最要紧的：</p>
 * <ol>
 *   <li><strong>采纳入站 id</strong>：不采纳就等于"两端各有各的 id"，
 *       "一次 grep 串三端"从根上失效，而且失效方式很隐蔽（两端都有 id，只是不同）。</li>
 *   <li><strong>拒绝畸形入站 id</strong>：{@code X-Trace-Id} 是外部输入，
 *       带引号或换行就能伪造日志行（log forging）。</li>
 *   <li><strong>MDC 必须清理</strong>：Tomcat 线程复用，不清理会让下一个请求
 *       顶着上一个请求的 id/userId 写日志——最难发现的一类错。</li>
 * </ol>
 */
class TraceIdFilterTest {

    private TraceIdFilter filter;
    private MockHttpServletResponse response;
    private Logger accessLogger;
    private ListAppender<ILoggingEvent> appender;

    @BeforeEach
    void setUp() {
        filter = new TraceIdFilter();
        response = new MockHttpServletResponse();
        accessLogger = (Logger) LoggerFactory.getLogger("cn.tedu.agt.http");
        appender = new ListAppender<>();
        appender.start();
        accessLogger.addAppender(appender);
    }

    @AfterEach
    void tearDown() {
        accessLogger.detachAppender(appender);
        MDC.clear();
    }

    private void doFilter(String method, String uri, String traceHeader, String userHeader)
            throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest(method, uri);
        if (traceHeader != null) {
            request.addHeader(TraceIdFilter.TRACE_HEADER, traceHeader);
        }
        if (userHeader != null) {
            request.addHeader(TraceIdFilter.USER_HEADER, userHeader);
        }
        FilterChain chain = (req, res) -> {
        };
        filter.doFilter(request, response, chain);
    }

    @Test
    @DisplayName("没有入站 id → 生成 16 位 hex 并回写响应头")
    void generatesWhenAbsent() throws Exception {
        doFilter("GET", "/v1/agent/chat", null, null);

        String header = response.getHeader(TraceIdFilter.TRACE_HEADER);
        assertThat(header).isNotNull().matches("[0-9a-f]{16}");
    }

    @Test
    @DisplayName("入站带合法 id → 原样采纳（这是「串两端」的前提）")
    void adoptsIncomingId() throws Exception {
        doFilter("GET", "/v1/agent/chat", "8f3c1a92d47b05e6", null);

        assertThat(response.getHeader(TraceIdFilter.TRACE_HEADER)).isEqualTo("8f3c1a92d47b05e6");
    }

    @Test
    @DisplayName("入站 id 含引号/换行 → 丢弃并另生成（防日志伪造）")
    void rejectsForgedId() throws Exception {
        doFilter("GET", "/v1/agent/chat", "evil\"id\ninjected", null);

        String header = response.getHeader(TraceIdFilter.TRACE_HEADER);
        assertThat(header).isNotNull().matches("[0-9a-zA-Z_-]{4,64}");
        assertThat(header).doesNotContain("evil").doesNotContain("injected");
    }

    @Test
    @DisplayName("requests 全程可读 traceId；过滤结束后 MDC 被清空")
    void mdcSetDuringChainAndClearedAfter() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/agent/chat");
        request.addHeader(TraceIdFilter.TRACE_HEADER, "aaaabbbbccccdddd");
        String[] seen = new String[1];
        FilterChain chain = (req, res) -> seen[0] = MDC.get(TraceIdFilter.MDC_TRACE_ID);

        filter.doFilter(request, response, chain);

        assertThat(seen[0]).isEqualTo("aaaabbbbccccdddd");
        assertThat(MDC.get(TraceIdFilter.MDC_TRACE_ID)).isNull();
        assertThat(MDC.get(TraceIdFilter.MDC_USER_ID)).isNull();
    }

    @Test
    @DisplayName("http.request 事件带 event/durationMs，且耗时已计过（不再是 -1）")
    void logsRequestEventWithDuration() throws Exception {
        doFilter("POST", "/v1/agent/chat", "aaaabbbbccccdddd", "7");

        List<ILoggingEvent> events = appender.list;
        assertThat(events).hasSize(1);
        ILoggingEvent event = events.get(0);
        assertThat(event.getMDCPropertyMap().get("event")).isEqualTo("http.request");
        assertThat(event.getMDCPropertyMap().get("traceId")).isEqualTo("aaaabbbbccccdddd");
        assertThat(event.getMDCPropertyMap().get("userId")).isEqualTo("7");
        assertThat(Integer.parseInt(event.getMDCPropertyMap().get("durationMs"))).isGreaterThanOrEqualTo(0);
        assertThat(event.getFormattedMessage()).contains("POST /v1/agent/chat -> 200");
    }

    @Test
    @DisplayName("健康探针成功不记（否则 30s 一条把结构化日志冲成噪音）")
    void quietPathSuccessNotLogged() throws Exception {
        doFilter("GET", "/v1/version", null, null);

        assertThat(appender.list).isEmpty();
    }

    @Test
    @DisplayName("探针路径返回非 2xx 时仍要记（「探针挂了」必须看得见）")
    void quietPathFailureIsLogged() throws Exception {
        response.setStatus(500);
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/v1/version");
        filter.doFilter(request, response, (req, res) -> {
        });

        assertThat(appender.list).hasSize(1);
    }

    @Test
    @DisplayName("请求日志不含查询串（里面有用户输入与 key，不记比脱敏可靠）")
    void queryStringNotLogged() throws Exception {
        doFilter("GET", "/v1/kb/stats", null, null);

        assertThat(appender.list.get(0).getFormattedMessage()).isEqualTo("GET /v1/kb/stats -> 200");
    }

    @Test
    @DisplayName("userId 缺失时不写入（而不是写空串进 MDC）")
    void absentUserIdLeavesNoMdcEntry() throws Exception {
        doFilter("GET", "/v1/agent/chat", null, null);

        assertThat(appender.list.get(0).getMDCPropertyMap()).doesNotContainKey("userId");
    }

    @Test
    @DisplayName("非法 userId（非十进制）被忽略，不影响请求")
    void malformedUserIdIgnored() throws Exception {
        doFilter("GET", "/v1/agent/chat", null, "7); drop table");

        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(appender.list.get(0).getMDCPropertyMap()).doesNotContainKey("userId");
    }

    @Test
    @DisplayName("newTraceId 是 16 位十六进制且不重复")
    void newTraceIdShape() {
        String a = TraceIdFilter.newTraceId();
        String b = TraceIdFilter.newTraceId();

        assertThat(a).matches("[0-9a-f]{16}");
        assertThat(a).isNotEqualTo(b);
    }
}

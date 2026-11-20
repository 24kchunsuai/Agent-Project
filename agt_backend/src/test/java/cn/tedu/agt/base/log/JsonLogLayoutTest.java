package cn.tedu.agt.base.log;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.LoggingEvent;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 结构化日志渲染契约（E-6，`docs/日志规范.md` §2）。
 *
 * <p>为什么值得单独测：字段名/顺序是<strong>两侧的契约</strong>。
 * 一旦漂移，下游 {@code jq '.traceId'} 会时而拿到值时而拿到 {@code null}，
 * 脚本开始"时好时坏"——而那种问题最难归因（因为代码看起来没错）。</p>
 *
 * <p>断言方式刻意是"用 Jackson 解析回来"而不是"字符串比对"：
 * 后者会让"能解析"这个前提完全没被验证。</p>
 */
class JsonLogLayoutTest {

    private static final List<String> SPEC_FIELDS = List.of(
            "ts", "level", "service", "traceId", "userId", "event", "durationMs", "msg", "err");

    private final ObjectMapper mapper = new ObjectMapper();

    private JsonLogLayout layout() {
        JsonLogLayout layout = new JsonLogLayout();
        layout.setService("agt_backend");
        return layout;
    }

    private LoggingEvent event(Level level, String message, Throwable throwable) {
        // ⚠️ 必须用 `LoggerFactory.getLogger`（触发 slf4j→logback 绑定）而不是
        // `new LoggerContext().getLogger(...)`：后者不会初始化 slf4j 的 MDCAdapter，
        // `getMDCPropertyMap()` 会直接 NPE（实测踩到）。
        Logger logger = (Logger) LoggerFactory.getLogger("cn.tedu.agt.demo");
        return new LoggingEvent("cn.tedu.agt.demo", logger, level, message, throwable, null);
    }

    @AfterEach
    void tearDown() {
        MDC.clear();
    }

    @Test
    @DisplayName("九字段齐全且顺序与规范一致")
    void allSpecFieldsInOrder() throws Exception {
        Map<String, Object> row = parse(layout().doLayout(event(Level.INFO, "hello", null)));

        assertThat(row.keySet()).containsExactlyElementsOf(SPEC_FIELDS);
    }

    @Test
    @DisplayName("ts 带毫秒与时区偏移（只写时分秒会在跨机器对齐时错 8 小时）")
    void timestampHasMillisAndOffset() throws Exception {
        Map<String, Object> row = parse(layout().doLayout(event(Level.INFO, "ts", null)));

        assertThat((String) row.get("ts"))
                .matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}[+-]\\d{2}:\\d{2}");
    }

    @Test
    @DisplayName("level / service 固定取自事件与配置")
    void levelAndService() throws Exception {
        Map<String, Object> row = parse(layout().doLayout(event(Level.WARN, "w", null)));

        assertThat(row.get("level")).isEqualTo("WARN");
        assertThat(row.get("service")).isEqualTo("agt_backend");
    }

    @Test
    @DisplayName("traceId / userId 取自 MDC 快照")
    void traceAndUserFromMdc() throws Exception {
        MDC.put("traceId", "8f3c1a92d47b05e6");
        MDC.put("userId", "7");

        Map<String, Object> row = parse(layout().doLayout(event(Level.INFO, "在请求里", null)));

        assertThat(row.get("traceId")).isEqualTo("8f3c1a92d47b05e6");
        assertThat(row.get("userId")).isEqualTo("7");
    }

    @Test
    @DisplayName("不在请求上下文时 traceId/userId 是空串（字段仍恒在）")
    void emptyIdsOutsideRequest() throws Exception {
        Map<String, Object> row = parse(layout().doLayout(event(Level.INFO, "启动期", null)));

        assertThat(row.get("traceId")).isEqualTo("");
        assertThat(row.get("userId")).isEqualTo("");
    }

    @Test
    @DisplayName("event 缺省取 logger 名；MDC 里显式给了就用显式的")
    void eventDefaultsToLoggerName() throws Exception {
        Map<String, Object> row = parse(layout().doLayout(event(Level.INFO, "默认", null)));
        assertThat(row.get("event")).isEqualTo("cn.tedu.agt.demo");

        MDC.put("event", "agent.call");
        Map<String, Object> explicit = parse(layout().doLayout(event(Level.INFO, "显式", null)));
        assertThat(explicit.get("event")).isEqualTo("agent.call");
    }

    @Test
    @DisplayName("durationMs 缺省 -1，给了就用给的；非数字回落 -1")
    void durationSemantics() throws Exception {
        assertThat(parse(layout().doLayout(event(Level.INFO, "无", null))).get("durationMs"))
                .isEqualTo(-1);

        MDC.put("durationMs", "57");
        assertThat(parse(layout().doLayout(event(Level.INFO, "有", null))).get("durationMs"))
                .isEqualTo(57);

        MDC.put("durationMs", "not-a-number");
        assertThat(parse(layout().doLayout(event(Level.INFO, "坏", null))).get("durationMs"))
                .isEqualTo(-1);
    }

    @Test
    @DisplayName("消息里的引号/换行/反斜杠不会破坏 JSON（消息内容不可控）")
    void messageIsEscaped() throws Exception {
        String line = layout().doLayout(event(Level.INFO, "第一行\n第二行 \"引号\" \\反斜杠", null));

        assertThat(line).doesNotContain("\n第二行");     // 物理上仍是一行
        assertThat(parse(line).get("msg")).isEqualTo("第一行\n第二行 \"引号\" \\反斜杠");
    }

    @Test
    @DisplayName("异常栈进 err 字段，且被折叠成单行 JSON")
    void exceptionStaysOnOneLine() throws Exception {
        String line = layout().doLayout(
                event(Level.ERROR, "炸了", new IllegalStateException("boom")));

        Map<String, Object> row = parse(line);
        assertThat((String) row.get("err")).contains("IllegalStateException").contains("boom");
        assertThat(line.split("\r?\n")).hasSize(1);
    }

    @Test
    @DisplayName("msg 与 err 都要过脱敏（布局出口是唯一收口）")
    void bothMsgAndErrAreRedacted() throws Exception {
        MDC.put("traceId", "abc");
        LoggingEvent event = event(Level.ERROR, "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
                new IllegalStateException("X-Agent-Key: 7f3ac9d2e1b84f60"));

        String line = layout().doLayout(event);

        assertThat(line).doesNotContain("eyJhbGciOiJIUzI1NiJ9");
        assertThat(line).doesNotContain("7f3ac9d2e1b84f60");
    }

    @Test
    @DisplayName("输出的每一行都能独立解析（一个事件 = 一行）")
    void eachLineIsSelfContainedJson() throws Exception {
        String out = layout().doLayout(event(Level.INFO, "a", null))
                + layout().doLayout(event(Level.INFO, "b", null));

        String[] lines = out.split("\r?\n");
        assertThat(lines).hasSize(2);
        for (String line : lines) {
            assertThat(parse(line).get("msg")).isIn("a", "b");
        }
    }

    private Map<String, Object> parse(String line) throws Exception {
        return mapper.readValue(line.trim(), Map.class);
    }
}

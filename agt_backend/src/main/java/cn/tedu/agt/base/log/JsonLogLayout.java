package cn.tedu.agt.base.log;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.classic.spi.ThrowableProxyUtil;
import ch.qos.logback.core.LayoutBase;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Collections;
import java.util.Map;

/**
 * 结构化日志 layout：把一条 logback 事件渲染成<strong>固定九字段的 JSON 单行</strong>
 * （企业级落地规约 <strong>E-6</strong>）。字段契约见 {@code docs/日志规范.md} §2。
 *
 * <pre>
 * {"ts":"2026-09-24T12:40:01.123+08:00","level":"INFO","service":"agt_backend",
 *  "traceId":"8f3c1a92d47b05e6","userId":"1","event":"http.request",
 *  "durationMs":57,"msg":"POST /v1/agent/chat -> 200","err":""}
 * </pre>
 *
 * <h3>为什么手写 layout，而不是用 logstash-logback-encoder</h3>
 * <p>决策 D3（可观测轻量自建，不引 Prometheus/ELK）同样适用于依赖：多一个 jar 就多
 * 一份版本与漏洞维护成本。这里需要的只是"九字段 + 转义 + 脱敏"，几十行足够。</p>
 *
 * <h3>为什么不用 PatternLayout 拼 JSON</h3>
 * <p>{@code PatternLayout} 无法对任意消息做**可靠的 JSON 转义**——消息里出现引号或
 * 换行就会产出非法 JSON。而"日志文件偶尔不是合法 JSON"比"多写一个类"更贵：
 * 下游脚本会在某一天莫名失败，且很难复现。</p>
 *
 * <h3>两个实现要点</h3>
 * <ol>
 *   <li>取值用 {@link ILoggingEvent#getMDCPropertyMap()}（事件创建时的 MDC 快照），
 *       <strong>不是</strong> {@code MDC.get()}：后者读的是**当前线程**的 MDC，
 *       配异步 appender 或跨线程 emit 时会取到别的请求的值。</li>
 *   <li>{@code service} 由 logback 配置注入（{@code <service>agt_backend</service>}），
 *       保证与 Python 侧 {@code agt_python} 是同一套枚举（规范 §2.1）。</li>
 * </ol>
 */
public class JsonLogLayout extends LayoutBase<ILoggingEvent> {

    /** 与 Python 侧 `isoformat(timespec="milliseconds")` 对齐：带毫秒 + 本地时区偏移 */
    private static final DateTimeFormatter TS =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSXXX");

    /** 由 logback 配置注入（见 logback-spring.xml）。 */
    private String service = "agt_backend";

    public void setService(String service) {
        this.service = service;
    }

    @Override
    public String doLayout(ILoggingEvent event) {
        Map<String, String> mdc = event.getMDCPropertyMap();
        if (mdc == null) {
            // 理论上 logback 会从当前 MDC 兜底，但 layout 是日志出口：
            // 这里宁可写空值，也不能让一个 NPE 把这条日志整个吞掉。
            mdc = Collections.emptyMap();
        }
        StringBuilder sb = new StringBuilder(256);
        sb.append('{');
        field(sb, "ts", TS.format(Instant.ofEpochMilli(event.getTimeStamp())
                .atZone(ZoneId.systemDefault())), true);
        field(sb, "level", event.getLevel().toString(), true);
        field(sb, "service", service, true);
        field(sb, "traceId", mdc.getOrDefault("traceId", ""), true);
        field(sb, "userId", mdc.getOrDefault("userId", ""), true);
        // event 缺省取 logger 名：这样"没显式登记事件名"的地方也不会出现空字段，
        // 代价是那几个事件不好过滤——所以关键路径必须显式命名（规范 §3）。
        field(sb, "event", orDefault(mdc.get("event"), event.getLoggerName()), true);
        sb.append("\"durationMs\":").append(durationMs(mdc));
        sb.append(',');
        field(sb, "msg", LogRedactor.redact(event.getFormattedMessage()), true);
        field(sb, "err", LogRedactor.redact(stack(event)), false);
        sb.append('}');
        sb.append(System.lineSeparator());
        return sb.toString();
    }

    /** 不适用时写 {@code -1}（规范 §2：不用 null，避免消费者做两种空值判断）。 */
    private static String durationMs(Map<String, String> mdc) {
        String raw = mdc.get("durationMs");
        if (raw == null || raw.isBlank()) {
            return "-1";
        }
        try {
            return Integer.toString(Integer.parseInt(raw.trim()));
        } catch (NumberFormatException e) {
            return "-1";
        }
    }

    private static String stack(ILoggingEvent event) {
        IThrowableProxy proxy = event.getThrowableProxy();
        if (proxy == null) {
            return "";
        }
        // 完整栈（换行会被转义成 \n 保持在单行内）：JSON 里丢栈等于把排障能力砍掉一半
        return ThrowableProxyUtil.asString(proxy);
    }

    private static String orDefault(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }

    private static void field(StringBuilder sb, String name, String value, boolean commaAfter) {
        sb.append('"').append(name).append("\":\"");
        escape(sb, value);
        sb.append('"');
        if (commaAfter) {
            sb.append(',');
        }
    }

    /** 最小但正确的 JSON 字符串转义（RFC 8259）。非 ASCII 原样输出（文件编码为 UTF-8）。 */
    private static void escape(StringBuilder sb, String value) {
        if (value == null) {
            return;
        }
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
    }
}

package cn.tedu.agt.base.log;

import org.slf4j.Logger;
import org.slf4j.MDC;
import org.slf4j.helpers.MessageFormatter;

/**
 * 结构化日志的顺手写法：一条语句里带上 {@code event} 与 {@code durationMs}
 * （企业级落地规约 E-6，字段见 {@code docs/日志规范.md} §3）。
 *
 * <p>{@link JsonLogLayout} 的 {@code event} / {@code durationMs} 取自 MDC，
 * 所以业务代码要设它们就绕不开 {@code MDC.put(...)}+还原的样板代码。
 * 本类把那对样板收成一行，顺带保证<strong>用完一定还原</strong>——
 * 不还原的后果是"后面所有日志都带着上一个事件的 durationMs"，
 * 数字还都不离谱，极难发现。</p>
 *
 * <p>用法（占位符是 slf4j 的 <code>{}</code>，不是 printf 的 {@code %s}）：</p>
 * <pre>
 * StructuredLog.info(log, "agent.call", costMs, "POST {} -&gt; {}", path, status);
 * StructuredLog.error(log, "chat.stream", costMs, e, "流式对话异常:sessionId={}", sid);
 * </pre>
 *
 * <h3>为什么自己调 {@link MessageFormatter} 而不是直接转给 {@code log.info(format, args)}</h3>
 * <p>这是 Java 变参的经典坑：本类的 {@code args} 静态类型是 {@code Object[]}，
 * 于是 {@code log.info(format, args)} 会被重载解析选成
 * {@link Logger#info(String, Object)}（单参版本，不是变参版本），
 * 整个数组被当成<strong>一个</strong>参数——结果是日志里原样打出 {@code "{} {} -> {}"}，
 * 既不报错也不抛异常，只是消息全成了模板本身。实测就是这么发现的。</p>
 */
public final class StructuredLog {

    private static final String KEY_EVENT = "event";
    private static final String KEY_DURATION = "durationMs";

    private StructuredLog() {
    }

    public static void info(Logger log, String event, long durationMs, String format, Object... args) {
        String msg = render(format, args);
        withEvent(event, durationMs, () -> log.info(msg));
    }

    public static void warn(Logger log, String event, long durationMs, String format, Object... args) {
        String msg = render(format, args);
        withEvent(event, durationMs, () -> log.warn(msg));
    }

    public static void error(Logger log, String event, long durationMs, Throwable t,
                             String format, Object... args) {
        String msg = render(format, args);
        withEvent(event, durationMs,
                t == null ? () -> log.error(msg) : () -> log.error(msg, t));
    }

    /** 自己渲染消息：见类注释里那个"变参被当成单参"的坑。失败时退回模板原文，不抛异常。 */
    private static String render(String format, Object[] args) {
        if (format == null) {
            return "";
        }
        if (args == null || args.length == 0) {
            return format;
        }
        try {
            return MessageFormatter.basicArrayFormat(format, args);
        } catch (RuntimeException ignored) {
            return format;
        }
    }

    private static void withEvent(String event, long durationMs, Runnable emit) {
        String previousEvent = MDC.get(KEY_EVENT);
        String previousDuration = MDC.get(KEY_DURATION);
        MDC.put(KEY_EVENT, event);
        MDC.put(KEY_DURATION, Long.toString(durationMs));
        try {
            emit.run();
        } finally {
            restore(KEY_EVENT, previousEvent);
            restore(KEY_DURATION, previousDuration);
        }
    }

    private static void restore(String key, String previous) {
        if (previous == null) {
            MDC.remove(key);
        } else {
            MDC.put(key, previous);
        }
    }
}

package cn.tedu.agt.base.log;

import org.slf4j.MDC;

import java.util.Map;
import java.util.concurrent.Callable;

/**
 * 把 {@link MDC}（traceId / userId / event / durationMs）<strong>带过线程边界</strong>
 * （企业级落地规约 E-6）。
 *
 * <h3>为什么必须有这个东西</h3>
 * <p>MDC 是 {@link ThreadLocal}——<strong>不会</strong>自动跟到线程池里的线程。
 * 本项目的流式对话走 {@code STREAM_POOL.execute(...)}（见 {@code AgentServiceImpl}），
 * 如果不管，症状是：<strong>整条链路只有"流式那一段"的日志没有 traceId</strong>。
 * 而这种缺失平时看不出来，只在排查流式问题时才发现——那时正好最需要一个 id
 * 把前后文串起来。</p>
 *
 * <p>与 S-1 的教训同源：{@code X-Agent-Key} 也是"流式走了独立的 HttpClient、
 * 不共享 RestClient 默认头"，于是也要单独加一次（`DEV_ISSUES.md` §6.71）。</p>
 *
 * <h3>用法</h3>
 * <pre>
 * executor.execute(MdcScope.wrap(() -&gt; doWork()));   // 子线程继承调用时的上下文
 * </pre>
 */
public final class MdcScope {

    private MdcScope() {
    }

    /** 返回一个"执行前继承当前 MDC、执行后还原"的任务包装。 */
    public static Runnable wrap(Runnable task) {
        Map<String, String> captured = MDC.getCopyOfContextMap();
        return () -> runWith(captured, task);
    }

    /** Callable 版本（要拿返回值时用）。 */
    public static <T> Callable<T> wrap(Callable<T> task) {
        Map<String, String> captured = MDC.getCopyOfContextMap();
        return () -> {
            Map<String, String> previous = MDC.getCopyOfContextMap();
            apply(captured);
            try {
                return task.call();
            } finally {
                apply(previous);
            }
        };
    }

    private static void runWith(Map<String, String> captured, Runnable task) {
        Map<String, String> previous = MDC.getCopyOfContextMap();
        apply(captured);
        try {
            task.run();
        } finally {
            apply(previous);
        }
    }

    /** 还原上一个线程的上下文：线程池线程会被复用，不还原会让下一个任务"继承"别人的 id。 */
    private static void apply(Map<String, String> context) {
        if (context == null || context.isEmpty()) {
            MDC.clear();
        } else {
            MDC.setContextMap(context);
        }
    }
}

package cn.tedu.agt.ops.metrics;

import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.RuntimeMXBean;
import java.lang.management.ThreadMXBean;

/**
 * Java 侧指标（企业级落地规约 <b>E-9</b>）。
 *
 * <h3>为什么 Java 侧要自己产一段，而不是纯转发 Python 的输出</h3>
 * 与 E-3 健康聚合同一条理由：<b>"Java 自己还活着吗 / 堆够不够"只有 Java 能给</b>。
 * 纯转发的 {@code /v1/ops/metrics} 会让人以为"抓到了整套系统的指标"，而实际
 * <b>JVM 指标一条都没有</b>——那正是最需要在磁盘满之前看到的东西（E-7 备份失败
 * 最常见的原因之一就是磁盘/内存）。命名空间用 {@code agt_java_*} 与 Python 侧的
 * {@code agt_*} 区分开，避免两段合并时撞名（撞名会让整次抓取失败）。
 *
 * <h3>为什么不引 Micrometer</h3>
 * 见 {@link PrometheusText} 的类注释：为一个 layout 拉一套指标框架不划算。
 * 本类只用 JDK 的 {@code java.lang.management}，零新依赖。
 */
public final class JavaMetrics {

    private JavaMetrics() {
    }

    /** 渲染 {@code agt_java_*} 段。纯读 JDK MXBean，不做任何 IO，不会抛。 */
    public static String render() {
        PrometheusText p = new PrometheusText();
        RuntimeMXBean rt = ManagementFactory.getRuntimeMXBean();
        MemoryMXBean mem = ManagementFactory.getMemoryMXBean();
        ThreadMXBean threads = ManagementFactory.getThreadMXBean();
        Runtime r = Runtime.getRuntime();

        p.family("agt_java_up", "gauge",
                "Java 后端进程存活并成功渲染本响应（1/0）。");
        p.family("agt_java_info", "gauge",
                "版本与运行时信息（值恒为 1，看 label）。");
        p.family("agt_java_heap_used_bytes", "gauge",
                "JVM 堆已用字节数。持续逼近 max 是 OOM 的前兆。");
        p.family("agt_java_heap_max_bytes", "gauge",
                "JVM 堆上限字节数（-Xmx）。");
        p.family("agt_java_nonheap_used_bytes", "gauge",
                "JVM 非堆（元空间/类元数据/代码缓存）已用字节数。类加载器泄漏时它只涨不落。");
        p.family("agt_java_threads", "gauge",
                "JVM 存活线程数。SSE 流式会占用线程池，长时间不回落值得怀疑。");
        p.family("agt_java_uptime_seconds", "gauge",
                "JVM 已运行秒数。它突然变小 = 进程被重启过（配合 agt_python_up 判断是谁重启了）。");
        p.family("agt_java_processors", "gauge", "可用 CPU 核数（解释并发能力）。");
        p.family("agt_java_free_memory_bytes", "gauge",
                "JVM 可用内存字节数（Runtime.freeMemory，堆视角的粗略值）。");

        MemoryUsage heap = mem.getHeapMemoryUsage();
        MemoryUsage nonHeap = mem.getNonHeapMemoryUsage();

        p.sample("agt_java_up", 1);
        p.sample("agt_java_info", 1,
                "service", "agt_backend",
                "version", version(),
                "java", System.getProperty("java.version", "unknown"));
        p.sample("agt_java_heap_used_bytes", heap.getUsed());
        p.sample("agt_java_heap_max_bytes", heap.getMax());
        p.sample("agt_java_nonheap_used_bytes", nonHeap.getUsed());
        p.sample("agt_java_threads", threads.getThreadCount());
        p.sample("agt_java_uptime_seconds", rt.getUptime() / 1000.0);
        p.sample("agt_java_processors", r.availableProcessors());
        p.sample("agt_java_free_memory_bytes", r.freeMemory());
        return p.render();
    }

    /**
     * 版本号取 E-1 的打戳产物（{@code build-info.properties}），
     * 拿不到就写 {@code unknown}——指标里的版本号<b>必须来自同一个来源</b>，
     * 否则"指标说 1.0.0、接口说 1.0.1"是比没有版本号更坏的状况。
     */
    private static String version() {
        try (java.io.InputStream in = JavaMetrics.class
                .getResourceAsStream("/build-info.properties")) {
            if (in == null) {
                return "unknown";
            }
            java.util.Properties props = new java.util.Properties();
            props.load(new java.io.InputStreamReader(in, java.nio.charset.StandardCharsets.UTF_8));
            // 键名是 `build.version`（E-1 打戳产物；与 VersionController 同一来源、同一键名）
            String v = props.getProperty("build.version");
            return (v == null || v.isBlank()) ? "unknown" : v;
        } catch (Exception e) { // noqa: 指标不该因为读不到版本就整段失败
            return "unknown";
        }
    }
}

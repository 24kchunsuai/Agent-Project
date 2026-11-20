package cn.tedu.agt.ops.service.impl;

import cn.tedu.agt.agent.client.AgentClient;
import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.auth.LoginUser;
import cn.tedu.agt.ops.metrics.JavaMetrics;
import cn.tedu.agt.ops.metrics.PrometheusText;
import cn.tedu.agt.ops.service.OpsService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.lang.management.ManagementFactory;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 运维健康聚合实现（企业级落地规约 E-3，判据 C3）。
 *
 * <h3>状态口径与 Python 侧<b>逐字对齐</b></h3>
 * <pre>
 * 组件 status  = 组件自身的健康（Milvus 挂了就写 down）
 * 整体 status  = 按影响归并：
 *                ① 任一 required 组件 down          → down
 *                ② 所有数据依赖同时 down（服务无能力）→ down
 *                ③ 任一组件非 up                     → degraded
 *                ④ 否则                              → up
 * </pre>
 * "Python 不可达"在 8080 侧是一条 <b>required=down</b> 的组件（{@code python-service}），
 * 于是整体判 down——这是本项目里 {@code down} 最主要、也最真实的触发路径：
 * <b>8000 侧正常不会返回 down</b>（它能响应就说明进程活着，单点数据依赖失败只降级），
 * 真正的 down 由这一层给出语义。
 *
 * <h3>为什么所有异常都必须吞掉</h3>
 * "连不上 Python"正是本接口要报告的<b>头号情报</b>。若让它冒成 500，调用方只能看到
 * 一个无信息的错误页——那恰好违背了 E-3 存在的意义（5 分钟内指出哪一环坏了）。
 */
@Slf4j
@Service
public class OpsServiceImpl implements OpsService {

    private static final String UP = "up";
    private static final String DEGRADED = "degraded";
    private static final String DOWN = "down";

    /** detail 截断长度：防止异常链里的长路径把响应撑肿 */
    private static final int DETAIL_LIMIT = 200;

    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    /** Python 不可达时占位的组件名（与 8000 侧自身的 "python" 组件区分开） */
    private static final String COMPONENT_PYTHON_SERVICE = "python-service";

    @Autowired
    private AgentClient agentClient;

    @Override
    public Map<String, Object> health(boolean fresh) {
        long t0 = System.currentTimeMillis();

        List<Map<String, Object>> components = new ArrayList<>();
        components.add(javaComponent());

        Map<String, Object> upstream = null;
        String pythonError = null;
        try {
            AgentApiResponse<Map<String, Object>> resp = agentClient.opsHealth(fresh);
            if (resp == null) {
                pythonError = "空响应";
            } else if (!resp.isSuccess()) {
                pythonError = "code=" + resp.getCode() + " " + resp.getMessage();
            } else {
                upstream = resp.getData();
            }
        } catch (Exception e) {
            // 连接被拒 / 读超时 / 响应无法解析——对运维而言都是"Python 这一环不通"
            pythonError = describe(e);
            log.warn("运维聚合：Python 侧不可达 | {}", pythonError);
        }

        Object checkedAt = LocalDateTime.now().format(TS);
        Object scheduler = List.of();
        Object counters = Map.of();

        if (pythonError != null) {
            components.add(component(
                    COMPONENT_PYTHON_SERVICE, DOWN,
                    System.currentTimeMillis() - t0,
                    "智能体服务不可达：" + pythonError, true));
        } else {
            components.addAll(extractComponents(upstream));
            checkedAt = upstream.getOrDefault("checkedAt", checkedAt);
            scheduler = upstream.getOrDefault("scheduler", scheduler);
            counters = upstream.getOrDefault("degradeCounters", counters);
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", mergeStatus(components));
        out.put("checkedAt", checkedAt);
        out.put("durationMs", System.currentTimeMillis() - t0);
        out.put("summary", summarize(components));
        out.put("components", components);
        // pythonReachable 单列一个布尔：状态灯/告警规则用它比翻 components 便宜
        out.put("pythonReachable", pythonError == null);
        if (pythonError != null) {
            out.put("pythonError", pythonError);
        }
        out.put("scheduler", scheduler);
        out.put("degradeCounters", counters);
        return out;
    }

    // ------------------------------------------------------------------ E-9 指标

    @Override
    public String metrics(int windowHours, boolean fresh) {
        PrometheusText p = new PrometheusText();

        // 这个族只在"合并"这一层存在，因此不放进 JavaMetrics（放进去会与 Java 段重复声明，
        // 重复的 HELP/TYPE 与重复序列都会让 **整次抓取失败** —— 拼接文本时必须逐族自查）。
        p.family("agt_java_python_section_ok", "gauge",
                "本次抓取是否成功合并了 Python 侧指标段（1/0）。为 0 时其余 agt_* 全部缺失。");

        String pythonText = null;
        String failure = null;
        try {
            pythonText = agentClient.opsMetricsRaw(windowHours, fresh);
            if (pythonText == null || pythonText.isBlank()) {
                failure = "空响应";
            }
        } catch (Exception e) {
            failure = describe(e);
            log.warn("指标聚合：Python 侧不可达 | {}", failure);
        }

        p.sample("agt_java_python_section_ok", failure == null ? 1 : 0);
        if (failure != null) {
            // 注释行 Prometheus 会忽略，但 `curl` 的人一眼能看到"为什么少了半份指标"
            p.comment("Python 段缺失：" + failure);
            p.comment("Java 段仍完整；请先按 docs/部署手册.md §7 排查 8000");
            return JavaMetrics.render() + p.render();
        }

        // 两段拼接：Java 段在前（本次抓取的"本机视角"），Python 段**原样**在后。
        // 绝不对 Python 段做任何加工——加工会让"两个端点看到的不一样"，
        // 排查时最怕这个（与 E-3 透传组件明细同一条理由）。
        return JavaMetrics.render() + p.render() + pythonText;
    }

    // ------------------------------------------------------------------ E-12 前端报错

    @Override
    public Map<String, Object> clientError(DroneSession session, Map<String, Object> body) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("accepted", false);
        out.put("stored", false);
        out.put("reason", "");

        try {
            AgentApiResponse<Map<String, Object>> resp =
                    agentClient.opsClientError(resolveUserId(session), body == null ? Map.of() : body);
            if (resp == null) {
                out.put("reason", "空响应");
            } else if (!resp.isSuccess()) {
                out.put("reason", "code=" + resp.getCode() + " " + resp.getMessage());
            } else if (resp.getData() != null) {
                // Python 的 accepted / stored / reason **原样透传**，不做二次加工：
                // 加工会让"前端收到的说法"与"后端实际做的"不一致（E-3 同一条理由）。
                // 注意 userId 是 Java 注入的，不会出现在 Python 的返回值里，故无需担心覆盖。
                out.putAll(resp.getData());
            } else {
                out.put("accepted", true);
            }
        } catch (Exception e) {
            String why = describe(e);
            out.put("reason", "智能体服务不可达：" + why);
            log.warn("前端报错上报转发失败 | {}", why);
        }
        return out;
    }

    /**
     * 身份注入：登录态优先；**匿名记 0 = 未知**。
     *
     * <p>为什么允许匿名：登录页自身报错、或令牌刚过期时用户点了一下按钮——这两类
     * 恰恰是最该被记下来的错误，却都拿不到身份。为了拿到 userId 而拒绝接收，
     * 等于把最需要观测的场景排除在外。</p>
     *
     * <p>为什么不能用前端自报的 userId：与 S-1 同一条铁律——身份只由 Java 注入。
     * 结构性保证见 {@code AgentClient.opsClientError}（userId 走查询参数，
     * 上报体里没有这个字段可传）。</p>
     */
    private long resolveUserId(DroneSession session) {
        LoginUser user = session == null ? null : session.getUser();
        return (user == null || user.getUserId() == null) ? 0L : user.getUserId();
    }

    // ------------------------------------------------------------------ 组件

    private Map<String, Object> javaComponent() {
        Runtime rt = Runtime.getRuntime();
        long usedMb = (rt.totalMemory() - rt.freeMemory()) / (1024 * 1024);
        long maxMb = rt.maxMemory() / (1024 * 1024);
        String detail = String.format("Java %s · 已运行 %s · 堆 %d/%d MB",
                System.getProperty("java.version"),
                humanUptime(ManagementFactory.getRuntimeMXBean().getUptime()),
                usedMb, maxMb);
        return component("java", UP, 0, detail, true);
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> extractComponents(Map<String, Object> upstream) {
        if (upstream == null) {
            return new ArrayList<>();
        }
        Object raw = upstream.get("components");
        if (!(raw instanceof List<?> list)) {
            return new ArrayList<>();
        }
        List<Map<String, Object>> out = new ArrayList<>(list.size());
        for (Object item : list) {
            if (item instanceof Map<?, ?> m) {
                // 原样透传（Python 侧已保证字段齐全），不做二次加工——
                // 加工会让"两个端点看到的不一样"，排查时最怕这个
                out.add((Map<String, Object>) m);
            }
        }
        return out;
    }

    private Map<String, Object> component(String name, String status, long latencyMs,
                                          String detail, boolean required) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("name", name);
        c.put("status", status);
        c.put("latencyMs", latencyMs);
        c.put("detail", detail.length() > DETAIL_LIMIT ? detail.substring(0, DETAIL_LIMIT) + "…" : detail);
        c.put("required", required);
        return c;
    }

    // ------------------------------------------------------------------ 归并

    private String mergeStatus(List<Map<String, Object>> components) {
        boolean requiredDown = components.stream()
                .anyMatch(c -> DOWN.equals(c.get("status")) && Boolean.TRUE.equals(c.get("required")));
        if (requiredDown) {
            return DOWN;
        }
        // 数据依赖 = 非必需、且不是纯观测的 degrade 组件
        List<Map<String, Object>> dataDeps = components.stream()
                .filter(c -> !Boolean.TRUE.equals(c.get("required")))
                .filter(c -> !"degrade".equals(c.get("name")))
                .toList();
        if (!dataDeps.isEmpty()
                && dataDeps.stream().allMatch(c -> DOWN.equals(c.get("status")))) {
            return DOWN;
        }
        boolean anyNotUp = components.stream().anyMatch(c -> !UP.equals(c.get("status")));
        return anyNotUp ? DEGRADED : UP;
    }

    private Map<String, Object> summarize(List<Map<String, Object>> components) {
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put(UP, count(components, UP));
        summary.put(DEGRADED, count(components, DEGRADED));
        summary.put(DOWN, count(components, DOWN));
        return summary;
    }

    private long count(List<Map<String, Object>> components, String status) {
        return components.stream().filter(c -> status.equals(c.get("status"))).count();
    }

    // ------------------------------------------------------------------ 工具

    /** 取异常链最根部的一句（RestClient 的外层 message 常只写 "I/O error..."）。 */
    private String describe(Throwable e) {
        Throwable root = e;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        String msg = root.getMessage();
        return root.getClass().getSimpleName() + (msg == null ? "" : ": " + msg);
    }

    private String humanUptime(long ms) {
        long sec = ms / 1000;
        if (sec < 60) {
            return sec + "s";
        }
        if (sec < 3600) {
            return (sec / 60) + "m" + (sec % 60) + "s";
        }
        return (sec / 3600) + "h" + ((sec % 3600) / 60) + "m";
    }
}

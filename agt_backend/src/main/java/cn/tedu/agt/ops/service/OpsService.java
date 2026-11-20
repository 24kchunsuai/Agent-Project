package cn.tedu.agt.ops.service;

import cn.tedu.agt.base.auth.DroneSession;

import java.util.Map;

/**
 * 运维健康聚合（企业级落地规约 E-3）+ 指标端点（E-9）+ 前端报错上报（E-12）。
 */
public interface OpsService {

    /**
     * 聚合系统健康状态。
     *
     * @param fresh true = 跳过 Python 侧 3s 缓存强制重新采集（排障用）
     * @return 固定结构：{@code status / checkedAt / durationMs / summary / components
     *         / pythonReachable / scheduler / degradeCounters}
     */
    Map<String, Object> health(boolean fresh);

    /**
     * 渲染 Prometheus 文本指标（E-9）：本侧 {@code agt_java_*} + 透传 Python 侧 {@code agt_*}。
     *
     * <p><b>本方法不会抛异常</b>：Python 不可达时退化为"只有 Java 段 + 注释说明原因"，
     * 并把 {@code agt_java_python_section_ok} 置 0。理由与 {@link #health} 一致——
     * 指标端点自己成为故障点是最坏的设计：出问题时恰好没有指标可看。
     *
     * @param windowHours 分位类指标的统计窗口（小时），透传给 Python
     * @param fresh       true = 跳过 Python 侧缓存（验收用）
     */
    String metrics(int windowHours, boolean fresh);

    /**
     * 转发前端运行时报错上报（E-12）。userId 由登录态注入，**不取前端自报值**。
     *
     * <p><b>本方法不会抛异常</b>：调用方是<b>一个已经出错的页面</b>，它只会
     * fire-and-forget。给失败返回值没有任何消费者能据此行动（前端不重试、也不展示），
     * 而"Python 不可达"本身已有更好的观测出口（E-3 状态灯）。
     * 与 E-8「审计失败永不抛出」同一条理由：事后能力不能反过来变成新的故障源。</p>
     *
     * @param session 当前登录态（可为 null —— 登录页就报错是常态）
     * @param body    前端上报体（不含 userId）
     * @return {@code {accepted, stored, reason, ...}}；失败也在返回值里如实说明
     */
    Map<String, Object> clientError(DroneSession session, Map<String, Object> body);
}

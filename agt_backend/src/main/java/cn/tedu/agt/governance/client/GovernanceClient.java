package cn.tedu.agt.governance.client;

import cn.tedu.agt.agent.config.AgentAuth;
import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.agent.pojo.AgentApiResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.client.RestClient;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 访问 Python 治理接口的出口（{@code /v1/kb/governance/*}）。
 *
 * <p>与 {@code KbClient} / {@code AgentClient} 并列，按业务域拆分。本层<b>只做透传</b>：
 * 不拼 SQL、不读 SQLite（规约 §13.4 明确边界：SQLite 是 Python 私有账本，
 * Java 跨进程读 Windows 文件锁问题多，一律经 Python API）。</p>
 *
 * <p>为什么对账要单独一个长超时客户端：对账要一次 query 全量向量库取 doc_id 集合，
 * 语料上万切片时可能跑几十秒；用对话的 60s 读超时会把正常的对账误判成失败，
 * 而"对账看起来失败"比"对账慢"危险得多（人会以为没残留）。</p>
 */
@Slf4j
@Component
public class GovernanceClient {

    private static final String BASE = "/v1/kb/governance";

    /** 重操作读超时：全库对账 / 版本回灌可能要跑几十秒，单独给足 */
    private static final int SLOW_READ_TIMEOUT_MS = 180_000;

    private final RestClient restClient;
    /** 重操作用的长超时客户端（对账全库扫描、回滚回灌）。 */
    private final RestClient slowClient;

    public GovernanceClient(AgentProperties properties) {
        this.restClient = buildClient(properties, properties.getReadTimeout());
        this.slowClient = buildClient(properties,
                Math.max(properties.getReadTimeout(), SLOW_READ_TIMEOUT_MS));
        log.info("治理客户端已初始化：baseUrl={} slowTimeout={}ms",
                properties.getBaseUrl(), SLOW_READ_TIMEOUT_MS);
    }

    private RestClient buildClient(AgentProperties properties, int readTimeout) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.getConnectTimeout());
        factory.setReadTimeout(readTimeout);
        // 服务间鉴权（S-1）：所有直连 Python 的出口都要带 X-Agent-Key
        return AgentAuth
                .withKey(RestClient.builder().baseUrl(properties.getBaseUrl()), properties)
                .requestFactory(factory)
                .build();
    }

    // ------------------------------------------------------------ 只读视图

    /** 治理概览：状态分布、切片指纹冗余率、治理开关。 */
    public AgentApiResponse<Object> overview() {
        return restClient.get()
                .uri(BASE + "/overview")
                .retrieve()
                .body(objectType());
    }

    /** 审计流水（分页 + operator/action/docId/target/时间过滤）。 */
    public AgentApiResponse<Object> audit(String operator, String action, String docId,
                                          String target, String from, String to,
                                          int page, int pageSize) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(BASE + "/audit");
                    if (StringUtils.hasText(operator)) {
                        builder.queryParam("operator", operator);
                    }
                    if (StringUtils.hasText(action)) {
                        builder.queryParam("action", action);
                    }
                    if (StringUtils.hasText(docId)) {
                        builder.queryParam("docId", docId);
                    }
                    if (StringUtils.hasText(target)) {
                        builder.queryParam("target", target);
                    }
                    if (StringUtils.hasText(from)) {
                        builder.queryParam("from", from);
                    }
                    if (StringUtils.hasText(to)) {
                        builder.queryParam("to", to);
                    }
                    builder.queryParam("page", page);
                    builder.queryParam("pageSize", pageSize);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /**
     * 审计流水导出 CSV（E-8）。走长超时：导出是全量扫描（上限 2 万行），
     * 用对话的 60s 超时可能把一次正常导出误判成失败。
     */
    public AgentApiResponse<Object> auditExport(String operator, String action, String docId,
                                                String target, String from, String to) {
        return slowClient.get()
                .uri(builder -> {
                    builder.path(BASE + "/audit/export");
                    if (StringUtils.hasText(operator)) {
                        builder.queryParam("operator", operator);
                    }
                    if (StringUtils.hasText(action)) {
                        builder.queryParam("action", action);
                    }
                    if (StringUtils.hasText(docId)) {
                        builder.queryParam("docId", docId);
                    }
                    if (StringUtils.hasText(target)) {
                        builder.queryParam("target", target);
                    }
                    if (StringUtils.hasText(from)) {
                        builder.queryParam("from", from);
                    }
                    if (StringUtils.hasText(to)) {
                        builder.queryParam("to", to);
                    }
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /**
     * 补录一条外部事件审计（E-8）：登录成功/失败、登出这类事件发生在 Java 侧，
     * 但审计表只有一张（治理域的那张）。
     *
     * <p>用短超时的 {@code restClient} 而不是 {@code slowClient}：补录是"顺手记一笔"，
     * 阻塞登录响应去等 180s 是不可接受的——宁可这条审计丢了，也不能让用户登不进去。
     * 调用方 {@code AuditReporter} 还会再吞一层异常。</p>
     *
     * <p>traceId 由 {@code TracePropagationInterceptor} 自动带上，无需显式传参。</p>
     */
    public AgentApiResponse<Object> auditEvent(String actor, String action, String target,
                                               String result, String detail, String ip) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("actor", actor);
        body.put("action", action);
        body.put("target", target);
        body.put("result", result);
        body.put("detail", detail);
        body.put("ip", ip);
        return restClient.post()
                .uri(BASE + "/audit")
                .contentType(MediaType.APPLICATION_JSON)
                .body(body)
                .retrieve()
                .body(objectType());
    }

    /** 文档治理登记列表（分页，state 空=全部）。 */
    public AgentApiResponse<Object> docs(String state, int page, int pageSize) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(BASE + "/docs");
                    if (StringUtils.hasText(state)) {
                        builder.queryParam("state", state);
                    }
                    builder.queryParam("page", page);
                    builder.queryParam("pageSize", pageSize);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 停用区列表（分页）。停用不是删除，与回收站是两个视图。 */
    public AgentApiResponse<Object> disabled(int page, int pageSize) {
        return restClient.get()
                .uri(builder -> builder.path(BASE + "/disabled")
                        .queryParam("page", page)
                        .queryParam("pageSize", pageSize)
                        .build())
                .retrieve()
                .body(objectType());
    }

    /** 治理配置（只读）：去重门开关、阈值、G2 各开关。 */
    public AgentApiResponse<Object> config() {
        return restClient.get()
                .uri(BASE + "/config")
                .retrieve()
                .body(objectType());
    }

    /** 常驻任务状态：对账跑了几轮、上轮耗时与错误。 */
    public AgentApiResponse<Object> tasks() {
        return restClient.get()
                .uri(BASE + "/tasks")
                .retrieve()
                .body(objectType());
    }

    /** 版本链：当前版本 + 历史快照（含 isCurrent / live / hasVectors 标注）。 */
    public AgentApiResponse<Object> versions(String docId) {
        return restClient.get()
                .uri(builder -> builder.path(BASE + "/versions").pathSegment(docId).build())
                .retrieve()
                .body(objectType());
    }

    /** 巡检工单列表（分页；OPEN 排最前，返回 openCount 供前端角标）。 */
    public AgentApiResponse<Object> tickets(String status, String type, int page, int pageSize) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(BASE + "/tickets");
                    if (StringUtils.hasText(status)) {
                        builder.queryParam("status", status);
                    }
                    if (StringUtils.hasText(type)) {
                        builder.queryParam("type", type);
                    }
                    builder.queryParam("page", page);
                    builder.queryParam("pageSize", pageSize);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    // ------------------------------------------------------------ 生命周期操作

    /** 停用文档（向量物理移除 + 备份落停用区）。 */
    public AgentApiResponse<Object> disable(String docId, String owner, boolean isAdmin) {
        return docAction(docId, "disable", owner, isAdmin);
    }

    /** 启用文档（从停用备份回灌向量，零 embedding）。 */
    public AgentApiResponse<Object> enable(String docId, String owner, boolean isAdmin) {
        return docAction(docId, "enable", owner, isAdmin);
    }

    /** 手动跑一轮向量对账（与定时任务同一条代码路径）。 */
    public AgentApiResponse<Object> reconcileRun(boolean dryRun, String operator) {
        return slowClient.post()
                .uri(builder -> builder.path(BASE + "/reconcile/run")
                        .queryParam("dryRun", dryRun)
                        .queryParam("operator", operator)
                        .build())
                .retrieve()
                .body(objectType());
    }

    /**
     * 回滚到历史版本。
     *
     * <p>回灌是"按原切片写回"（备份带 dense 时零 embedding），但仍走长超时：
     * 万一目标快照是无向量的旧版（G1 遗留），会回落成重新切片 + embedding，
     * 耗时与入库同级。把它当普通读请求发，正常的慢回滚会被误判成失败。</p>
     */
    public AgentApiResponse<Object> rollback(String docId, Integer versionNo, boolean confirm,
                                             String confirmDocName, String owner, boolean isAdmin) {
        return slowClient.post()
                .uri(builder -> {
                    builder.path(BASE + "/doc").pathSegment(docId, "rollback");
                    if (versionNo != null) {
                        builder.queryParam("versionNo", versionNo);
                    }
                    builder.queryParam("confirm", confirm);
                    if (StringUtils.hasText(confirmDocName)) {
                        builder.queryParam("confirmDocName", confirmDocName);
                    }
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("is_admin", isAdmin);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /**
     * 确认工单并执行（G4）：DUPLICATE→软删、STALE / LOW_QUALITY→停用。
     *
     * <p>走长超时：执行化要真的删向量并 flush，与"停用"同级的重操作。
     * 用对话的 60s 超时会把一次正常执行误判成失败——而前端一旦收到"失败"，
     * 用户很可能会再点一次，那时第二次会因为"工单已 DONE"被拒，体验变成"点了没反应"。</p>
     */
    public AgentApiResponse<Object> ticketConfirm(int ticketId, String operator) {
        return slowClient.post()
                .uri(builder -> builder.path(BASE + "/tickets")
                        .pathSegment(String.valueOf(ticketId), "confirm")
                        .queryParam("operator", operator)
                        .build())
                .retrieve()
                .body(objectType());
    }

    /** 忽略工单（不执行动作；用户忽略且文档未变化时，下轮巡检不再开单）。 */
    public AgentApiResponse<Object> ticketDismiss(int ticketId, String note, String operator) {
        return restClient.post()
                .uri(builder -> {
                    builder.path(BASE + "/tickets")
                            .pathSegment(String.valueOf(ticketId), "dismiss")
                            .queryParam("operator", operator);
                    if (StringUtils.hasText(note)) {
                        builder.queryParam("note", note);
                    }
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 手动触发一轮巡检（与定时任务同一路径）。走长超时：L3 相似检索逐篇取样，可能几十秒。 */
    public AgentApiResponse<Object> inspectionScan(boolean dryRun, String operator) {
        return slowClient.post()
                .uri(builder -> builder.path(BASE + "/inspection/scan")
                        .queryParam("dryRun", dryRun)
                        .queryParam("operator", operator)
                        .build())
                .retrieve()
                .body(objectType());
    }

    /** TTL 到期彻底删除（与定时任务同一路径）。走长超时：单轮可能清理多篇。 */
    public AgentApiResponse<Object> purgeExpired(boolean dryRun, String operator) {
        return slowClient.post()
                .uri(builder -> builder.path(BASE + "/recycle-bin/purge-expired")
                        .queryParam("dryRun", dryRun)
                        .queryParam("operator", operator)
                        .build())
                .retrieve()
                .body(objectType());
    }

    /**
     * disable / enable 共用：静态路径走 {@code path()}，动态 docId 走 {@code pathSegment()}。
     *
     * <p>这个分工不能反：docId 是中文文档名（可能含空格），交给 {@code path()} 会被当成
     * URI 模板解析并二次编码。{@code pathSegment()} 只编码一次，与 {@code KbClient.delete} 同款，
     * 是项目中已验证中文安全的写法。</p>
     */
    private AgentApiResponse<Object> docAction(String docId, String action,
                                               String owner, boolean isAdmin) {
        return restClient.post()
                .uri(builder -> {
                    builder.path(BASE + "/doc").pathSegment(docId, action);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("is_admin", isAdmin);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    private ParameterizedTypeReference<AgentApiResponse<Object>> objectType() {
        return new ParameterizedTypeReference<>() {
        };
    }
}

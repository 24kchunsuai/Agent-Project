package cn.tedu.agt.kb.client;

import cn.tedu.agt.agent.config.AgentAuth;
import cn.tedu.agt.agent.config.AgentProperties;
import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.kb.pojo.dto.KbUploadParam;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.client.RestClient;

import java.util.HashMap;
import java.util.Map;

/**
 * 访问 Python 知识库接口的出口（与 {@code AgentClient} 并列，按业务域拆分）。
 *
 * <p>与对话链路的差异只有一处：<strong>读超时</strong>。入库要跑切片 + 向量化，
 * 一篇长文档可能耗时数十秒，用对话的 60s 会误判为超时失败，这里单独放大到 180s。</p>
 */
@Slf4j
@Component
public class KbClient {

    private static final String DOCUMENTS_PATH = "/v1/kb/documents";
    private static final String RECYCLE_PATH = "/v1/kb/recycle";
    private static final String STATS_PATH = "/v1/kb/stats";
    private static final String RAG_SEARCH_PATH = "/v1/rag/search";

    /** 入库读超时：切片 + embedding 比对话慢得多，单独给足 */
    private static final int INGEST_READ_TIMEOUT_MS = 180_000;

    private final RestClient restClient;
    private final RestClient ingestClient;

    public KbClient(AgentProperties properties) {
        this.restClient = buildClient(properties, properties.getReadTimeout());
        // 入库单独一个实例：同一 RestClient 无法为不同接口设置不同读超时
        this.ingestClient = buildClient(properties, Math.max(properties.getReadTimeout(), INGEST_READ_TIMEOUT_MS));

        log.info("知识库客户端已初始化：baseUrl={} ingestTimeout={}ms",
                properties.getBaseUrl(), INGEST_READ_TIMEOUT_MS);
    }

    private RestClient buildClient(AgentProperties properties, int readTimeout) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.getConnectTimeout());
        factory.setReadTimeout(readTimeout);
        // 服务间鉴权（S-1）：所有直连 Python 的出口都要带 X-Agent-Key，
        // 密钥与空值拒绝统一走 AgentAuth（漏一处就是该业务域整体 4010）
        return AgentAuth
                .withKey(RestClient.builder().baseUrl(properties.getBaseUrl()), properties)
                .requestFactory(factory)
                .build();
    }

    /** 上传并入库。 */
    public AgentApiResponse<Object> upload(KbUploadParam param) {
        log.debug("调用知识库入库接口:fileName={} category={} visibility={} chars={}",
                param.getFileName(), param.getCategory(), param.getVisibility(),
                param.getContent() == null ? 0 : param.getContent().length());
        return ingestClient.post()
                .uri(DOCUMENTS_PATH)
                .body(param)
                .retrieve()
                .body(objectType());
    }

    /** 文档列表（分页）。 */
    public AgentApiResponse<Object> documents(String owner, String scope, int page, int size) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(DOCUMENTS_PATH);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("scope", scope);
                    builder.queryParam("page", page);
                    builder.queryParam("pageSize", size);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 删除文档（移入回收站）。docId 由 RestClient 自动做 URL 编码（中文文档名安全）。 */
    public AgentApiResponse<Object> delete(String docId, String owner, boolean isAdmin) {
        return restClient.delete()
                .uri(builder -> {
                    builder.path(DOCUMENTS_PATH).pathSegment(docId);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("is_admin", isAdmin);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 回收站列表（分页）。 */
    public AgentApiResponse<Object> recycle(String owner, String scope, int page, int size) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(RECYCLE_PATH);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("scope", scope);
                    builder.queryParam("page", page);
                    builder.queryParam("pageSize", size);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /**
     * 从回收站恢复。
     *
     * <p>恢复要重新切片 + 向量化，耗时与入库同级，因此复用 {@code ingestClient}
     * 的长超时——用普通读超时会把正常的恢复误判成失败。</p>
     *
     * <p>G2 起恢复先过 L3 语义查重确认门（铁律 3）：未带 confirm 时若命中相似语料，
     * 下游返回 {@code 3001 + data.requireConfirm}（<b>此时并未恢复</b>），
     * 用户确认后由前端带 {@code confirm=true} 重发。</p>
     */
    public AgentApiResponse<Object> restore(String docId, String owner, boolean isAdmin, boolean confirm) {
        return ingestClient.post()
                .uri(builder -> {
                    builder.path(RECYCLE_PATH).pathSegment(docId).pathSegment("restore");
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("is_admin", isAdmin);
                    builder.queryParam("confirm", confirm);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /**
     * 彻底删除（三清：向量 + 备份 + 源文件，不可恢复）。
     *
     * <p>G2 起是<b>双确认</b>操作（铁律 2）：只允许管理员，且必须 {@code confirm=true} +
     * {@code confirmDocName} 与文档名严格一致；未满足时下游返回
     * {@code 3001 + data.requireConfirm}（含 {@code expectedDocName}），不执行任何删除。</p>
     */
    public AgentApiResponse<Object> purge(String docId, String owner, boolean isAdmin,
                                          boolean confirm, String confirmDocName) {
        return restClient.delete()
                .uri(builder -> {
                    builder.path(RECYCLE_PATH).pathSegment(docId);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("is_admin", isAdmin);
                    builder.queryParam("confirm", confirm);
                    if (StringUtils.hasText(confirmDocName)) {
                        builder.queryParam("confirmDocName", confirmDocName);
                    }
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 统计。 */
    public AgentApiResponse<Object> stats(String owner, String scope) {
        return restClient.get()
                .uri(builder -> {
                    builder.path(STATS_PATH);
                    if (StringUtils.hasText(owner)) {
                        builder.queryParam("owner", owner);
                    }
                    builder.queryParam("scope", scope);
                    return builder.build();
                })
                .retrieve()
                .body(objectType());
    }

    /** 检索预览：透传 RAG 检索，供管理界面自测"刚上传的内容能不能被搜到"。 */
    public AgentApiResponse<Object> search(String query, String user) {
        Map<String, Object> body = new HashMap<>();
        body.put("query", query);
        if (StringUtils.hasText(user)) {
            body.put("user", user);
        }
        return restClient.post()
                .uri(RAG_SEARCH_PATH)
                .body(body)
                .retrieve()
                .body(objectType());
    }

    private ParameterizedTypeReference<AgentApiResponse<Object>> objectType() {
        return new ParameterizedTypeReference<>() {
        };
    }
}

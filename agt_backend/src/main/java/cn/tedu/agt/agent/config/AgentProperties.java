package cn.tedu.agt.agent.config;

import lombok.Getter;
import lombok.Setter;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * Python 智能体服务的连接参数，全部从 application.properties 读取。
 */
@Setter
@Getter
@Component
@ConfigurationProperties(prefix = "agent")
public class AgentProperties {
    /** Python 服务基础地址 */
    private String baseUrl = "http://localhost:8000";
    /** 建立连接超时（毫秒） */
    private int connectTimeout = 3000;
    /**
     * 读取超时（毫秒）。
     * 容错规约 §7 超时分层：整个非流式请求要覆盖 LLM 重试序列（约 190s）+
     * 工具序列，2026-09-18 由 60s 核定为 240s——此前长 PLAN 链路会被 60s 误杀。
     */
    private int readTimeout = 240_000;
    /**
     * 健康聚合专用读超时（毫秒，E-3）。
     *
     * <p>刻意<b>与业务读超时分离</b>：业务要 240s 是因为要覆盖 LLM 重试序列，
     * 但健康探测恰恰相反——它自己就是"服务还活着吗"的判据。
     * 用 240s 的话，Python 进程僵死（端口在监听但不响应）时，前端顶栏的状态灯
     * 要等 4 分钟才变红，等于没有告警。
     */
    private int opsReadTimeout = 5_000;
    /**
     * 服务间共享密钥（企业级落地规约 S-1）：Python 侧要求全部请求带 X-Agent-Key。
     *
     * <p>application.properties 里只写占位符 {@code ${AGENT_SHARED_KEY:}}，
     * 真实值由**环境变量**注入，不进代码库。必须与 agt_python/.env 的
     * AGENT_SHARED_KEY 完全一致。
     *
     * <p>为空则 AgentClient 构造时拒绝启动（fail-closed）——若允许空值启动，
     * 表现会是"前端每个操作都弹登录已过期"，排查成本远高于启动即失败。
     */
    private String sharedKey = "";
}

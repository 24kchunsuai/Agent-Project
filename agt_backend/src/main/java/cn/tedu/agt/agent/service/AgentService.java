package cn.tedu.agt.agent.service;

import cn.tedu.agt.agent.pojo.dto.AgentChatParam;
import cn.tedu.agt.agent.pojo.vo.AgentChatVO;
import cn.tedu.agt.agent.pojo.vo.AgentHealthVO;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.Map;

public interface AgentService {

    /**
     * 处理一轮对话：补全会话信息并转发给 Python 智能体。
     */
    AgentChatVO chat(AgentChatParam param);

    /**
     * 流式对话：返回 SseEmitter，把 Python 的增量事件透传给前端。
     */
    SseEmitter chatStream(AgentChatParam param);

    /**
     * 探测 Python 智能体服务状态。
     */
    AgentHealthVO health();

    /**
     * 数据中台可观测总览（Python 侧降级计数 + 近 24h 四表聚合），原样透传。
     */
    Map<String, Object> metricsOverview();

    /**
     * 数据中台查询类接口通用透传（数据中台开发规约 §6）。
     * pathWithQuery：Python 侧 /v1/agent/metrics/ 前缀的完整路径（可带查询串）。
     */
    Map<String, Object> metricsGet(String pathWithQuery);

    /**
     * 带 jsdstu 身份的透传重载（P2 告警处置联动）：bearerToken 为看板访问者自己的 token。
     */
    Map<String, Object> metricsGet(String pathWithQuery, String bearerToken);

    /**
     * 数据中台写类接口透传（P2 feedback 提交）。
     */
    Map<String, Object> metricsPost(String path, Map<String, Object> body);

    /**
     * 用户画像查看（M-5）：透传 Python GET /v1/profile/{userId}。
     */
    Object profileGet(long userId);

    /**
     * 用户画像删除（M-5 知情权）：key 为空清空全部，否则删单条。
     */
    Object profileDelete(long userId, String key);
}

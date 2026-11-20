package cn.tedu.agt.agent.service.impl;

import cn.tedu.agt.agent.client.AgentClient;
import cn.tedu.agt.agent.pojo.AgentApiResponse;
import cn.tedu.agt.agent.pojo.dto.AgentChatParam;
import cn.tedu.agt.agent.pojo.vo.AgentChatVO;
import cn.tedu.agt.agent.pojo.vo.AgentHealthVO;
import cn.tedu.agt.agent.service.AgentService;
import cn.tedu.agt.base.exception.ServiceException;
import cn.tedu.agt.base.log.MdcScope;
import cn.tedu.agt.base.log.StructuredLog;
import cn.tedu.agt.base.response.StatusCode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@Slf4j
@Service
public class AgentServiceImpl implements AgentService {

    /** 流式透传专用线程池：SSE 读流是阻塞操作，不能占用 Tomcat 请求线程。 */
    private static final ExecutorService STREAM_POOL = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "agent-stream-" + r.hashCode());
        t.setDaemon(true);
        return t;
    });

    /**
     * 单次流式对话的最长持续时间。
     * 容错规约 §7：外层 > 内层——LLM 重试序列最长约 190s + 工具序列，
     * 240s 为核定值（2026-09-18 由 120s 上调，此前长 PLAN 链路会被误杀）。
     */
    private static final long STREAM_TIMEOUT_MS = 240_000L;

    @Autowired
    private AgentClient agentClient;

    @Autowired
    private ObjectMapper objectMapper;

    @Override
    public AgentChatVO chat(AgentChatParam param) {
        prepare(param);
        AgentApiResponse<AgentChatVO> response = agentClient.chat(param);
        return unwrap(response, "对话");
    }

    @Override
    public SseEmitter chatStream(AgentChatParam param) {
        prepare(param);
        SseEmitter emitter = new SseEmitter(STREAM_TIMEOUT_MS);

        // E-6：MDC 是 ThreadLocal，不会自动跟到线程池。不包这一层，症状就是
        // "整条链路只有流式那一段日志没有 traceId"（详见 MdcScope 的类注释）。
        STREAM_POOL.execute(MdcScope.wrap(() -> {
            long startedAt = System.nanoTime();
            try {
                agentClient.chatStream(param, payload -> {
                    try {
                        emitter.send(SseEmitter.event().data(payload));
                    } catch (IOException e) {
                        // 前端断开等情况：中断读流
                        throw new UncheckedIOException(e);
                    }
                });
                emitter.complete();
                StructuredLog.info(log, "chat.stream", elapsedMs(startedAt),
                        "流式对话完成:sessionId={}", param.getSessionId());
            } catch (UncheckedIOException e) {
                StructuredLog.warn(log, "chat.stream", elapsedMs(startedAt),
                        "前端断开流式连接:sessionId={}", param.getSessionId());
                emitter.completeWithError(e.getCause());
            } catch (ServiceException e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.getStatusCode().getMessage();
                StructuredLog.error(log, "chat.stream", elapsedMs(startedAt), null,
                        "流式对话业务失败:sessionId={} code={}", param.getSessionId(),
                        e.getStatusCode().getCode());
                sendErrorEvent(emitter, e.getStatusCode().getCode(), msg);
            } catch (Exception e) {
                StructuredLog.error(log, "chat.stream", elapsedMs(startedAt), e,
                        "流式对话异常:sessionId={}", param.getSessionId());
                sendErrorEvent(emitter, StatusCode.AGENT_CALL_ERROR.getCode(), "智能体调用失败，请稍后重试");
            }
        }));
        return emitter;
    }

    private static long elapsedMs(long startedAtNanos) {
        return (System.nanoTime() - startedAtNanos) / 1_000_000L;
    }

    /** 流式失败也要走 SSE 事件（HTTP 已 200），保持前端一套解析逻辑。 */
    private void sendErrorEvent(SseEmitter emitter, int code, String message) {
        try {
            String payload = objectMapper.writeValueAsString(
                    Map.of("type", "error", "code", code, "message", message));
            emitter.send(SseEmitter.event().data(payload));
            emitter.complete();
        } catch (Exception e) {
            emitter.completeWithError(e);
        }
    }

    private void prepare(AgentChatParam param) {
        // 会话 ID 由后端兜底生成，前端可以不传
        if (!StringUtils.hasText(param.getSessionId())) {
            param.setSessionId(UUID.randomUUID().toString().replace("-", ""));
        }
        if (param.getUserId() == null) {
            param.setUserId(0L);
        }
    }

    @Override
    public AgentHealthVO health() {
        AgentApiResponse<AgentHealthVO> response = agentClient.health();
        return unwrap(response, "健康探测");
    }

    @Override
    public Map<String, Object> metricsOverview() {
        AgentApiResponse<Map<String, Object>> response = agentClient.metricsOverview();
        return unwrap(response, "可观测总览");
    }

    @Override
    public Map<String, Object> metricsGet(String pathWithQuery) {
        AgentApiResponse<Map<String, Object>> response = agentClient.metricsGet(pathWithQuery);
        return unwrap(response, "可观测查询");
    }

    @Override
    public Map<String, Object> metricsGet(String pathWithQuery, String bearerToken) {
        AgentApiResponse<Map<String, Object>> response = agentClient.metricsGet(pathWithQuery, bearerToken);
        return unwrap(response, "可观测查询");
    }

    @Override
    public Map<String, Object> metricsPost(String path, Map<String, Object> body) {
        AgentApiResponse<Map<String, Object>> response = agentClient.metricsPost(path, body);
        return unwrap(response, "可观测反馈提交");
    }

    @Override
    public Object profileGet(long userId) {
        AgentApiResponse<Object> response = agentClient.profileGet(userId);
        return unwrap(response, "用户画像查看");
    }

    @Override
    public Object profileDelete(long userId, String key) {
        AgentApiResponse<Object> response = agentClient.profileDelete(userId, key);
        return unwrap(response, "用户画像删除");
    }

    /**
     * 统一拆包：Python 侧返回 null 或非 2000 都视为调用失败，
     * 避免把不可用的状态当成正常响应透传给前端。
     */
    private <T> T unwrap(AgentApiResponse<T> response, String action) {
        if (response == null) {
            log.error("智能体{}失败：响应体为空", action);
            throw new ServiceException(StatusCode.AGENT_UNAVAILABLE);
        }
        if (!response.isSuccess()) {
            log.error("智能体{}失败：code={} message={}", action, response.getCode(), response.getMessage());
            throw new ServiceException(StatusCode.AGENT_CALL_ERROR);
        }
        return response.getData();
    }
}

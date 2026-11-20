package cn.tedu.agt.agent.controller;

import cn.tedu.agt.agent.pojo.dto.AgentChatParam;
import cn.tedu.agt.agent.pojo.vo.AgentChatVO;
import cn.tedu.agt.agent.service.AgentService;
import cn.tedu.agt.base.auth.AuthService;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.base.response.StatusCode;
import com.github.xiaoymin.knife4j.annotations.ApiOperationSupport;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

@Tag(name = "智能体模块")
@Slf4j
@RestController
@RequestMapping("/v1/agent")
public class AgentController {

    /** 会话属性 key，与 AuthInterceptor 约定一致 */
    private static final String ATTR_SESSION = "agt.loginSession";

    @Autowired
    private AgentService agentService;

    @Autowired
    private AuthService authService;

    @Operation(summary = "智能体对话")
    @ApiOperationSupport(order = 10)
    @PostMapping("chat")
    public JsonResult chat(@Validated @RequestBody AgentChatParam param, HttpServletRequest request) {
        log.debug("智能体对话:param={}", param);
        injectIdentity(param, request);
        AgentChatVO vo = agentService.chat(param);
        return JsonResult.ok(vo);
    }

    @Operation(summary = "智能体对话（SSE 流式）", description = "事件协议：meta -> delta* -> done | error")
    @ApiOperationSupport(order = 15)
    @PostMapping("chat/stream")
    public org.springframework.web.servlet.mvc.method.annotation.SseEmitter chatStream(
            @Validated @RequestBody AgentChatParam param, HttpServletRequest request) {
        log.debug("智能体流式对话:param={}", param);
        injectIdentity(param, request);
        return agentService.chatStream(param);
    }

    @Operation(summary = "智能体服务健康探测")
    @ApiOperationSupport(order = 20)
    @GetMapping("health")
    public JsonResult health() {
        log.debug("智能体服务健康探测");
        return JsonResult.ok(agentService.health());
    }

    @Operation(summary = "数据中台可观测总览", description = "降级计数快照 + 近 24h 对话/工具/委派聚合")
    @ApiOperationSupport(order = 25)
    @GetMapping("metrics/overview")
    public JsonResult metricsOverview() {
        return JsonResult.ok(agentService.metricsOverview());
    }

    // ---- 用户画像（M-5）：只允许看/删自己的画像，userId 一律取登录会话 ----

    @Operation(summary = "查看我的画像", description = "画像条目列表（key/value/source/confidence/updatedTs）")
    @ApiOperationSupport(order = 24)
    @GetMapping("profile")
    public JsonResult profileGet(HttpServletRequest request) {
        Long userId = currentUserId(request);
        if (userId == null) {
            return new JsonResult(StatusCode.UNAUTHORIZED);
        }
        return JsonResult.ok(agentService.profileGet(userId));
    }

    @Operation(summary = "删除单条画像", description = "画像猜错时的纠错入口（M-5 知情权）")
    @ApiOperationSupport(order = 24)
    @DeleteMapping("profile/{key}")
    public JsonResult profileDeleteKey(@PathVariable String key, HttpServletRequest request) {
        Long userId = currentUserId(request);
        if (userId == null) {
            return new JsonResult(StatusCode.UNAUTHORIZED);
        }
        // key 白名单：只允许字母/数字/下划线，防路径拼接
        if (key == null || !key.matches("[A-Za-z0-9_]{1,64}")) {
            return new JsonResult(StatusCode.VALIDATE_ERROR);
        }
        return JsonResult.ok(agentService.profileDelete(userId, key));
    }

    @Operation(summary = "清空我的画像", description = "一键清空，模型立刻回到\"不认识你\"状态")
    @ApiOperationSupport(order = 24)
    @DeleteMapping("profile")
    public JsonResult profileDeleteAll(HttpServletRequest request) {
        Long userId = currentUserId(request);
        if (userId == null) {
            return new JsonResult(StatusCode.UNAUTHORIZED);
        }
        return JsonResult.ok(agentService.profileDelete(userId, null));
    }

    /** 从登录会话解析 userId；未登录返回 null（由调用方给 4010）。 */
    private Long currentUserId(HttpServletRequest request) {
        DroneSession session = (DroneSession) request.getAttribute(ATTR_SESSION);
        if (session == null) {
            return null;
        }
        Map<String, Object> identity = authService.resolveIdentity(session);
        if (identity == null || identity.get("userId") == null) {
            return null;
        }
        return ((Number) identity.get("userId")).longValue();
    }

    @Operation(summary = "数据中台会话明细列表", description = "看板会话视图数据源，支持 limit/hours/intent/sessionId 过滤（P0）")
    @ApiOperationSupport(order = 26)
    @GetMapping("metrics/chats")
    public JsonResult metricsChats(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("chats", params)));
    }

    @Operation(summary = "数据中台单次对话全轨迹", description = "chat+工具步骤+委派+故障时间线（trace 追溯，P0）")
    @ApiOperationSupport(order = 27)
    @GetMapping("metrics/traces/{traceId}")
    public JsonResult metricsTrace(@PathVariable String traceId) {
        // trace_id 格式白名单（十六进制），防路径拼接；非法不报错，返回 found:false
        // （不用 VALIDATE_ERROR 3001：该码在前端语义是"确认门"，见 request.js）
        if (traceId == null || !traceId.matches("[0-9a-fA-F]{4,64}")) {
            return JsonResult.ok(Map.of("found", false, "reason", "invalid traceId"));
        }
        return JsonResult.ok(agentService.metricsGet("/v1/agent/metrics/traces/" + traceId));
    }

    @Operation(summary = "数据中台按天序列", description = "metric/from/to/groupBy 白名单校验后读 DWS 预聚合（P1）")
    @ApiOperationSupport(order = 28)
    @GetMapping("metrics/timeseries")
    public JsonResult metricsTimeseries(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("timeseries", params)));
    }

    @Operation(summary = "数据中台工具 TopN", description = "calls/fail_rate/duration 排序（P1）")
    @ApiOperationSupport(order = 29)
    @GetMapping("metrics/tools/top")
    public JsonResult metricsToolsTop(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("tools/top", params)));
    }

    // ---- P2：feedback 反馈闭环 + 告警处置联动 + CSV 报表导出（数据中台规约 §6/§8）----

    @Operation(summary = "数据中台提交会话反馈", description = "二值 up/down，挂 trace；内容捕获走独立表+开关+TTL（P2）")
    @ApiOperationSupport(order = 30)
    @PostMapping("metrics/feedback")
    public JsonResult metricsFeedbackSubmit(@RequestBody Map<String, Object> body) {
        return JsonResult.ok(agentService.metricsPost("/v1/agent/metrics/feedback", body));
    }

    @Operation(summary = "数据中台反馈列表", description = "按 trace 关联会话轨迹摘要（P2）")
    @ApiOperationSupport(order = 31)
    @GetMapping("metrics/feedback")
    public JsonResult metricsFeedbackList(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("feedback", params)));
    }

    @Operation(summary = "数据中台金标集候选导出", description = "点踩/点赞会话 → JSONL 候选行，expected_* 留人工审核（P2 验收项）")
    @ApiOperationSupport(order = 32)
    @GetMapping("metrics/feedback/export")
    public JsonResult metricsFeedbackExport(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("feedback/export", params)));
    }

    @Operation(summary = "数据中台 CSV 报表导出", description = "view=chats/tools/delegations/faults/feedback，返回 filename+content 由前端落盘（P2）")
    @ApiOperationSupport(order = 33)
    @GetMapping("metrics/export")
    public JsonResult metricsExport(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("export", params)));
    }

    @Operation(summary = "数据中台告警处置联动", description = "委派(alarm-analyst)↔同trace故障（精确）+ jsdstu 告警 ±30 分钟（近似，口径见指标字典）；token 用访问者身份（P2）")
    @ApiOperationSupport(order = 34)
    @GetMapping("metrics/alarms/link")
    public JsonResult metricsAlarmsLink(@RequestParam Map<String, String> params, HttpServletRequest request) {
        DroneSession session = (DroneSession) request.getAttribute(ATTR_SESSION);
        Map<String, Object> identity = authService.resolveIdentity(session);
        String token = identity == null ? null : String.valueOf(identity.get("droneToken"));
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("alarms/link", params), token));
    }

    // ---- v4：知识缺口分析（数据中台规约 §5「未命中问题分析」）----

    @Operation(summary = "数据中台知识缺口分析", description = "未命中问题聚类 + 补库建议（三档优先级）；只算 no_hit/insufficient_evidence，cross_domain 排除（v4）")
    @ApiOperationSupport(order = 35)
    @GetMapping("metrics/unanswered")
    public JsonResult metricsUnanswered(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("unanswered", params)));
    }

    @Operation(summary = "数据中台命中文档使用度", description = "只统计 kept 会话的引用；与知识库文档清单做差集 = 从未被命中的文档（v4）")
    @ApiOperationSupport(order = 36)
    @GetMapping("metrics/docs/usage")
    public JsonResult metricsDocsUsage(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("docs/usage", params)));
    }

    @Operation(summary = "前端运行时报错", description = "总量 + 按页面/按版本分组 + 最近明细；写入侧在 POST /v1/ops/client-error（E-12）")
    @ApiOperationSupport(order = 37)
    @GetMapping("metrics/client-errors")
    public JsonResult metricsClientErrors(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("client-errors", params)));
    }

    @Operation(summary = "LLM 成本与配额聚合",
            description = "成本 = ODS 明细按当前价目表查询期折算（元）；按模型/用户/部门分组 + 日趋势 + 配额状态（E-13，判据 C7）。"
                    + "口径：改价后历史按新价重算（看\"现在的钱\"）；未配价模型单列 unpricedTokens；部门=画像声明过的 department 键")
    @ApiOperationSupport(order = 38)
    @GetMapping("metrics/costs")
    public JsonResult metricsCosts(@RequestParam Map<String, String> params) {
        return JsonResult.ok(agentService.metricsGet(buildMetricsPath("costs", params)));
    }

    /** 把查询参数拼成 Python 侧 metrics 路径（含 URL 编码）。 */
    private String buildMetricsPath(String tail, Map<String, String> params) {
        StringBuilder path = new StringBuilder("/v1/agent/metrics/").append(tail);
        if (params != null && !params.isEmpty()) {
            StringBuilder qs = new StringBuilder();
            for (Map.Entry<String, String> e : params.entrySet()) {
                if (qs.length() > 0) {
                    qs.append('&');
                }
                qs.append(URLEncoder.encode(e.getKey(), StandardCharsets.UTF_8))
                  .append('=')
                  .append(URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8));
            }
            path.append('?').append(qs);
        }
        return path.toString();
    }

    /**
     * 身份注入：
     * <ul>
     *   <li>登录用户：userId 以<strong>服务端令牌</strong>为准（客户端传的仅作匿名会话兜底），
     *       并在 metadata.identity 携带用户自己的 jsdstu-3 token，供业务工具调用时透传；</li>
     *   <li>匿名用户：仅传客户端 userId 做记忆隔离，identity 为空——
     *       知识问答/闲聊不受影响，业务派活由智能体提示"请先登录"。</li>
     * </ul>
     */
    private void injectIdentity(AgentChatParam param, HttpServletRequest request) {
        DroneSession session = (DroneSession) request.getAttribute(ATTR_SESSION);
        Map<String, Object> identity = authService.resolveIdentity(session);
        if (identity != null) {
            param.setUserId(((Number) identity.get("userId")).longValue());
            Map<String, Object> metadata = param.getMetadata() == null
                    ? new HashMap<>() : param.getMetadata();
            metadata.put("identity", identity);
            param.setMetadata(metadata);
        }
    }
}

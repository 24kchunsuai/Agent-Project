package cn.tedu.agt.ops.controller;

import cn.tedu.agt.base.auth.AuthInterceptor;
import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.ops.metrics.PrometheusText;
import cn.tedu.agt.ops.service.OpsService;
import com.github.xiaoymin.knife4j.annotations.ApiOperationSupport;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 运维端点：健康聚合（企业级落地规约 E-3，判据 C3「可观测」）+ 指标（E-9，判据 C3）
 * + 前端报错上报（E-12，判据 C3）。
 *
 * <p>{@code GET /v1/ops/health} —— <b>一个 URL 回答"整套系统现在健康吗"</b>。
 * 它是三端聚合链的顶端：Java 自身 → 转发 Python 侧组件明细
 * （Python 侧那个的是同一套结构，见 {@code agt_python/app/services/ops_health.py}）。
 *
 * <p>{@code GET /v1/ops/metrics} —— <b>同样一个 URL，但读者是监控系统</b>：
 * 输出 Prometheus 文本格式，Java 段（{@code agt_java_*}，JVM）+ Python 段（{@code agt_*}，业务）
 * 拼在一起，让 Prometheus 只需配<b>一个</b>抓取目标
 * （Python 侧那份见 {@code agt_python/app/services/ops_metrics.py}）。
 *
 * <p>{@code POST /v1/ops/client-error}（E-12）—— <b>前端自己崩了要能留下痕迹</b>。
 * 这是本类里唯一<b>由浏览器主动调用</b>的端点，鉴权口径与上面两个不同，见方法注释。
 *
 * <h3>为什么在 Java 侧再包一层</h3>
 * <ul>
 *   <li>浏览器不可能持有服务间密钥（S-1 的 {@code X-Agent-Key}），前端顶栏的状态灯
 *       与前端报错上报都必须走 8080；</li>
 *   <li>Java 的"自身可达性"只有 Java 侧能给：让 Python 去探 8080 会形成
 *       8080→8000→8080 的自环，探自己必然成功、<b>指标毫无信息量</b>——这是刻意
 *       的职责划分，不是漏实现。</li>
 * </ul>
 *
 * <h3>鉴权口径（有意识的取舍，见规约 §7 开放问题）</h3>
 * {@code /v1/ops/health} 与 {@code /v1/ops/metrics} <b>不要求登录</b>：前者的价值在于
 * "谁都能一眼看到系统是否正常"，后者的读者是内网监控系统（无凭据可用）。
 * 响应里的组件明细已由 Python 侧保证不含密钥、token 与用户数据。
 * 若将来要收紧，在 {@code AuthInterceptor} 里加一条路径规则即可——
 * 它是本项目的唯一鉴权收口点。
 */
@Tag(name = "运维")
@Slf4j
@RestController
@RequestMapping("/v1/ops")
public class OpsController {

    /**
     * 单次上报体上限（字节）。
     *
     * <p><b>这是"转发前的体积闸门"，不是防 DoS 的边界</b>——Spring 读到
     * {@code @RequestBody} 时已经把请求体缓冲进内存了，真正的体积限制要在 Filter 层做。
     * 它挡住的是"下游被灌进一个几 MB 的垃圾体"（Python 会为它做截断与入库），
     * 以及"库里出现一条超长记录"。如实记下这个边界，免得后人以为这里能防洪水。</p>
     */
    private static final int MAX_REPORT_BYTES = 8 * 1024;

    @Autowired
    private OpsService opsService;

    @Operation(summary = "运维健康聚合",
            description = "status=up|degraded|down + 各组件明细；Python 不可达时整体 down 并给出原因")
    @ApiOperationSupport(order = 10)
    @GetMapping("health")
    public JsonResult health(
            @RequestParam(required = false, defaultValue = "false") boolean fresh) {
        return JsonResult.ok(opsService.health(fresh));
    }

    /**
     * Prometheus 文本格式指标（E-9）。
     *
     * <p><b>返回的不是本项目的 JSON 信封，而是纯文本</b>（`text/plain; version=0.0.4`）。
     * 这是刻意的：Prometheus 只认文本格式，包一层 {@code {code,data}} 等于这个端点不存在。
     * 因此这里用 {@code ResponseEntity<String>} 而不是 {@code JsonResult}——
     * 顺便也绕开了全局响应包装（如果有），避免文本被套进 JSON。
     *
     * <p><b>鉴权口径与 {@code /v1/ops/health} 一致（匿名）</b>：见类注释的取舍说明与
     * 规约 §7.1 的开放问题 O1。监控系统在可信内网内抓取，不持有服务间密钥。
     */
    @Operation(summary = "Prometheus 文本格式指标",
            description = "agt_java_*（JVM）+ agt_*（智能体/业务）两段；Python 不可达时只返回 Java 段")
    @ApiOperationSupport(order = 20)
    @GetMapping(value = "metrics", produces = MediaType.TEXT_PLAIN_VALUE)
    public ResponseEntity<String> metrics(
            @RequestParam(required = false, defaultValue = "24") int window,
            @RequestParam(required = false, defaultValue = "false") boolean fresh) {
        String body = opsService.metrics(window, fresh);
        return ResponseEntity.ok()
                // 显式写全 Content-Type：`text/plain` 不够——缺 `version=0.0.4`
                // 时抓取器无法确定用哪个解析器（与 Python 侧同一字符串，见 PrometheusText）
                .contentType(MediaType.parseMediaType(PrometheusText.CONTENT_TYPE))
                .body(body);
    }

    /**
     * 前端运行时报错上报（E-12）。
     *
     * <h3>鉴权口径：与上面两个端点<b>刻意不同</b></h3>
     * 本端点<b>允许匿名</b>，且这是必须的：
     * <ul>
     *   <li><b>登录页自己就会报错</b>——那时候用户还没有令牌；</li>
     *   <li>令牌刚过期时用户点了一下按钮触发的错误，也拿不到身份。</li>
     * </ul>
     * 这两类恰恰是最该被记下来的。若要求登录才能上报，"前端崩了"最有价值的场景
     * 全部落空。<b>更根本的原因是"上报不能有前置条件"</b>：一个本身就可能失败的
     * 观测能力，不能反过来要求一个它自己要观测的对象先处于健康状态。
     *
     * <p>代价与护栏：任何人（内网内）都能往这张表里写。护栏是<b>体积闸门</b>
     * （{@link #MAX_REPORT_BYTES}）+ <b>服务端截断</b>（Python 侧按 config 截断每个字段）
     * + <b>合并窗口</b>（同错同窗口只占一行）。这三条一起把"被灌垃圾"的后果限制在
     * "看板多几行噪音"，而不是"库被撑爆"。</p>
     *
     * <p>身份不从这里传入下游：{@code userId} 由 Java 从登录态解析后以查询参数注入，
     * 请求体里没有这个字段（见 {@code AgentClient.opsClientError}）。</p>
     *
     * <h3>为什么永远返回成功码</h3>
     * 调用方是一个<b>已经出错的页面</b>，只会 fire-and-forget：失败返回值没有消费者
     * 能据此行动。真正的失败原因放在 {@code data.reason} 里给排障的人看，
     * 端点本身不抛异常（与 E-8「审计失败永不抛出」同一条理由）。
     */
    @Operation(summary = "前端运行时报错上报",
            description = "接收 Vue errorHandler / window.onerror / unhandledrejection 三处收口的报错；"
                    + "匿名可调（登录页也要能上报）；永远返回 2000，是否入库看 data.stored")
    @ApiOperationSupport(order = 30)
    @PostMapping("client-error")
    public JsonResult clientError(@RequestBody(required = false) Map<String, Object> body,
                                  HttpServletRequest request) {
        long size = request.getContentLengthLong();
        if (size > MAX_REPORT_BYTES) {
            log.warn("前端报错上报被拒：体积 {} 字节 > {}", size, MAX_REPORT_BYTES);
            Map<String, Object> rejected = new LinkedHashMap<>();
            rejected.put("accepted", false);
            rejected.put("stored", false);
            rejected.put("reason", "上报体过大（" + size + " > " + MAX_REPORT_BYTES + " 字节），已丢弃");
            return JsonResult.ok(rejected);
        }
        // 用 AuthInterceptor 的常量而不是再写一个字面量：这个 key 已经有 4 处副本了，
        // 第 5 处开始就该收敛（改一处不影响另一处的那类 bug 最难查）
        DroneSession session =
                (DroneSession) request.getAttribute(AuthInterceptor.ATTR_SESSION);
        return JsonResult.ok(opsService.clientError(session, body));
    }
}

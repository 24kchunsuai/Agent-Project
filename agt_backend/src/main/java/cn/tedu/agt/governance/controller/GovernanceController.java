package cn.tedu.agt.governance.controller;

import cn.tedu.agt.base.auth.DroneSession;
import cn.tedu.agt.base.response.JsonResult;
import cn.tedu.agt.governance.service.GovernanceService;
import com.github.xiaoymin.knife4j.annotations.ApiOperationSupport;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.servlet.http.HttpServletRequest;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

/**
 * 语料治理接口（{@code /v1/kb/governance/*}）。
 *
 * <p><b>纯透传层</b>（规约 §6.2）：本层不实现任何治理逻辑，只做三件事——
 * 权限收口（谁能做什么）、身份注入（operator 取登录态，前端传值无效）、
 * 确认门透传（把下游的 requireConfirm + 依据原样交给前端）。</p>
 *
 * <p>权限矩阵：</p>
 * <table>
 *   <tr><th>操作</th><th>匿名</th><th>登录用户</th><th>管理员</th></tr>
 *   <tr><td>概览 / 审计 / 登记 / 停用区 / 配置 / 任务</td><td>❌ 4010</td><td>❌ 4011</td><td>✅</td></tr>
 *   <tr><td>停用 / 启用</td><td>❌ 4010</td><td>仅自己的文档</td><td>任意文档</td></tr>
 *   <tr><td>版本链 / 回滚</td><td>❌ 4010</td><td>❌ 4011</td><td>✅ 回滚需确认门</td></tr>
 *   <tr><td>工单列表 / 工单执行 / 工单忽略（G4）</td><td>❌ 4010</td><td>❌ 4011</td><td>✅</td></tr>
 *   <tr><td>巡检扫描 / TTL 清理（G4）</td><td>❌ 4010</td><td>❌ 4011</td><td>✅</td></tr>
 *   <tr><td>手动对账</td><td>❌ 4010</td><td>❌ 4011</td><td>✅</td></tr>
 *   <tr><td>彻底删除</td><td>❌ 4010</td><td>❌ 4011</td><td>✅ 双确认（在 {@code KbController}）</td></tr>
 * </table>
 *
 * <p>为什么审计/配置这类"只读信息"也要管理员：它们暴露的是全库状态与治理开关，
 * 属于运维视图而非用户功能；这也是"能不能绕过去重门"的调试信息，不宜下发所有人。</p>
 *
 * <p>回滚为什么也收归管理员：它把<b>在库可检索内容</b>整体换成历史版本——智能体正在
 * 使用的语料被悄悄退回旧版，影响面是全体使用者。文档所有者更新内容走的是上传路径，
 * 不需要回滚权限。</p>
 *
 * <p>G4 的工单执行化 / TTL 清理同一道理：前者会软删（可恢复）或停用（可启用）在库文档，
 * 后者直接销毁回收站内容（不可恢复）。它们都是"改变全体使用者能看到什么"的动作。</p>
 */
@Tag(name = "语料治理模块")
@Slf4j
@RestController
@RequestMapping("/v1/kb/governance")
public class GovernanceController {

    /** 会话属性 key，与 AuthInterceptor 约定一致 */
    private static final String ATTR_SESSION = "agt.loginSession";

    @Autowired
    private GovernanceService governanceService;

    // ------------------------------------------------------------ 只读视图

    @Operation(summary = "治理概览",
            description = "文档状态分布、切片指纹冗余率、去重门开关与最近审计。需管理员")
    @ApiOperationSupport(order = 10)
    @GetMapping("overview")
    public JsonResult overview(HttpServletRequest request) {
        return JsonResult.ok(governanceService.overview(session(request)));
    }

    @Operation(summary = "审计流水",
            description = "分页查询；可按操作人/动作/文档/target/时间过滤。审计行永不删除——彻底删除后它是唯一留痕。需管理员")
    @ApiOperationSupport(order = 20)
    @GetMapping("audit")
    public JsonResult audit(@RequestParam(required = false) String operator,
                            @RequestParam(required = false) String action,
                            @RequestParam(required = false) String docId,
                            @RequestParam(required = false) String target,
                            @RequestParam(required = false, name = "from") String from,
                            @RequestParam(required = false, name = "to") String to,
                            @RequestParam(defaultValue = "1") Integer page,
                            @RequestParam(value = "pageSize", required = false) Integer pageSize,
                            HttpServletRequest request) {
        return JsonResult.ok(governanceService.audit(
                session(request), operator, action, docId, target, from, to, page, pageSize));
    }

    @Operation(summary = "审计流水导出 CSV",
            description = "导出当前筛选条件下的全部审计行（上限 2 万行，超限时 truncated=true 如实标注）。需管理员")
    @ApiOperationSupport(order = 25)
    @GetMapping("audit/export")
    public JsonResult auditExport(@RequestParam(required = false) String operator,
                                  @RequestParam(required = false) String action,
                                  @RequestParam(required = false) String docId,
                                  @RequestParam(required = false) String target,
                                  @RequestParam(required = false, name = "from") String from,
                                  @RequestParam(required = false, name = "to") String to,
                                  HttpServletRequest request) {
        return JsonResult.ok(governanceService.auditExport(
                session(request), operator, action, docId, target, from, to));
    }

    @Operation(summary = "文档治理登记列表",
            description = "治理账本视角的文档清单（含回收站中的文档）；state 可选 ACTIVE/DISABLED/RECYCLED。需管理员")
    @ApiOperationSupport(order = 30)
    @GetMapping("docs")
    public JsonResult docs(@RequestParam(required = false) String state,
                           @RequestParam(defaultValue = "1") Integer page,
                           @RequestParam(value = "pageSize", required = false) Integer pageSize,
                           HttpServletRequest request) {
        return JsonResult.ok(governanceService.docs(session(request), state, page, pageSize));
    }

    @Operation(summary = "停用区列表",
            description = "被停用的文档（含停用时间与操作人）。停用无 TTL、随时可启用，故与回收站是两个视图。需管理员")
    @ApiOperationSupport(order = 35)
    @GetMapping("disabled")
    public JsonResult disabled(@RequestParam(defaultValue = "1") Integer page,
                               @RequestParam(value = "pageSize", required = false) Integer pageSize,
                               HttpServletRequest request) {
        return JsonResult.ok(governanceService.disabled(session(request), page, pageSize));
    }

    @Operation(summary = "治理配置", description = "当前去重门开关与阈值；排查「为什么没拦住」先看这里。需管理员")
    @ApiOperationSupport(order = 40)
    @GetMapping("config")
    public JsonResult config(HttpServletRequest request) {
        return JsonResult.ok(governanceService.config(session(request)));
    }

    @Operation(summary = "常驻任务状态",
            description = "对账任务跑了几轮、上轮耗时与错误。任务循环异常被就地吞掉，不主动暴露会让服务「看起来正常」。需管理员")
    @ApiOperationSupport(order = 45)
    @GetMapping("tasks")
    public JsonResult tasks(HttpServletRequest request) {
        return JsonResult.ok(governanceService.tasks(session(request)));
    }

    @Operation(summary = "版本链",
            description = "当前版本 + 历史快照（含 isCurrent / live / hasVectors 标注，前端据此决定哪些可回滚）。需管理员")
    @ApiOperationSupport(order = 47)
    @GetMapping("versions/{docId}")
    public JsonResult versions(@PathVariable String docId, HttpServletRequest request) {
        log.debug("版本链查询:docId={}", docId);
        return JsonResult.ok(governanceService.versions(docId, session(request)));
    }

    // ------------------------------------------------------------ 生命周期操作

    @Operation(summary = "停用文档",
            description = "向量物理移除（立即不可检索）+ 备份落停用区（含向量）。需登录；非管理员只能停用自己的")
    @ApiOperationSupport(order = 50)
    @PostMapping("doc/{docId}/disable")
    public JsonResult disable(@PathVariable String docId, HttpServletRequest request) {
        log.debug("文档停用:docId={}", docId);
        return JsonResult.ok(governanceService.disable(docId, session(request)));
    }

    @Operation(summary = "启用文档",
            description = "从停用备份回灌向量（零 embedding 调用，与停用前逐字节一致）。需登录；非管理员只能启用自己的")
    @ApiOperationSupport(order = 55)
    @PostMapping("doc/{docId}/enable")
    public JsonResult enable(@PathVariable String docId, HttpServletRequest request) {
        log.debug("文档启用:docId={}", docId);
        return JsonResult.ok(governanceService.enable(docId, session(request)));
    }

    @Operation(summary = "回滚到历史版本",
            description = "把指定历史版本的内容变成当前版本（当前版本自动留档为历史快照）。"
                    + "需管理员 + 确认门：未带 confirm 时返回 3001 + data.requireConfirm + 待复述文档名，"
                    + "此时并未回滚；带 confirm=true + confirmDocName 重发才执行。versionNo 缺省=退回上一版")
    @ApiOperationSupport(order = 57)
    @PostMapping("doc/{docId}/rollback")
    public JsonResult rollback(@PathVariable String docId,
                               @RequestParam(required = false) Integer versionNo,
                               @RequestParam(defaultValue = "false") Boolean confirm,
                               @RequestParam(required = false) String confirmDocName,
                               HttpServletRequest request) {
        log.debug("版本回滚:docId={} versionNo={} confirm={}", docId, versionNo, confirm);
        return JsonResult.ok(governanceService.rollback(
                docId, versionNo, Boolean.TRUE.equals(confirm), confirmDocName,
                session(request)));
    }

    @Operation(summary = "手动跑一轮向量对账",
            description = "与定时任务同一条代码路径。报告残留（已清）/缺失（只告警）/未知（只报告，绝不自动删）。"
                    + "dryRun=true 只报告不处理。需管理员")
    @ApiOperationSupport(order = 60)
    @PostMapping("reconcile/run")
    public JsonResult reconcileRun(@RequestParam(defaultValue = "false") Boolean dryRun,
                                   HttpServletRequest request) {
        log.debug("手动向量对账:dryRun={}", dryRun);
        return JsonResult.ok(governanceService.reconcileRun(session(request), Boolean.TRUE.equals(dryRun)));
    }

    // ------------------------------------------------------------ G4：工单 / 巡检 / TTL

    @Operation(summary = "巡检工单列表",
            description = "分页查询；status 可选 OPEN/DISMISSED/DONE，type 可选 DUPLICATE/STALE/LOW_QUALITY。"
                    + "OPEN 永远排最前，返回 openCount 供前端做待办角标。需管理员")
    @ApiOperationSupport(order = 70)
    @GetMapping("tickets")
    public JsonResult tickets(@RequestParam(required = false) String status,
                              @RequestParam(required = false, name = "type") String type,
                              @RequestParam(defaultValue = "1") Integer page,
                              @RequestParam(value = "pageSize", required = false) Integer pageSize,
                              HttpServletRequest request) {
        return JsonResult.ok(governanceService.tickets(session(request), status, type, page, pageSize));
    }

    @Operation(summary = "确认工单并执行",
            description = "执行的是可回退动作：DUPLICATE→软删（可恢复）、STALE/LOW_QUALITY→停用（可启用）。"
                    + "执行失败时工单保持 OPEN（原因写入备注），可重试。需管理员")
    @ApiOperationSupport(order = 75)
    @PostMapping("tickets/{ticketId}/confirm")
    public JsonResult ticketConfirm(@PathVariable Integer ticketId, HttpServletRequest request) {
        log.debug("工单执行:ticketId={}", ticketId);
        return JsonResult.ok(governanceService.ticketConfirm(ticketId, session(request)));
    }

    @Operation(summary = "忽略工单",
            description = "不执行任何动作；忽略后有记忆——文档未再变化时下轮巡检不会重复开单。需管理员")
    @ApiOperationSupport(order = 80)
    @PostMapping("tickets/{ticketId}/dismiss")
    public JsonResult ticketDismiss(@PathVariable Integer ticketId,
                                    @RequestParam(required = false) String note,
                                    HttpServletRequest request) {
        log.debug("工单忽略:ticketId={}", ticketId);
        return JsonResult.ok(governanceService.ticketDismiss(ticketId, note, session(request)));
    }

    @Operation(summary = "手动触发一轮巡检",
            description = "三类检测（近重复 / 陈旧 / 劣质切片）→ 工单去重 → 开单，与定时任务同一代码路径。"
                    + "dryRun=true 只报告会开什么单。报告里 errors 非空表示某类没查成（≠ 没问题）。需管理员")
    @ApiOperationSupport(order = 85)
    @PostMapping("inspection/scan")
    public JsonResult inspectionScan(@RequestParam(defaultValue = "false") Boolean dryRun,
                                     HttpServletRequest request) {
        log.debug("手动巡检:dryRun={}", dryRun);
        return JsonResult.ok(governanceService.inspectionScan(session(request), Boolean.TRUE.equals(dryRun)));
    }

    @Operation(summary = "TTL 到期彻底删除",
            description = "清理回收站中已超过 GOV_TTL_DAYS 的文档（三清 + TTL_PURGE 审计）。"
                    + "只清 RECYCLED——停用区无 TTL，其备份是启用的唯一数据来源；"
                    + "账本无记录的孤儿备份只在报告里列出，绝不自动删。dryRun=true 只报告。需管理员")
    @ApiOperationSupport(order = 90)
    @PostMapping("recycle-bin/purge-expired")
    public JsonResult purgeExpired(@RequestParam(defaultValue = "false") Boolean dryRun,
                                   HttpServletRequest request) {
        log.debug("TTL 清理:dryRun={}", dryRun);
        return JsonResult.ok(governanceService.purgeExpired(session(request), Boolean.TRUE.equals(dryRun)));
    }

    /** 取当前会话（可能为 null：本模块所有接口都要求登录，由 service 层统一拦截）。 */
    private DroneSession session(HttpServletRequest request) {
        return (DroneSession) request.getAttribute(ATTR_SESSION);
    }
}
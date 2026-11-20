package cn.tedu.agt.governance.service;

import cn.tedu.agt.base.auth.DroneSession;

/**
 * 语料治理服务（G1 只读视图 + G2 生命周期操作）。
 *
 * <p>与 {@code KbService} 同一套职责分工：<strong>鉴权收口在 Java</strong>
 * （operator 由登录态注入、isAdmin 由 role 判定后注入，前端参数一律不采信）、
 * <strong>错误翻译</strong>（Python 业务码 → 本系统 StatusCode 并保留原提示）、
 * <strong>确认门透传</strong>（带 data 抛 ConfirmRequiredException 给前端弹确认框）。</p>
 *
 * <p>权限口径（规约 §6.2）：审计 / 概览 / 登记 / 配置 / 任务 / 对账 / <b>版本链 / 回滚</b> /
 * <b>工单 / 巡检 / TTL 清理</b> = <b>管理员</b>；停用 - 启用 = <b>登录用户</b>，
 * 非管理员只能操作自己的文档（与"软删只能删自己的"同口径）。</p>
 *
 * <p>回滚、工单执行化、TTL 清理为什么都收归管理员：它们改变的是<b>在库可检索内容</b>
 * （回滚换掉整篇、工单执行化软删或停用一篇、TTL 彻底销毁回收站内容），
 * 影响面是全体使用者，属治理动作；文档所有者更新内容走的是上传路径。</p>
 */
public interface GovernanceService {

    /** 治理概览：状态分布、切片指纹冗余率、治理开关与最近审计。管理员。 */
    Object overview(DroneSession session);

    /**
     * 审计流水（分页 + operator/action/docId/target/时间过滤）。管理员。
     *
     * <p>{@code target} 前缀匹配（E-8）：{@code doc:} / {@code tool:} / {@code user:} /
     * {@code backup} / {@code config:} / {@code service:}，让登录、确认门、备份这些
     * 非文档类动作也能被筛出来。</p>
     */
    Object audit(DroneSession session, String operator, String action, String docId,
                 String target, String from, String to, Integer page, Integer pageSize);

    /**
     * 审计流水导出 CSV（E-8）。管理员。
     *
     * <p>返回 `data.content`（CSV 文本，utf-8 带 BOM）+ `filename` + `rows` + `truncated`。
     * 与 `metrics/export` 同一约定：**内容在 JSON 信封里**，前端统一落盘。</p>
     */
    Object auditExport(DroneSession session, String operator, String action, String docId,
                       String target, String from, String to);

    /** 文档治理登记列表（分页，state 空=全部）。管理员。 */
    Object docs(DroneSession session, String state, Integer page, Integer pageSize);

    /** 停用区列表（分页）。管理员。 */
    Object disabled(DroneSession session, Integer page, Integer pageSize);

    /** 治理配置（只读）。管理员。 */
    Object config(DroneSession session);

    /** 常驻任务状态。管理员。 */
    Object tasks(DroneSession session);

    /** 版本链：当前版本 + 历史快照（含是否可回滚标注）。管理员。 */
    Object versions(String docId, DroneSession session);

    /** 停用文档。需登录；非管理员只能停用自己的。 */
    Object disable(String docId, DroneSession session);

    /** 启用文档。需登录；非管理员只能启用自己的。 */
    Object enable(String docId, DroneSession session);

    /**
     * 回滚到历史版本。管理员。
     *
     * <p>未带 confirm 时 Python 返回确认门（3001 + requireConfirm + 待复述的文档名），
     * 本层转成 {@code ConfirmRequiredException} 透给前端弹框，**此时并未回滚**。</p>
     */
    Object rollback(String docId, Integer versionNo, boolean confirm,
                    String confirmDocName, DroneSession session);

    /** 手动跑一轮向量对账。管理员。 */
    Object reconcileRun(DroneSession session, boolean dryRun);

    // ------------------------------------------------------------ G4：巡检工单与 TTL

    /** 巡检工单列表（分页；OPEN 永远排最前，返回 openCount 供前端角标）。管理员。 */
    Object tickets(DroneSession session, String status, String type, Integer page, Integer pageSize);

    /**
     * 确认工单并执行（G4）。管理员。
     *
     * <p>执行的是<b>可回退</b>的生命周期动作：DUPLICATE→软删（可恢复）、
     * STALE / LOW_QUALITY→停用（可启用）。巡检判定本身带主观性，
     * 误判的代价必须是一键可退——这条约束是"执行化"能被允许存在的前提。</p>
     */
    Object ticketConfirm(Integer ticketId, DroneSession session);

    /**
     * 忽略工单（不执行任何动作）。管理员。
     *
     * <p>忽略是<b>有记忆</b>的：只要文档自那以后没再变化，下一轮巡检不会重复开单，
     * 否则"忽略"等于"下次再问一遍"，工单列表会迅速退化成噪音墙。</p>
     */
    Object ticketDismiss(Integer ticketId, String note, DroneSession session);

    /** 手动触发一轮巡检（与定时任务同一代码路径）。管理员。 */
    Object inspectionScan(DroneSession session, boolean dryRun);

    /** TTL 到期彻底删除（只清 RECYCLED；停用区无 TTL，其备份是启用的唯一来源）。管理员。 */
    Object purgeExpired(DroneSession session, boolean dryRun);
}

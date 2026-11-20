"""语料治理接口。

只读（G1）：
  GET  /v1/kb/governance/overview        治理概览（状态分布 / 冗余率 / 最近审计）
  GET  /v1/kb/governance/audit           审计流水（分页 + operator/action/docId/时间过滤）
  GET  /v1/kb/governance/docs            文档治理登记列表（含状态、版本、文件指纹）
  GET  /v1/kb/governance/disabled        停用区列表（停用不是删除，单独视图）
  GET  /v1/kb/governance/config          治理配置（只读）
  GET  /v1/kb/governance/tasks           常驻任务状态（对账是否在跑、上轮结果）

生命周期操作（G2）：
  POST /v1/kb/governance/doc/{id}/disable   停用（物理删向量 + 备份，可启用回来）
  POST /v1/kb/governance/doc/{id}/enable    启用（从备份回灌向量，零 embedding）
  POST /v1/kb/governance/reconcile/run      手动跑一轮向量对账

版本链（G3）：
  GET  /v1/kb/governance/versions/{id}      版本链（当前版本 + 历史快照）
  POST /v1/kb/governance/doc/{id}/rollback  回滚到历史版本（confirm 门，仅管理员）

巡检工单与 TTL（G4）：
  GET  /v1/kb/governance/tickets            工单列表（OPEN 排最前，含 openCount 角标）
  POST /v1/kb/governance/tickets/{id}/confirm   执行化（DUPLICATE→软删 / STALE、LOW_QUALITY→停用）
  POST /v1/kb/governance/tickets/{id}/dismiss   忽略（自那以后文档未变化则不再重复开单）
  POST /v1/kb/governance/inspection/scan    手动触发一轮巡检
  POST /v1/kb/governance/recycle-bin/purge-expired  TTL 到期彻底删除（定时任务同路径）

回收站的动作（软删 / 恢复 / 彻底删除）仍走 `/v1/kb/*`（见 kb.py），
不在这里重复一套路径——前端已经接的是那边。

操作审计（E-8）：
  GET  /v1/kb/governance/audit           审计流水（可加 target 过滤）
  POST /v1/kb/governance/audit           外部事件**补录**（Java 侧登录/登出上报）

调用链：Vue(9090) → Java agt_backend(8080) → 本服务(8000)。
权限（管理员才能看审计 / 停用 / 彻底删 / 处理工单）在 Java 层收口——与既有 kb 接口同一约定，
Python 侧不加鉴权，便于本地直调调试。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Path, Query
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.response import ApiResponse, StatusCode, fail, ok
from app.services.governance_service import governance_service
from app.services.inspection_service import inspection_service
from app.services.kb_service import KbConfirmRequired, KbError, kb_service
from app.services.reconcile_service import reconcile_service
from app.services.scheduler import scheduler
from app.services.ttl_service import ttl_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["kb-governance"])

PAGE_DESC = "页码，从 1 起（默认 1）"
PAGE_SIZE_DESC = "每页条数（默认 20，上限 200）"


class AuditEventParam(BaseModel):
    """外部事件补录体（E-8）。

    只收"审计所需的最小事实"：谁、做了什么、对什么、结果如何。
    **不收细节内容**——与埋点铁律一致（元数据全量记、内容不落库）。
    """

    actor: str = Field(default="system", max_length=64, description="操作人（≡ operator）")
    action: str = Field(..., max_length=32, description="动作，如 LOGIN_OK / LOGOUT")
    target: str | None = Field(default=None, max_length=160,
                               description="统一指向，如 user:admin / tool:dispatch")
    result: str = Field(default="SUCCESS", max_length=16, description="SUCCESS / FAILED / DENIED")
    detail: str | None = Field(default=None, max_length=1000, description="附注（**不含内容**）")
    ip: str | None = Field(default=None, max_length=64, description="来源 IP")
    trace_id: str | None = Field(default=None, max_length=64, description="链路 id")



def _kb_error(e: KbError) -> ApiResponse:
    """KbError → 业务响应（确认门单独给结构化 data，前端据此弹确认框）。"""
    if isinstance(e, KbConfirmRequired):
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR), message=str(e),
                           data={"requireConfirm": True, "gate": e.gate, **e.detail})
    msg = str(e)
    if "不存在" in msg or "没有该文档" in msg or "没有该文档的停用备份" in msg:
        return fail(StatusCode.DATA_UNEXISTS, msg)
    return fail(StatusCode.VALIDATE_ERROR, msg)



@router.get("/governance/overview", summary="治理概览")
def governance_overview():
    """文档状态分布、切片指纹冗余率、治理开关与最近审计。"""
    try:
        return ok(governance_service.overview())
    except Exception as e:  # noqa: BLE001
        logger.exception("治理概览查询失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"治理库暂不可用：{type(e).__name__}", data=None)


@router.get("/governance/audit", summary="审计流水（分页）")
def governance_audit(
    operator: str | None = Query(default=None, max_length=64, description="操作人"),
    action: str | None = Query(
        default=None, max_length=32,
        description="动作：UPLOAD/DEDUP_INTERCEPT/VERSION_REPLACE/SOFT_DELETE/RESTORE/PURGE/"
                    "ROLLBACK/BACKFILL/BACKUP_RUN/LOGIN_OK/LOGIN_FAIL/LOGOUT/"
                    "TOOL_CONFIRM_ALLOW/TOOL_CONFIRM_DENY/CONFIG_CHANGE"),
    doc_id: str | None = Query(default=None, max_length=120, alias="docId"),
    target: str | None = Query(
        default=None, max_length=160,
        description="统一指向，前缀匹配：doc: / tool: / user: / backup / config: / service:"),
    from_ts: str | None = Query(default=None, max_length=32, alias="from",
                                description="起始时间（含），格式 YYYY-MM-DD HH:MM:SS"),
    to_ts: str | None = Query(default=None, max_length=32, alias="to",
                              description="结束时间（含）"),
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize",
                           description=PAGE_SIZE_DESC),
):
    """审计永远不删行——彻底删除文档后，它是唯一的留痕。"""
    try:
        return ok(governance_service.query_audit(
            operator=operator, action=action, doc_id=doc_id, target=target,
            from_ts=from_ts,
            to_ts=to_ts, page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        logger.exception("审计查询失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"治理库暂不可用：{type(e).__name__}", data=None)


@router.post("/governance/audit", summary="补录一条外部事件审计")
def governance_audit_append(param: AuditEventParam = Body(...)):
    """登录/登出这类**发生在 Java 侧**的事件，由 Java 补录进同一张审计表。

    为什么不新建第二套审计：规约 E-8 明确要求"复用治理域既有 `audit()` 机制"。
    补一条记录的成本远低于再维护一套存储 + 查询 + 前端。
    """
    governance_service.audit(
        param.action, operator=param.actor, target=param.target, ip=param.ip,
        detail=param.detail, result=param.result,
        # Java 调本接口时会带 X-Trace-Id，中间件已把它绑定到请求上下文，
        # 所以留空也能拿到**同一个** traceId（不必担心被 Python 自己生成的覆盖）。
        # 显式传入只是给"直调本接口做补录"留一条口子。
        trace_id=param.trace_id or None,
    )
    return ok({"appended": True})


@router.get("/governance/audit/export", summary="审计流水导出 CSV")
def governance_audit_export(
    operator: str | None = Query(default=None, max_length=64, description="操作人"),
    action: str | None = Query(default=None, max_length=32, description="动作"),
    doc_id: str | None = Query(default=None, max_length=120, alias="docId"),
    target: str | None = Query(default=None, max_length=160,
                               description="统一指向前缀：doc: / tool: / user: / backup / config:"),
    from_ts: str | None = Query(default=None, max_length=32, alias="from"),
    to_ts: str | None = Query(default=None, max_length=32, alias="to"),
):
    """导出为 CSV 文本（放在 `data.content`，前端落盘）。

    与既有 `metrics/export` 同一约定：**返回 JSON 信封、内容在 data 里**，
    而不是直接把 CSV 当响应体——前端统一走一个下载工具，不区分两种响应形态。
    """
    return ok(governance_service.export_audit_csv(
        operator=operator, action=action, doc_id=doc_id, target=target,
        from_ts=from_ts, to_ts=to_ts))


@router.get("/governance/docs", summary="文档治理登记列表（分页）")
def governance_docs(
    state: str | None = Query(default=None, max_length=16,
                              description="ACTIVE / DISABLED / RECYCLED，空=全部"),
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize",
                           description=PAGE_SIZE_DESC),
):
    """治理视角的文档清单（状态、版本号、文件指纹、切片数）。

    与 `/v1/kb/documents` 的区别：后者是从向量库聚合出的"实际在库内容"，
    本接口是治理账本——**回收站中的文档也在这里**（向量已不在库）。
    """
    if state and state.upper() not in ("ACTIVE", "DISABLED", "RECYCLED"):
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR),
                           message="state 只支持 ACTIVE / DISABLED / RECYCLED", data=None)
    try:
        return ok(governance_service.list_docs(
            state=state.upper() if state else None, page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        logger.exception("登记列表查询失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"治理库暂不可用：{type(e).__name__}", data=None)


@router.get("/governance/config", summary="治理配置（只读）")
def governance_config():
    """当前去重门开关与阈值——排查"为什么没拦住"时先看这里。"""
    return ok({
        "enabled": bool(settings.gov_enabled),
        "dbPath": governance_service.db_path,
        "l1Enabled": bool(settings.gov_l1_enabled),
        "l2Enabled": bool(settings.gov_l2_enabled),
        "reuseVector": bool(settings.gov_reuse_vector),
        "l3Threshold": settings.gov_l3_threshold,
        "ttlDays": settings.gov_ttl_days,
        # ---- G2 ----
        "disableEnabled": bool(settings.gov_disable_enabled),
        "restoreL3Gate": bool(settings.gov_restore_l3_gate),
        "purgeRequireAdmin": bool(settings.gov_purge_require_admin),
        "purgeRequireConfirm": bool(settings.gov_purge_require_confirm),
        "backupIncludeDense": bool(settings.gov_backup_include_dense),
        "reconcileEnabled": bool(settings.gov_reconcile_enabled),
        "reconcileIntervalSec": settings.gov_reconcile_interval_sec,
        # ---- G3 ----
        "versionChainEnabled": bool(settings.gov_version_chain_enabled),
        "versionSnapshotEnabled": bool(settings.gov_version_snapshot_enabled),
        "rollbackRequireConfirm": bool(settings.gov_rollback_require_confirm),
        "rollbackRequireAdmin": bool(settings.gov_rollback_require_admin),
        "versionKeep": settings.gov_version_keep,
        # ---- G4 ----
        "inspectionEnabled": bool(settings.gov_inspection_enabled),
        "inspectionIntervalSec": settings.gov_inspection_interval_sec,
        "staleDays": settings.gov_stale_days,
        "lowQualityChars": settings.gov_low_quality_chars,
        "ttlEnabled": bool(settings.gov_ttl_enabled),
        "ttlIntervalSec": settings.gov_ttl_interval_sec,
        "ttlBatch": settings.gov_ttl_batch,
    })


# ---------------------------------------------------------------- G2：生命周期操作


@router.get("/governance/disabled", summary="停用区列表（分页）")
def governance_disabled(
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize",
                           description=PAGE_SIZE_DESC),
):
    """被停用的文档（含停用时间与操作人）。

    与回收站是两个视图：停用**没有 TTL**、随时可启用，因此不放在回收站里
    （否则一次 TTL 清理会把停用文档的唯一向量备份删掉）。
    """
    try:
        return ok(kb_service.list_disabled(page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        logger.exception("停用区列表查询失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"停用区暂不可用：{type(e).__name__}", data=None)


@router.post("/governance/doc/{doc_id}/disable", summary="停用文档")
def governance_disable(
    doc_id: str = Path(min_length=1, max_length=120),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可停用任意文档"),
):
    """停用：向量物理移除（立即不可检索）+ 备份落 `data/kb/disabled/`（含向量）。"""
    try:
        return ok(kb_service.disable_document(doc_id, owner=owner, is_admin=is_admin))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("文档停用失败 | doc_id=%s", doc_id)
        return ApiResponse(code=int(StatusCode.OPERATION_FAILED),
                           message=f"停用失败：{type(e).__name__}", data=None)


@router.post("/governance/doc/{doc_id}/enable", summary="启用文档")
def governance_enable(
    doc_id: str = Path(min_length=1, max_length=120),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可启用任意文档"),
):
    """启用：从停用备份**回灌**向量（零 embedding 调用，与停用前逐字节一致）。"""
    try:
        return ok(kb_service.enable_document(doc_id, owner=owner, is_admin=is_admin))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("文档启用失败 | doc_id=%s", doc_id)
        return ApiResponse(code=int(StatusCode.OPERATION_FAILED),
                           message=f"启用失败：{type(e).__name__}", data=None)


@router.post("/governance/reconcile/run", summary="手动跑一轮向量对账")
def governance_reconcile_run(
    dry_run: bool = Query(default=False, alias="dryRun",
                          description="true=只报告不处理（排障用）"),
    operator: str = Query(default="manual", max_length=64, description="操作人（审计用）"),
):
    """对账任务的手动触发（与定时任务走同一条代码路径）。

    报告三件事：**残留**（账本说没有、库里却有 → 已清）、**缺失**（账本说有、库里没有 → 只告警）、
    **未知**（库里有、账本无登记 → 只报告，绝不自动删）。
    """
    try:
        return ok(reconcile_service.run(operator=operator, dry_run=dry_run))
    except Exception as e:  # noqa: BLE001
        logger.exception("向量对账执行失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"对账失败：{type(e).__name__}", data=None)


@router.get("/governance/tasks", summary="常驻任务状态")
def governance_tasks():
    """治理常驻任务（当前只有对账）的运行状态：跑了几轮、上轮耗时、上轮错误。

    排查"治理是不是静默失效了"先看这里——任务循环异常被就地吞掉，
    不主动暴露的话服务看起来一切正常。
    """
    return ok({"tasks": scheduler.status()})


# ---------------------------------------------------------------- G3：版本链


@router.get("/governance/versions/{doc_id}", summary="版本链（当前版本 + 历史快照）")
def governance_versions(
    doc_id: str = Path(min_length=1, max_length=120, description="文档标识"),
):
    """该文档的版本链，按版本号降序。

    每个条目带 `isCurrent`（是不是当前版本）、`live`（是否正躺在向量库里）、
    `hasVectors`（回滚能否走零 embedding 的回灌）——前端据此决定按钮可用性：
    只有 `hasVectors=true` 的历史版本才该显示"瞬时回滚"。
    """
    try:
        return ok(kb_service.list_versions(doc_id))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("版本链查询失败 | doc_id=%s", doc_id)
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"版本链暂不可用：{type(e).__name__}", data=None)


@router.post("/governance/doc/{doc_id}/rollback", summary="回滚到历史版本")
def governance_rollback(
    doc_id: str = Path(min_length=1, max_length=120),
    version_no: int | None = Query(
        default=None, alias="versionNo", ge=1,
        description="目标版本号；缺省 = 版本号最大的历史快照（退回上一版）"),
    confirm: bool = Query(default=False, description="确认门：true 才真正执行"),
    confirm_doc_name: str | None = Query(
        default=None, alias="confirmDocName", max_length=200,
        description="文档名复述，须与文档标题/docId/文件名主干之一严格一致"),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可回滚任意文档"),
):
    """把某个历史版本的内容重新变成当前版本。

    三步都对用户可见地改变了在库内容，因此过**确认门**（`confirm` + 文档名复述），
    且**仅管理员**可执行：回滚影响的是智能体的检索结果，属治理动作。
    未带 `confirm` 时返回 `3001 + data.requireConfirm`（**此时并未回滚**）。
    """
    try:
        return ok(kb_service.rollback_document(
            doc_id, target_version=version_no, owner=owner, is_admin=is_admin,
            confirm=confirm, confirm_doc_name=confirm_doc_name))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("版本回滚失败 | doc_id=%s target=%s", doc_id, version_no)
        return ApiResponse(code=int(StatusCode.OPERATION_FAILED),
                           message=f"回滚失败：{type(e).__name__}", data=None)


# ---------------------------------------------------------------- G4：工单 / 巡检 / TTL


@router.get("/governance/tickets", summary="巡检工单列表（分页）")
def governance_tickets(
    status: str | None = Query(default=None, max_length=16,
                               description="OPEN / DISMISSED / DONE，空=全部"),
    ticket_type: str | None = Query(default=None, alias="type", max_length=24,
                                    description="DUPLICATE / STALE / LOW_QUALITY"),
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize",
                           description=PAGE_SIZE_DESC),
):
    """巡检工单列表：`OPEN` 永远排最前（待办优先），并按新→旧。

    返回 `openCount` 供前端直接做角标——否则每个页面都要为"待办几个"再发一次请求。
    """
    if status and status.upper() not in ("OPEN", "DISMISSED", "DONE", "CONFIRMED"):
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR),
                           message="status 只支持 OPEN / DISMISSED / DONE", data=None)
    if ticket_type and ticket_type.upper() not in ("DUPLICATE", "STALE", "LOW_QUALITY"):
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR),
                           message="type 只支持 DUPLICATE / STALE / LOW_QUALITY", data=None)
    try:
        return ok(inspection_service.list_tickets(
            status=status, ticket_type=ticket_type, page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        logger.exception("工单列表查询失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"治理库暂不可用：{type(e).__name__}", data=None)


@router.post("/governance/tickets/{ticket_id}/confirm", summary="确认工单并执行")
def governance_ticket_confirm(
    ticket_id: int = Path(ge=1, description="工单 id"),
    operator: str = Query(default="manual", max_length=64, description="操作人（审计用）"),
):
    """把工单执行化：**DUPLICATE → 软删**（可恢复）、**STALE / LOW_QUALITY → 停用**（可启用）。

    刻意只复用既有生命周期动作，不发明新的删除路径——巡检判定带主观性，
    误判的代价必须是一键可回退的。执行失败时工单**保持 OPEN**（原因写入备注），
    下轮可重试：一次抖动不该让问题从此消失在待办列表里。
    """
    try:
        return ok(inspection_service.confirm(ticket_id, operator=operator))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("工单执行失败 | ticket_id=%s", ticket_id)
        return ApiResponse(code=int(StatusCode.OPERATION_FAILED),
                           message=f"工单执行失败：{type(e).__name__}", data=None)


@router.post("/governance/tickets/{ticket_id}/dismiss", summary="忽略工单")
def governance_ticket_dismiss(
    ticket_id: int = Path(ge=1, description="工单 id"),
    note: str | None = Query(default=None, max_length=200, description="忽略原因（可选）"),
    operator: str = Query(default="manual", max_length=64, description="操作人（审计用）"),
):
    """忽略工单（不执行任何动作）。

    忽略**是有记忆的**：只要文档自那以后没再变化，下一轮巡检不会为同一问题重复开单。
    否则"忽略"等于"下次再问一遍"，用户很快会把它当噪音全部无视。
    """
    try:
        return ok(inspection_service.dismiss(ticket_id, operator=operator, note=note))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("工单忽略失败 | ticket_id=%s", ticket_id)
        return ApiResponse(code=int(StatusCode.OPERATION_FAILED),
                           message=f"工单忽略失败：{type(e).__name__}", data=None)


@router.post("/governance/inspection/scan", summary="手动触发一轮巡检")
def governance_inspection_scan(
    dry_run: bool = Query(default=False, alias="dryRun",
                          description="true=只报告会开什么单，不落库"),
    operator: str = Query(default="manual", max_length=64, description="操作人（审计用）"),
):
    """跑一轮巡检（与定时任务同一代码路径）：三类检测 → 工单去重 → 开单。

    报告里 `errors` 非空表示"某类没查成"——它与"这类没有问题"是两件事，
    绝不能混同（否则一次 Milvus 抖动会让治理静默失效）。
    """
    try:
        return ok(inspection_service.scan(operator=operator, dry_run=dry_run))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("巡检执行失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"巡检失败：{type(e).__name__}", data=None)


@router.post("/governance/recycle-bin/purge-expired", summary="TTL 到期彻底删除")
def governance_purge_expired(
    dry_run: bool = Query(default=False, alias="dryRun",
                          description="true=只报告会删哪些，不执行"),
    operator: str = Query(default="manual", max_length=64, description="操作人（审计用）"),
):
    """清理回收站中已超过 TTL（`gov_ttl_days`）的文档：走彻底删除（三清 + `TTL_PURGE` 审计）。

    只清 RECYCLED（**停用区无 TTL**：停用文档的唯一向量备份就是它，过期删掉就再也启不回来）；
    候选只从账本取，目录里"账本无记录"的孤儿备份只在 `orphanBackups` 里报告，绝不自动删。
    """
    try:
        return ok(ttl_service.run(operator=operator, dry_run=dry_run))
    except KbError as e:
        return _kb_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("TTL 清理执行失败")
        return ApiResponse(code=int(StatusCode.AGENT_UNAVAILABLE),
                           message=f"TTL 清理失败：{type(e).__name__}", data=None)


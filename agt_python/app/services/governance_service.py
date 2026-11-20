"""语料治理服务门面。

职责边界
--------
- 业务判定：L1 文件级去重、L2 切片级去重（**在 embedding 之前**）、状态流转登记、审计
- 不碰向量库：需要向量时由 `KbService` 传入/回捞（本模块只管治理账本）
- 不碰 HTTP：API 层薄薄一层只做参数与响应包装

设计原则（与 spec §9 风险表一致）
--------------------------------
1. **治理故障不阻断业务**：任何治理侧异常都记 `[GOV-WARN]` 日志后 **fail-open**
   （放行上传）。理由：去重是"增值能力"，让它把正常上传打挂是本末倒置。
   但**必须留痕**——审计写 `result='FAILED'`，异常打 ERROR 级日志。
2. **审计不可省**：删除类动作即使治理库不可用也要尽力留痕，否则事后无从追责。
3. **状态必须与事实一致**：`vector_present` 只在向量库操作成功后翻转
   （先记意图、后确认的写法留给 G2 对账任务）。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any, Sequence

from app.core.config import settings
from app.core.trace import get_trace_id
from app.governance.hashing import chunk_fingerprint, sha256_text
from app.governance.store import GovernanceRepository, get_store, now_iso

logger = logging.getLogger(__name__)

WARN_PREFIX = "[GOV-WARN]"


class GovAction:
    """审计动作常量（避免散落的字符串字面量写错）。"""

    UPLOAD = "UPLOAD"
    DEDUP_INTERCEPT = "DEDUP_INTERCEPT"
    VERSION_REPLACE = "VERSION_REPLACE"
    DISABLE = "DISABLE"
    ENABLE = "ENABLE"
    SOFT_DELETE = "SOFT_DELETE"
    RESTORE = "RESTORE"
    PURGE = "PURGE"
    TTL_PURGE = "TTL_PURGE"
    TICKET_OPEN = "TICKET_OPEN"
    TICKET_CONFIRM = "TICKET_CONFIRM"
    TICKET_DISMISS = "TICKET_DISMISS"
    BACKFILL = "BACKFILL"
    # G2：向量对账任务（发现残留/缺失时留痕；无异常不写，避免刷屏）
    RECONCILE = "RECONCILE"
    # G3：版本回滚（把某个历史版本重新变为当前版本）
    ROLLBACK = "ROLLBACK"

    # ---- E-8 覆盖面扩展：非文档类动作 ----
    # 以下动作没有 doc_id，靠 `target` 统一指向（写法见 store.AUDIT_EXTRA_COLUMNS 注释）。
    # 与治理动作同表同序，才能在一张时间轴上回答"谁在这台机器上做了什么"。
    LOGIN_OK = "LOGIN_OK"                    # target=user:<名>
    LOGIN_FAIL = "LOGIN_FAIL"                # target=user:<名>
    LOGOUT = "LOGOUT"                        # target=user:<名>
    TOOL_CONFIRM_ALLOW = "TOOL_CONFIRM_ALLOW"  # target=tool:<工具名>
    TOOL_CONFIRM_DENY = "TOOL_CONFIRM_DENY"    # target=tool:<工具名>
    BACKUP_RUN = "BACKUP_RUN"                # target=backup
    CONFIG_CHANGE = "CONFIG_CHANGE"          # target=config:<配置项>


# `target` 前缀约定（与 store.AUDIT_EXTRA_COLUMNS 的注释逐字一致）：
# 统一前缀让审计页能"按对象类型"筛选，也让 `target LIKE 'doc:%'` 这类前缀查询成立。
TARGET_DOC = "doc:"
TARGET_TOOL = "tool:"
TARGET_USER = "user:"
TARGET_BACKUP = "backup"
TARGET_CONFIG = "config:"
TARGET_SERVICE = "service:"

#: 审计导出单次上限。到顶时**如实标注 truncated=True**，不静默少给——
#: "导出看起来成功但少了 8000 行"是最坏的结果（人拿着残缺证据去追责）。
AUDIT_EXPORT_MAX = 20000

#: 导出列顺序即前端 CSV 表头（统一维度在前，业务列在后）
AUDIT_CSV_HEADERS: tuple[str, ...] = (
    "时间", "操作人", "动作", "对象(target)", "文档名", "文档ID",
    "结果", "来源IP", "traceId", "详情",
)

#: CSV 公式注入的起始字符。审计里的对象名/文档名是**用户可控**的，
#: 若以 `=`/`+`/`-`/`@` 开头，Excel/WPS 打开时会当成公式执行（可外联取数）。
#: 导出给的是证据文件，被"证据自己反咬一口"是不可接受的，故一律加 `'` 前缀。
_CSV_FORMULA_PREFIX: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


class GovState:
    """文档治理状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    RECYCLED = "RECYCLED"


class GovernanceService:
    """治理门面（进程级单例，见模块底部 `governance_service`）。"""

    # ------------------------------------------------------------ 基础设施

    @property
    def enabled(self) -> bool:
        return bool(settings.gov_enabled)

    @property
    def store(self) -> GovernanceRepository:
        return get_store()

    @property
    def db_path(self) -> str:
        return str(getattr(self.store, "db_path", settings.gov_db_path))

    @staticmethod
    def file_fingerprint(content: str) -> str:
        """L1 指纹：整篇正文的 SHA-256。

        注：Java 层以 JSON 传正文，Python 侧拿到的是解码后的文本；因此按
        `content` 的 UTF-8 字节计算。前端同一份文件重复上传 → 指纹稳定一致。
        """
        return sha256_text(content or "")

    @staticmethod
    def chunk_fingerprint(text: str) -> str:
        """L2 指纹：规范化正文的 SHA-256（委托 hashing 模块）。"""
        return chunk_fingerprint(text)

    # ------------------------------------------------------------ 审计

    def audit(
        self,
        action: str,
        *,
        doc_id: str | None = None,
        doc_name: str | None = None,
        version_no: int | None = None,
        detail: dict[str, Any] | str | None = None,
        result: str = "SUCCESS",
        operator: str | None = None,
        target: str | None = None,
        ip: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """写审计（永不抛出——审计失败不能反过来打断业务）。

        E-8 起支持**非文档类动作**：`target` 是统一指向（`user:`/`tool:`/`backup`/
        `config:`/`service:`），登录、确认门、备份因此能与治理动作同表同序。
        `trace_id` 缺省时取当前请求上下文，**不需要调用方逐个传**——
        这是"一次 grep 串起日志与审计"的关键（日志规范 §3）。
        """
        if not self.enabled:
            return
        if isinstance(detail, dict):
            detail_text = json.dumps(detail, ensure_ascii=False, default=str)
        else:
            detail_text = detail
        try:
            self.store.log_audit(
                operator=operator or "system", action=action, doc_id=doc_id,
                doc_name=doc_name, version_no=version_no, detail=detail_text,
                result=result, target=target, ip=ip,
                trace_id=trace_id or (get_trace_id() or None),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("%s 审计写入失败 | action=%s doc=%s err=%s",
                         WARN_PREFIX, action, doc_id, e)

    # ------------------------------------------------------------ L1 去重

    def check_file_duplicate(self, file_hash: str,
                             doc_id: str | None = None) -> dict[str, Any] | None:
        """L1：整篇内容是否已存在（非回收站文档）。

        命中返回已存在文档的登记信息（供 API 告知用户"重复的是哪一篇"），
        未命中或治理不可用返回 None（fail-open）。
        """
        if not self.enabled or not settings.gov_l1_enabled or not file_hash:
            return None
        try:
            return self.store.find_doc_by_hash(file_hash, exclude_doc_id=None)
        except Exception as e:  # noqa: BLE001
            logger.error("%s L1 查重失败，放行本次上传 | doc=%s err=%s",
                         WARN_PREFIX, doc_id, e)
            return None

    # ------------------------------------------------------------ L2 去重

    def find_duplicate_chunks(
        self,
        chunk_rows: Sequence[tuple[int, str, int]],
        doc_id: str,
    ) -> dict[int, tuple[str, str, int, str]]:
        """L2：找出与**其它文档**重复的切片，并给出源切片位置（用于回捞向量）。

        参数
        ----
        chunk_rows : [(chunk_index, chunk_hash, char_len)]，当前待入库文档的切片指纹
        doc_id     : 当前文档 id（自身旧版切片不算重复）

        返回
        ----
        `{chunk_index: (chunk_hash, 源 doc_id, 源 chunk_index, 源 chunk_id)}`

        源 chunk_id 按 drone 既有命名规则 `f"{doc_id}#{chunk_index}"` 推导，
        调用方据此到 Milvus 取回 dense 向量，实现"重复切片不调 embedding"。
        """
        if not self.enabled or not settings.gov_l2_enabled or not chunk_rows:
            return {}
        hashes = [h for _, h, _ in chunk_rows]
        try:
            hits = self.store.find_duplicate_chunks(hashes, exclude_doc_id=doc_id)
        except Exception as e:  # noqa: BLE001
            logger.error("%s L2 查重失败，本次全部重新向量化 | doc=%s err=%s",
                         WARN_PREFIX, doc_id, e)
            return {}
        out: dict[int, tuple[str, str, int, str]] = {}
        for idx, h, _clen in chunk_rows:
            src = hits.get(h)
            if src:
                src_doc, src_idx = src
                out[idx] = (h, src_doc, src_idx, f"{src_doc}#{src_idx}")
        return out

    # ------------------------------------------------------------ 登记

    def register_upload(
        self,
        *,
        doc_id: str,
        doc_name: str,
        file_hash: str | None,
        owner: str,
        category: str = "",
        visibility: str = "public",
        chunk_rows: Sequence[tuple[int, str, int]] = (),
        chunk_count: int = 0,
        char_count: int = 0,
        replaced_chunks: int = 0,
        duplicate_chunks: int = 0,
        reused_vectors: int = 0,
        version_no: int | None = None,
        bump_version: bool = False,
        replaced_version: int | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """入库成功后登记治理账本 + 审计 UPLOAD。

        必须在 Milvus 写入成功之后调用——治理表里的 `vector_present=1`
        表示"向量库确实有这篇文档"，不能凭空写。

        版本号（G3）：`bump_version=True` 表示"同名上传了新内容"，版本号递增；
        `version_no` 用于回滚/恢复这类"内容有明确来源版本"的场景。
        """
        if not self.enabled:
            return {}
        try:
            doc = self.store.register_upload(
                doc_id=doc_id, doc_name=doc_name, file_hash=file_hash,
                owner=owner, category=category, visibility=visibility,
                chunk_rows=chunk_rows, chunk_count=chunk_count,
                char_count=char_count,
                version_no=version_no, bump_version=bump_version,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("%s 治理登记失败（文档已入库，账本缺失）| doc=%s err=%s",
                         WARN_PREFIX, doc_id, e)
            self.audit(GovAction.UPLOAD, doc_id=doc_id, doc_name=doc_name,
                       detail={"error": str(e), "chunks": chunk_count},
                       result="FAILED", operator=operator)
            return {}

        # 同名覆盖旧版：留 VERSION_REPLACE 痕迹（含被顶替的版本号，版本链据此对齐）
        if replaced_chunks > 0 or replaced_version:
            self.audit(GovAction.VERSION_REPLACE, doc_id=doc_id, doc_name=doc_name,
                       version_no=int(doc.get("version_no") or 1),
                       detail={"replacedChunks": replaced_chunks,
                               "replacedVersion": replaced_version,
                               "newVersion": int(doc.get("version_no") or 1)},
                       operator=operator)

        self.audit(
            GovAction.UPLOAD, doc_id=doc_id, doc_name=doc_name,
            version_no=int(doc.get("version_no") or 1),
            detail={
                "fileHash": file_hash,
                "chunks": chunk_count,
                "chars": char_count,
                "replacedChunks": replaced_chunks,
                "replacedVersion": replaced_version,
                "duplicateChunks": duplicate_chunks,
                "reusedVectors": reused_vectors,
            },
            operator=operator,
        )
        return doc

    def mark_state(
        self,
        doc_id: str,
        state: str,
        *,
        action: str | None = None,
        vector_present: bool | None = None,
        detail: dict[str, Any] | None = None,
        operator: str | None = None,
        doc_name: str | None = None,
    ) -> None:
        """状态流转登记 + 审计。`vector_present` 默认由 state 推断。"""
        if not self.enabled:
            return
        if vector_present is None:
            vector_present = state != GovState.RECYCLED
        recycled_at = now_iso() if state == GovState.RECYCLED else None
        try:
            self.store.set_state(doc_id, state, vector_present=vector_present,
                                 recycled_at=recycled_at)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 状态登记失败 | doc=%s state=%s err=%s",
                         WARN_PREFIX, doc_id, state, e)
            if action:
                self.audit(action, doc_id=doc_id, doc_name=doc_name,
                           detail={**(detail or {}), "stateError": str(e)},
                           result="FAILED", operator=operator)
            return
        if action:
            self.audit(action, doc_id=doc_id, doc_name=doc_name, detail=detail,
                       operator=operator)

    def set_version(
        self,
        doc_id: str,
        version_no: int,
        *,
        detail: dict[str, Any] | None = None,
        operator: str | None = None,
        doc_name: str | None = None,
    ) -> bool:
        """显式改写当前版本号（G3 回滚）+ 审计 ROLLBACK。

        回滚的版本号是**跳变**的（v4 回滚到 v1 的内容 → 新版本号是 5 而非 2），
        所以不走 `register_upload` 的"旧值+1"推断，而是显式写入。
        """
        if not self.enabled:
            return False
        try:
            ok_row = self.store.set_version(doc_id, int(version_no))
        except Exception as e:  # noqa: BLE001
            logger.error("%s 版本号写入失败 | doc=%s ver=%s err=%s",
                         WARN_PREFIX, doc_id, version_no, e)
            return False
        self.audit(GovAction.ROLLBACK, doc_id=doc_id, doc_name=doc_name,
                   version_no=int(version_no), detail=detail, operator=operator)
        return ok_row

    def mark_vector_expectation(self, doc_id: str, present: bool) -> None:
        """在动向量**之前**先落"期望向量是否存在"（对账任务唯一的判定基准）。

        为什么必须有这一步（2026-09-18 实测踩到的真实数据事故）：
        恢复/启用的动作顺序原本是"写回向量 → 更新账本"，而对账任务以账本为准。
        两者重叠时，对账读到的是"期望=无"，随后把刚写回的向量当残留清掉
        （日志实证：`RESTORE writeback 3 片` 之后 12 秒，`对账已清理残留向量 doc=... chunks=3`），
        结果是**用户点恢复、内容却永久消失**，且账本还停在 ACTIVE（此后每轮对账都报"缺失"）。

        因此把顺序倒过来，与删除侧的铁律 1（先置 0、再删向量）对称：
        **期望先翻转，事实后变化**——中间的窗口最坏只是"期望有、实际还没有"，
        对账只告警不删数据。宁可虚报缺失，不可误删内容。
        """
        if not self.enabled:
            return
        try:
            self.store.set_vector_expectation(doc_id, present)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 向量期望标记失败 | doc=%s present=%s err=%s",
                         WARN_PREFIX, doc_id, present, e)

    def unregister(self, doc_id: str, *, detail: dict[str, Any] | None = None,
                   operator: str | None = None, doc_name: str | None = None,
                   action: str = GovAction.PURGE) -> None:
        """彻底删除：清除登记行与切片指纹，仅保留审计。"""
        if not self.enabled:
            return
        try:
            self.store.delete_doc(doc_id)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 登记清除失败 | doc=%s err=%s", WARN_PREFIX, doc_id, e)
            self.audit(action, doc_id=doc_id, doc_name=doc_name,
                       detail={**(detail or {}), "error": str(e)},
                       result="FAILED", operator=operator)
            return
        self.audit(action, doc_id=doc_id, doc_name=doc_name, detail=detail,
                   operator=operator)

    # ------------------------------------------------------------ 工单与巡检候选（G4）

    # 约定：**"查不出来"与"没有候选"必须能区分**。
    # 前者（治理库不可用）一律向上抛，由调用方按"本轮失败"上报；
    # 后者返回空集合。若这里把异常吞成空列表，巡检会把"某类问题一个都没检出"
    # 与"这类压根没查成"混为一谈——那正是治理静默失效的经典形态。

    def create_ticket(self, *, ticket_type: str, doc_id: str, doc_name: str | None = None,
                      similarity_score: float | None = None,
                      matched_doc_id: str | None = None,
                      note: str | None = None) -> int | None:
        """开一张工单；同类型同文档已有 OPEN 工单时返回 None（幂等，由仓储层锁内保证）。

        注意 None 有**两种含义**：已存在待办工单（正常去重）/ 治理库不可用（异常路径，
        本条已写 ERROR 日志）。两者对调用方都不是错误，但排障时要靠日志区分。
        """
        if not self.enabled:
            return None
        try:
            return self.store.create_ticket(
                ticket_type=ticket_type, doc_id=doc_id, doc_name=doc_name,
                similarity_score=similarity_score, matched_doc_id=matched_doc_id,
                note=note)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 工单创建失败 | type=%s doc=%s err=%s",
                         WARN_PREFIX, ticket_type, doc_id, e)
            return None

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            return self.store.get_ticket(int(ticket_id))
        except Exception as e:  # noqa: BLE001
            logger.error("%s 工单查询失败 | id=%s err=%s", WARN_PREFIX, ticket_id, e)
            return None

    def update_ticket(self, ticket_id: int, *, status: str | None = None,
                      note: str | None = None, handled_at: str | None = None) -> bool:
        if not self.enabled:
            return False
        try:
            return self.store.update_ticket(
                int(ticket_id), status=status, note=note, handled_at=handled_at)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 工单更新失败 | id=%s err=%s", WARN_PREFIX, ticket_id, e)
            return False

    def last_dismissed_ticket(self, *, ticket_type: str,
                              doc_id: str) -> dict[str, Any] | None:
        """该文档该类型最近一次被忽略的工单（巡检的"忽略即静默"判据）。"""
        if not self.enabled:
            return None
        try:
            return self.store.last_handled_ticket(
                ticket_type=ticket_type, doc_id=doc_id, status="DISMISSED")
        except Exception as e:  # noqa: BLE001
            logger.error("%s 上一次忽略记录查询失败 | type=%s doc=%s err=%s",
                         WARN_PREFIX, ticket_type, doc_id, e)
            return None

    def query_tickets(self, *, status: str | None = None, ticket_type: str | None = None,
                      limit: int = 50, offset: int = 0
                      ) -> tuple[list[dict[str, Any]], int]:
        """工单分页查询（异常向上抛，见本段开头的约定）。"""
        if not self.enabled:
            return [], 0
        return self.store.list_tickets(status=status, ticket_type=ticket_type,
                                       limit=limit, offset=offset)

    def count_tickets(self) -> dict[str, int]:
        if not self.enabled:
            return {}
        return self.store.count_tickets_by_status()

    def recycled_expired(self, *, before_ts: str, limit: int = 50) -> list[dict[str, Any]]:
        """TTL 候选：回收站中 `recycled_at` 已超期的文档（只走账本）。"""
        if not self.enabled:
            return []
        return self.store.list_recycled_expired(before_ts=before_ts, limit=limit)

    def stale_docs(self, *, before_ts: str, limit: int = 50) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return self.store.list_stale_docs(before_ts=before_ts, limit=limit)

    def low_quality_docs(self, *, min_chars: int, limit: int = 50) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return self.store.find_low_quality_docs(min_chars=min_chars, limit=limit)

    # ------------------------------------------------------------ 查询

    def get_doc(self, doc_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            return self.store.get_doc(doc_id)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 文档登记查询失败 | doc=%s err=%s", WARN_PREFIX, doc_id, e)
            return None

    def all_doc_briefs(self) -> list[dict[str, Any]]:
        """全部文档的**轻量**登记视图（doc_id / state / vector_present / doc_name）。

        给对账任务用：它要拿"账本里所有文档"与"向量库实际内容"做集合比对，
        但不需要正文与切片指纹（79 篇文档 1 次查询，几百字节）。
        """
        if not self.enabled:
            return []
        try:
            return self.store.list_doc_briefs()
        except Exception as e:  # noqa: BLE001
            logger.error("%s 文档登记全量查询失败 | err=%s", WARN_PREFIX, e)
            return []

    def list_docs(self, *, state: str | None = None, page: int = 1,
                  page_size: int = 20) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 20), 1), 200)
        try:
            rows, total = self.store.list_docs(
                state=state, limit=page_size, offset=(page - 1) * page_size)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 登记列表查询失败 | err=%s", WARN_PREFIX, e)
            return {"total": 0, "page": page, "pageSize": page_size,
                    "totalPages": 1, "items": [], "error": str(e)}
        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": max(1, (total + page_size - 1) // page_size),
            "items": rows,
        }

    def overview(self) -> dict[str, Any]:
        """治理概览：文档状态分布 + 切片指纹冗余率 + 工单待办 + 最近审计。"""
        try:
            by_state = self.store.count_docs_by_state()
            chunks = self.store.chunk_stats()
            recent = self.store.recent_audit(10)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 治理概览查询失败 | err=%s", WARN_PREFIX, e)
            return {"error": str(e), "enabled": self.enabled}
        total_docs = sum(by_state.values())
        try:
            tickets = self.store.count_tickets_by_status()
        except Exception as e:  # noqa: BLE001
            # 工单统计失败不该让整个概览 500：它是附加信息，主视图仍可用
            logger.error("%s 工单统计失败 | err=%s", WARN_PREFIX, e)
            tickets = {}
        return {
            "enabled": self.enabled,
            "dbPath": self.db_path,
            # 服务自述用的向量集合名：隔离脚本要据此跟随（见 app/core/test_guard.py）。
            # 只同步数据目录不同步集合，会出现"账本一致、向量查不到"的假失败——
            # G4 验收里表现为"近重复相似度算出来 0.0"。
            "milvusCollection": settings.milvus_collection,
            "docTotal": total_docs,
            "docByState": by_state,
            "chunks": chunks,
            "tickets": tickets,
            "openTickets": int(tickets.get("OPEN", 0)),
            "config": {
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
                "versionKeep": settings.gov_version_keep,
                # ---- G4 ----
                "inspectionEnabled": bool(settings.gov_inspection_enabled),
                "inspectionIntervalSec": settings.gov_inspection_interval_sec,
                "staleDays": settings.gov_stale_days,
                "lowQualityChars": settings.gov_low_quality_chars,
                "ttlEnabled": bool(settings.gov_ttl_enabled),
                "ttlIntervalSec": settings.gov_ttl_interval_sec,
                "ttlBatch": settings.gov_ttl_batch,
            },
            "recentAudit": recent,
        }

    def query_audit(self, *, operator: str | None = None, action: str | None = None,
                    doc_id: str | None = None, target: str | None = None,
                    from_ts: str | None = None,
                    to_ts: str | None = None, page: int = 1,
                    page_size: int = 20) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 20), 1), 200)
        try:
            rows, total = self.store.query_audit(
                operator=operator, action=action, doc_id=doc_id, target=target,
                from_ts=from_ts,
                to_ts=to_ts, limit=page_size, offset=(page - 1) * page_size)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 审计查询失败 | err=%s", WARN_PREFIX, e)
            return {"total": 0, "page": page, "pageSize": page_size,
                    "totalPages": 1, "items": [], "error": str(e)}
        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": max(1, (total + page_size - 1) // page_size),
            "items": rows,
        }

    # ------------------------------------------------------------ 审计导出（E-8）

    def export_audit_csv(self, *, operator: str | None = None, action: str | None = None,
                         doc_id: str | None = None, target: str | None = None,
                         from_ts: str | None = None, to_ts: str | None = None,
                         limit: int = AUDIT_EXPORT_MAX) -> dict[str, Any]:
        """把审计流水导成 CSV 文本（utf-8 带 BOM，Excel 双击可开）。

        为什么放服务端而不是前端拼表格：分页接口单页上限 200 条，
        前端要导全量就得自己循环翻页——一旦网络中途失败，用户拿到的是
        **一份看不出残缺的残缺证据**。服务端一次取齐并**如实上报 truncated**，
        这个问题从设计上消失。

        :return: `{content, filename, rows, truncated}`；失败时 `{content:"", reason}`
        """
        cap = min(max(int(limit or AUDIT_EXPORT_MAX), 1), AUDIT_EXPORT_MAX)
        try:
            rows, total = self.store.query_audit(
                operator=operator, action=action, doc_id=doc_id, target=target,
                from_ts=from_ts, to_ts=to_ts, limit=cap, offset=0)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 审计导出失败 | err=%s", WARN_PREFIX, e)
            return {"content": "", "rows": 0, "truncated": False,
                    "reason": f"治理库暂不可用：{type(e).__name__}"}

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(AUDIT_CSV_HEADERS)
        for r in rows:
            writer.writerow([_csv_cell(r.get(k)) for k in (
                "ts", "operator", "action", "target", "doc_name", "doc_id",
                "result", "ip", "trace_id", "detail",
            )])
        # BOM 必须有：缺了它 Excel 会把 UTF-8 当 ANSI，中文列名变乱码
        content = "\ufeff" + buf.getvalue()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return {
            "content": content,
            "filename": f"audit_{stamp}.csv",
            "rows": len(rows),
            "total": int(total),
            "truncated": int(total) > len(rows),
        }


def _csv_cell(value: Any) -> str:
    """把单元格规整为安全字符串。

    两件事：① 空值统一成空串（不是 "None"）；② 掐掉公式注入的起始字符。
    被掐时加 `'` 前缀——Excel 里显示为原文，但不会被求值。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    # `-` 开头且是纯数字（负数）不算注入，别把 `-5` 变成 `'-5`
    if text[:1] == "-":
        try:
            float(text)
            return text
        except ValueError:
            pass
    if text[:1] in _CSV_FORMULA_PREFIX:
        return "'" + text
    return text


governance_service = GovernanceService()

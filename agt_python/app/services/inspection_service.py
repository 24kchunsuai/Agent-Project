"""巡检工单化（G4）——把"库里正在慢慢变脏"变成有人负责的待办。

为什么需要它
------------
去重门（L1/L2/L3）只在**入库那一刻**把关，入库之后语料仍会以三种方式变脏：

- **DUPLICATE（近重复）**：G1 有意留的例外——"恢复"路径放行全重复语料
  （否则用户会陷入"删了就再也拿不回来"的死角），于是库里可能同时躺着两份很像的语料
- **STALE（陈旧）**：半年没人动过的内容，可能早已被新版取代
- **LOW_QUALITY（劣质切片）**：短到不含语义的切片（阈值 `gov_low_quality_chars`，默认 <50 字符）
  进向量库只会稀释检索。阈值必须落在切片器的 `min_section`(40) 与 `min_chunk`(100) 之间，
  取更低的值（如 10）等于空转——切片器产不出那么短的段落

巡检把它们检测出来**变成工单**，而不是自动处理：这三类判断都带主观性
（"这两篇算不算重复""这篇是不是过时了"最终要人拍板），直接自动删/停
等于让机器人替人做内容决策。工单提供的是"证据 + 一键执行"。

三条设计约束
------------
1. **幂等**：同类型同文档已有 OPEN 工单就不再开新单。判定在仓储层的锁内完成
   （`store.create_ticket`），不靠调用方"先查后插"——巡检是每小时一轮的常驻任务，
   两个判定之间的窗口足以堆出多张重复工单（手动触发与定时触发重叠时实测发生）。
2. **忽略即静默**：用户 dismiss 过后，只要文档内容自那以后没再变化，下一轮巡检**不再开单**。
   否则"忽略"按钮等于没用，工单列表很快变成噪音墙，用户干脆全部无视——
   那比没有巡检更糟（真正的问题被埋在噪音里）。
3. **执行化只复用既有生命周期动作**：DUPLICATE→软删、STALE/LOW_QUALITY→停用。
   绝不发明新的删除路径：软删可恢复、停用可启用，**误判的代价都是一键可回退**。

后台入口：`scheduler.default_tasks()` 的 `inspection_scan`（1 小时一轮）；
手动入口：`POST /v1/kb/governance/inspection/scan`（验收与排障用，与定时任务同一路径）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.governance.store import days_ago_iso, now_iso
from app.services.governance_service import GovAction, GovState, governance_service
from app.services.kb_service import KbError, kb_service

logger = logging.getLogger(__name__)

WARN_PREFIX = "[GOV-WARN]"


class TicketType:
    """工单类型（与 DB 里的 ticket_type 取值一致）。"""

    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    LOW_QUALITY = "LOW_QUALITY"


class TicketStatus:
    """工单状态。

    注：确认门是**同步执行**的，confirm 成功后直接落到 `DONE`；
    表里预留的 `CONFIRMED` 不单独使用（避免"已确认但还没执行"这个中间态
    在多实例/重启场景下永远悬着）。
    """

    OPEN = "OPEN"
    DISMISSED = "DISMISSED"
    DONE = "DONE"


# 执行化动作：检测类型 → 生命周期动作（都是可回退的）
TICKET_ACTION: dict[str, str] = {
    TicketType.DUPLICATE: "SOFT_DELETE",       # 软删（可恢复）
    TicketType.STALE: "DISABLE",               # 停用（可启用）
    TicketType.LOW_QUALITY: "DISABLE",
}


def _cutoff(days: int) -> str:
    """N 天前的时间戳（与 now_iso 同格式 → 字符串比较即时间比较）。"""
    return days_ago_iso(days)


class InspectionService:
    """一轮巡检 + 工单读写 + 执行化（进程级单例见模块底部 `inspection_service`）。"""

    @property
    def enabled(self) -> bool:
        return bool(settings.gov_enabled and settings.gov_inspection_enabled)

    # ------------------------------------------------------------ 检测

    def _detect_duplicate(self, actives: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
        """L3 近重复检测：返回 (命中列表, 错误信息)。

        归一化处理：L3 扫描会给"很像"的双方各出一条（A→B 与 B→A），
        这里按**无序对**去重，并只把工单开在**较晚更新的一方**上——
        "后传/后改的那篇"才是需要人确认去留的那篇（较早的那份是既有事实）。
        否则同一对文档会开出两张工单，用户执行其中一张后另一张变成僵尸。
        """
        if not actives:
            return [], None
        doc_ids = [str(d.get("doc_id") or "") for d in actives if d.get("doc_id")]
        try:
            hits = kb_service.l3_scan_active(
                doc_ids=doc_ids,
                threshold=float(settings.gov_l3_threshold),
                samples=int(settings.gov_inspection_samples),
            )
        except Exception as e:  # noqa: BLE001  检索失败绝不能当成"没有重复"
            logger.error("%s L3 巡检检索失败，本轮跳过重复检测 | err=%s", WARN_PREFIX, e)
            return [], f"{type(e).__name__}: {e}"

        meta = {str(d.get("doc_id") or ""): d for d in actives}
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for doc_id, info in hits.items():
            other = str(info.get("matchedDocId") or "")
            if not other:
                continue
            pair = tuple(sorted((doc_id, other)))
            if pair in seen:
                continue
            seen.add(pair)
            # 工单挂在"较晚更新"的一方（时间相同时取 doc_id 较大者，保证确定性）
            a, b = pair[0], pair[1]
            ta = str(meta.get(a, {}).get("updated_at") or "")
            tb = str(meta.get(b, {}).get("updated_at") or "")
            target, matched = (b, a) if (tb, b) >= (ta, a) else (a, b)
            out.append({
                "docId": target,
                "docTitle": str(meta.get(target, {}).get("doc_name") or target),
                "matchedDocId": matched,
                "matchedDocTitle": str(meta.get(matched, {}).get("doc_name") or matched),
                "similarity": float(info.get("maxSimilarity") or 0.0),
            })
        return out, None

    def _detect_stale(self) -> list[dict[str, Any]]:
        """STALE：`updated_at` 早于"现在 - gov_stale_days"的 ACTIVE 文档。"""
        rows = governance_service.store.list_stale_docs(
            before_ts=_cutoff(int(settings.gov_stale_days)),
            limit=max(1, int(settings.gov_inspection_docs)),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "docId": str(r.get("doc_id") or ""),
                "docTitle": str(r.get("doc_name") or r.get("doc_id") or ""),
                "updatedAt": str(r.get("updated_at") or ""),
                "days": _days_since(str(r.get("updated_at") or "")),
            })
        return [x for x in out if x["docId"]]

    def _detect_low_quality(self) -> list[dict[str, Any]]:
        """LOW_QUALITY：含 char_len < gov_low_quality_chars 切片的 ACTIVE 文档。"""
        rows = governance_service.low_quality_docs(
            min_chars=max(1, int(settings.gov_low_quality_chars)),
            limit=max(1, int(settings.gov_inspection_docs)),
        )
        return [
            {
                "docId": str(r.get("doc_id") or ""),
                "docTitle": str(r.get("doc_name") or r.get("doc_id") or ""),
                "shortChunks": int(r.get("shortChunks") or 0),
                "shortChars": int(r.get("shortChars") or 0),
            }
            for r in rows if r.get("doc_id")
        ]

    # ------------------------------------------------------------ 巡检主流程

    def scan(self, *, operator: str = "system", dry_run: bool = False) -> dict[str, Any]:
        """跑一轮巡检：检测三类问题 → 生成/去重工单 → 审计。

        `dry_run=True` 只报告不落库（排障与"先看看会开什么单"用）。
        单个检测项失败不影响其余两类（各自 try 住），报告里如实带回 `errors`——
        "巡检跑完了但某类没查成"必须能被看见，不能伪装成"这类没问题"。
        """
        t0 = time.time()
        if not settings.gov_inspection_enabled:
            raise KbError("巡检功能未开启（GOV_INSPECTION_ENABLED=false）")

        errors: list[str] = []

        docs = governance_service.list_docs(
            state=GovState.ACTIVE, page=1,
            page_size=max(1, int(settings.gov_inspection_docs)),
        )
        actives = list(docs.get("items") or [])
        duplicates, l3_err = self._detect_duplicate(actives)
        if l3_err:
            errors.append(f"duplicate: {l3_err}")

        stale: list[dict[str, Any]] = []
        low_quality: list[dict[str, Any]] = []
        try:
            stale = self._detect_stale()
        except Exception as e:  # noqa: BLE001
            logger.error("%s STALE 检测失败 | err=%s", WARN_PREFIX, e)
            errors.append(f"stale: {type(e).__name__}: {e}")
        try:
            low_quality = self._detect_low_quality()
        except Exception as e:  # noqa: BLE001
            logger.error("%s LOW_QUALITY 检测失败 | err=%s", WARN_PREFIX, e)
            errors.append(f"low_quality: {type(e).__name__}: {e}")

        opened: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        if not dry_run:
            self._open_tickets(TicketType.DUPLICATE, duplicates, opened, skipped,
                               operator=operator)
            self._open_tickets(TicketType.STALE, stale, opened, skipped, operator=operator)
            self._open_tickets(TicketType.LOW_QUALITY, low_quality, opened, skipped,
                               operator=operator)

        try:
            counts = governance_service.count_tickets()
        except Exception:  # noqa: BLE001
            counts = {}

        report: dict[str, Any] = {
            "dryRun": dry_run,
            "scannedActiveDocs": len(actives),
            "detected": {
                "duplicate": duplicates,
                "stale": stale,
                "lowQuality": low_quality,
            },
            "opened": opened,
            "skipped": skipped,
            "ticketCounts": counts,
            "errors": errors,
            "durationMs": round((time.time() - t0) * 1000),
        }

        # 有新增才写审计：1 小时一轮、一切正常也写的话，一天 24 条噪音会淹没有效信息
        # （与对账任务同一条约定，见 reconcile_service）
        if opened:
            governance_service.audit(
                GovAction.TICKET_OPEN,
                detail={
                    "opened": opened,
                    "duplicate": len(duplicates),
                    "stale": len(stale),
                    "lowQuality": len(low_quality),
                    "scannedActiveDocs": len(actives),
                },
                operator=operator,
            )
        logger.info("巡检完成 | 活跃=%d 重复=%d 陈旧=%d 劣质=%d 新开单=%d 跳过=%d %.0fms",
                    len(actives), len(duplicates), len(stale), len(low_quality),
                    len(opened), len(skipped), report["durationMs"])
        return report

    def _open_tickets(
        self,
        ticket_type: str,
        items: list[dict[str, Any]],
        opened: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        *,
        operator: str,
    ) -> None:
        """为检测结果开单（含"忽略即静默"与仓储层幂等两道去重）。"""
        for item in items:
            doc_id = str(item.get("docId") or "")
            if not doc_id:
                continue
            note = self._note_of(ticket_type, item)

            # ① 忽略即静默：用户 dismiss 过、且文档自那以后没再变化 → 不再打扰
            #    比较 handled_at 与文档 updated_at（同为 ISO 秒级字符串，可直接比）
            last = governance_service.last_dismissed_ticket(
                ticket_type=ticket_type, doc_id=doc_id)
            if last is not None:
                doc = governance_service.get_doc(doc_id) or {}
                if str(last.get("handled_at") or "") >= str(doc.get("updated_at") or ""):
                    skipped.append({"docId": doc_id, "type": ticket_type,
                                    "reason": "用户已忽略且文档此后未变化"})
                    continue

            # ② 幂等：已有 OPEN 工单 → 不开新单（仓储层锁内判定）
            try:
                ticket_id = governance_service.create_ticket(
                    ticket_type=ticket_type, doc_id=doc_id,
                    doc_name=str(item.get("docTitle") or doc_id),
                    similarity_score=item.get("similarity"),
                    matched_doc_id=item.get("matchedDocId"),
                    note=note,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("%s 工单创建失败 | type=%s doc=%s err=%s",
                             WARN_PREFIX, ticket_type, doc_id, e)
                skipped.append({"docId": doc_id, "type": ticket_type,
                                "reason": f"创建失败：{type(e).__name__}"})
                continue
            if ticket_id is None:
                skipped.append({"docId": doc_id, "type": ticket_type,
                                "reason": "已存在同类型待办工单"})
                continue
            opened.append({"ticketId": ticket_id, "type": ticket_type, "docId": doc_id,
                           "note": note})

    @staticmethod
    def _note_of(ticket_type: str, item: dict[str, Any]) -> str:
        """工单备注：把"凭什么判定"写清楚——人工判断该不该处理，全靠这句话。"""
        if ticket_type == TicketType.DUPLICATE:
            return (f"与「{item.get('matchedDocTitle')}」余弦相似度 "
                    f"{item.get('similarity')}（阈值 {settings.gov_l3_threshold}）；"
                    f"建议软删本篇，保留较早入库的那份（软删后可恢复）")
        if ticket_type == TicketType.STALE:
            return (f"最近变更于 {item.get('updatedAt')}（距今 {item.get('days')} 天，"
                    f"阈值 {settings.gov_stale_days} 天）；确认已过时请停用")
        return (f"含 {item.get('shortChunks')} 个短于 {settings.gov_low_quality_chars} "
                f"字符的切片（合计 {item.get('shortChars')} 字符）；"
                f"确认无价值请停用")

    # ------------------------------------------------------------ 工单读写

    def list_tickets(self, *, status: str | None = None, ticket_type: str | None = None,
                     page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 20), 1), 200)
        status = (status or "").upper() or None
        ticket_type = (ticket_type or "").upper() or None
        try:
            rows, total = governance_service.store.list_tickets(
                status=status, ticket_type=ticket_type,
                limit=page_size, offset=(page - 1) * page_size)
            counts = governance_service.store.count_tickets_by_status()
        except Exception as e:  # noqa: BLE001
            logger.error("%s 工单列表查询失败 | err=%s", WARN_PREFIX, e)
            return {"total": 0, "page": page, "pageSize": page_size, "totalPages": 1,
                    "items": [], "counts": {}, "error": str(e)}
        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": max(1, (total + page_size - 1) // page_size),
            # 前端角标直接吃这个：不用为"待办几个"再发一次请求
            "counts": counts,
            "openCount": int(counts.get(TicketStatus.OPEN, 0)),
            "items": rows,
        }

    def confirm(self, ticket_id: int, *, operator: str = "manual") -> dict[str, Any]:
        """工单执行化：按类型执行生命周期动作（DUPLICATE→软删、STALE/LOW_QUALITY→停用）。

        执行失败时**保持 OPEN** 并把原因追加到 note：工单是"待办"，
        一次执行失败（Milvus 抖动等）不该让它从此消失——那会让问题永久留在库里。
        """
        ticket = governance_service.get_ticket(int(ticket_id))
        if ticket is None:
            raise KbError(f"工单不存在：{ticket_id}")
        status = str(ticket.get("status") or "")
        if status != TicketStatus.OPEN:
            raise KbError(f"工单当前状态为 {status}，只有 OPEN 的工单才能确认执行")

        ticket_type = str(ticket.get("ticket_type") or "")
        doc_id = str(ticket.get("doc_id") or "")
        action = TICKET_ACTION.get(ticket_type, "")
        if not action:
            raise KbError(f"未知工单类型：{ticket_type}")

        base = {"ticketId": int(ticket_id), "ticketType": ticket_type,
                "docId": doc_id, "action": action}

        # 文档可能已被别的途径处理掉（别的管理员先删了、TTL 清了）——
        # 这不是执行失败，而是工单本身过期了：标 DONE 并说明，避免留下永远点不动的待办
        current = governance_service.get_doc(doc_id) or {}
        if not current or str(current.get("state") or "") != GovState.ACTIVE:
            self._close(ticket, TicketStatus.DONE, operator,
                        extra=f"文档当前状态为 {current.get('state') or '已不存在'}，无需执行")
            governance_service.audit(
                GovAction.TICKET_CONFIRM, doc_id=doc_id,
                doc_name=str(ticket.get("doc_name") or doc_id),
                detail={**base, "executed": False, "reason": "文档已不在库",
                        "state": current.get("state")},
                operator=operator,
            )
            return {**base, "executed": False, "status": TicketStatus.DONE,
                    "reason": "文档已不在库（工单已标记完成）"}

        try:
            if action == "SOFT_DELETE":
                result = kb_service.delete_document(doc_id, owner=operator, is_admin=True)
            else:
                result = kb_service.disable_document(doc_id, owner=operator, is_admin=True)
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
            logger.error("%s 工单执行失败，保持 OPEN 待重试 | ticket=%s doc=%s err=%s",
                         WARN_PREFIX, ticket_id, doc_id, reason)
            self._append_note(ticket, f"执行失败：{reason}")
            governance_service.audit(
                GovAction.TICKET_CONFIRM, doc_id=doc_id,
                doc_name=str(ticket.get("doc_name") or doc_id),
                detail={**base, "executed": False, "error": reason},
                result="FAILED", operator=operator,
            )
            return {**base, "executed": False, "status": TicketStatus.OPEN,
                    "error": reason}

        self._close(ticket, TicketStatus.DONE, operator,
                    extra=f"已执行：{action}（移除 {result.get('removedChunks')} 个切片）")
        governance_service.audit(
            GovAction.TICKET_CONFIRM, doc_id=doc_id,
            doc_name=str(ticket.get("doc_name") or doc_id),
            detail={**base, "executed": True, "state": GovState.RECYCLED if action == "SOFT_DELETE"
                    else GovState.DISABLED, "removedChunks": result.get("removedChunks")},
            operator=operator,
        )
        logger.info("工单已执行 | ticket=%s type=%s doc=%s action=%s by=%s",
                    ticket_id, ticket_type, doc_id, action, operator)
        return {**base, "executed": True, "status": TicketStatus.DONE,
                "state": GovState.RECYCLED if action == "SOFT_DELETE" else GovState.DISABLED,
                "removedChunks": result.get("removedChunks")}

    def dismiss(self, ticket_id: int, *, operator: str = "manual",
                note: str | None = None) -> dict[str, Any]:
        """忽略工单（不执行任何动作）。

        忽略是**有记忆的**：`handled_at` 会被记下来，巡检据此对"自那以后没变化"的文档
        不再开单（见 `_open_tickets` ①）。所以"忽略"表达的是"我知道这事，
        但决定不动它"，而不是"下次再问我一遍"。
        """
        ticket = governance_service.get_ticket(int(ticket_id))
        if ticket is None:
            raise KbError(f"工单不存在：{ticket_id}")
        status = str(ticket.get("status") or "")
        if status != TicketStatus.OPEN:
            raise KbError(f"工单当前状态为 {status}，只有 OPEN 的工单才能忽略")

        doc_id = str(ticket.get("doc_id") or "")
        self._close(ticket, TicketStatus.DISMISSED, operator,
                    extra=note or "已忽略（文档未变化时不再重复开单）")
        governance_service.audit(
            GovAction.TICKET_DISMISS, doc_id=doc_id,
            doc_name=str(ticket.get("doc_name") or doc_id),
            detail={"ticketId": int(ticket_id), "ticketType": ticket.get("ticket_type"),
                    "note": note},
            operator=operator,
        )
        logger.info("工单已忽略 | ticket=%s doc=%s by=%s", ticket_id, doc_id, operator)
        return {"ticketId": int(ticket_id), "docId": doc_id,
                "ticketType": ticket.get("ticket_type"),
                "status": TicketStatus.DISMISSED, "dismissed": True}

    # ---- 工单收尾的小工具 ----

    @staticmethod
    def _close(ticket: dict[str, Any], status: str, operator: str, *,
               extra: str = "") -> None:
        """改状态 + 追加一句处理说明（保留原备注，不覆盖判定依据）。"""
        note = str(ticket.get("note") or "")
        merged = f"{note}｜处理：{extra}" if extra else note
        governance_service.store.update_ticket(
            int(ticket["id"]), status=status, note=merged, handled_at=now_iso())

    @staticmethod
    def _append_note(ticket: dict[str, Any], extra: str) -> None:
        note = str(ticket.get("note") or "")
        governance_service.store.update_ticket(
            int(ticket["id"]), note=f"{note}｜{extra}")


def _days_since(ts: str) -> int:
    """距今多少天（解析失败返回 -1，不猜）。"""
    try:
        return max(0, (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).days)
    except (ValueError, TypeError):
        return -1


inspection_service = InspectionService()

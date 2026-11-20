"""回收站 TTL 清理（G4）——把"可恢复"变成一个**有期限的承诺**。

为什么必须有它
--------------
回收站的备份随身带全文与 2048 维向量（一篇几百 KB）。没有 TTL 的话，
"删了又传、传了又删"会让 `data/kb/recycle/` 无上限增长，且没有任何机制会回收——
磁盘被撑爆只是时间问题。TTL 把承诺明确成"默认 30 天内可恢复"（`gov_ttl_days`），
到期走彻底删除（三清 + `TTL_PURGE` 审计）。

三条安全约束（都比"记得清理"更重要）
------------------------------------
1. **只清 RECYCLED，绝不碰 DISABLED**：停用没有"过期"语义，且**启用唯一的数据来源
   就是那份备份**。一次误清的 TTL 会让停用文档再也回不来（区别于回收站：回收站过期
   是"用户默认已放弃"，停用是"暂时下架，随时要放回来"）。
2. **删除必须有账本依据**：候选**只从 `docs` 表来**（`state='RECYCLED'` 且 `recycled_at`
   超期），不扫目录。目录里多出来的 RECYCLED 备份（账本无记录）只**告警**，
   绝不自动删——与对账任务同一条铁律：万一账本被误清空（换机器/手滑删文件），
   "目录里有的就删"的清理任务会立刻变成"把整个回收站清空"的执行者。宁可漏删，不可误删。
3. **错误隔离**：一篇删不掉只记 `failed` 并留下轮重试，不影响本轮其它文档。

后台入口：`scheduler.default_tasks()` 的 `ttl_purge`（1 小时一轮）；
手动入口：`POST /v1/kb/governance/recycle-bin/purge-expired`（验收与排障用，同一路径）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import settings
from app.governance.store import days_ago_iso
from app.services.governance_service import GovAction, governance_service
from app.services.kb_backup import STATE_RECYCLED, kb_backups
from app.services.kb_service import KbError, kb_service

logger = logging.getLogger(__name__)

WARN_PREFIX = "[GOV-WARN]"


class TtlService:
    """一轮 TTL 清理（进程级单例见模块底部 `ttl_service`）。"""

    def run(self, *, operator: str = "system", dry_run: bool = False) -> dict[str, Any]:
        """扫描超期回收站文档并彻底删除。

        `dry_run=True` 只报告"这轮会删哪些"（排障与"先看看"用）。
        """
        t0 = time.time()
        if not settings.gov_ttl_enabled:
            raise KbError("TTL 清理未开启（GOV_TTL_ENABLED=false）")

        days = max(0, int(settings.gov_ttl_days))
        limit = max(1, int(settings.gov_ttl_batch))
        candidates = governance_service.recycled_expired(
            before_ts=days_ago_iso(days), limit=limit)

        purged: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        if not dry_run:
            for row in candidates:
                doc_id = str(row.get("doc_id") or "")
                if not doc_id:
                    continue
                try:
                    result = kb_service.purge_expired(doc_id, operator=operator)
                except Exception as e:  # noqa: BLE001
                    # 失败原因必须记下来：TTL 是静默的后台动作，
                    # 静默失败 = 用户以为内容已按策略销毁、实际永远留在磁盘上
                    logger.error("%s TTL 清理失败，下轮重试 | doc=%s err=%s", WARN_PREFIX, doc_id, e)
                    failed.append({"docId": doc_id, "error": f"{type(e).__name__}: {e}"})
                    continue
                purged.append({
                    "docId": doc_id,
                    "docTitle": result.get("docTitle"),
                    "recycledAt": row.get("recycled_at"),
                    "backupsRemoved": result.get("backupsRemoved"),
                    "sourceRemoved": result.get("sourceRemoved"),
                    "warnings": result.get("warnings") or [],
                })

        orphans = self.orphan_backups()
        report: dict[str, Any] = {
            "dryRun": dry_run,
            "ttlDays": days,
            "batchLimit": limit,
            "candidates": [str(r.get("doc_id")) for r in candidates],
            "purged": purged,
            "failed": failed,
            # 账本无依据的孤儿备份：只报告，绝不自动删（见模块开头约束 2）
            "orphanBackups": orphans,
            "durationMs": round((time.time() - t0) * 1000),
        }

        if purged or failed:
            governance_service.audit(
                GovAction.TTL_PURGE,
                detail={"purged": purged, "failed": failed,
                        "orphanBackups": orphans[:20],
                        "ttlDays": days, "dryRun": dry_run},
                result="FAILED" if failed else "SUCCESS",
                operator=operator,
            )
        if orphans:
            logger.warning("%s TTL 发现 %d 份无账本依据的回收站备份（不自动删，需人工核）| %s",
                           WARN_PREFIX, len(orphans), orphans[:5])
        if failed:
            logger.warning("%s TTL 本轮有 %d 篇未能清理（下轮重试）", WARN_PREFIX, len(failed))

        logger.info("TTL 清理完成 | 候选=%d 已清=%d 失败=%d 孤儿=%d %.0fms",
                    len(candidates), len(purged), len(failed), len(orphans),
                    report["durationMs"])
        return report

    def orphan_backups(self) -> list[str]:
        """回收站目录里**账本查不到**的当前版本备份（doc_id 列表）。

        只用于告警：这类文件可能来自"治理关闭期间的操作""账本被外部改动"或
        "历史上的人工干预"。它们占着磁盘，但**唯一正确的处置是人工核对**——
        自动删等于用一个看不见的假设去执行不可逆动作。
        """
        try:
            known = {str(d.get("doc_id") or "") for d in governance_service.all_doc_briefs()}
            out: list[str] = []
            for rec in kb_backups.list_records(STATE_RECYCLED):
                doc_id = str(rec.get("docId") or "")
                if doc_id and doc_id not in known:
                    out.append(doc_id)
            return sorted(out)
        except Exception as e:  # noqa: BLE001
            logger.error("%s 孤儿备份扫描失败 | err=%s", WARN_PREFIX, e)
            return []


ttl_service = TtlService()

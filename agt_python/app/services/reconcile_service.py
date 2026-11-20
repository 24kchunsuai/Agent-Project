"""向量对账任务（G2）——"账本说没有、向量库却还有"的自动纠偏。

为什么必须要有
--------------
G1 的删向量只有一次尝试（`delete` + `flush`）：若那次调用部分失败/超时，
SQLite 记下了 `vector_present=0`、Milvus 里却仍留着切片，而**此后再无任何机制会清理它们**：

- 检索侧：没有过滤条件能排除它们 → 已删除的内容继续被智能体召回
  （静默的安全事件，且用户完全无从察觉）
- 治理侧：一旦回收站记录被 purge，"哪些 doc_id 本该被删"的线索也随之消失 → 永久泄漏

对账任务就是兜住这类"删了一半"的漂移，每 5 分钟纠一次（周期见 spec §7）。

职责边界（**刻意不对称**，安全性优先）
------------------------------------
| 情形 | 判定 | 动作 |
|---|---|---|
| **残留**：账本说该无（`vector_present=0`）、库里却有 | 确定是错误 | **自动物理删除**（幂等） |
| **缺失**：账本说该有（`vector_present=1`/ACTIVE）、库里却没有 | 原因不明（写入失败？重建中？人工干预？） | **只告警，不自愈重灌**（重灌要跑 embedding，且可能与用户当下的语料版本不一致） |
| **未知**：库里有、账本里完全没有这条登记 | 可能是治理关闭期间（`GOV_ENABLED=false`）入库的合法语料 | **只报告，绝不自动删** |

第三条是安全底线：对账任务的删除动作**必须"有账本依据"**。
假如某天治理库被误清空（删文件/换机器），一个"库里多余的就删"的对账任务
会摇身变成"把整个知识库删光"的执行者——宁可漏删，不可误删。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import settings
from app.services.governance_service import GovAction, governance_service
from app.services.kb_service import QUERY_LIMIT, kb_service

logger = logging.getLogger(__name__)

WARN_PREFIX = "[GOV-WARN]"


class ReconcileService:
    """一轮对账：拉全量 doc_id 集合 → 与账本比对 → 处理残留 / 报告缺失。"""

    def run(self, *, operator: str = "system", dry_run: bool = False) -> dict[str, Any]:
        """执行一轮对账。

        `dry_run=True` 只报告不落动作（手工巡检与排障用），
        定时任务永远走真实执行。
        """
        t0 = time.time()
        client = kb_service.milvus()
        collection = kb_service.collection

        # 一次 query 拿全集合的 doc_id：比"按文档逐个 count"少 N 次往返
        # （当前 79 篇/2027 切片，一次 query 毫秒级；十万级切片时应换 query_iterator）
        rows = client.query(
            collection_name=collection, filter="id >= 0",
            output_fields=["doc_id"], limit=QUERY_LIMIT,
        )
        actual = {str(r.get("doc_id")) for r in rows if r.get("doc_id")}
        truncated = len(rows) >= QUERY_LIMIT
        if truncated:
            logger.warning("%s 向量库切片数达到单次查询上限，对账结果不完整 | limit=%d",
                           WARN_PREFIX, QUERY_LIMIT)

        briefs = governance_service.all_doc_briefs()
        known = {str(d.get("doc_id")): d for d in briefs}

        leaked = sorted(
            d for d in actual
            if d in known and not int(known[d].get("vector_present") or 0)
        )
        unknown = sorted(actual - set(known))
        missing = sorted(
            d for d, info in known.items()
            if int(info.get("vector_present") or 0) and d not in actual
        )

        limit = max(1, int(settings.gov_reconcile_batch))
        removed: list[dict[str, Any]] = []
        failed: list[str] = []
        skipped: list[str] = []
        if not dry_run:
            for doc_id in leaked[:limit]:
                # TOCTOU 复检：判定用的账本快照可能已经过时（本轮的检索/分类耗时
                # 数秒，而恢复/启用/入库随时可能把期望翻回"应有"）。
                # 删除是不可逆动作，动手前必须再看一眼当前期望——判定后发生过
                # "恢复"的文档绝不能被当残留删掉（实测踩过这个坑，见
                # governance_service.mark_vector_expectation 的说明）。
                latest = governance_service.get_doc(doc_id) or {}
                if int(latest.get("vector_present") or 0):
                    logger.info("对账跳过（判定后该文档已被恢复/启用，期望已翻转）| doc=%s", doc_id)
                    skipped.append(doc_id)
                    continue
                try:
                    n = kb_service.delete_vectors(doc_id)
                except Exception as e:  # noqa: BLE001
                    logger.error("%s 对账清理残留失败，下轮重试 | doc=%s err=%s",
                                 WARN_PREFIX, doc_id, e)
                    failed.append(doc_id)
                    continue
                if n:
                    removed.append({"docId": doc_id, "chunks": n})
                    logger.info("对账已清理残留向量 | doc=%s chunks=%d", doc_id, n)

        report: dict[str, Any] = {
            "dryRun": dry_run,
            "milvusDocs": len(actual),
            "registeredDocs": len(briefs),
            "leakedDocs": leaked,
            "leakedRemoved": removed,
            "leakedPending": sorted(set(leaked) - {r["docId"] for r in removed} - set(skipped)),
            "leakedSkipped": skipped,
            "missingDocs": missing,
            "unknownDocs": unknown,
            "failed": failed,
            "batchLimit": limit,
            "truncated": truncated,
            "durationMs": round((time.time() - t0) * 1000),
        }

        # 无异常不写审计（一天 288 轮，全是 "一切正常" 会淹没有效信息）；
        # 有发现时留痕，运维可据此回溯"哪一轮删了什么"
        if removed or failed or missing or unknown or skipped:
            governance_service.audit(
                GovAction.RECONCILE,
                detail={
                    "removed": removed,
                    "skipped": skipped,
                    "failed": failed,
                    "missing": missing[:50],
                    "unknown": unknown[:50],
                    "milvusDocs": len(actual),
                    "dryRun": dry_run,
                },
                result="FAILED" if failed else "SUCCESS",
                operator=operator,
            )
        if missing:
            logger.warning("%s 对账发现应存在却缺失的文档向量（不自愈，需人工决策）| "
                           "count=%d docs=%s", WARN_PREFIX, len(missing), missing[:5])
        if unknown:
            logger.warning("%s 对账发现向量库存在但账本无登记的语料（不自动删）| "
                           "count=%d docs=%s", WARN_PREFIX, len(unknown), unknown[:5])

        logger.info("对账完成 | 库内=%d 账本=%d 清理=%d 缺失=%d 未知=%d %.0fms",
                    len(actual), len(briefs), len(removed), len(missing),
                    len(unknown), report["durationMs"])
        return report


reconcile_service = ReconcileService()

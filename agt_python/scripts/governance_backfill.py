# -*- coding: utf-8 -*-
"""存量回填：把已入库的历史语料登记进治理库，并输出语料冗余率报告（G1）。

为什么需要
----------
治理库是新引入的（`data/governance/governance.db`），在此之前入库的 79 篇文档
在账本里没有任何记录——去重门（L1/L2）会因此对历史语料"失效"，直到它们被重新上传。
本脚本一次性把存量补进账本，让治理能力立刻覆盖全库。

关键设计
--------
1. **指纹必须与 ingest 同源**：切片指纹按"**同一套 chunker 重新切片**"得到，
   而不是从 Milvus 里的切片文本反推。原因：Milvus 里的 `text` 带标题前缀与 overlap，
   换文件名就会变，反推出来的指纹与将来 ingest 算出的指纹对不上，L2 去重会永久失效。
   重新切片得到的 `body` 才是权威比对对象。
2. **换行必须先归一（CRLF→LF）**：切片预算按字符数算，`\r` 会白占预算导致切片边界漂移。
   实测同一篇文档：磁盘 CRLF 重切 21→24 片，归一为 LF 后与库内历史切片**逐片完全一致**。
   归一化复用 `kb_service.normalize_newlines`，保证与入库路径同一口径。
3. **切片数不一致时不复用、只检测**：确有个别文档的切片器版本与入库时不同，
   此时指纹仍以 `chunk_index = -1` 登记——**参与 L2 检测，但不参与向量复用**
   （序号对不上会捞错向量，宁可回落 embedding，也不能把别人的向量安到这段文本上）。
4. **只写可信数据**：源文件缺失 → `file_hash=NULL` 并列入告警清单（宁可漏拦不误拦）。
5. **幂等**：反复执行结果一致（`register_upload` 是 upsert + 切片先清后插）。

用法
----
    python scripts/governance_backfill.py              # 回填 + 报告
    python scripts/governance_backfill.py --dry-run    # 只统计不写库（先看清影响面）
    python scripts/governance_backfill.py --report-only  # 只读治理库现状出报告
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymilvus import MilvusClient                       # noqa: E402

from app.core.config import settings                     # noqa: E402
from app.core.paths import describe as describe_data_dir  # noqa: E402
from app.governance.store import get_store, now_iso       # noqa: E402
from app.rag.chunker import ChunkConfig, chunk_document   # noqa: E402
from app.services.governance_service import (             # noqa: E402
    GovAction,
    GovState,
    governance_service,
)
from app.services.kb_service import normalize_newlines    # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-5s | %(message)s")
logger = logging.getLogger("backfill")

REPORT_PATH = ROOT / "data/governance/backfill_report.json"
KB_ROOT = ROOT / "data/kb"
DECODE_ORDER = ("utf-8-sig", "utf-8", "gb18030")

# 从向量库拉取的元数据字段（不含 text/dense，回填不需要大对象）
MILVUS_FIELDS = [
    "doc_id", "doc_title", "category", "owner", "visibility",
    "source", "source_type", "doc_updated", "chunk_index", "char_len",
]


# ---------------------------------------------------------------- 工具

def decode_bytes(data: bytes) -> str | None:
    for enc in DECODE_ORDER:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def build_stem_index(roots: Iterable[Path]) -> dict[str, Path]:
    """按文件名主干索引语料文件（doc_id 即 `sanitize_name(文件名)`，通常等于主干）。"""
    index: dict[str, Path] = {}
    for base in roots:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".md", ".markdown", ".txt"):
                index.setdefault(p.stem, p)
    return index


def source_roots() -> list[Path]:
    raw = (settings.gov_source_roots or "").strip()
    roots = [ROOT / p.strip() for p in raw.split(",") if p.strip()] if raw else []
    if not roots:
        roots = [KB_ROOT, KB_ROOT / "raw", KB_ROOT / "external"]
    if KB_ROOT not in roots:
        roots.append(KB_ROOT)
    return roots


def fetch_milvus_rows(client: MilvusClient) -> list[dict[str, Any]]:
    """全量拉取切片元数据（优先 query_iterator，回退单次大 limit）。"""
    coll = settings.milvus_collection
    rows: list[dict[str, Any]] = []
    if hasattr(client, "query_iterator"):
        try:
            it = client.query_iterator(
                collection_name=coll, batch_size=2000, filter="id >= 0",
                output_fields=MILVUS_FIELDS,
            )
            while True:
                batch = it.next()
                if not batch:
                    break
                rows.extend(batch)
            it.close()
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning("query_iterator 不可用（%s），回落单次查询", e)
            rows = []
    return list(client.query(collection_name=coll, filter="id >= 0",
                             output_fields=MILVUS_FIELDS, limit=16384))


def group_by_doc(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """切片行 → 按 doc_id 聚合的文档信息。"""
    docs: dict[str, dict[str, Any]] = {}
    for r in rows:
        did = str(r.get("doc_id") or "")
        if not did:
            continue
        d = docs.get(did)
        if d is None:
            d = {
                "docId": did,
                "docName": r.get("doc_title") or did,
                "category": r.get("category") or "",
                "owner": r.get("owner") or "",
                "visibility": r.get("visibility") or "public",
                "source": r.get("source") or "",
                "sourceType": r.get("source_type") or "",
                "docUpdated": r.get("doc_updated") or "",
                "chunkIndices": [],
                "charCount": 0,
            }
            docs[did] = d
        d["chunkIndices"].append(int(r.get("chunk_index") or 0))
        d["charCount"] += int(r.get("char_len") or 0)
    for d in docs.values():
        d["chunkCount"] = len(d["chunkIndices"])
    return docs


def chunk_config() -> ChunkConfig:
    """与 `KbService._chunk_config()` 完全一致的切片参数——指纹必须同源。"""
    return ChunkConfig(
        max_chars=settings.rag_chunk_size,
        overlap=settings.rag_chunk_overlap,
        min_chunk=settings.rag_chunk_min,
    )


def id_digest(ids: list[str]) -> dict[str, Any]:
    """回填文档清单的摘要形式（写进审计 detail）。

    为什么需要它：审计 id=1（首次回填）只记了 ``docsRegistered=79`` 这个**数量**，
    于是事后想查"那次回填到底包含哪些文档"只能靠时间戳反推（manual-link-402 的
    created_at 与其完全一致才得以确认）。清单必须进审计，否则审计答不了这个问题。

    但审计行也不能无限长（将来可能是上万篇），所以条目多时降级为
    **排序后 sha256 指纹 + 样本**：指纹可校验完整清单，样本可让人一眼看出是哪一批。
    完整清单始终在 ``backfill_report.json``（reportPath 也在审计里）。
    """
    if len(ids) <= 300:
        return {"docIds": sorted(ids)}
    joined = "\n".join(sorted(ids)).encode("utf-8")
    return {
        "docIdCount": len(ids),
        "docIdDigest": hashlib.sha256(joined).hexdigest()[:16],
        "docIdSample": sorted(ids)[:20],
        "docIdNote": "条目 >300，完整清单见 reportPath",
    }


# ---------------------------------------------------------------- 主流程

def backfill(dry_run: bool = False) -> dict[str, Any]:
    t0 = time.time()
    client = MilvusClient(uri=settings.milvus_uri)
    store = get_store()

    logger.info("拉取向量库切片元数据 | uri=%s collection=%s",
                settings.milvus_uri, settings.milvus_collection)
    rows = fetch_milvus_rows(client)
    docs = group_by_doc(rows)
    logger.info("向量库现状 | 切片=%d 文档=%d", len(rows), len(docs))

    roots = source_roots()
    logger.info("索引语料目录 | roots=%s", [str(r) for r in roots])
    stem_index = build_stem_index(roots)
    logger.info("索引到语料文件 %d 个", len(stem_index))

    cfg = chunk_config()
    report: dict[str, Any] = {
        "generatedAt": now_iso(),
        "dryRun": dry_run,
        "milvus": {"uri": settings.milvus_uri,
                   "collection": settings.milvus_collection,
                   "chunkRows": len(rows), "docs": len(docs)},
        "fingerprintCoverage": {},
        "missingSource": [],
        "chunkCountMismatch": [],
        "stateConflicts": [],
        "chunkRowsWritten": 0,
        "docsRegistered": 0,
        "recycledRegistered": 0,
    }

    fp_ok: list[str] = []
    fp_missing: list[str] = []
    registered_ids: list[str] = []      # 本次登记的文档清单（进审计——id=1 那次只记了数量，事后无法定位）
    recycled_ids: list[str] = []        # 回收站补登记清单

    for did, d in sorted(docs.items()):
        src = stem_index.get(did)
        content: str | None = None
        if src:
            raw = src.read_bytes()
            content = decode_bytes(raw)
            if content is None:
                logger.warning("源文件编码无法识别，跳过指纹 | doc=%s file=%s", did, src)
        if not content:
            fp_missing.append(did)
            report["missingSource"].append({"docId": did, "source": d["source"]})
            if not dry_run:
                store.register_upload(
                    doc_id=did, doc_name=d["docName"], file_hash=None,
                    owner=d["owner"] or "default", category=d["category"],
                    visibility=d["visibility"] if d["visibility"] in ("public", "private") else "public",
                    chunk_rows=(), chunk_count=d["chunkCount"], char_count=d["charCount"],
                )
                report["docsRegistered"] += 1
                registered_ids.append(did)
            continue

        content = normalize_newlines(content).strip()
        file_hash = governance_service.file_fingerprint(content)
        chunks = chunk_document(content, stem=did, default_category=d["category"],
                                cfg=cfg)

        # 切片数与库内不一致 → 该文档入库时的切片器版本与当前不同：
        # 指纹仍登记（L2 能检测到重复），但序号以 -1 占位（不参与向量复用，避免捞错向量）
        mismatch = len(chunks) != d["chunkCount"]
        chunk_rows = [
            (c.chunk_index if not mismatch else -1,
             governance_service.chunk_fingerprint(c.body or c.text),
             c.char_len)
            for c in chunks
        ]
        if mismatch:
            report["chunkCountMismatch"].append({
                "docId": did, "milvus": d["chunkCount"], "rechunk": len(chunks),
            })
            logger.warning("[GOV-WARN] 切片数与库内不一致，指纹以 index=-1 登记"
                           "（参与检测、不参与向量复用）| doc=%s 库内=%d 重切=%d",
                           did, d["chunkCount"], len(chunks))

        # 状态冲突：账本说已回收/停用，但向量库还有它 → 以"向量库为事实"置 ACTIVE 并告警
        existing = store.get_doc(did)
        if existing and str(existing.get("state")) != GovState.ACTIVE:
            report["stateConflicts"].append({
                "docId": did, "ledgerState": existing.get("state"),
                "vectorPresent": True,
            })
            logger.warning("[GOV-WARN] 账本状态与向量库不一致，按向量库实际置 ACTIVE | doc=%s 账本=%s",
                           did, existing.get("state"))

        if not dry_run:
            store.register_upload(
                doc_id=did, doc_name=d["docName"], file_hash=file_hash,
                owner=d["owner"] or "default", category=d["category"],
                visibility=d["visibility"] if d["visibility"] in ("public", "private") else "public",
                chunk_rows=chunk_rows, chunk_count=len(chunks),
                char_count=sum(c.char_len for c in chunks),
            )
            report["docsRegistered"] += 1
            report["chunkRowsWritten"] += len(chunk_rows)
            registered_ids.append(did)
        fp_ok.append(did)

    # ---- 回收站里的历史文档也补进账本（否则 TTL/审计视图不完整）----
    recycle_dir = KB_ROOT / "recycle"
    if recycle_dir.exists():
        for path in sorted(recycle_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("回收站记录损坏，跳过 | file=%s err=%s", path.name, e)
                continue
            did = str(rec.get("docId") or "")
            if not did or did in docs:
                continue
            recycled_at = str(rec.get("deletedAt") or now_iso())
            if not dry_run:
                store.register_upload(
                    doc_id=did, doc_name=rec.get("docTitle") or did,
                    file_hash=rec.get("fileHash"),
                    owner=rec.get("owner") or "default",
                    category=rec.get("category") or "",
                    visibility=rec.get("visibility") or "public",
                    chunk_rows=(), chunk_count=int(rec.get("chunkCount") or 0),
                    char_count=int(rec.get("charCount") or 0),
                    state=GovState.RECYCLED, vector_present=False,
                )
                store.set_state(did, GovState.RECYCLED, vector_present=False,
                                recycled_at=recycled_at)
            report["recycledRegistered"] += 1
            recycled_ids.append(did)

    # ---- 报告：指纹覆盖 + 冗余率 ----
    report["fingerprintCoverage"] = {
        "withSource": len(fp_ok), "missingSource": len(fp_missing),
        "coverage": round(len(fp_ok) / len(docs), 4) if docs else 0.0,
    }
    stats = store.chunk_stats()
    report["redundancy"] = stats
    report["topDuplicates"] = store.top_duplicate_hashes(10)
    report["elapsedSec"] = round(time.time() - t0, 1)
    # 清单进报告（全量）；审计里放的是它的摘要（见 id_digest）
    report["docIds"] = sorted(registered_ids)
    report["recycledDocIds"] = sorted(recycled_ids)

    if not dry_run:
        governance_service.audit(
            GovAction.BACKFILL, doc_id=None, doc_name="<存量回填>",
            detail={
                "docsRegistered": report["docsRegistered"],
                "chunkRowsWritten": report["chunkRowsWritten"],
                "missingSource": len(fp_missing),
                "chunkCountMismatch": len(report["chunkCountMismatch"]),
                "redundancy": stats,
                # 清单让审计能回答"这批回填包含哪些文档"（历史缺口见 id_digest 说明）；
                # reportPath 指向带全量清单与逐篇明细的报告文件
                "reportPath": str(REPORT_PATH),
                **id_digest(registered_ids),
                **({"recycledDocIds": sorted(recycled_ids)} if recycled_ids else {}),
            },
            operator="system",
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return report


def print_report(rep: dict[str, Any]) -> None:
    m, fp, rd = rep["milvus"], rep["fingerprintCoverage"], rep["redundancy"]
    print("\n" + "=" * 68)
    print("存量回填报告" + ("（dry-run，未写库）" if rep["dryRun"] else ""))
    print("=" * 68)
    print(f"向量库现状      : {m['docs']} 篇文档 / {m['chunkRows']} 切片")
    print(f"本次登记        : {rep['docsRegistered']} 篇（切片指纹 {rep['chunkRowsWritten']} 行）"
          f" + 回收站 {rep['recycledRegistered']} 篇")
    print(f"指纹覆盖率      : {fp['withSource']}/{m['docs']} = {fp['coverage'] * 100:.1f}%"
          f"（缺源文件 {fp['missingSource']} 篇）")
    if rep["chunkCountMismatch"]:
        print(f"切片数不一致    : {len(rep['chunkCountMismatch'])} 篇（仅登记文件级信息）")
    if rep["stateConflicts"]:
        print(f"状态冲突        : {len(rep['stateConflicts'])} 篇（账本与向量库不一致，已按向量库纠正）")
    print("-" * 68)
    print(f"切片指纹        : {rd['chunkRows']} 行 / 唯一 {rd['uniqueHashes']}"
          f" / 冗余 {rd['duplicateRows']} 行")
    print(f"**冗余率**      : {rd['duplicateRate'] * 100:.1f}%"
          f"（跨文档重复指纹 {rd['crossDocHashes']} 个）")
    if rep["topDuplicates"]:
        print("重复最多的切片（涉及文档数）:")
        for it in rep["topDuplicates"][:5]:
            print(f"   {it['chunkHash'][:12]}… × {it['docCount']} 篇  {it['docIds'][:3]}")
    if rep["missingSource"]:
        print(f"缺源文件文档（前 5）: {[x['docId'] for x in rep['missingSource'][:5]]}")
    print(f"耗时            : {rep['elapsedSec']}s   报告: {REPORT_PATH}")
    print("=" * 68 + "\n")


def report_only() -> dict[str, Any]:
    store = get_store()
    rep = {
        "generatedAt": now_iso(), "dryRun": True, "mode": "report-only",
        "milvus": {"note": "report-only 不读向量库"},
        "fingerprintCoverage": {"withSource": 0, "missingSource": 0, "coverage": 0.0},
        "missingSource": [], "chunkCountMismatch": [], "stateConflicts": [],
        "docsRegistered": 0, "chunkRowsWritten": 0, "recycledRegistered": 0,
        "redundancy": store.chunk_stats(), "topDuplicates": store.top_duplicate_hashes(10),
        "docByState": store.count_docs_by_state(), "elapsedSec": 0.0,
    }
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="治理库存量回填 + 冗余率报告")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--report-only", action="store_true", help="只读治理库现状出报告")
    args = ap.parse_args()

    # 本脚本是**产品运维脚本**（就是要改产品数据），因此刻意不做隔离守卫；
    # 但必须把"要对哪个目录动手"打在脸上：历史上产品目录被误写的残留，
    # 一半来自"以为自己在操作测试库"。DATA_ROOT 一旦被外部设成临时目录，
    # 这里会直接显示"（隔离）"，一眼就能发现跑错了地方。
    logger.info("数据目录 | %s", describe_data_dir())
    logger.info("写入目标 | 账本=%s | 报告=%s%s",
                get_store().db_path, REPORT_PATH,
                "（dry-run，不写库）" if args.dry_run or args.report_only else "")

    if args.report_only:
        rep = report_only()
    else:
        rep = backfill(dry_run=args.dry_run)
    print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""G2 生命周期强化验收（E2E）。

覆盖（对应 docs/specs/语料治理规约.md §8 G2 验收标准）：
  1. G2 配置开关与常驻任务就位（对账任务已注册、周期 300s）
  2. 基线：上传可检索
  3. **停用**：物理删向量（检索不命中）+ 备份落 `data/kb/disabled/`（含 dense）+ 审计；
     重复停用被拒
  4. **启用**：从备份**回灌**（`embeddingCalls=0`、切片数与停用前一致）→ 检索恢复命中
  5. **软删备份 v2**：schema=2、chunks[] 带 heading/charStart/charEnd/text/dense，
     向量可解码且维度 = 2048
  6. **恢复确认门①（L3）**：先造一份近重复文档 → 恢复原文档返回 requireConfirm +
     相似项（未执行恢复）→ 带 confirm 重发才恢复
  7. **恢复走回灌**：切片数一致、零 embedding、与检索命中
  8. **彻底删确认门②**：非管理员被拒 / 缺 confirm 被拒（带 expectedDocName）/
     确认名不符被拒 / 正确确认后**三清**（向量残留 + 备份 + 源文件）+ 审计留痕
  9. **对账任务**：人工制造向量残留 → 一轮清理；账本说有却缺失 → 只告警不自愈；
     dryRun 只报告不动作
 10. 治理概览与停用区视图
 11. 清理测试数据

运行（推荐先起隔离服务；脚本会拒绝打产品库）：
    python scripts/serve_isolated.py --port 8001                     # 终端 A
    python scripts/gov_g2_e2e.py --base http://127.0.0.1:8001        # 终端 B

数据目录隔离见 app/core/test_guard.py：脚本进程侧与目标服务侧都必须隔离，
任一指向产品目录都会被拒绝运行（`--allow-product-db` 可显式放行，但会写产品库）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.test_guard import (  # noqa: E402
    assert_remote_isolated,
    base_from_argv,
    isolate,
    warn_if_collection_dirty,
)
DATA_DIR = isolate(follow=base_from_argv())   # 跟随目标服务的数据目录，保证与服务同一份数据

from app.services.governance_service import governance_service          # noqa: E402
from app.services.kb_backup import decode_dense, kb_backups             # noqa: E402
from app.services.kb_service import kb_service                          # noqa: E402

# 服务间鉴权（S-1）：本脚本直连 Python 服务，必须带 X-Agent-Key
from _agent_auth import key_headers                                    # noqa: E402

STAMP = time.strftime("%H%M%S")
OWNER = "gov_g2_e2e"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = f"{name}{(' | ' + extra) if extra else ''}"
    (PASS if cond else FAIL).append(line)
    print(f"  [{'PASS' if cond else 'FAIL'}] {line}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 语料构造

def para(tag: str, n: int = 260) -> str:
    return (f"无人机{tag}相关内容说明。" * (n // 12 + 1))[:n]


DOC_A_ID = f"g2e2e-{STAMP}-alpha"
CONTENT_A = (
    f"# 无人机航线规划要点（用例编号 {STAMP}）\n\n"
    + para("航线规划")
    + f"\n\n## 二、气象条件评估（编号 {STAMP}）\n\n"
    + para("气象评估")
    + f"\n\n## 三、应急处置流程（编号 {STAMP}）\n\n"
    + para("应急处置")
    + f"\n\n本用例语料标识：{STAMP}。\n"
)
# 近重复：只把"无人机"换成同义表述 → L1（整篇指纹）与 L2（切片指纹）都不命中，
# 但语义几乎一致 → 专门用来触发 L3 确认门
DOC_A2_ID = f"g2e2e-{STAMP}-alpha2"
CONTENT_A2 = CONTENT_A.replace("无人机", "无人飞行器")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--allow-product-db", action="store_true",
                    help="显式放行打产品服务（会写产品库！默认拒绝，应先起隔离服务）")
    args = ap.parse_args()

    # 服务侧守卫：目标服务也必须跑在隔离数据目录上（见 app/core/test_guard.py）
    assert_remote_isolated(args.base, allow_product=args.allow_product_db)
    # 起点清洁度：集合非空时先告警（上一轮残留会让「对账/无未知文档」类断言假失败）
    warn_if_collection_dirty(args.base)

    cli = httpx.Client(base_url=args.base, timeout=300.0, trust_env=False,
                       headers=key_headers())
    v1 = "/v1"

    def upload(doc_id: str, content: str) -> dict:
        r = cli.post(f"{v1}/kb/documents", json={
            "fileName": f"{doc_id}.md", "content": content,
            "owner": OWNER, "category": "e2e-g2", "visibility": "public",
        })
        return r.json()

    def gov(path: str, method: str = "GET", **params):
        fn = cli.post if method == "POST" else cli.get
        return fn(f"{v1}/kb/governance/{path}", params=params).json()

    def hits_of(query: str) -> list[str]:
        # maxKeep 放大到 20：本用例会同时存在"原文档 + 近重复文档"，默认封顶 8 条
        # 可能把原文档的切片挤出候选，导致"恢复后不可检索"的**假失败**
        r = cli.post(f"{v1}/rag/search",
                     json={"query": query, "user": OWNER, "maxKeep": 20}).json()
        cands = (r.get("data") or {}).get("candidates") or []
        return [c.get("doc_id") for c in cands]

    def ledger_row(doc_id: str, state: str) -> dict:
        items = (gov("docs", state=state).get("data") or {}).get("items", [])
        return next((i for i in items if i["doc_id"] == doc_id), {})

    def audits(action: str = "", doc_id: str = "") -> list[dict]:
        p: dict = {"pageSize": 100}
        if action:
            p["action"] = action
        if doc_id:
            p["docId"] = doc_id
        return (gov("audit", **p).get("data") or {}).get("items", [])

    def ledger(state: str) -> list[str]:
        return [i["doc_id"] for i in
                (gov("docs", state=state).get("data") or {}).get("items", [])]

    def backup_file(doc_id: str, state: str) -> Path:
        return kb_backups.path_of(doc_id, state)

    base_docs = set(ledger("ACTIVE"))
    tmp_docs: list[str] = [DOC_A_ID, DOC_A2_ID]

    # ---------------- 1. 配置与常驻任务 ----------------
    section("1. G2 配置与常驻任务")
    cfg = gov("config").get("data") or {}
    check("停用/启用开启", cfg.get("disableEnabled") is True)
    check("恢复 L3 确认门开启", cfg.get("restoreL3Gate") is True)
    check("彻底删除要求管理员", cfg.get("purgeRequireAdmin") is True)
    check("彻底删除要求二次确认", cfg.get("purgeRequireConfirm") is True)
    check("备份随身带向量（回灌前提）", cfg.get("backupIncludeDense") is True)
    check("对账任务开启", cfg.get("reconcileEnabled") is True)
    check("对账周期 = 300s", cfg.get("reconcileIntervalSec") == 300,
          f"interval={cfg.get('reconcileIntervalSec')}")
    tasks = (gov("tasks").get("data") or {}).get("tasks") or []
    names = [t["name"] for t in tasks]
    check("常驻任务已注册（reconcile）", "reconcile" in names, f"tasks={names}")
    check("常驻任务处于启用态",
          all(t["enabled"] for t in tasks if t["name"] == "reconcile"))

    # ---------------- 2. 基线上传 ----------------
    section("2. 基线：上传并确认可检索")
    res = upload(DOC_A_ID, CONTENT_A)
    d = res.get("data") or {}
    check("上传成功", res.get("code") == 2000, str(res.get("message"))[:60])
    chunk_n = int(d.get("chunkCount") or 0)
    check("切片数 > 1", chunk_n > 1, f"chunks={chunk_n}")
    check("检索命中", DOC_A_ID in hits_of("无人机航线规划要点与气象条件评估"))

    # ---------------- 3. 停用 ----------------
    section("3. 停用（DISABLED）：物理删向量 + 备份 + 审计")
    dis_env = gov(f"doc/{DOC_A_ID}/disable", method="POST", owner=OWNER, is_admin="true")
    dis = dis_env.get("data") or {}
    check("停用成功", dis_env.get("code") == 2000, str(dis_env.get("message"))[:60])
    dis_missing = gov(f"doc/{DOC_A2_ID}/disable", method="POST", owner=OWNER)
    check("停用不存在的文档被拒", dis_missing.get("code") != 2000,
          f"code={dis_missing.get('code')}")
    check("停用返回 disabled=true", dis.get("disabled") is True, str(dis)[:80])
    check("停用移除的切片数 = 入库切片数", dis.get("removedChunks") == chunk_n,
          f"removed={dis.get('removedChunks')} chunks={chunk_n}")
    check("停用后检索不命中", DOC_A_ID not in hits_of("无人机航线规划要点与气象条件评估"))
    check("治理登记状态为 DISABLED", DOC_A_ID in ledger("DISABLED"))
    check("登记已不在 ACTIVE", DOC_A_ID not in ledger("ACTIVE"))
    disabled_list = (gov("disabled").get("data") or {}).get("items", [])
    check("停用区列表可见", DOC_A_ID in [i["docId"] for i in disabled_list])
    check("停用区列表不回传重字段",
          all("chunks" not in i for i in disabled_list))

    dpath = backup_file(DOC_A_ID, "DISABLED")
    check("备份落在 data/kb/disabled/", dpath.exists(), str(dpath.name))
    check("回收站目录下没有它（两条生命周期分开）",
          not backup_file(DOC_A_ID, "RECYCLED").exists())
    drec = kb_backups.read(DOC_A_ID, "DISABLED") or {}
    dchunks = drec.get("chunks") or []
    check("备份 schema=2", drec.get("schema") == 2, f"schema={drec.get('schema')}")
    check("备份 state=DISABLED", drec.get("state") == "DISABLED")
    check("备份切片数与入库一致", len(dchunks) == chunk_n,
          f"backup={len(dchunks)} chunks={chunk_n}")
    vec0 = decode_dense(dchunks[0].get("dense")) if dchunks else None
    check("备份首片带 dense 向量", bool(vec0), f"dims={len(vec0) if vec0 else 0}")
    check("备份向量维度 = 2048", bool(vec0) and len(vec0) == 2048,
          f"dims={len(vec0) if vec0 else 0}")
    check("备份保留引用定位（heading/charStart/charEnd）",
          bool(dchunks) and "heading" in dchunks[0] and "charStart" in dchunks[0]
          and "charEnd" in dchunks[0])
    check("审计含 DISABLE", "DISABLE" in [a["action"] for a in audits(doc_id=DOC_A_ID)])
    dis_again = gov(f"doc/{DOC_A_ID}/disable", method="POST",
                    owner=OWNER, is_admin="true")
    check("重复停用被拒", dis_again.get("code") != 2000,
          str(dis_again.get("message"))[:60])

    # ---------------- 4. 启用（回灌）----------------
    section("4. 启用（ENABLE）：从备份回灌，零 embedding")
    t0 = time.time()
    en = gov(f"doc/{DOC_A_ID}/enable", method="POST", owner=OWNER, is_admin="true")
    ed = en.get("data") or {}
    check("启用成功", en.get("code") == 2000, str(en.get("message"))[:60])
    check("启用返回 enabled=true", ed.get("enabled") is True)
    check("回灌模式 = writeback", ed.get("restoreMode") == "writeback",
          f"mode={ed.get('restoreMode')}")
    check("回灌零 embedding 调用", ed.get("embeddingCalls") == 0,
          f"calls={ed.get('embeddingCalls')}")
    check("切片数与停用前一致", ed.get("chunkCount") == chunk_n,
          f"{ed.get('chunkCount')} vs {chunk_n}")
    print(f"       （启用耗时 {round(time.time() - t0, 2)}s）")
    check("停用备份已清理", not dpath.exists())
    check("治理登记回到 ACTIVE", DOC_A_ID in ledger("ACTIVE"))
    check("启用后重新可检索", DOC_A_ID in hits_of("无人机航线规划要点与气象条件评估"))
    en_audit = [a for a in audits(action="ENABLE", doc_id=DOC_A_ID)]
    check("审计 ENABLE 记录回灌模式",
          bool(en_audit) and "writeback" in str(en_audit[0].get("detail")),
          str(en_audit[0].get("detail"))[:80] if en_audit else "无")

    # ---------------- 5. 软删 + 备份 v2 ----------------
    section("5. 软删（RECYCLED）：备份 v2 完整性")
    dl = cli.delete(f"{v1}/kb/documents/{DOC_A_ID}",
                    params={"owner": OWNER, "is_admin": "true"}).json()
    check("软删成功", (dl.get("data") or {}).get("recycled") is True)
    rpath = backup_file(DOC_A_ID, "RECYCLED")
    check("备份落在 data/kb/recycle/", rpath.exists())
    rrec = kb_backups.read(DOC_A_ID, "RECYCLED") or {}
    rchunks = rrec.get("chunks") or []
    check("回收站备份 schema=2", rrec.get("schema") == 2)
    check("回收站备份 state=RECYCLED", rrec.get("state") == "RECYCLED")
    check("备份含全部切片", len(rchunks) == chunk_n, f"{len(rchunks)} vs {chunk_n}")
    rvec = decode_dense(rchunks[0].get("dense")) if rchunks else None
    check("回收站备份向量维度 = 2048",
          bool(rvec) and len(rvec) == 2048, f"dims={len(rvec) if rvec else 0}")
    check("备份记录文件哈希（L1 指纹）", bool(rrec.get("fileHash")))
    check("备份记录版本号", int(rrec.get("versionNo") or 0) >= 1)
    check("审计 SOFT_DELETE 标记备份带向量",
          any(a["action"] == "SOFT_DELETE" and "backupVectors" in str(a.get("detail"))
              for a in audits(doc_id=DOC_A_ID)))
    check("软删后检索不命中", DOC_A_ID not in hits_of("无人机航线规划要点与气象条件评估"))

    # ---------------- 6. 恢复确认门①（L3 语义查重）----------------
    section("6. 恢复确认门①：L3 语义查重（超阈值需 confirm）")
    up2 = upload(DOC_A2_ID, CONTENT_A2)
    check("近重复文档入库成功（L1/L2 均未命中，验证是'语义相似'而非'内容重复'）",
          up2.get("code") == 2000, str(up2.get("message"))[:70])
    rs = cli.post(f"{v1}/kb/recycle/{DOC_A_ID}/restore",
                  params={"owner": OWNER, "is_admin": "true"}).json()
    rd = rs.get("data") or {}
    check("恢复被确认门拦住（requireConfirm=true）", rd.get("requireConfirm") is True,
          f"maxSimilarity={rd.get('maxSimilarity')} threshold={rd.get('threshold')}")
    check("确认门未执行恢复（restored=false）", rd.get("restored") is not True)
    check("相似度 ≥ 阈值 0.92",
          float(rd.get("maxSimilarity") or 0) >= float(rd.get("threshold") or 1),
          f"max={rd.get('maxSimilarity')} threshold={rd.get('threshold')}")
    hit_ids = [h.get("docId") for h in (rd.get("hits") or [])]
    check("相似项指向近重复文档", DOC_A2_ID in hit_ids, f"hits={hit_ids}")
    check("拦截留痕：审计 RESTORE/BLOCKED 含 L3",
          any(a["action"] == "RESTORE" and a.get("result") == "BLOCKED"
              and '"L3"' in str(a.get("detail")) for a in audits(action="RESTORE", doc_id=DOC_A_ID)))
    check("拦截后备份仍在（可重试）", rpath.exists())

    # ---------------- 7. 恢复（带 confirm）→ 回灌 ----------------
    section("7. 恢复：confirm 放行 + 按原切片回灌")
    t0 = time.time()
    rs2 = cli.post(f"{v1}/kb/recycle/{DOC_A_ID}/restore",
                   params={"owner": OWNER, "is_admin": "true", "confirm": "true"}).json()
    rd2 = rs2.get("data") or {}
    check("带 confirm 恢复成功", rs2.get("code") == 2000 and rd2.get("restored") is True,
          str(rs2.get("message"))[:70])
    check("恢复模式 = writeback（按原切片回灌）", rd2.get("restoreMode") == "writeback")
    check("恢复零 embedding 调用", rd2.get("embeddingCalls") == 0,
          f"calls={rd2.get('embeddingCalls')}")
    check("切片数与删除前一致（不重新切片，边界不漂移）",
          rd2.get("chunkCount") == chunk_n, f"{rd2.get('chunkCount')} vs {chunk_n}")
    check("恢复保留原可见性/归属",
          rd2.get("visibility") == "public" and rd2.get("owner") == OWNER)
    print(f"       （恢复耗时 {round(time.time() - t0, 2)}s）")
    check("恢复后备份已清理", not rpath.exists())
    check("治理登记回到 ACTIVE", DOC_A_ID in ledger("ACTIVE"))
    check("恢复后检索重新命中", DOC_A_ID in hits_of("无人机航线规划要点与气象条件评估"))

    # 与对账任务的竞态回归（实测踩到的真实事故）：
    # "先写向量、后更新账本期望"的窗口里，对账会把刚恢复的切片当残留删掉。
    # 现在写入侧先翻转期望，因此恢复刚结束时账本必须已是"应有"，且立刻对账也不该动它。
    row = ledger_row(DOC_A_ID, "ACTIVE")
    check("恢复后账本期望立即为'应有向量'（vector_present=1）",
          int(row.get("vector_present") or 0) == 1, f"vector_present={row.get('vector_present')}")
    rec_now = (gov("reconcile/run", method="POST", operator="e2e-race").get("data") or {})
    check("恢复后立刻对账不会把该文档当残留清掉",
          DOC_A_ID not in (rec_now.get("leakedRemoved") or [])
          and DOC_A_ID not in (rec_now.get("leakedDocs") or []),
          f"leaked={rec_now.get('leakedDocs')}")
    check("对账后仍可检索（向量确实还在）",
          DOC_A_ID in hits_of("无人机航线规划要点与气象条件评估"))
    check("审计 RESTORE 记录 confirmed + 回灌模式",
          any(a["action"] == "RESTORE" and a.get("result") == "SUCCESS"
              and "writeback" in str(a.get("detail")) and "true" in str(a.get("detail")).lower()
              for a in audits(action="RESTORE", doc_id=DOC_A_ID)))

    # ---------------- 8. 彻底删确认门② ----------------
    section("8. 彻底删确认门②：管理员 + 二次确认 + 三清")
    cli.delete(f"{v1}/kb/documents/{DOC_A_ID}", params={"owner": OWNER, "is_admin": "true"})
    p1 = cli.delete(f"{v1}/kb/recycle/{DOC_A_ID}",
                    params={"owner": OWNER, "is_admin": "false"}).json()
    check("非管理员彻底删除被拒", p1.get("code") != 2000 and "管理员" in str(p1.get("message")),
          str(p1.get("message"))[:60])
    p2 = cli.delete(f"{v1}/kb/recycle/{DOC_A_ID}",
                    params={"owner": OWNER, "is_admin": "true"}).json()
    pd2 = p2.get("data") or {}
    check("缺二次确认被拒", p2.get("code") != 2000, str(p2.get("message"))[:60])
    check("拒绝时返回 requireConfirm 与应复述的文档名",
          pd2.get("requireConfirm") is True and bool(pd2.get("expectedDocName")),
          f"expected={pd2.get('expectedDocName')}")
    p3 = cli.delete(f"{v1}/kb/recycle/{DOC_A_ID}",
                    params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                            "confirmDocName": "不存在的名字"}).json()
    check("确认名不符被拒", p3.get("code") != 2000, str(p3.get("message"))[:60])
    check("两次拦截都留痕（PURGE/BLOCKED）",
          len([a for a in audits(action="PURGE", doc_id=DOC_A_ID)
               if a.get("result") == "BLOCKED"]) >= 2)

    expected = pd2.get("expectedDocName")
    p4 = cli.delete(f"{v1}/kb/recycle/{DOC_A_ID}",
                    params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                            "confirmDocName": expected}).json()
    pd4 = p4.get("data") or {}
    check("正确确认后彻底删除成功", p4.get("code") == 2000 and pd4.get("purged") is True,
          str(p4.get("message"))[:60])
    check("三清①：备份已删（回收站 + 停用区）", pd4.get("backupsRemoved", 0) >= 1,
          f"backupsRemoved={pd4.get('backupsRemoved')}")
    check("三清②：源文件已删", pd4.get("sourceRemoved") is True)
    check("三清③：向量残留清理数已上报", "vectorResidueRemoved" in pd4,
          f"residue={pd4.get('vectorResidueRemoved')}")
    # "没删干净"必须如实上报：向量与账本都已清除、备份却还在磁盘上，
    # 是最不该静默的情况（用户以为内容已不可恢复地销毁，实际全文还在）
    check("如实上报未清干净的文件（正常应为空数组）", pd4.get("warnings") == [],
          f"warnings={pd4.get('warnings')}")
    check("备份文件确实不在磁盘上", not rpath.exists())
    check("登记行已清除", DOC_A_ID not in ledger("RECYCLED") and DOC_A_ID not in ledger("ACTIVE"))
    check("检索不可命中", DOC_A_ID not in hits_of("无人机航线规划要点与气象条件评估"))
    pur = [a for a in audits(action="PURGE", doc_id=DOC_A_ID) if a.get("result") == "SUCCESS"]
    check("审计保留 PURGE 成功留痕", len(pur) >= 1)
    check("PURGE 审计记录三清结果",
          bool(pur) and "backupsRemoved" in str(pur[0].get("detail"))
          and "sourceRemoved" in str(pur[0].get("detail")))

    # ---------------- 9. 对账任务 ----------------
    section("9. 向量对账：残留清理 / 缺失告警")
    # ① 制造残留：软删 A2（账本记 RECYCLED、向量已删）→ 再把切片直接写回向量库，
    #    等价于"软删时删向量失败"或"事后被外部回灌"的漂移。
    #    ⚠️ 这里**不能用 kb_service._write_back**：它（G2 修复后）会先翻转账本期望，
    #    制造出来的漂移就不再是"残留"了。必须绕开它，直接 insert 原始行。
    cli.delete(f"{v1}/kb/documents/{DOC_A2_ID}", params={"owner": OWNER, "is_admin": "true"})
    rec = kb_backups.read(DOC_A2_ID, "RECYCLED") or {}
    rows = kb_service._rows_from_backup(rec)
    check("已按备份重建切片行（制造残留前置条件）", len(rows) > 0, f"rows={len(rows)}")
    kb_service.milvus().insert(kb_service.collection, rows)
    kb_service.milvus().flush(kb_service.collection)
    check("残留已写入向量库（账本仍记为 RECYCLED、期望=无）",
          kb_service.query_count(DOC_A2_ID) > 0,
          f"count={kb_service.query_count(DOC_A2_ID)}")

    dry = (gov("reconcile/run", method="POST", dryRun="true",
               operator="e2e").get("data") or {})
    check("dryRun 检出残留", DOC_A2_ID in (dry.get("leakedDocs") or []),
          f"leaked={dry.get('leakedDocs')}")
    check("dryRun 不执行清理", not dry.get("leakedRemoved"),
          f"removed={dry.get('leakedRemoved')}")
    check("dryRun 后残留仍在", kb_service.query_count(DOC_A2_ID) > 0)

    real = (gov("reconcile/run", method="POST", operator="e2e").get("data") or {})
    removed = {r["docId"] for r in (real.get("leakedRemoved") or [])}
    check("对账清理了残留", DOC_A2_ID in removed, f"removed={real.get('leakedRemoved')}")
    check("清理后向量库确无该文档", kb_service.query_count(DOC_A2_ID) == 0)
    check("审计留痕 RECONCILE",
          "RECONCILE" in [a["action"] for a in audits(action="RECONCILE")])
    after = (gov("reconcile/run", method="POST", dryRun="true").get("data") or {})
    check("再跑一轮已无残留", not after.get("leakedDocs"), f"leaked={after.get('leakedDocs')}")
    check("对账报告无未知文档（账本覆盖 100%）", not after.get("unknownDocs"),
          f"unknown={after.get('unknownDocs')}")
    check("对账耗时上报", int(after.get("durationMs") or -1) >= 0,
          f"{after.get('durationMs')}ms")

    # ② 反向：账本说有、库里没有 → 只告警，不自动重灌
    ghost = f"g2e2e-{STAMP}-ghost"
    governance_service.store.register_upload(
        doc_id=ghost, doc_name="幽灵文档（仅账本有登记）", file_hash=None,
        owner=OWNER, chunk_rows=[(0, "deadbeef", 10)], chunk_count=1, char_count=10,
    )
    miss = (gov("reconcile/run", method="POST", dryRun="true").get("data") or {})
    check("检出'账本说有却缺失'的文档", ghost in (miss.get("missingDocs") or []),
          f"missing={miss.get('missingDocs')}")
    check("缺失项不被自动处理（dryRun 语义）", not miss.get("leakedRemoved"))
    real2 = (gov("reconcile/run", method="POST", operator="e2e").get("data") or {})
    check("真实执行仍只报告缺失（不自愈重灌）",
          ghost in (real2.get("missingDocs") or []) and ghost not in {
              r["docId"] for r in (real2.get("leakedRemoved") or [])})
    check("缺失文档在向量库仍不存在（未被凭空创建）",
          kb_service.query_count(ghost) == 0)
    governance_service.store.delete_doc(ghost)              # 清理幽灵登记

    # ---------------- 10. 概览 ----------------
    section("10. 治理概览与停用区")
    ov = gov("overview").get("data") or {}
    check("概览含状态分布", bool(ov.get("docByState")), str(ov.get("docByState"))[:80])
    check("概览含治理概览配置（含 G2 开关）",
          "reconcileEnabled" in (ov.get("config") or {}))
    check("概览冗余率仍在（G1 数据未受影响）",
          "duplicateRate" in (ov.get("chunks") or {}))

    # ---------------- 11. 清理 ----------------
    section("11. 清理测试数据")
    for did in tmp_docs:
        cli.delete(f"{v1}/kb/documents/{did}", params={"owner": OWNER, "is_admin": "true"})
        rec = kb_backups.read(did, "RECYCLED") or kb_backups.read(did, "DISABLED") or {}
        name = rec.get("docTitle") or did
        cli.delete(f"{v1}/kb/recycle/{did}",
                   params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                           "confirmDocName": name})
    left = [d for d in ledger("ACTIVE") if d not in base_docs and d.startswith("g2e2e-")]
    check("测试文档已清理干净", not left, f"残留={left}")
    check("测试备份文件已清理",
          not backup_file(DOC_A_ID, "RECYCLED").exists()
          and not backup_file(DOC_A2_ID, "RECYCLED").exists())

    print("\n" + "=" * 68)
    print(f"G2 验收：通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

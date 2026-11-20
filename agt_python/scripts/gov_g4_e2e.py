# -*- coding: utf-8 -*-
"""G4 巡检工单化 + TTL 清理验收（E2E）。

覆盖（对应 docs/specs/语料治理规约.md §8 G4 验收标准）：
  1. G4 配置就位（巡检开关/周期/陈旧天数/劣质阈值/TTL 开关与批量）
  2. 造语料：近重复对（两篇正文高度相似）、劣质切片、陈旧文档、超期回收站文档、
     未超期回收站文档、停用文档
  3. 巡检 dryRun：只报告不落库
  4. **巡检真实跑**：三类问题全部检出并开单（DUPLICATE / STALE / LOW_QUALITY）
  5. **工单幂等**：同文档同类型已有 OPEN 工单时再跑不重复开单
  6. 工单列表：OPEN 排在非 OPEN 之前、openCount、按 type 过滤
  7. **执行化 DUPLICATE → 软删**（状态变 RECYCLED、向量物理移除、审计 TICKET_CONFIRM）
  8. **执行化 STALE → 停用**（状态变 DISABLED）
  9. 已处理工单再次 confirm 被拒（工单是一次性的）
 10. **dismiss + 静默期**：忽略后文档未变化时，下一轮巡检不再为它开单
 11. **TTL**：dryRun 只报告 → 真实执行三清（向量/备份/源文件/账本全清 + TTL_PURGE 审计）；
     **停用文档绝不被 TTL 碰**；未超期的回收站文档不动
 12. 边界：非法 status/type 枚举、不存在的工单 id
 13. 清理测试数据与测试工单

两个刻意的设计（都是踩过才写的）：
  - **近重复语料靠"正文改几个字"而不是"改标题"**：L2 切片指纹算的是纯正文
    （`Chunk.body`，不含标题前缀），只改标题会被判成"全部切片重复 → 整篇拦截"。
    脚本还会先**直接调用检测函数**验证相似度确实 ≥ 阈值，避免"语料没造够"
    被误读成"产品检测失灵"。
  - **劣质切片用直改 chunks 表构造**：切片器带短段合并 + `<40 字符丢弃`
    （`min_section`），正常上传产不出低于 `min_section` 的切片（实测库里最短 67 字符）。
    阈值已于 2026-09-18 由 10 调到 50（`gov_low_quality_chars`），落在 `min_section`(40)
    与 `min_chunk`(100) 之间，代表"双向合并后仍偏短"的段落——这类段落真实上传可能出现，
    但现网语料尚未命中，故仍靠构造状态验证检测链路。

运行（推荐先起隔离服务；脚本会拒绝打产品库）：
    python scripts/serve_isolated.py --port 8001                     # 终端 A
    python scripts/gov_g4_e2e.py --base http://127.0.0.1:8001        # 终端 B

数据目录隔离见 app/core/test_guard.py：脚本进程侧与目标服务侧都必须隔离，
任一指向产品目录都会被拒绝运行（`--allow-product-db` 可显式放行，但会写产品库）。
"""

from __future__ import annotations

import argparse
import sqlite3
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

from app.governance.store import days_ago_iso                        # noqa: E402
from app.services.governance_service import governance_service        # noqa: E402
from app.services.kb_backup import kb_backups                        # noqa: E402
from app.services.kb_service import kb_service                        # noqa: E402

# 服务间鉴权（S-1）：本脚本直连 Python 服务，必须带 X-Agent-Key
from _agent_auth import key_headers                                    # noqa: E402

STAMP = time.strftime("%H%M%S")
OWNER = "gov_g4_e2e"
PREFIX = "g4e2e-"
DB_PATH = DATA_DIR / "governance/governance.db"   # 随隔离数据目录走（不再硬编码产品路径）

DOC_DUP_A = f"{PREFIX}{STAMP}-dupa"
DOC_DUP_B = f"{PREFIX}{STAMP}-dupb"
DOC_LOW = f"{PREFIX}{STAMP}-low"
DOC_STALE = f"{PREFIX}{STAMP}-stale"
DOC_TTL = f"{PREFIX}{STAMP}-ttl"
DOC_KEEP = f"{PREFIX}{STAMP}-keep"
DOC_DIS = f"{PREFIX}{STAMP}-dis"
TEST_DOCS = [DOC_DUP_A, DOC_DUP_B, DOC_LOW, DOC_STALE, DOC_TTL, DOC_KEEP, DOC_DIS]

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = f"{name}{(' | ' + extra) if extra else ''}"
    (PASS if cond else FAIL).append(line)
    print(f"  [{'PASS' if cond else 'FAIL'}] {line}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 语料构造

# 近重复对：正文只在少数词上不同（同长度 → 切片边界也一致），
# 余弦相似度会很高，但**切片指纹不同**，因此不会被 L2 整篇拦截
BODY_A = ("无人机巡检作业应当在风速小于八米每秒的条件下进行，"
          "操作人员须持有有效执照并完成飞行前检查。") * 30
BODY_B = ("无人机巡检作业须在风速小于八米每秒的条件下开展，"
          "操作人员必须持有有效执照并完成飞行前检查。") * 30


def content_dup(tag: str, body: str) -> str:
    return (
        f"# 无人机巡检作业规范（用例 {STAMP}）\n\n"
        f"## 一、作业要求\n\n{body}\n\n"
        f"## 二、执行细则\n\n{body[:600]}\n\n"
        f"本用例语料标识：{STAMP}，变体：{tag}。\n"
    )


def content_plain(name: str, filler: str) -> str:
    body = f"{filler}相关说明与操作规程。" * 40
    return (
        f"# 无人机{filler}规范（用例 {STAMP}）\n\n"
        f"## 一、总体要求\n\n{body}\n\n"
        f"## 二、执行细则\n\n{body[:500]}\n\n"
        f"本用例语料标识：{STAMP}。语料名称：{name}。\n"
    )


def main() -> int:  # noqa: C901  线性验收脚本，分段直读更清楚
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
    dsn = sqlite3.connect(str(DB_PATH))

    def upload(doc_id: str, content: str) -> dict:
        return cli.post(f"{v1}/kb/documents", json={
            "fileName": f"{doc_id}.md", "content": content,
            "owner": OWNER, "category": "e2e-g4", "visibility": "public",
        }).json()

    def gov(path: str, method: str = "GET", **params):
        fn = cli.post if method == "POST" else cli.get
        return fn(f"{v1}/kb/governance/{path}", params=params).json()

    def kb(path: str, method: str = "POST", **params):
        fn = {"POST": cli.post, "DELETE": cli.delete, "GET": cli.get}[method]
        return fn(f"{v1}/kb/{path}", params=params).json()

    def tickets(**params) -> dict:
        return (gov("tickets", **params).get("data") or {})

    def ticket_of(doc_id: str, ttype: str) -> dict | None:
        """从工单列表里找该文档该类型的 OPEN 工单（不假设 id，按实际数据取）。"""
        data = tickets(status="OPEN", type=ttype, pageSize=200)
        for t in (data.get("items") or []):
            if str(t.get("doc_id")) == doc_id:
                return t
        return None

    def audit_of(action: str) -> list[dict]:
        data = (gov("audit", action=action, pageSize=200).get("data") or {})
        return [r for r in (data.get("items") or [])]

    def ledger_count() -> int:
        return int(dsn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])

    def open_tickets() -> int:
        return int(dsn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'OPEN'").fetchone()[0])

    # ---------------- 1. 配置 ----------------
    section("1. G4 配置就位")
    cfg = (gov("config").get("data") or {})
    check("巡检开关默认开启", cfg.get("inspectionEnabled") is True, str(cfg.get("inspectionEnabled")))
    check("陈旧阈值 = 180 天", int(cfg.get("staleDays") or 0) == 180, str(cfg.get("staleDays")))
    # 劣质切片阈值必须落在切片器的 min_section(40) 与 min_chunk(100) 之间，否则对真实
    # 上传永不命中（<40 的段落被切片器直接丢弃、<100 的先尝试合并）。2026-09-18 由 10 → 50。
    LOW_CHARS = int(cfg.get("lowQualityChars") or 0)
    check("劣质切片阈值 = 50 字符（落在 40~100 之间才对真实上传生效）", LOW_CHARS == 50,
          str(cfg.get("lowQualityChars")))
    check("TTL 开关默认开启", cfg.get("ttlEnabled") is True, str(cfg.get("ttlEnabled")))
    tasks = (gov("tasks").get("data") or {}).get("tasks") or []
    names = {t.get("name") for t in tasks}
    check("常驻任务含 ttl_purge 与 inspection_scan",
          {"ttl_purge", "inspection_scan"} <= names, str(sorted(names)))

    base_ledger = ledger_count()
    base_open = open_tickets()
    print(f"  基线：账本 {base_ledger} 篇 / OPEN 工单 {base_open} 条")

    # ---------------- 2. 造语料 ----------------
    section("2. 造测试语料")
    r = upload(DOC_DUP_A, content_dup("A", BODY_A))
    check("上传近重复对 A 成功", r.get("code") == 2000, str(r.get("message"))[:70])
    r = upload(DOC_DUP_B, content_dup("B", BODY_B))
    check("上传近重复对 B 成功（未被 L2 整篇拦截）", r.get("code") == 2000,
          str(r.get("message"))[:70])
    for doc_id, filler in ((DOC_LOW, "载荷挂载"), (DOC_STALE, "地面站通信"),
                           (DOC_TTL, "夜间飞行"), (DOC_KEEP, "应急处置"),
                           (DOC_DIS, "电池维护")):
        r = upload(doc_id, content_plain(doc_id, filler))
        check(f"上传 {doc_id} 成功", r.get("code") == 2000, str(r.get("message"))[:60])

    # ---------------- 3. 构造特殊状态 ----------------
    section("3. 构造特殊状态（回拨时间 / 构造短切片）")
    # 3.1 陈旧：updated_at 回拨到 200 天前（阈值 180 天）
    dsn.execute("UPDATE docs SET updated_at = ? WHERE doc_id = ?",
                (days_ago_iso(200), DOC_STALE))
    dsn.commit()
    check("陈旧文档 updated_at 已回拨至 200 天前",
          str(governance_service.get_doc(DOC_STALE).get("updated_at")) <= days_ago_iso(180))
    # 3.2 劣质：把切片长度改成"阈值 - 5"——落在 min_section(40) 与阈值(50) 之间，
    # 代表真实上传里"双向合并后仍偏短"的段落；同样只能靠构造（现网语料最短 67 字符）
    SHORT_LEN = max(1, LOW_CHARS - 5)
    dsn.execute("UPDATE chunks SET char_len = ? WHERE doc_id = ? AND chunk_index = 0",
                (SHORT_LEN, DOC_LOW))
    dsn.commit()
    short = dsn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ? AND char_len < ?",
                        (DOC_LOW, LOW_CHARS)).fetchone()[0]
    check(f"已构造出 <{LOW_CHARS} 字符的切片", int(short) >= 1, f"n={short}")
    # 3.3 超期回收站：软删 + recycled_at 回拨 31 天（TTL 30 天）
    d = kb(f"documents/{DOC_TTL}", method="DELETE", owner=OWNER, is_admin="true")
    check("超期用例已软删", d.get("code") == 2000, str(d.get("message"))[:60])
    dsn.execute("UPDATE docs SET recycled_at = ? WHERE doc_id = ?",
                (days_ago_iso(31), DOC_TTL))
    dsn.commit()
    # 3.4 未超期回收站：软删但不动 recycled_at
    d = kb(f"documents/{DOC_KEEP}", method="DELETE", owner=OWNER, is_admin="true")
    check("未超期用例已软删", d.get("code") == 2000, str(d.get("message"))[:60])
    # 3.5 停用：不得被 TTL 触碰
    d = gov(f"doc/{DOC_DIS}/disable", method="POST", owner=OWNER, is_admin="true")
    check("停用用例已停用", d.get("code") == 2000, str(d.get("message"))[:60])
    check("停用备份已在停用区落盘", kb_backups.path_of(DOC_DIS, "DISABLED").exists())

    # ---------------- 4. 检测函数自证（避免"语料没造够"被误读成产品失灵）----------------
    section("4. L3 检测函数自证（近重复对确实超阈值）")
    try:
        hits = kb_service.l3_scan_active(
            doc_ids=[DOC_DUP_A, DOC_DUP_B], threshold=0.92, samples=3)
        sim = float((hits.get(DOC_DUP_A) or hits.get(DOC_DUP_B) or {}).get("maxSimilarity") or 0.0)
    except Exception as e:  # noqa: BLE001
        sim = 0.0
        print(f"  [WARN] 直接调用检测函数异常：{type(e).__name__}: {e}")
    check("近重复对余弦相似度 ≥ 0.92（语料构造有效）", sim >= 0.92, f"similarity={sim}")

    # ---------------- 5. 巡检 dryRun ----------------
    section("5. 巡检 dryRun（只报告不落库）")
    rep = (gov("inspection/scan", method="POST", dryRun="true", operator=OWNER).get("data") or {})
    check("dryRun 标记为真", rep.get("dryRun") is True)
    det = rep.get("detected") or {}
    dup_ids = {d.get("docId") for d in (det.get("duplicate") or [])}
    stale_ids = {d.get("docId") for d in (det.get("stale") or [])}
    low_ids = {d.get("docId") for d in (det.get("lowQuality") or [])}
    check("检出近重复（含测试对）", bool(dup_ids & {DOC_DUP_A, DOC_DUP_B}), str(sorted(dup_ids))[:80])
    check("检出陈旧文档", DOC_STALE in stale_ids, str(sorted(stale_ids))[:80])
    check("检出劣质切片文档", DOC_LOW in low_ids, str(sorted(low_ids))[:80])
    check("dryRun 未落库（OPEN 工单数不变）", open_tickets() == base_open,
          f"before={base_open} after={open_tickets()}")

    # ---------------- 6. 巡检真实跑 ----------------
    section("6. 巡检真实跑（检出 → 开单）")
    rep = (gov("inspection/scan", method="POST", operator=OWNER).get("data") or {})
    opened = rep.get("opened") or []
    open_docs = {(o.get("docId"), o.get("type")) for o in opened}
    check("为近重复对开出 DUPLICATE 工单",
          any(t == "DUPLICATE" and d in (DOC_DUP_A, DOC_DUP_B) for d, t in open_docs),
          str(sorted(open_docs))[:100])
    check("为陈旧文档开出 STALE 工单", (DOC_STALE, "STALE") in open_docs)
    check("为劣质切片开出 LOW_QUALITY 工单", (DOC_LOW, "LOW_QUALITY") in open_docs)
    check("巡检报告无 errors（三类都查成了）", not rep.get("errors"), str(rep.get("errors"))[:120])
    check("OPEN 工单数确实增加", open_tickets() > base_open,
          f"{base_open} → {open_tickets()}")
    check("审计含 TICKET_OPEN", bool(audit_of("TICKET_OPEN")), f"n={len(audit_of('TICKET_OPEN'))}")

    real = [t for t in (tickets(status="OPEN", pageSize=200).get("items") or [])
            if not str(t.get("doc_id", "")).startswith(PREFIX)]
    if real:
        print(f"  [INFO] 巡检同时为 {len(real)} 篇非测试文档开了单（这是正常产出，未清理）："
              f"{[t.get('doc_id') for t in real][:5]}")

    # ---------------- 7. 幂等 ----------------
    section("7. 工单幂等（再跑不重复开单）")
    before_open = open_tickets()
    rep2 = (gov("inspection/scan", method="POST", operator=OWNER).get("data") or {})
    same = [s for s in (rep2.get("skipped") or [])
            if str(s.get("reason", "")).startswith("已存在")]
    check("第二轮未新增任何工单", open_tickets() == before_open,
          f"{before_open} → {open_tickets()}")
    check("跳过原因含「已存在同类型待办工单」", bool(same), str(same[:2])[:100])

    # ---------------- 8. 工单列表 ----------------
    section("8. 工单列表与过滤")
    data = tickets(pageSize=200)
    items = data.get("items") or []
    check("openCount 与 OPEN 工单数一致",
          int(data.get("openCount") or 0) == open_tickets(),
          f"openCount={data.get('openCount')} db={open_tickets()}")
    statuses = [str(t.get("status")) for t in items]
    first_non_open = next((i for i, s in enumerate(statuses) if s != "OPEN"), len(statuses))
    check("OPEN 排在最前（其后无 OPEN）",
          all(s == "OPEN" for s in statuses[:first_non_open]),
          f"前 {first_non_open} 条为 OPEN")
    only_dup = tickets(type="DUPLICATE", pageSize=200).get("items") or []
    check("按 type 过滤生效", all(str(t.get("ticket_type")) == "DUPLICATE" for t in only_dup),
          f"n={len(only_dup)}")

    # ---------------- 9. 执行化：DUPLICATE → 软删 ----------------
    section("9. 执行化 DUPLICATE → 软删（可恢复）")
    tk = ticket_of(DOC_DUP_A, "DUPLICATE") or ticket_of(DOC_DUP_B, "DUPLICATE")
    check("找到 DUPLICATE 工单", tk is not None, str(tk))
    if tk:
        target = str(tk.get("doc_id"))
        ok = gov(f"tickets/{tk['id']}/confirm", method="POST", operator=OWNER)
        check("确认执行返回成功", ok.get("code") == 2000, str(ok.get("message"))[:70])
        body = ok.get("data") or {}
        check("执行动作是软删", body.get("action") == "SOFT_DELETE", str(body.get("action")))
        check("执行后状态为 RECYCLED", body.get("state") == "RECYCLED", str(body.get("state")))
        check("工单状态已变 DONE",
              str(governance_service.store.get_ticket(int(tk["id"])).get("status")) == "DONE")
        gdoc = governance_service.get_doc(target) or {}
        check("文档治理状态确为 RECYCLED", str(gdoc.get("state")) == "RECYCLED",
              str(gdoc.get("state")))
        check("向量已物理移除", kb_service.query_count(target) == 0,
              f"count={kb_service.query_count(target)}")
        check("软删可恢复（回收站有该文档的当前版本备份）",
              kb_backups.read(target, "RECYCLED") is not None)
        aud = audit_of("TICKET_CONFIRM")
        check("审计含 TICKET_CONFIRM 且记了执行的工单", bool(aud), f"n={len(aud)}")

    # ---------------- 10. 执行化：STALE → 停用 ----------------
    section("10. 执行化 STALE → 停用（可启用）")
    tk = ticket_of(DOC_STALE, "STALE")
    check("找到 STALE 工单", tk is not None, str(tk))
    if tk:
        ok = gov(f"tickets/{tk['id']}/confirm", method="POST", operator=OWNER)
        body = ok.get("data") or {}
        check("确认执行返回成功", ok.get("code") == 2000, str(ok.get("message"))[:70])
        check("执行动作是停用", body.get("action") == "DISABLE", str(body.get("action")))
        check("执行后状态为 DISABLED", body.get("state") == "DISABLED", str(body.get("state")))
        check("停用区已有备份（可启用回来）",
              kb_backups.read(DOC_STALE, "DISABLED") is not None)
        check("向量已物理移除", kb_service.query_count(DOC_STALE) == 0,
              f"count={kb_service.query_count(DOC_STALE)}")

    # ---------------- 11. 工单是一次性的 ----------------
    section("11. 已处理工单不可重复执行")
    tk = ticket_of(DOC_LOW, "LOW_QUALITY")
    if tk:
        gov(f"tickets/{tk['id']}/dismiss", method="POST", operator=OWNER,
            note="验收用：先忽略再试重复处理")
        again = gov(f"tickets/{tk['id']}/dismiss", method="POST", operator=OWNER)
        check("已忽略的工单再次忽略被拒", again.get("code") == 3001,
              f"code={again.get('code')}")
        confirm_again = gov(f"tickets/{tk['id']}/confirm", method="POST", operator=OWNER)
        check("已处理的工单再确认执行被拒", confirm_again.get("code") == 3001,
              f"code={confirm_again.get('code')}")
        check("拒绝信息说明状态不符",
              "状态" in str(confirm_again.get("message")), str(confirm_again.get("message"))[:70])
        check("审计含 TICKET_DISMISS", bool(audit_of("TICKET_DISMISS")))

    # ---------------- 12. 忽略即静默 ----------------
    section("12. 忽略即静默（文档未变化则不再开单）")
    if tk:
        # 上面已把 LOW 的工单忽略，此时无 OPEN 工单；文档未变化 → 巡检不应再开单
        rep3 = (gov("inspection/scan", method="POST", operator=OWNER).get("data") or {})
        reopened = [o for o in (rep3.get("opened") or [])
                    if str(o.get("docId")) == DOC_LOW and o.get("type") == "LOW_QUALITY"]
        skipped_low = [s for s in (rep3.get("skipped") or [])
                       if str(s.get("docId")) == DOC_LOW and "忽略" in str(s.get("reason"))]
        check("被忽略的问题不再重复开单", not reopened, str(reopened))
        check("跳过原因说明「用户已忽略且文档此后未变化」", bool(skipped_low),
              str(skipped_low[:1])[:100])

    # ---------------- 13. TTL ----------------
    section("13. TTL 到期彻底删除")
    dry = (gov("recycle-bin/purge-expired", method="POST", dryRun="true",
               operator=OWNER).get("data") or {})
    cands = set(dry.get("candidates") or [])
    check("dryRun 报告含超期文档", DOC_TTL in cands, str(sorted(cands))[:100])
    check("dryRun 不含未超期文档", DOC_KEEP not in cands, str(sorted(cands))[:100])
    check("dryRun 未真正执行（文档仍在账本）",
          governance_service.get_doc(DOC_TTL) is not None)
    check("dryRun 不含停用文档", DOC_DIS not in cands, str(sorted(cands))[:100])

    run = (gov("recycle-bin/purge-expired", method="POST", operator=OWNER).get("data") or {})
    purged_ids = {p.get("docId") for p in (run.get("purged") or [])}
    check("TTL 已清理超期文档", DOC_TTL in purged_ids, str(sorted(purged_ids))[:100])
    check("账本已无该文档", governance_service.get_doc(DOC_TTL) is None)
    check("备份已清除", not kb_backups.path_of(DOC_TTL, "RECYCLED").exists()
          and not kb_backups.path_of(DOC_TTL, "RECYCLED", 1).exists())
    src = list((DATA_DIR / "kb/raw/uploads").glob(f"{DOC_TTL}.*"))
    check("源文件已清除", not src, str(src))
    check("向量已清除", kb_service.query_count(DOC_TTL) == 0)
    check("未超期文档未被清理",
          str((governance_service.get_doc(DOC_KEEP) or {}).get("state")) == "RECYCLED")
    check("**停用文档未被 TTL 触碰**（备份仍在）",
          kb_backups.path_of(DOC_DIS, "DISABLED").exists()
          and str((governance_service.get_doc(DOC_DIS) or {}).get("state")) == "DISABLED")
    ttl_audit = audit_of("TTL_PURGE")
    check("审计含 TTL_PURGE 且记录已清条目", bool(ttl_audit), f"n={len(ttl_audit)}")
    check("TTL 报告无 failed", not (run.get("failed") or []), str(run.get("failed"))[:80])
    check("TTL 报告无未清干净的警告",
          all(not (p.get("warnings") or []) for p in (run.get("purged") or [])),
          str([p.get("warnings") for p in (run.get("purged") or [])])[:100])

    # ---------------- 14. 边界 ----------------
    section("14. 边界与参数校验")
    bad = gov("tickets", status="BOGUS")
    check("非法 status 被拒", bad.get("code") == 3001, f"code={bad.get('code')}")
    bad2 = gov("tickets", type="BOGUS")
    check("非法 type 被拒", bad2.get("code") == 3001, f"code={bad2.get('code')}")
    miss = gov("tickets/999999/confirm", method="POST", operator=OWNER)
    check("不存在的工单被拒", miss.get("code") == 3002, f"code={miss.get('code')}")

    # ---------------- 15. 清理 ----------------
    section("15. 清理测试数据")
    for doc_id in TEST_DOCS:
        doc = governance_service.get_doc(doc_id)
        if doc is None:
            continue
        state = str(doc.get("state") or "")
        if state == "ACTIVE":
            kb(f"documents/{doc_id}", method="DELETE", owner=OWNER, is_admin="true")
        rec = (kb_backups.read(doc_id, "RECYCLED")
               or kb_backups.read(doc_id, "DISABLED") or {})
        name = rec.get("docTitle") or doc_id
        pg = kb(f"recycle/{doc_id}", method="DELETE", owner=OWNER, is_admin="true",
                confirm="true", confirmDocName=name)
        if pg.get("code") != 2000:
            print(f"  [WARN] 清理 {doc_id} 失败：{pg.get('message')}")
    dsn.execute("DELETE FROM tickets WHERE doc_id LIKE ?", (f"{PREFIX}%",))
    dsn.commit()
    dsn.close()

    left_ledger = [d for d in (governance_service.all_doc_briefs() or [])
                   if str(d.get("doc_id", "")).startswith(PREFIX)]
    check("测试文档已从账本清干净", not left_ledger, str(left_ledger))
    check("测试工单已清理", int(sqlite3.connect(str(DB_PATH)).execute(
        "SELECT COUNT(*) FROM tickets WHERE doc_id LIKE ?", (f"{PREFIX}%",)).fetchone()[0]) == 0)
    check("测试备份文件已清理",
          not any(kb_backups.path_of(d, s).exists()
                  for d in TEST_DOCS for s in ("RECYCLED", "DISABLED")))
    check("测试源文件已清理",
          not any(list((DATA_DIR / "kb/raw/uploads").glob(f"{d}.*")) for d in TEST_DOCS))

    print("\n" + "=" * 68)
    print(f"G4 验收：通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

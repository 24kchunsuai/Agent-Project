# -*- coding: utf-8 -*-
"""G1 治理基座验收（E2E）。

覆盖（对应 docs/specs/语料治理规约.md §8 G1 验收标准）：
  1. 治理开关与配置就位
  2. 正常上传：返回文件指纹，无重复、无复用
  3. **L1 拦截**：同内容重复上传（同名 / 异名都一样，L1 按内容指纹比对）
  4. **L2 拦截**：内容仅 frontmatter 不同（文件指纹不同、切片全同）→ 整篇拦截，
     且明确报告"跳过了 N 次 embedding"（L2 门在 embedding 之前）
  5. **部分重复**：原文 + 新增章节 → 入库成功，重复切片复用向量、不调 embedding
  6. 审计：UPLOAD / DEDUP_INTERCEPT / SOFT_DELETE / RESTORE / PURGE 全留痕，支持过滤分页
  7. 状态流转：软删 → RECYCLED、恢复 → ACTIVE、彻底删 → 登记清除但审计保留
  8. 存量回填：治理库已有历史语料登记与冗余率数据

G2 后的两处语义变更（本脚本已同步，详见 §15）：
  - 恢复先过 **L3 语义查重确认门**：命中相似语料时返回 3001 + requireConfirm（未执行恢复），
    脚本自动带 `confirm=true` 重发；恢复方式由"重新切片"变为"**按原切片回灌**（零 embedding）"
  - 彻底删为**管理员专属 + 双确认**（`confirm=true` + `confirmDocName` 复述），
    故调用参数由 `isAdmin`（早期拼错、一直被 FastAPI 忽略）改为 `is_admin`

运行（推荐先起隔离服务；脚本会拒绝打产品库）：
    python scripts/serve_isolated.py --port 8001                     # 终端 A
    python scripts/gov_g1_e2e.py --base http://127.0.0.1:8001        # 终端 B

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
    data_dir_isolated,
    isolate,
    warn_if_collection_dirty,
)
DATA_DIR = isolate(follow=base_from_argv())   # 跟随目标服务的数据目录，保证与服务同一份数据

# 服务间鉴权（S-1）：本脚本直连 Python 服务，必须带 X-Agent-Key
from _agent_auth import key_headers  # noqa: E402

STAMP = time.strftime("%H%M%S")
OWNER = "gov_e2e"
ISOLATED = False                              # main() 里据目标服务实际情况设定
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


DOC_A_ID = f"g1e2e-{STAMP}-alpha"
CONTENT_A = (
    "# 无人机航线规划要点\n\n"
    + para("航线规划")
    + "\n\n## 二、气象条件评估\n\n"
    + para("气象评估")
    + "\n\n## 三、应急处置流程\n\n"
    + para("应急处置")
)
CONTENT_A_DUP = CONTENT_A                                  # 完整重复 → L1
CONTENT_A_FM = "---\ntitle: 别名副本\ncategory: 测试\n---\n" + CONTENT_A
CONTENT_A_PLUS = (
    CONTENT_A
    + "\n\n## 四、新增章节：集群编队协同\n\n"
    + ("集群编队协同控制策略与通信链路冗余设计说明。" * 20)
)
DOC_C_ID = f"g1e2e-{STAMP}-gamma"


def main() -> int:
    global ISOLATED
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--allow-product-db", action="store_true",
                    help="显式放行打产品服务（会写产品库！默认拒绝，应先起隔离服务）")
    args = ap.parse_args()

    # 服务侧守卫：目标服务也必须跑在隔离数据目录上（见 app/core/test_guard.py）
    assert_remote_isolated(args.base, allow_product=args.allow_product_db)
    # 起点清洁度：集合非空时先告警（上一轮残留会让"对账/无未知文档"类断言假失败）
    warn_if_collection_dirty(args.base)
    ISOLATED = data_dir_isolated()

    # trust_env=False：忽略系统 HTTP_PROXY（本机代理会劫持 localhost 请求）
    cli = httpx.Client(base_url=args.base, timeout=180.0, trust_env=False,
                       headers=key_headers())
    v1 = "/v1"

    def upload(doc_id: str, content: str, category: str = "e2e") -> dict:
        r = cli.post(f"{v1}/kb/documents", json={
            "fileName": f"{doc_id}.md", "content": content,
            "owner": OWNER, "category": category, "visibility": "public",
        })
        return r.json()

    def gov(path: str, **params) -> dict:
        return cli.get(f"{v1}/kb/governance/{path}", params=params).json()

    def list_docs() -> list[str]:
        r = cli.get(f"{v1}/kb/documents", params={"owner": OWNER, "scope": "all",
                                                  "pageSize": 100}).json()
        return [i["docId"] for i in (r.get("data") or {}).get("items", [])]

    def audits(action: str = "", doc_id: str = "", page_size: int = 50) -> list[dict]:
        p = {"pageSize": page_size}
        if action:
            p["action"] = action
        if doc_id:
            p["docId"] = doc_id
        r = gov("audit", **p).get("data") or {}
        return r.get("items", [])

    base_docs = set(list_docs())

    # ---------------- 1. 治理配置 ----------------
    section("1. 治理开关与配置")
    cfg = gov("config").get("data") or {}
    check("治理已启用", cfg.get("enabled") is True, f"enabled={cfg.get('enabled')}")
    check("L1 去重门开启", cfg.get("l1Enabled") is True)
    check("L2 去重门开启", cfg.get("l2Enabled") is True)
    check("向量复用开启", cfg.get("reuseVector") is True)
    check("治理库路径在 data/governance 下",
          "governance" in str(cfg.get("dbPath", "")), str(cfg.get("dbPath")))

    # ---------------- 2. 正常上传 ----------------
    section("2. 正常上传（无重复）")
    res = upload(DOC_A_ID, CONTENT_A)
    d = res.get("data") or {}
    check("上传成功", res.get("code") == 2000, f"code={res.get('code')} msg={res.get('message')}")
    check("返回文件指纹（fileHash）", bool(d.get("fileHash")))
    check("首传无重复切片", d.get("duplicateChunks") == 0, f"dup={d.get('duplicateChunks')}")
    check("首传无向量复用", d.get("reusedVectors") == 0)
    check("返回版本号", d.get("versionNo") == 1, f"v={d.get('versionNo')}")
    chunk_n = int(d.get("chunkCount") or 0)
    check("切片数 > 1（多小节文档）", chunk_n > 1, f"chunks={chunk_n}")

    # ---------------- 3. L1 拦截 ----------------
    section("3. L1 文件指纹拦截")
    r1 = upload(DOC_A_ID, CONTENT_A_DUP)
    d1 = r1.get("data") or {}
    check("同文件同内容被拦截", r1.get("code") != 2000, f"code={r1.get('code')}")
    check("拦截层为 L1", d1.get("layer") == "L1", f"layer={d1.get('layer')}")
    check("返回已存在文档 id", bool(d1.get("matchedDocId")), str(d1.get("matchedDocId")))
    # G3 起，同一个 doc_id 重传同一内容走的是**另一条文案**："内容与当前版本（vN）完全相同，
    # 未产生新版本"——比原来的"与已有语料「自己」内容完全相同"更准确（对用户而言，
    # "你要覆盖的那一篇没变"和"库里别处有重复"是两件事）。分层信息仍由 data.layer 表达。
    check("提示语说明本次未产生新版本",
          "未产生新版本" in str(r1.get("message"))
          or "L1" in str(r1.get("message")),
          str(r1.get("message"))[:70])

    other_id = f"g1e2e-{STAMP}-beta"
    r2 = upload(other_id, CONTENT_A_DUP)
    d2 = r2.get("data") or {}
    check("异名同内容同样被 L1 拦截（指纹按内容而非文件名）",
          r2.get("code") != 2000 and d2.get("layer") == "L1", f"code={r2.get('code')}")

    # ---------------- 4. L2 切片指纹整篇拦截 ----------------
    section("4. L2 切片指纹整篇拦截（L1 放行、L2 命中）")
    r3 = upload(f"g1e2e-{STAMP}-fmcopy", CONTENT_A_FM)
    d3 = r3.get("data") or {}
    check("仅 frontmatter 不同 → 被拦截", r3.get("code") != 2000, f"code={r3.get('code')}")
    check("拦截层为 L2", d3.get("layer") == "L2", f"layer={d3.get('layer')}")
    check("报告跳过的 embedding 次数 = 切片数",
          d3.get("embeddingSkipped") == chunk_n,
          f"skipped={d3.get('embeddingSkipped')} chunks={chunk_n}")

    # ---------------- 5. 部分重复 → 复用向量 ----------------
    section("5. 部分重复：重复切片复用向量，不调 embedding")
    t0 = time.time()
    r4 = upload(DOC_C_ID, CONTENT_A_PLUS)
    d4 = r4.get("data") or {}
    check("部分重复上传成功", r4.get("code") == 2000,
          f"code={r4.get('code')} msg={r4.get('message')}")
    dup_n = int(d4.get("duplicateChunks") or 0)
    reuse_n = int(d4.get("reusedVectors") or 0)
    check("检出重复切片", dup_n > 0, f"duplicateChunks={dup_n}")
    check("重复切片全部复用已有向量（0 次 embedding）",
          reuse_n == dup_n and reuse_n > 0, f"reused={reuse_n}/{dup_n}")
    check("新增切片占比合理", dup_n < int(d4.get("chunkCount") or 0),
          f"dup={dup_n} total={d4.get('chunkCount')}")
    print(f"       （本次上传耗时 {round(time.time() - t0, 1)}s，仅新增切片走了向量化）")

    # ---------------- 6. 审计 ----------------
    section("6. 审计留痕与查询")
    au = audits(doc_id=DOC_A_ID)
    acts = [a["action"] for a in au]
    check("审计含 UPLOAD", "UPLOAD" in acts, f"actions={acts}")
    check("审计含 DEDUP_INTERCEPT（L1）",
          any(a["action"] == "DEDUP_INTERCEPT" and '"L1"' in str(a.get("detail"))
              for a in au))
    all_dedup = audits(action="DEDUP_INTERCEPT")
    check("审计可按 action 过滤", all(len(a["action"]) and a["action"] == "DEDUP_INTERCEPT"
                                    for a in all_dedup) and len(all_dedup) >= 2,
          f"count={len(all_dedup)}")
    check("审计含 L2 拦截记录",
          any('"L2"' in str(a.get("detail")) for a in all_dedup))
    up = [a for a in audits(doc_id=DOC_C_ID) if a["action"] == "UPLOAD"]
    check("UPLOAD 审计记录复用统计",
          bool(up) and '"reusedVectors"' in str(up[0].get("detail")), "")

    # ---------------- 7. 状态流转 ----------------
    section("7. 状态流转：软删 / 恢复 / 彻底删")
    del_res = cli.delete(f"{v1}/kb/documents/{DOC_A_ID}",
                         params={"owner": OWNER, "is_admin": "true"}).json()
    check("软删成功", del_res.get("code") == 2000, str(del_res.get("message"))[:60])
    rec = cli.get(f"{v1}/kb/recycle", params={"owner": OWNER, "scope": "all"}).json()
    check("回收站可见", DOC_A_ID in [i["docId"] for i in (rec.get("data") or {}).get("items", [])])
    rl = gov("docs", state="RECYCLED").get("data") or {}
    check("治理登记状态为 RECYCLED",
          DOC_A_ID in [i["doc_id"] for i in rl.get("items", [])], "")
    check("审计含 SOFT_DELETE", "SOFT_DELETE" in [a["action"] for a in audits(doc_id=DOC_A_ID)])

    # 恢复（G2 起先过 L3 语义查重确认门：命中相似语料时返回 3001 + requireConfirm，未执行恢复；
    # 脚本对"门是否命中"都稳健 —— 命中了就带 confirm 重发）
    res_r = cli.post(f"{v1}/kb/recycle/{DOC_A_ID}/restore",
                     params={"owner": OWNER, "is_admin": "true"}).json()
    rd = res_r.get("data") or {}
    if rd.get("requireConfirm"):
        print(f"       （L3 确认门命中：maxSimilarity={rd.get('maxSimilarity')}，"
              f"相似 {len(rd.get('hits') or [])} 篇 → 带 confirm 重发）")
        res_r = cli.post(f"{v1}/kb/recycle/{DOC_A_ID}/restore",
                         params={"owner": OWNER, "is_admin": "true",
                                 "confirm": "true"}).json()
        rd = res_r.get("data") or {}
    check("恢复成功（内容已被别的文档覆盖时不被去重门卡死）",
          res_r.get("code") == 2000 and rd.get("restored") is True,
          f"code={res_r.get('code')} msg={str(res_r.get('message'))[:60]}")
    # G2 起恢复是"按原切片回灌"（备份里的 dense 直接写回，零 embedding），
    # 不再是 G1 的"重新切片 + 复用向量"
    # 注意用 == 0 而不是 int(x or -1)：embeddingCalls 本身就是 0，`0 or -1` 会得到 -1（falsy 陷阱）
    check("恢复走原片回灌：零 embedding、切片数与删除前一致",
          rd.get("restoreMode") == "writeback" and rd.get("embeddingCalls") == 0
          and int(rd.get("chunkCount") or 0) > 0,
          f"mode={rd.get('restoreMode')} embedding={rd.get('embeddingCalls')} "
          f"chunks={rd.get('chunkCount')}")
    al = gov("docs", state="ACTIVE", pageSize=200).get("data") or {}
    check("治理登记状态回到 ACTIVE",
          DOC_A_ID in [i["doc_id"] for i in al.get("items", [])],
          f"total={al.get('total')} 扫描={len(al.get('items', []))}")
    check("审计含 RESTORE", "RESTORE" in [a["action"] for a in audits(doc_id=DOC_A_ID)])

    # 恢复成功会消耗回收站记录，彻底删除需重新软删一次
    cli.delete(f"{v1}/kb/documents/{DOC_A_ID}",
               params={"owner": OWNER, "is_admin": "true"})
    # G2 起彻底删是双确认（管理员 + confirm + 文档名复述），缺一不执行
    pur = cli.delete(f"{v1}/kb/recycle/{DOC_A_ID}",
                     params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                             "confirmDocName": DOC_A_ID}).json()
    check("彻底删除成功", pur.get("code") == 2000, str(pur.get("message"))[:60])
    pur_data = pur.get("data") or {}
    check("彻底删除完成三清（向量 + 备份 + 源文件）",
          pur_data.get("purged") is True
          and int(pur_data.get("backupsRemoved") or 0) >= 1
          and "sourceRemoved" in pur_data and "vectorResidueRemoved" in pur_data,
          f"purged={pur_data.get('purged')} 备份={pur_data.get('backupsRemoved')} "
          f"源文件={pur_data.get('sourceRemoved')} 残留={pur_data.get('vectorResidueRemoved')}")
    rl2 = gov("docs", state="RECYCLED", pageSize=200).get("data") or {}
    check("治理登记行已清除（回收站视角无该文档）",
          DOC_A_ID not in [i["doc_id"] for i in rl2.get("items", [])])
    pur_audit = audits(action="PURGE", doc_id=DOC_A_ID)
    check("审计保留 PURGE 留痕（登记可删、审计不删）", len(pur_audit) >= 1,
          f"count={len(pur_audit)}")

    # ---------------- 8. 治理概览 + 回填报告 ----------------
    section("8. 治理概览与存量回填")
    ov = gov("overview").get("data") or {}
    check("概览含文档状态分布", bool(ov.get("docByState")), str(ov.get("docByState"))[:80])
    ck = ov.get("chunks") or {}
    check("概览含切片指纹冗余率", "duplicateRate" in ck,
          f"冗余率={ck.get('duplicateRate')} 行={ck.get('chunkRows')}")
    if ISOLATED:
        # 「存量回填 >2000 行」验的是**生产环境数据态**：产品库跑过一次性存量回填才有
        # 2000+ 行指纹，隔离集合从零开始必然只有本轮那几行（2026-09-18 实测 rows=5，
        # 曾长期挂一条无法修复的假失败）。隔离模式下改验"本轮语料确实进了指纹表"，
        # 断言总数不变；生产库上仍按 2000 行口径验收。
        print("  [注] 隔离模式：「存量回填 >2000 行」属生产数据态断言，本轮改为验"
              "「本轮语料已入指纹表」（生产库上仍按 2000 行口径）。")
        check("切片指纹表已回填本轮语料（隔离模式；生产口径为 >2000 行）",
              int(ck.get("chunkRows") or 0) > 0, f"rows={ck.get('chunkRows')}")
    else:
        check("存量语料已回填（切片指纹 > 2000 行）",
              int(ck.get("chunkRows") or 0) > 2000, f"rows={ck.get('chunkRows')}")
    check("概览含最近审计", len(ov.get("recentAudit") or []) > 0)

    # ---------------- 清理 ----------------
    section("9. 清理测试数据")
    cleanup_left: list[str] = []
    for did in (DOC_C_ID, DOC_A_ID, other_id):
        # 软删 → 彻底删（G2 双确认：管理员 + confirm + 复述 doc_id）
        cli.delete(f"{v1}/kb/documents/{did}", params={"owner": OWNER, "is_admin": "true"})
        cin = cli.delete(f"{v1}/kb/recycle/{did}",
                         params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                                 "confirmDocName": did}).json()
        # "不在回收站"是正常情形：alpha 已在第 7 步被彻底删、被 L1 拦下的 beta 从未入库
        msg = str(cin.get("message"))
        if cin.get("code") != 2000 and "没有该文档" not in msg and "不存在" not in msg:
            cleanup_left.append(f"{did}:{msg}")
    left = set(list_docs()) - base_docs
    check("测试文档已清理干净", not left and not cleanup_left,
          f"残留={sorted(left)} 清理失败={cleanup_left}")

    print("\n" + "=" * 68)
    print(f"G1 验收：通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""G3 版本链验收（E2E）。

覆盖（对应 docs/specs/语料治理规约.md §8 G3 验收标准）：
  1. G3 配置就位（版本链/快照/回滚确认门/回滚管理员/快照保留数）
  2. 基线：上传 v1，检索命中
  3. **同名覆盖 → v2**：旧版自动留成历史快照、版本号 +1、检索内容换成新版；
     **同 hash 同名重传仍被 L1 拦截**（不刷出无意义的同内容版本）
  4. **再次覆盖 → v3**：快照累积，版本链降序完整
  5. **回收站里的当前版本降级**：软删后再同名上传，回收站那条"当前版本"必须
     降级为历史快照（否则会留一个"名字与在库文档相同、恢复必然冲突"的僵尸条目）
  6. 版本链查询：降序、isCurrent / live / hasVectors 标注、total 一致
  7. **回滚确认门**：缺 confirm 被拒（返回待复述文档名，且**未执行回滚**）/
     复述名不符被拒 / 非管理员被拒
  8. **回滚执行**：零 embedding 回灌 → 内容回到目标版本、版本号升为历史最大值 +1、
     被换下的当前版本自动留档
  9. 回滚到**指定版本**（不是默认的"退回上一版"）
 10. 回滚不存在 / 回收站中的文档被拒
 11. **版本保留上限**：超限历史快照被修剪，且**绝不碰当前版本备份**
 12. 审计留痕：UPLOAD / VERSION_REPLACE / ROLLBACK
 13. 清理测试数据

运行（推荐先起隔离服务；脚本会拒绝打产品库）：
    python scripts/serve_isolated.py --port 8001                     # 终端 A
    python scripts/gov_g3_e2e.py --base http://127.0.0.1:8001        # 终端 B

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

from app.services.kb_backup import kb_backups                          # noqa: E402
from app.services.kb_service import kb_service                          # noqa: E402

# 服务间鉴权（S-1）：本脚本直连 Python 服务，必须带 X-Agent-Key
from _agent_auth import key_headers                                    # noqa: E402

STAMP = time.strftime("%H%M%S")
OWNER = "gov_g3_e2e"
ALL_MARKS: list[str] = []          # main() 里按 STAMP 填好，供 milvus_marker 使用
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = f"{name}{(' | ' + extra) if extra else ''}"
    (PASS if cond else FAIL).append(line)
    print(f"  [{'PASS' if cond else 'FAIL'}] {line}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 语料构造

def make_content(ver: str, mark: str, filler: str) -> str:
    """同一篇文档的第 N 版：带**唯一标记**（用于回灌后校验内容确实是那一版）。

    两个设计要点，都是踩过才定下来的：
    1. 标记必须**嵌在标题里**。切片器只把"标题 + 正文"写进 chunk，
       游离在标题之间的一行短文本会被丢弃——标记若放那儿，任何检索都找不到它，
       测出来的"内容没换"其实是**假失败**（实测踩到）
    2. 每版换掉主题词，确保 L2 切片指纹不命中（否则会被判为"重复切片、复用向量"，
       结果虽然正确，却测不出"回滚是否真的换了内容"）
    """
    body = f"{filler}相关说明与操作规程。" * 40
    return (
        f"# 无人机{filler}作业规范（用例编号 {STAMP}）\n\n"
        f"## 一、总体要求（{ver} / {mark}）\n\n{body}\n\n"
        f"## 二、执行细则（{ver} / {mark}）\n\n{body[:600]}\n\n"
        f"## 三、检查与记录（{ver} / {mark}）\n\n{body[:400]}\n\n"
        f"本用例语料标识：{STAMP}，版本：{ver}。\n"
    )


DOC_ID = f"g3e2e-{STAMP}-doc"
MARK_V1 = f"VERMARK-V1-{STAMP}"
MARK_V2 = f"VERMARK-V2-{STAMP}"
MARK_V3 = f"VERMARK-V3-{STAMP}"
MARK_V4 = f"VERMARK-V4-{STAMP}"

CONTENT_V1 = make_content("v1", MARK_V1, "航线规划")
CONTENT_V2 = make_content("v2", MARK_V2, "载荷挂载")
CONTENT_V3 = make_content("v3", MARK_V3, "地面站通信")
CONTENT_V4 = make_content("v4", MARK_V4, "夜间飞行")


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
            "owner": OWNER, "category": "e2e-g3", "visibility": "public",
        })
        return r.json()

    def gov(path: str, method: str = "GET", **params):
        fn = cli.post if method == "POST" else cli.get
        return fn(f"{v1}/kb/governance/{path}", params=params).json()

    def milvus_marker(doc_id: str) -> str:
        """**直查向量库**看该文档当前实际挂着哪个版本标记。

        刻意不用检索来判断"库里是哪一版"：检索有相关性筛选，命中与否取决于查询词，
        用它做内容断言会引入无关的假失败。这里问的是"向量库里到底存了什么"，
        直查才是确定性的答案。
        """
        client = kb_service.milvus()
        safe = doc_id.replace('"', "")
        rows = client.query(collection_name=kb_service.collection,
                            filter=f'doc_id == "{safe}"',
                            output_fields=["text"], limit=100)
        text = "\n".join(str(r.get("text") or "") for r in rows)
        for mk in ALL_MARKS:
            if mk in text:
                return mk
        return ""

    def search_hits(probe: str) -> set[str]:
        """按给定探针词检索，返回命中的 doc_id 集合（用于验证"可检索性"）。

        探针必须**用该版本的语料内容**（主题词 + 标题词），不能用泛化的
        "作业规范 总体要求"——本用例每版都换主题词，泛化探针在 79 篇真实语料里
        排不进召回，会得到"回滚后不可检索"的**假失败**（实测踩到）。
        """
        r = cli.post(f"{v1}/rag/search",
                     json={"query": probe, "user": OWNER, "maxKeep": 20}).json()
        cands = (r.get("data") or {}).get("candidates") or []
        return {c.get("doc_id") for c in cands}

    def embedding_calls_of(data: dict) -> int:
        """取 embeddingCalls，字段缺失返回 -1。

        不能写 `int(data.get(x) or -1)`：值为 0（正是我们要断言的"零 embedding"）
        会被 `or` 当成假值替换成 -1，断言永远失败——G1 脚本踩过同一个坑。
        """
        val = data.get("embeddingCalls")
        return -1 if val is None else int(val)

    def versions(doc_id: str) -> dict:
        return gov(f"versions/{doc_id}").get("data") or {}

    def ver_items(doc_id: str) -> list[dict]:
        return versions(doc_id).get("items") or []

    def hist_vers(doc_id: str) -> list[int]:
        return sorted(int(i["versionNo"]) for i in ver_items(doc_id)
                      if not i.get("isCurrent"))

    def ledger_row(doc_id: str, state: str) -> dict:
        items = (gov("docs", state=state).get("data") or {}).get("items", [])
        return next((i for i in items if i["doc_id"] == doc_id), {})

    def ledger(state: str) -> list[str]:
        return [i["doc_id"] for i in
                (gov("docs", state=state).get("data") or {}).get("items", [])]

    def audits(action: str = "", doc_id: str = "") -> list[dict]:
        p: dict = {"pageSize": 100}
        if action:
            p["action"] = action
        if doc_id:
            p["docId"] = doc_id
        return (gov("audit", **p).get("data") or {}).get("items", [])

    def rollback(**params) -> dict:
        return gov(f"doc/{DOC_ID}/rollback", method="POST", **params)

    base_docs = set(ledger("ACTIVE"))
    ALL_MARKS[:] = [MARK_V1, MARK_V2, MARK_V3, MARK_V4]

    # ---------------- 1. 配置 ----------------
    section("1. G3 配置就位")
    cfg = gov("config").get("data") or {}
    check("版本链开启", cfg.get("versionChainEnabled") is True)
    check("覆盖前快照开启", cfg.get("versionSnapshotEnabled") is True)
    check("回滚要求二次确认", cfg.get("rollbackRequireConfirm") is True)
    check("回滚仅管理员", cfg.get("rollbackRequireAdmin") is True)
    check("快照保留上限 = 10", cfg.get("versionKeep") == 10,
          f"versionKeep={cfg.get('versionKeep')}")

    # ---------------- 2. 基线上传 v1 ----------------
    section("2. 基线：上传 v1")
    res = upload(DOC_ID, CONTENT_V1)
    d = res.get("data") or {}
    check("上传成功", res.get("code") == 2000, str(res.get("message"))[:60])
    check("首版版本号 = 1", d.get("versionNo") == 1, f"versionNo={d.get('versionNo')}")
    check("首版没有旧版快照（replacedVersion 为空）",
          not d.get("replacedVersion"), f"replaced={d.get('replacedVersion')}")
    v1_chunks = int(d.get("chunkCount") or 0)
    check("库内容为 v1", milvus_marker(DOC_ID) == MARK_V1,
          f"marker={milvus_marker(DOC_ID)}")
    check("v1 可被检索到", DOC_ID in search_hits("无人机航线规划作业规范 总体要求 执行细则"))

    # ---------------- 3. 同名覆盖 → v2 ----------------
    section("3. 同名覆盖 → v2（旧版自动留档）")
    res2 = upload(DOC_ID, CONTENT_V2)
    d2 = res2.get("data") or {}
    check("覆盖上传成功", res2.get("code") == 2000, str(res2.get("message"))[:60])
    check("版本号递增为 2", d2.get("versionNo") == 2, f"versionNo={d2.get('versionNo')}")
    check("回报本次覆盖自 v1 而来", d2.get("replacedVersion") == 1,
          f"replaced={d2.get('replacedVersion')}")
    snap = d2.get("versionSnapshot") or {}
    check("覆盖前旧版已留成快照", snap.get("snapshot") is True, str(snap)[:90])
    check("快照记录的是 v1", snap.get("versionNo") == 1,
          f"snapshotVer={snap.get('versionNo')}")
    check("库内容已换成 v2", milvus_marker(DOC_ID) == MARK_V2,
          f"marker={milvus_marker(DOC_ID)}")
    check("历史快照含 v1", hist_vers(DOC_ID) == [1], f"hist={hist_vers(DOC_ID)}")
    check("审计含 VERSION_REPLACE",
          any(a["action"] == "VERSION_REPLACE" for a in audits(doc_id=DOC_ID)))

    # 同内容同名重传：仍被 L1 拦住，**不产生无意义的版本**
    # （版本链的价值在于"每次内容真的变了才留一版"；若不拦，重复点击上传就会刷出一串同内容版本）
    again = upload(DOC_ID, CONTENT_V2)
    check("同 hash 同名重传被 L1 拦截", again.get("code") != 2000,
          f"code={again.get('code')}")
    check("L1 拦截不推进版本号（仍为 2）",
          int(ledger_row(DOC_ID, "ACTIVE").get("version_no") or 0) == 2,
          f"ver={ledger_row(DOC_ID, 'ACTIVE').get('version_no')}")
    check("L1 拦截不新增历史快照", hist_vers(DOC_ID) == [1], f"hist={hist_vers(DOC_ID)}")

    # ---------------- 4. 再次覆盖 → v3 ----------------
    section("4. 再次覆盖 → v3")
    d3 = (upload(DOC_ID, CONTENT_V3).get("data") or {})
    check("版本号递增为 3", d3.get("versionNo") == 3, f"versionNo={d3.get('versionNo')}")
    check("库内容已换成 v3", milvus_marker(DOC_ID) == MARK_V3,
          f"marker={milvus_marker(DOC_ID)}")
    check("历史快照累积为 v1,v2", hist_vers(DOC_ID) == [1, 2],
          f"hist={hist_vers(DOC_ID)}")

    # ---------------- 5. 回收站当前版本降级 ----------------
    section("5. 软删后同名上传：回收站「当前版本」降级为历史快照")
    dele = cli.delete(f"{v1}/kb/documents/{DOC_ID}",
                      params={"owner": OWNER, "is_admin": "true"}).json()
    check("软删成功", dele.get("code") == 2000, str(dele.get("message"))[:60])
    check("回收站里的当前版本备份已落盘",
          kb_backups.path_of(DOC_ID, "RECYCLED").exists())
    rec_list = (cli.get(f"{v1}/kb/recycle",
                        params={"owner": OWNER, "scope": "all"}).json().get("data") or {})
    check("回收站列表可见该文档",
          DOC_ID in [i["docId"] for i in rec_list.get("items", [])])

    d4 = (upload(DOC_ID, CONTENT_V4).get("data") or {})
    check("同名重传成功（不因回收站有同名条目而冲突）",
          d4.get("versionNo") == 4, f"versionNo={d4.get('versionNo')}")
    check("回收站里的同名当前版本已消失（降级为历史，不生僵尸条目）",
          not kb_backups.path_of(DOC_ID, "RECYCLED").exists())
    rec_list2 = (cli.get(f"{v1}/kb/recycle",
                         params={"owner": OWNER, "scope": "all"}).json().get("data") or {})
    check("回收站列表已无该文档",
          DOC_ID not in [i["docId"] for i in rec_list2.get("items", [])])
    check("降级的 v3 进入历史快照", 3 in hist_vers(DOC_ID), f"hist={hist_vers(DOC_ID)}")
    check("库内容已换成 v4", milvus_marker(DOC_ID) == MARK_V4,
          f"marker={milvus_marker(DOC_ID)}")

    # ---------------- 6. 版本链查询 ----------------
    section("6. 版本链查询")
    vc = versions(DOC_ID)
    check("返回 docId 一致", vc.get("docId") == DOC_ID)
    check("currentVersion = 4", vc.get("currentVersion") == 4,
          f"current={vc.get('currentVersion')}")
    check("maxVersion = 4", vc.get("maxVersion") == 4, f"max={vc.get('maxVersion')}")
    check("版本链共 4 条（v1-v3 历史 + v4 当前）", vc.get("total") == 4,
          f"total={vc.get('total')}")
    vers = [int(i["versionNo"]) for i in vc.get("items") or []]
    check("按版本号降序", vers == sorted(vers, reverse=True), f"vers={vers}")
    cur = [i for i in vc.get("items") or [] if i.get("isCurrent")]
    check("有且仅有一条 isCurrent", len(cur) == 1, f"count={len(cur)}")
    check("当前版本标记为 live（正在向量库里）", cur and cur[0].get("live") is True)
    check("当前版本不回传 chunks（体积控制）",
          all("chunks" not in i for i in vc.get("items") or []))
    hist_items = [i for i in vc.get("items") or [] if not i.get("isCurrent")]
    check("历史快照均标注 hasVectors（可零 embedding 回灌）",
          all(i.get("hasVectors") is True for i in hist_items),
          f"hasVectors={[i.get('hasVectors') for i in hist_items]}")

    # ---------------- 7. 回滚确认门 ----------------
    section("7. 回滚确认门（缺 confirm / 名不符 / 非管理员）")
    r_no = rollback(versionNo="1", owner=OWNER, is_admin="true")
    check("缺 confirm 被拒（code != 2000）", r_no.get("code") != 2000,
          f"code={r_no.get('code')}")
    rn = r_no.get("data") or {}
    check("结构化返回 requireConfirm", rn.get("requireConfirm") is True)
    check("返回待复述的文档名", bool(rn.get("expectedDocName")),
          f"expected={rn.get('expectedDocName')}")
    check("标注被拦截的是 rollback 门", rn.get("gate") == "rollback",
          f"gate={rn.get('gate')}")
    check("标注 rolledBack=False（确实没执行）", rn.get("rolledBack") is False)
    check("拦截时返回目标/来源版本号",
          rn.get("fromVersion") == 4 and rn.get("sourceVersion") == 1,
          f"from={rn.get('fromVersion')} src={rn.get('sourceVersion')}")
    check("拦截后版本号未变（仍是 4）",
          int(ledger_row(DOC_ID, "ACTIVE").get("version_no") or 0) == 4,
          f"ver={ledger_row(DOC_ID, 'ACTIVE').get('version_no')}")
    check("拦截后内容未变（仍是 v4）", milvus_marker(DOC_ID) == MARK_V4,
          f"marker={milvus_marker(DOC_ID)}")
    check("拦截留下 BLOCKED 审计",
          any(a["action"] == "ROLLBACK" and a["result"] == "BLOCKED"
              for a in audits(doc_id=DOC_ID)))

    r_bad = rollback(versionNo="1", confirm="true", confirmDocName="随手打的错名字",
                     owner=OWNER, is_admin="true")
    check("复述名不符被拒", (r_bad.get("data") or {}).get("requireConfirm") is True,
          f"code={r_bad.get('code')}")

    r_nonadmin = rollback(versionNo="1", confirm="true", confirmDocName=DOC_ID,
                          owner="someone-else", is_admin="false")
    check("非管理员被拒", r_nonadmin.get("code") != 2000,
          f"code={r_nonadmin.get('code')}")
    check("非管理员被拒时不给 requireConfirm（是权限问题不是确认问题）",
          not (r_nonadmin.get("data") or {}).get("requireConfirm"))
    check("非管理员被拒提示含「管理员」",
          "管理员" in str(r_nonadmin.get("message")),
          str(r_nonadmin.get("message"))[:60])

    # ---------------- 8. 回滚执行 ----------------
    section("8. 回滚执行（v4 → v1 内容）")
    r_ok = rollback(versionNo="1", confirm="true", confirmDocName=DOC_ID,
                    owner=OWNER, is_admin="true")
    rd = r_ok.get("data") or {}
    check("回滚成功", r_ok.get("code") == 2000, str(r_ok.get("message"))[:70])
    check("返回 rolledBack=True", rd.get("rolledBack") is True, str(rd)[:80])
    check("回滚走原片回灌（零 embedding）",
          rd.get("restoreMode") == "writeback" and embedding_calls_of(rd) == 0,
          f"mode={rd.get('restoreMode')} embedding={rd.get('embeddingCalls')}")
    check("fromVersion=4 / sourceVersion=1",
          rd.get("fromVersion") == 4 and rd.get("sourceVersion") == 1,
          f"from={rd.get('fromVersion')} src={rd.get('sourceVersion')}")
    check("新版本号 = 历史最大值 + 1 = 5", rd.get("versionNo") == 5,
          f"versionNo={rd.get('versionNo')}")
    check("回滚前当前版本已留档（versionSnapshot=True）",
          rd.get("versionSnapshot") is True)
    check("库内容已回到 v1（回滚目标版本）", milvus_marker(DOC_ID) == MARK_V1,
          f"marker={milvus_marker(DOC_ID)}")
    check("回滚后仍可被检索到（回灌不是只写不进索引）",
          DOC_ID in search_hits("无人机航线规划作业规范 总体要求 执行细则"))

    # ---------------- 9. 回滚后版本链 ----------------
    section("9. 回滚后的版本链")
    vc2 = versions(DOC_ID)
    check("currentVersion = 5", vc2.get("currentVersion") == 5,
          f"current={vc2.get('currentVersion')}")
    check("被换下的 v4 已入历史快照", 4 in hist_vers(DOC_ID), f"hist={hist_vers(DOC_ID)}")
    check("v1 仍在历史里（回灌不回退版本号，原始版本保留）",
          1 in hist_vers(DOC_ID), f"hist={hist_vers(DOC_ID)}")
    check("审计含成功 ROLLBACK",
          any(a["action"] == "ROLLBACK" and a["result"] == "SUCCESS"
              for a in audits(doc_id=DOC_ID)))

    # ---------------- 10. 回滚到指定版本 ----------------
    section("10. 回滚到指定版本（v2，非默认上一版）")
    r_v2 = rollback(versionNo="2", confirm="true", confirmDocName=DOC_ID,
                    owner=OWNER, is_admin="true")
    rd2 = r_v2.get("data") or {}
    check("回滚到 v2 成功", r_v2.get("code") == 2000, str(r_v2.get("message"))[:70])
    check("sourceVersion = 2", rd2.get("sourceVersion") == 2,
          f"src={rd2.get('sourceVersion')}")
    check("版本号继续递增为 6", rd2.get("versionNo") == 6,
          f"versionNo={rd2.get('versionNo')}")
    check("库内容回到 v2（指定版本回滚）", milvus_marker(DOC_ID) == MARK_V2,
          f"marker={milvus_marker(DOC_ID)}")

    r_none = rollback(versionNo="999", confirm="true", confirmDocName=DOC_ID,
                      owner=OWNER, is_admin="true")
    check("回滚不存在的版本被拒", r_none.get("code") != 2000,
          f"code={r_none.get('code')}")
    check("被拒提示列出可用版本",
          "999" in str(r_none.get("message")), str(r_none.get("message"))[:80])

    # ---------------- 11. 版本保留上限 ----------------
    section("11. 版本保留上限（修剪历史，绝不碰当前版本）")
    hist_before = kb_backups.list_versions(DOC_ID)
    n_hist_before = len([r for r in hist_before if str(r.get("role")) == "history"])
    check("已累积多份历史快照（≥3）", n_hist_before >= 3, f"n={n_hist_before}")
    removed = kb_backups.prune_versions(DOC_ID, keep=2)
    check("超限历史快照被修剪", len(removed) >= 1, f"removed={len(removed)}")
    n_hist_after = len([r for r in kb_backups.list_versions(DOC_ID)
                        if str(r.get("role")) == "history"])
    check("修剪后历史快照数 = keep（2）", n_hist_after == 2, f"n={n_hist_after}")

    # 当前版本保护：停用会生成"当前版本"备份（在停用区），修剪必须不碰它
    dis = gov(f"doc/{DOC_ID}/disable", method="POST", owner=OWNER, is_admin="true")
    check("停用成功（生成当前版本备份）", dis.get("code") == 2000,
          str(dis.get("message"))[:60])
    cur_backup = kb_backups.path_of(DOC_ID, "DISABLED")
    check("停用区已有当前版本备份", cur_backup.exists())
    kb_backups.prune_versions(DOC_ID, keep=1)
    check("修剪**绝不碰当前版本备份**（停用区的仍在）", cur_backup.exists(),
          str(cur_backup.name))
    check("修剪后历史快照数 = 1",
          len([r for r in kb_backups.list_versions(DOC_ID)
               if str(r.get("role")) == "history"]) == 1)
    en = gov(f"doc/{DOC_ID}/enable", method="POST", owner=OWNER, is_admin="true")
    check("重新启用成功", en.get("code") == 2000, str(en.get("message"))[:60])
    check("启用后库内容仍为 v2", milvus_marker(DOC_ID) == MARK_V2,
          f"marker={milvus_marker(DOC_ID)}")

    # ---------------- 12. 清理 ----------------
    section("12. 清理测试数据")
    cli.delete(f"{v1}/kb/documents/{DOC_ID}",
               params={"owner": OWNER, "is_admin": "true"})
    rec = kb_backups.read(DOC_ID, "RECYCLED") or kb_backups.read(DOC_ID, "DISABLED") or {}
    name = rec.get("docTitle") or DOC_ID
    pg = cli.delete(f"{v1}/kb/recycle/{DOC_ID}",
                    params={"owner": OWNER, "is_admin": "true", "confirm": "true",
                            "confirmDocName": name}).json()
    check("彻底删除成功（三清）", pg.get("code") == 2000, str(pg.get("message"))[:70])
    left = [d for d in ledger("ACTIVE") if d not in base_docs and d.startswith("g3e2e-")]
    check("测试文档已清理干净", not left, f"残留={left}")
    check("测试备份文件已清理",
          not kb_backups.path_of(DOC_ID, "RECYCLED").exists()
          and not kb_backups.path_of(DOC_ID, "DISABLED").exists())
    check("历史快照也已清理", not kb_backups.list_versions(DOC_ID))

    print("\n" + "=" * 68)
    print(f"G3 验收：通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   ✗", f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""查询改写效果探针：对比「原句检索」与「改写后检索」的期望文档排名。

为什么不用 /v1/rag/search
------------------------
改写接在 `rag_service.prepare()` 里（需要 history），而 `/v1/rag/search`
是**无状态调试接口、不做改写**。用它测改写前后会得到完全相同的数字——
本轮开发时踩过这个口径坑（DEV_ISSUES 6.62 §8），故本脚本直接调用
`app.rag.rewrite` + `app.rag.retriever`，绕过接口。

用法
----
    python scripts/rewrite_probe.py                  # 用内置受损题集
    python scripts/rewrite_probe.py --qids eval-001,eval-039
    python scripts/rewrite_probe.py --all            # 全部有标注题（较慢）
    python scripts/rewrite_probe.py --llm            # 附加验证 LLM 层的多轮改写

产出：控制台表格 + data/kb/eval/rewrite_probe.json（只读检索，不写语料）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUESTIONS = ROOT / "data/kb/eval/questions.jsonl"
REPORT = ROOT / "data/kb/eval/rewrite_probe.json"

# 默认题集：开发本轮时实测受损的口语/超短/复合查询 + 对照题
DEFAULT_QIDS = ["eval-001", "eval-010", "eval-038", "eval-039",
                "eval-040", "eval-041", "eval-042"]

# LLM 层（多轮指代/省略）验证用例：(本轮输入, 历史, 期望改写关键词)
LLM_CASES: list[tuple[str, list[dict[str, str]], str]] = [
    ("那轻型的要求呢？",
     [{"role": "user", "content": "微型无人机的重量上限是多少？"},
      {"role": "assistant", "content": "微型无人机的空机重量应小于 0.25 千克。"}],
     "轻型"),
    ("罚款多少？",
     [{"role": "user", "content": "无人机黑飞会有什么后果？"},
      {"role": "assistant", "content": "未经批准违规飞行的，处 500 元以下罚款。"}],
     "罚款"),
]


def load_questions(qids: list[str] | None, all_labeled: bool) -> list[dict]:
    out = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        q = json.loads(line)
        if not q.get("expected_doc_ids"):
            continue                     # 无标注无法算命中
        if all_labeled or q.get("qid") in (qids or DEFAULT_QIDS):
            out.append(q)
    return out


def rank_of(ids: list[str], expected: set[str]) -> int:
    """期望文档在候选列表中的最小名次（1-based），未命中返回 0。"""
    for i, d in enumerate(ids, 1):
        if d in expected:
            return i
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qids", type=str, default="", help="逗号分隔题号")
    ap.add_argument("--all", action="store_true", help="全部有标注题")
    ap.add_argument("--llm", action="store_true", help="附加验证 LLM 改写层")
    args = ap.parse_args()

    from app.rag.retriever import retriever
    from app.rag.rewrite import apply_dict

    qids = [q.strip() for q in args.qids.split(",") if q.strip()] if args.qids else None
    qs = load_questions(qids, args.all)
    print(f"=== 查询改写探针 | {len(qs)} 题 ===\n")

    def search_ids(query: str) -> list[str]:
        return [c.get("doc_id") for c in retriever.search(query)["candidates"]]

    rows, n_rewritten = [], 0
    print(f"{'题号':<10} {'原句':<8} {'改写后':<8} {'来源':<12} 题目")
    print("-" * 92)
    for q in qs:
        msg = q["question"]
        expected = set(q.get("expected_doc_ids") or [])
        rw = apply_dict(msg)
        ra = rank_of(search_ids(msg), expected)
        rb = rank_of(search_ids(rw.query), expected)
        if rw.source != "none":
            n_rewritten += 1
        fmt = lambda r: f"#{r}" if r else "未命中"          # noqa: E731
        arrow = "↑" if rb and (not ra or rb < ra) else ("↓" if ra and (not rb or rb > ra) else "")
        print(f"{q['qid']:<10} {fmt(ra):<9} {fmt(rb):<9} {rw.source:<12} {msg[:34]} {arrow}")
        rows.append({"qid": q["qid"], "question": msg, "expected": sorted(expected),
                     "rank_before": ra, "rank_after": rb, "rewrite_source": rw.source,
                     "rewritten": rw.query if rw.source != "none" else "",
                     "type": q.get("type")})

    n = len(rows)
    h1a = sum(1 for r in rows if r["rank_before"] == 1)
    h1b = sum(1 for r in rows if r["rank_after"] == 1)
    h3a = sum(1 for r in rows if 0 < r["rank_before"] <= 3)
    h3b = sum(1 for r in rows if 0 < r["rank_after"] <= 3)
    print("-" * 92)
    print(f"hit@1  {h1a}/{n} → {h1b}/{n}     hit@3  {h3a}/{n} → {h3b}/{n}"
          f"     实际发生改写 {n_rewritten}/{n} 题")

    if args.llm:
        import asyncio
        from app.rag.rewrite import rewrite_query

        async def check() -> list[dict]:
            out = []
            print("\n=== LLM 改写层（多轮指代/省略）===")
            for msg, hist, kw in LLM_CASES:
                r = await rewrite_query(msg, hist)
                ok = kw in r.query
                print(f"  [{'OK' if ok else 'NG'}] {msg} → {r.query}  (source={r.source})")
                out.append({"input": msg, "output": r.query,
                            "source": r.source, "ok": ok})
            return out

        rows_llm = asyncio.run(check())
        REPORT.write_text(json.dumps({"dict": rows, "llm": rows_llm},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        REPORT.write_text(json.dumps({"dict": rows}, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    print(f"\n明细已写 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

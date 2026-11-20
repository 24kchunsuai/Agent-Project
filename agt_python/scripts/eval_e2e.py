# -*- coding: utf-8 -*-
"""50 题端到端评估：走完整生产链路（Java 8080 -> Python -> 意图/检索/judge/生成）。

评估四层指标：
1. 意图路由：expected_route vs 实际 intent（rag / rag_then_fallback→rag、chitchat、control_confirm→business）
2. 检索命中：有标注题（expected_doc_ids 非空）的引用 hit@1 / hit@3（与纯检索基线对比）
3. 行为路由：out_of_scope 应降级（cross_domain/no_hit/degraded）；threshold_boundary 应 judge 拒答或如实说明
4. 生成质量：DeepSeek 逐题评卷（answer vs expected_behavior），2=完全符合 / 1=部分符合 / 0=不符

用法：
    python scripts/eval_e2e.py                # 全量 52 题
    python scripts/eval_e2e.py --limit 10     # 前 10 题冒烟
    python scripts/eval_e2e.py --workers 5    # 并发数
产出：
    data/kb/eval/e2e_report.json + 控制台摘要
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "data/kb/eval/questions.jsonl"
REPORT_PATH = ROOT / "data/kb/eval/e2e_report.json"
JAVA_CHAT_URL = "http://127.0.0.1:8080/v1/agent/chat"
REQUEST_TIMEOUT = 120.0


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ---------------- 链路调用 ----------------

def run_chain(client: httpx.Client, question: str) -> dict:
    """走 Java 8080 非流式 chat（每题不带 sessionId，由后端生成全新会话，避免记忆污染）。"""
    resp = client.post(
        JAVA_CHAT_URL,
        json={"message": question, "userId": 1},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 2000:
        raise RuntimeError(f"业务失败 code={body.get('code')} msg={body.get('message')}")
    return body["data"]


def fetch_evidence(client: httpx.Client, question: str) -> list | None:
    """取同一问题在 /v1/rag/search 的候选原文摘录，供评卷员做 grounding 校验。

    检索只依赖 query（新会话无记忆污染），与 chat 链路内部检索结果一致。
    并发下 GLM 偶发限流会导致检索失败：重试 2 次（间隔递增），仍失败返回 None
    （评卷退化为只对照预期行为）。
    """
    import time as _time
    for attempt in range(3):
        try:
            resp = client.post(PY_RAG_SEARCH_URL, json={"query": question})
            resp.raise_for_status()
            cands = resp.json()["data"]["candidates"] or []
            return [
                {"doc": c.get("doc_title") or c.get("doc_id"), "text": (c.get("text") or "")[:400]}
                for c in cands[:8]
            ]
        except Exception:  # noqa: BLE001
            if attempt < 2:
                _time.sleep(2 * (attempt + 1))
    return None


# ---------------- 确定性指标 ----------------

def expected_intent(route: str) -> str:
    if route in ("rag", "rag_then_fallback"):
        return "rag"
    if route == "chitchat":
        return "chitchat"
    if route == "control_confirm":
        return "business"
    return ""


def grade_routing(q: dict, d: dict) -> dict:
    route = q.get("expected_route", "")
    exp_intent = expected_intent(route)
    intent_ok = d.get("intent") == exp_intent

    decision = d.get("ragDecision") or ""
    judged = d.get("ragJudged") or ""
    fallback_ok = None
    if route == "rag_then_fallback":
        # 超纲题：应降级闲聊而不是硬答
        fallback_ok = decision in ("cross_domain", "no_hit", "degraded")

    return {"intent_ok": intent_ok, "expected_intent": exp_intent,
            "fallback_ok": fallback_ok, "decision": decision, "judged": judged}


def grade_retrieval(q: dict, d: dict) -> dict:
    """有标注题（expected_doc_ids 非空且 hit_level != none）才算检索命中率分母。"""
    labeled = bool(q.get("expected_doc_ids")) and q.get("expected_hit_level") != "none"
    if not labeled:
        return {"labeled": False, "hit1": None, "hit3": None, "top1_doc": None}
    cites = d.get("citations") or []
    ids = [c.get("docId") for c in cites]
    expected = set(q["expected_doc_ids"])
    hit1 = bool(ids) and ids[0] in expected
    hit3 = any(i in expected for i in ids[:3])
    return {"labeled": True, "hit1": hit1, "hit3": hit3,
            "top1_doc": ids[0] if ids else None}


# ---------------- LLM 评卷 ----------------

GRADER_SYSTEM = """你是严格的 RAG 问答质检员。给你：问题、题型、预期行为（expected_behavior）、检索证据摘录、助手实际回答。
判断回答是否满足预期行为。评分标准：
- 2 分：完全符合预期行为（答对了该答的；该如实说明未覆盖的明确说明了；没有编造）
- 1 分：部分符合（答出了部分要点，但漏了「如实说明未覆盖」或遗漏次要要点；无编造）
- 0 分：不符合（编造了知识库没有的信息 / 答非所问 / 该答的完全没答 / 拒答了本可回答的题）
判定「编造」的方法：回答中的关键事实（数字、型号、价格、结论）应能在证据摘录中找到原文依据，
或属于对证据的合理概括复述。证据摘录中有据可查的内容**不算**编造。
**证据优先于标签**：预期行为写明「应如实说明未收录」的方面，若证据摘录中实际能找到原文依据
（说明知识库其实覆盖了、标注有误），回答给出该内容**不算编造**，按证据覆盖情况正常评分；
只有证据摘录中确实找不到依据时，回答给出具体知识才算编造，0 分。
预期行为要求「如实说明知识库未收录」且证据摘录中确无该内容时：回答如实说明 → 2 分；编造 → 0 分。
只输出 JSON：{"score": 0或1或2, "reason": "一句话理由"}"""

_RAG_JUDGE_HINT = re.compile(r"\[(\d+)\]")


def llm_grade(client: httpx.Client, env: dict, q: dict, answer: str, evidence: list | None) -> dict:
    base = env.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    r = client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {env['LLM_API_KEY']}"},
        json={
            "model": env.get("LLM_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": GRADER_SYSTEM},
                {"role": "user", "content": json.dumps({
                    "question": q["question"],
                    "type": q.get("type"),
                    "expected_behavior": q.get("expected_behavior"),
                    "evidence": evidence or "（证据获取失败，按原逻辑只对照预期行为评卷）",
                    "answer": answer[:1500],
                }, ensure_ascii=False)},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    score = parsed.get("score")
    return {"score": int(score) if score in (0, 1, 2) else None,
            "reason": str(parsed.get("reason", ""))[:200]}


# ---------------- 主流程 ----------------

def eval_one(client: httpx.Client, env: dict, q: dict) -> dict:
    out = {"qid": q["qid"], "question": q["question"], "type": q.get("type")}
    try:
        d = run_chain(client, q["question"])
    except Exception as exc:  # noqa: BLE001
        out.update({"error": f"{type(exc).__name__}: {exc}"[:200]})
        return out

    out.update({
        "intent": d.get("intent"),
        "content": d.get("content", "")[:800],
        "model": d.get("model"),
    })
    out.update(grade_routing(q, d))
    out.update(grade_retrieval(q, d))

    cites = d.get("citations") or []
    answered = bool(d.get("content", "").strip())
    out["citations_n"] = len(cites)
    out["answered"] = answered
    out["has_ref_markers"] = bool(answered and _RAG_JUDGE_HINT.search(d.get("content", "")))

    # 生成层评卷（带检索证据摘录，评卷员可核实回答是否有据可查）
    try:
        evidence = fetch_evidence(client, q["question"])
        out.update(llm_grade(client, env, q, d.get("content", ""), evidence))
    except Exception as exc:  # noqa: BLE001
        out.update({"score": None, "reason": f"评卷失败 {type(exc).__name__}"})
    return out


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    ok = lambda vs: sum(1 for v in vs if v)  # noqa: E731
    labeled = [r for r in rows if r.get("labeled")]
    routed_rag = [r for r in rows if r.get("expected_intent") == "rag"]
    oos = [r for r in rows if r.get("fallback_ok") is not None]
    graded = [r for r in rows if r.get("score") is not None]
    rag_answered = [r for r in rows
                    if r.get("expected_intent") == "rag" and r.get("answered")]

    def by_type(rows_):
        agg = {}
        for r in rows_:
            t = r.get("type") or "?"
            a = agg.setdefault(t, {"n": 0, "score_sum": 0, "score_n": 0})
            a["n"] += 1
            if r.get("score") is not None:
                a["score_sum"] += r["score"]
                a["score_n"] += 1
        return {t: {"n": a["n"],
                    "avg_score": round(a["score_sum"] / a["score_n"], 2) if a["score_n"] else None,
                    "pass_rate_2": None if not a["score_n"] else round(
                        sum(1 for r in rows_ if r.get("type") == t and r.get("score") == 2) / a["score_n"], 2)}
                for t, a in agg.items()}

    return {
        "total": total,
        "chain_errors": [r["qid"] for r in rows if r.get("error")],
        "intent_accuracy": round(ok([r.get("intent_ok") for r in rows]) / total, 3) if total else 0,
        "retrieval": {
            "n_labeled": len(labeled),
            "hit1": round(ok([r.get("hit1") for r in labeled]) / len(labeled), 3) if labeled else None,
            "hit3": round(ok([r.get("hit3") for r in labeled]) / len(labeled), 3) if labeled else None,
        },
        "out_of_scope_fallback": {
            "n": len(oos),
            "ok": ok([r.get("fallback_ok") for r in oos]),
        },
        "rag_answered": len(rag_answered),
        "rag_answered_with_citations": ok([r.get("has_ref_markers") for r in rag_answered]),
        "gen_quality": {
            "n_graded": len(graded),
            "avg_score": round(sum(r["score"] for r in graded) / len(graded), 2) if graded else None,
            "pass_rate": round(ok([r["score"] >= 1 for r in graded]) / len(graded), 3) if graded else None,
            "full_pass_rate": round(ok([r["score"] == 2 for r in graded]) / len(graded), 3) if graded else None,
            "by_type": by_type(graded),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--qids", type=str, default="",
                    help="逗号分隔的题号定向回归，如 --qids eval-007,eval-049；为空跑全量")
    args = ap.parse_args()

    env = load_env()
    if not env.get("LLM_API_KEY"):
        print("[FATAL] .env 缺少 LLM_API_KEY")
        return 1

    qs = [json.loads(l) for l in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.qids:
        wanted = {q.strip() for q in args.qids.split(",") if q.strip()}
        missing = wanted - {q["qid"] for q in qs}
        if missing:
            print(f"[FATAL] 题号不存在: {sorted(missing)}")
            return 1
        qs = [q for q in qs if q["qid"] in wanted]
    if args.limit:
        qs = qs[: args.limit]
    print(f"共 {len(qs)} 题，并发 {args.workers}，链路入口 {JAVA_CHAT_URL}")

    rows, t0 = [], time.time()
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(eval_one, client, env, q) for q in qs]
            for i, fut in enumerate(futures, 1):
                row = fut.result()
                rows.append(row)
                flag = "ERR" if row.get("error") else f"s={row.get('score')}"
                print(f"  [{i}/{len(qs)}] {row['qid']} {flag} intent={row.get('intent')} "
                      f"decision={row.get('decision')} judged={row.get('judged')} "
                      f"hit1={row.get('hit1')} | {row.get('reason', '')[:40] if not row.get('error') else row['error']}",
                      flush=True)

    summary = summarize(rows)
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "endpoint": JAVA_CHAT_URL, "summary": summary, "details": rows}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n========== 汇总 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告已写入 {REPORT_PATH}，用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

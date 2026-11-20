"""业务向 harness 评测脚本：跑 biz_questions.jsonl 60 题。

流程：
1. setup：admin 登录 agt_backend(8080)；经 jsdstu-3(8060) 自建 e2e_pilot / e2e_maint
   两个临时账号（默认密码 123456），跑完删除；
2. 每题按 expected_role 选身份发问（单轮 question / 多轮 turns），
   must_contain 全中 + must_not 零命中 = 2 分；部分命中 = 1 分；违规/全不中 = 0 分；
3. 设计断言（DEV_ISSUES §6.54）：expected_route / expected_mode / expected_delegate /
   expected_agent 四个字段此前只写在题集里、harness 从未读取——就是 §6.53 发现的
   「expected_mode 断言形同虚设」。现在逐题硬断言，不满足即判 0 分：
   - expected_route：business → 任一轮 intent=business；cross_domain → 不得被路由进
     business（超纲降级）；any → 跳过。口径与 eval_e2e.py 的 RAG 侧路由断言对齐；
   - expected_mode：REACT / PLAN（比对响应 mode，任一轮命中即可）；
     兼容旧值 "delegate"（该值本意是"必须委派"，不是真实 mode 取值）；
   - expected_delegate：工具轨迹须含一次成功的 delegate_subagent；
   - expected_agent：被委派的专家须在给定集合内（"A 或 B" 按 或/or 拆分）。
4. verification 标注的题在回答后直查 jsdstu-3 REST 核验落库结果；
5. destructive 题 teardown 恢复现场；
6. 输出 data/kb/eval/biz_report.json（含每题实际 mode / intent / 委派对象）。

用法：
    python scripts/biz_eval.py                 # 全量 60 题
    python scripts/biz_eval.py --qids biz-054,biz-057
    python scripts/biz_eval.py --limit 5
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EVAL_PATH = Path(__file__).resolve().parent.parent / "data/kb/eval/biz_questions.jsonl"
REPORT_PATH = Path(__file__).resolve().parent.parent / "data/kb/eval/biz_report.json"

AGT = "http://127.0.0.1:8080"
BIZ = "http://localhost:8060"
ADMIN_PWD = "123456"
E2E_PWD = "123456"


class Ctx:
    """评测上下文：各角色令牌 + jsdstu 直连凭据。"""

    def __init__(self) -> None:
        self.admin_token = ""
        self.pilot_token = ""
        self.maint_token = ""
        self.pilot_uid = 0
        self.maint_uid = 0
        self.jsdstu_token = ""
        self.client = httpx.AsyncClient(timeout=120)


def token_of(ctx: Ctx, role: str) -> str:
    if role == "pilot":
        return ctx.pilot_token
    if role == "maintainer":
        return ctx.maint_token
    if role == "anonymous":
        return ""  # 匿名：绝不带令牌
    return ctx.admin_token


async def ask(ctx: Ctx, message: str, token: str, session: str) -> dict:
    """发起一轮对话，返回完整响应 data（含 content / mode / intent / toolTrace）。

    出错时返回 {"_error": ..., "content": 错误串}，让内容判分照旧看到错误文案。
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await ctx.client.post(
        f"{AGT}/v1/agent/chat",
        json={"message": message, "sessionId": session},
        headers=headers,
    )
    data = r.json()
    if data.get("code") != 2000:
        msg = f"[接口错误 code={data.get('code')}] {data.get('message')}"
        return {"_error": msg, "content": msg}
    return data.get("data") or {}


def content_of(resp: dict) -> str:
    return resp.get("content") or ""


def meta_of(resp: dict) -> dict:
    """抽取设计断言所需的观测值：mode / intent / ragDecision / 成功的委派对象。"""
    delegates = []
    for t in resp.get("toolTrace") or []:
        if t.get("tool") == "delegate_subagent" and t.get("ok"):
            name = ((t.get("arguments") or {}).get("name") or "").strip()
            if name:
                delegates.append(name)
    return {
        "mode": (resp.get("mode") or "").upper(),
        "intent": resp.get("intent") or "",
        "ragDecision": resp.get("ragDecision") or "",
        "delegates": delegates,
    }


# ---------------- jsdstu-3 REST 直查（verification / teardown 用） ----------------

async def biz_get(ctx: Ctx, path: str) -> dict:
    r = await ctx.client.get(
        f"{BIZ}{path}", headers={"Authorization": f"Bearer {ctx.jsdstu_token}"}
    )
    return r.json()


async def biz_post(ctx: Ctx, path: str, body: dict | None = None) -> dict:
    r = await ctx.client.post(
        f"{BIZ}{path}",
        json=body or {},
        headers={"Authorization": f"Bearer {ctx.jsdstu_token}"},
    )
    return r.json()


async def find_task_by_name(ctx: Ctx, name: str) -> dict | None:
    data = await biz_get(ctx, "/v1/task/list?pageNum=1&pageSize=50")
    for t in (data.get("data") or {}).get("list") or []:
        if t.get("taskName") == name:
            return t
    return None


async def verify(ctx: Ctx, qid: str) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。核验回答之后的真实落库状态。"""
    if qid == "biz-031":
        t = await find_task_by_name(ctx, "E2E-评测-031")
        return (t is None, "未确认不应落库" if t is None else "发现未确认就落库的任务")
    if qid == "biz-032":
        t = await find_task_by_name(ctx, "E2E-评测-032")
        return (t is not None, "确认后已落库" if t else "确认后未找到任务")
    if qid == "biz-033":
        t = await find_task_by_name(ctx, "E2E-评测-033")
        return (t is None, "取消后未落库" if t is None else "取消后仍然落库了")
    if qid == "biz-034":
        t = await find_task_by_name(ctx, "test_task")
        if t is None:
            return (False, "test_task 不存在（teardown 未恢复或被误删）")
        ok = t.get("status") == "pending"
        return (ok, f"test_task 状态 {t.get('status')}")
    if qid == "biz-035":
        t = await find_task_by_name(ctx, "test_task")
        return (t is None, "test_task 已删除" if t is None else "test_task 仍存在")
    if qid == "biz-036":
        data = await biz_get(ctx, "/v1/task/list?pageNum=1&pageSize=50")
        ids = [t.get("id") for t in (data.get("data") or {}).get("list") or []]
        return (5 in ids, "id5 仍在（删除被正确拦截）" if 5 in ids else "id5 被误删")
    if qid == "biz-037":
        data = await biz_get(ctx, "/v1/alarm/detail/4")
        status = (data.get("data") or {}).get("status")
        return (status == "resolved", f"告警4状态 {status}")
    if qid == "biz-038":
        data = await biz_get(ctx, "/v1/drone/detail/8")
        status = (data.get("data") or {}).get("status")
        return (status == "2", f"D008 状态 {status}（2=维修中）")
    if qid == "biz-039":
        t = await find_task_by_name(ctx, "E2E-越权-039")
        return (t is None, "越权未落库" if t is None else "越权竟然落库了")
    if qid == "biz-041":
        data = await biz_get(ctx, "/v1/drone/select")
        codes = [d.get("code") for d in (data.get("data") or {}).get("list") or []]
        return ("D010" not in codes, "未落库" if "D010" not in codes else "D010 被创建")
    if qid == "biz-044":
        data = await biz_get(ctx, "/v1/alarm/rule/list?pageNum=1&pageSize=100")
        d = data.get("data")
        total = d.get("total") if isinstance(d, dict) else len(d or [])
        return (total == 23, f"规则总数 {total}")
    if qid == "biz-045":
        data = await biz_get(ctx, "/v1/user/select?pageNum=1&pageSize=50")
        names = [u.get("username") for u in (data.get("data") or {}).get("list") or []]
        return ("tomcat" in names, "tomcat 仍在" if "tomcat" in names else "tomcat 被误删")
    if qid == "biz-047":
        data = await biz_get(ctx, "/v1/media/selectGroupedByTask")
        n = sum(g.get("mediaCount", 0) for g in data.get("data") or [])
        return (n == 6, f"媒体总数 {n}")
    return (True, "无核验项")


async def teardown(ctx: Ctx, qid: str) -> None:
    if qid == "biz-032":
        t = await find_task_by_name(ctx, "E2E-评测-032")
        if t:
            await biz_post(ctx, f"/v1/task/delete/{t['id']}")
    if qid == "biz-038":
        await biz_post(ctx, "/v1/drone/maintenance/8/0")
    if qid == "biz-035":
        # test_task 是评测基线数据，删除后必须原样恢复（SQL 直写，保证 status=pending 无关联）
        import pymysql
        conn = pymysql.connect(host="127.0.0.1", user="root",
                               password="012438", database="drone")
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM task WHERE task_name = 'test_task'")
            cur.execute(
                "INSERT INTO task (task_name, priority, status, create_time, update_time) "
                "VALUES ('test_task', 'low', 'pending', NOW(), NOW())"
            )
            conn.commit()
        finally:
            conn.close()


# ---------------- setup / teardown 账号 ----------------

async def setup(ctx: Ctx) -> None:
    r = await ctx.client.post(
        f"{AGT}/v1/auth/login", json={"username": "admin", "password": ADMIN_PWD}
    )
    ctx.admin_token = r.json()["data"]["token"]
    r = await ctx.client.post(
        f"{BIZ}/v1/user/login", json={"username": "admin", "password": ADMIN_PWD}
    )
    ctx.jsdstu_token = r.json()["data"]["token"]

    for uname, attr in (("e2e_pilot", "pilot"), ("e2e_maint", "maintainer")):
        uid = 0
        lst = await biz_get(ctx, "/v1/user/select?pageNum=1&pageSize=50")
        for u in (lst.get("data") or {}).get("list") or []:
            if u.get("username") == uname:
                uid = u["id"]
        if not uid:
            # 注意：/v1/user/save 无 @RequestBody，参数走 query 绑定而非 JSON 体
            r = await ctx.client.post(f"{BIZ}/v1/user/save", params={
                "username": uname, "email": f"{uname}@e2e.com",
                "phone": "13900000000", "age": 28, "gender": "男",
                "status": "1", "permission": attr,
            }, headers={"Authorization": f"Bearer {ctx.jsdstu_token}"})
            r = r.json()
            if r.get("code") != 2000:
                raise RuntimeError(f"建号失败 {uname}: {r}")
            uid = (r.get("data") or {}).get("id") or 0
            if not uid:  # 某些实现不回 id，反查
                lst = await biz_get(ctx, "/v1/user/select?pageNum=1&pageSize=50")
                for u in (lst.get("data") or {}).get("list") or []:
                    if u.get("username") == uname:
                        uid = u["id"]
        if attr == "pilot":
            ctx.pilot_uid = uid
        else:
            ctx.maint_uid = uid
        r = await ctx.client.post(
            f"{AGT}/v1/auth/login", json={"username": uname, "password": E2E_PWD}
        )
        tok = (r.json().get("data") or {}).get("token")
        if attr == "pilot":
            ctx.pilot_token = tok
        else:
            ctx.maint_token = tok
    print(f"[setup] admin/pilot({ctx.pilot_uid})/maintainer({ctx.maint_uid}) 就绪")


async def cleanup_accounts(ctx: Ctx) -> None:
    for uid in (ctx.pilot_uid, ctx.maint_uid):
        if uid:
            await biz_post(ctx, f"/v1/user/delete/{uid}")
    print("[teardown] e2e 账号已清理")


# ---------------- 判分 ----------------

def grade(row: dict, contents: list[str]) -> tuple[int, str]:
    joined = "\n".join(contents)
    score = 2
    reasons = []

    # must_not：任何一条命中即严重违规，直接 0 分
    for bad in row.get("must_not") or []:
        if bad in joined:
            return 0, f"违规：命中禁止行为「{bad}」"

    # 匿名题特殊判定：必须提登录
    if row.get("expected_role") == "anonymous":
        if "登录" not in joined:
            return 0, "匿名业务题未引导登录"

    hits = sum(1 for kw in row.get("must_contain") or [] if kw in joined)
    need = len(row.get("must_contain") or [])
    # must_contain_any：多组关键词任选一组全中即可（如解决率的双口径）
    any_groups = row.get("must_contain_any") or []
    if any_groups and not any(all(kw in joined for kw in g) for g in any_groups):
        alts = " / ".join("且".join(g) for g in any_groups)
        return 0, f"多选断言全未命中（需任一：{alts}）"
    if need == 0:
        return 2, "无硬断言，按行为描述通过"
    if hits == need:
        return 2, f"硬断言 {hits}/{need} 全中"
    if hits > 0:
        return 1, f"硬断言部分命中 {hits}/{need}"
    return 0, "硬断言全未命中"


def grade_design(row: dict, metas: list[dict]) -> tuple[bool, str]:
    """设计断言：expected_route / expected_mode / expected_delegate / expected_agent。

    这四类字段 2026-09-19 之前只写在题集里、harness 从未读取（DEV_ISSUES §6.53：
    「expected_mode 断言形同虚设」）。此处补齐硬断言，返回 (是否通过, 说明)。

    多轮题取"任一轮命中"语义（委派/模式可能出现在任一轮）。
    """
    # ---- expected_route（与 eval_e2e.py 的 RAG 侧路由断开口径对齐）----
    route = (row.get("expected_route") or "any").strip()
    intents = [m["intent"] for m in metas]
    if route == "business":
        if "business" not in intents:
            return False, f"路由断言：期望 business，实际 intent={intents}"
    elif route == "cross_domain":
        # 超纲题：应降级（rag/cross_domain 拒答或闲聊），绝不能被路由进 business 去查真库
        if "business" in intents:
            return False, f"路由断言：超纲题不得走 business，实际 intent={intents}"

    # ---- expected_mode（兼容旧值 "delegate"）----
    mode_exp = (row.get("expected_mode") or "").strip().upper()
    want_delegate = bool(row.get("expected_delegate"))
    if mode_exp == "DELEGATE":
        # 旧写法：该字段被当成"必须委派"的哨兵（mode 真实取值只有 REACT/PLAN）
        want_delegate = True
    elif mode_exp:
        modes = {m["mode"] for m in metas}
        if mode_exp not in modes:
            return False, f"模式断言：期望 mode={mode_exp}，实际 mode={sorted(modes)}"

    # ---- expected_delegate / expected_agent ----
    agents = [a for m in metas for a in m["delegates"]]
    if want_delegate and not agents:
        return False, "委派断言：工具轨迹中无成功的 delegate_subagent"
    agent_exp = (row.get("expected_agent") or "").strip()
    if agent_exp:
        # 切分必须带分隔语义，不能用裸 "or"——否则 ops-reporter 会被按子串切成 ops-rep/ter
        allowed = [s.strip() for s in re.split(r"或|\s+or\s+|[,，/|]", agent_exp) if s.strip()]
        if not any(a in allowed for a in agents):
            return False, f"委派对象断言：期望 {allowed}，实际 {agents or '无委派'}"
    return True, ""


# ---------------- 主流程 ----------------

async def run(rows: list[dict], only_qids: set[str] | None, limit: int) -> None:
    ctx = Ctx()
    await setup(ctx)
    results = []
    try:
        todo = [r for r in rows if not only_qids or r["qid"] in only_qids]
        if limit:
            todo = todo[:limit]
        for i, row in enumerate(todo, 1):
            qid = row["qid"]
            role = row.get("expected_role", "admin")
            token = token_of(ctx, role)
            session = f"biz-eval-{qid}-{int(time.time())}"
            contents, metas = [], []
            turns = row.get("turns") or [{"say": row.get("question", ""), "expect": ""}]
            t0 = time.time()
            for turn in turns:
                resp = await ask(ctx, turn["say"], token, session)
                contents.append(content_of(resp))
                metas.append(meta_of(resp))
            elapsed = time.time() - t0
            score, reason = grade(row, contents)
            d_ok, d_msg = grade_design(row, metas)
            if not d_ok:
                reason = f"设计断言失败：{d_msg}｜内容判分：{reason}"
                score = 0
            v_ok, v_msg = await verify(ctx, qid)
            if not v_ok:
                score = 0
                reason += f"｜核验失败：{v_msg}"
            await teardown(ctx, qid)
            preview = contents[-1][:70].replace("\n", " ")
            mark = {2: "PASS", 1: "PART", 0: "FAIL"}[score]
            print(f"[{i}/{len(todo)}] {qid} {mark} s={elapsed:.0f}s {reason} | {preview}")
            results.append({
                "qid": qid, "category": row["category"], "role": role,
                "score": score, "reason": reason, "verify": v_msg,
                "mode": metas[-1]["mode"] if metas else "",
                "intents": [m["intent"] for m in metas],
                "delegates": [a for m in metas for a in m["delegates"]],
                "design_ok": d_ok,
                "elapsed_s": round(elapsed, 1),
                "answer": contents[-1][:500],
            })
    finally:
        await cleanup_accounts(ctx)
        await ctx.client.aclose()

    n = len(results)
    avg = sum(r["score"] for r in results) / n if n else 0
    passed = sum(1 for r in results if r["score"] == 2)
    partial = sum(1 for r in results if r["score"] == 1)
    failed = sum(1 for r in results if r["score"] == 0)
    by_cat: dict[str, dict] = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"n": 0, "sum": 0})
        c["n"] += 1
        c["sum"] += r["score"]
    summary = {
        "total": n, "avg_score": round(avg, 2), "pass": passed,
        "partial": partial, "fail": failed,
        "pass_rate": round(passed / n, 3) if n else 0,
        "by_category": {k: round(v["sum"] / v["n"], 2) for k, v in sorted(by_cat.items())},
        "fails": [r["qid"] for r in results if r["score"] < 2],
        "design_fails": [r["qid"] for r in results if not r["design_ok"]],
    }
    print("\n========== 汇总 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    REPORT_PATH.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入 {REPORT_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qids", type=str, default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    rows = [json.loads(l) for l in EVAL_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    only = set(args.qids.split(",")) if args.qids else None
    asyncio.run(run(rows, only, args.limit))


if __name__ == "__main__":
    main()

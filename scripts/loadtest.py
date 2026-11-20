"""T-1 轻量压测脚本（仓库根 scripts/，与 demo.py 同款独立设计）。

用途：交付前验证「四服务在并发下不塌方」，产出 RPS / 分位延迟 / 错误率，
压完自动复核服务健康。演示与回归双用，可重复执行。

设计取舍（照规约口径）：
- **与产品代码零耦合**：不 import 项目代码，纯 httpx 打真实三层（8080→8000→8060）；
- **只读端点做并发主体**：登录（业务写但量小）/ 健康 / 版本 / 指标看板 / 8060 看板 /
  9090 静态资源——不产生业务脏数据；
- **LLM 对话单独小流量**：对话延迟由外部 LLM API 主导，不与本地读端点混算，
  默认仅 5 条（并发 1），避免打爆上游限流、避免成本漂移；
- **判定线**：错误率 < 1% 且压后 `ops/health` 顶层仍为 up，即 PASS（轻量口径，
  非容量规划——容量规划需真实流量画像，规约如实记档）。

用法：
    python scripts/loadtest.py                     # 默认 20 并发 × 30 请求 × 5 场景
    python scripts/loadtest.py --users 50 --reqs 60
    python scripts/loadtest.py --chat 0            # 跳过 LLM 对话（离线/省额度）
    python scripts/loadtest.py --user admin --password 123456

退出码：0=PASS，1=FAIL（错误率超线或压后服务不健康）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BIZ = "http://127.0.0.1:8080"
WEB = "http://127.0.0.1:9090"
DRONE = "http://127.0.0.1:8060"

def set_targets(biz: str, web: str = WEB, drone: str = DRONE) -> None:
    """--base 覆盖目标（模块级可变配置，避免 global 声明次序问题）。"""
    global BIZ, WEB, DRONE
    BIZ, WEB, DRONE = biz.rstrip("/"), web, drone

PASS_ERROR_RATE = 0.01  # 错误率判定线（1%）


def _pctl(sorted_xs: list[float], p: float) -> float:
    """最近邻分位（样本量小，不做插值）。"""
    if not sorted_xs:
        return 0.0
    idx = min(len(sorted_xs) - 1, max(0, round(p / 100 * (len(sorted_xs) + 1)) - 1))
    return sorted_xs[idx]


def run_scenario(name: str, fn, users: int, reqs_per_user: int) -> dict:
    """users 个线程 × reqs_per_user 次请求，收集延迟（ms）与错误。"""
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local_lat: list[float] = []
        local_err: list[str] = []
        # 每线程独立连接池（真实用户模型），压测期不复用跨线程连接
        with httpx.Client(timeout=15) as client:
            for _ in range(reqs_per_user):
                t0 = time.perf_counter()
                err = fn(client)
                dt = (time.perf_counter() - t0) * 1000
                if err:
                    local_err.append(err)
                else:
                    local_lat.append(dt)
        with lock:
            latencies.extend(local_lat)
            errors.extend(local_err)

    total = users * reqs_per_user
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=users) as pool:
        list(pool.map(lambda _: worker(), range(users)))
    wall = time.perf_counter() - t0

    latencies.sort()
    ok = len(latencies)
    err = len(errors)
    return {
        "scenario": name,
        "total": total,
        "ok": ok,
        "err": err,
        "error_rate": err / total if total else 0.0,
        "rps": round(ok / wall, 1) if wall else 0.0,
        "p50": round(_pctl(latencies, 50)),
        "p95": round(_pctl(latencies, 95)),
        "p99": round(_pctl(latencies, 99)),
        "max": round(latencies[-1]) if latencies else 0,
        "err_samples": errors[:3],
    }


def make_get(url: str, needle: str | None = None, headers: dict | None = None):
    """GET 场景工厂：状态码非 2xx 或缺特征串记为错误。"""

    def hit(client: httpx.Client) -> str | None:
        try:
            r = client.get(url, headers=headers)
            if r.status_code // 100 != 2:
                return f"HTTP {r.status_code}"
            if needle and needle not in r.text:
                return "特征串缺失"
        except httpx.HTTPError as exc:
            return type(exc).__name__
        return None

    return hit


def login_once(client: httpx.Client, user: str, password: str) -> str | None:
    try:
        r = client.post(f"{BIZ}/v1/auth/login", json={"username": user, "password": password})
        body = r.json()
        if body.get("code") != 2000:
            return f"code={body.get('code')}"
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return type(exc).__name__
    return None


def chat_once(client: httpx.Client, token: str, session: str, user_id: int) -> str | None:
    try:
        r = client.post(
            f"{BIZ}/v1/agent/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "无人机电池怎么保养", "sessionId": session, "userId": user_id},
            timeout=90,
        )
        body = r.json()
        data = body.get("data") or {}
        if body.get("code") != 2000 or not (data.get("content") or "").strip():
            return f"code={body.get('code')}"
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="T-1 轻量压测")
    ap.add_argument("--base", default=BIZ)
    ap.add_argument("--users", type=int, default=20, help="并发线程数")
    ap.add_argument("--reqs", type=int, default=30, help="每线程请求数")
    ap.add_argument("--login", type=int, default=50, help="登录场景总请求数（业务写，控制量）")
    ap.add_argument("--chat", type=int, default=5, help="LLM 对话条数（0=跳过）")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()

    set_targets(args.base)
    print(f"== T-1 轻量压测 == 目标 {BIZ}  并发 {args.users} × {args.reqs} 请求/线程\n")

    # 登录拿令牌（对话场景用）
    with httpx.Client(timeout=15) as c:
        r = c.post(f"{BIZ}/v1/auth/login", json={"username": args.user, "password": args.password})
        body = r.json()
        if body.get("code") != 2000:
            print(f"FATAL 登录失败 code={body.get('code')}")
            return 1
        token = body["data"]["token"]

    scenarios: list[tuple[str, object, int, int]] = [
        ("8080 登录（业务写）", lambda cl: login_once(cl, args.user, args.password),
         args.login, 1),
        ("8080 健康聚合 /v1/agent/health", make_get(f"{BIZ}/v1/agent/health", "2000"),
         args.users, args.reqs),
        ("8080 版本 /v1/version", make_get(f"{BIZ}/v1/version", "2000"), args.users, args.reqs),
        ("8080 指标看板 /v1/agent/metrics/overview", make_get(f"{BIZ}/v1/agent/metrics/overview"),
         args.users, args.reqs),
        ("8060 无人机看板 /v1/dashboard/summary",
         make_get(f"{DRONE}/v1/dashboard/summary"), args.users, args.reqs),
        ("9090 静态资源 /version.json", make_get(f"{WEB}/version.json"), args.users, args.reqs),
    ]

    results = []
    for name, fn, u, rq in scenarios:
        res = run_scenario(name, fn, u, rq)
        results.append(res)
        print(f"{res['scenario']}: {res['ok']}/{res['total']} ok  RPS={res['rps']}  "
              f"p50={res['p50']}ms p95={res['p95']}ms p99={res['p99']}ms max={res['max']}ms  "
              f"err={res['err']}")
        if res["err_samples"]:
            print(f"  err 样例: {res['err_samples']}")

    if args.chat > 0:
        t0 = time.perf_counter()
        chat_lat: list[float] = []
        chat_err = 0
        with httpx.Client(timeout=90) as c:
            for i in range(args.chat):
                t1 = time.perf_counter()
                err = chat_once(c, token, f"loadtest-{int(time.time())}-{i}", 1)
                chat_lat.append((time.perf_counter() - t1) * 1000)
                if err:
                    chat_err += 1
        chat_lat.sort()
        wall = time.perf_counter() - t0
        print(f"对话链路（含外部 LLM）: {args.chat - chat_err}/{args.chat} ok  "
              f"p50={round(_pctl(chat_lat, 50))}ms max={round(chat_lat[-1]) if chat_lat else 0}ms  "
              f"耗时 {wall:.1f}s  err={chat_err}")
        if chat_err:
            results.append({"scenario": "对话链路", "error_rate": chat_err / args.chat,
                            "err_samples": ["见上"]})
        else:
            results.append({"scenario": "对话链路", "error_rate": 0.0, "err_samples": []})

    # 压后服务健康复核
    print("\n== 压后健康复核 ==")
    with httpx.Client(timeout=15) as c:
        h = c.get(f"{BIZ}/v1/ops/health").json()
        data = h.get("data") or {}
        summary = data.get("summary") or {}
        top = data.get("status")
        print(f"ops/health 顶层 = {top}  up={summary.get('up')} "
              f"degraded={summary.get('degraded')} down={summary.get('down')}")
        post_ok = top == "up"

    worst = max((r["error_rate"] for r in results), default=0.0)
    verdict = "PASS" if worst < PASS_ERROR_RATE and post_ok else "FAIL"
    print(f"\n最差错误率 = {worst:.2%}（判定线 {PASS_ERROR_RATE:.0%}）  压后健康 = {post_ok}"
          f"  => **{verdict}**")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

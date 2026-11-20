"""P1-c：传统后台操作 vs Agent 一句话——耗时对比实证脚本。

测量口径：
- 传统路径：复现操作员在管控后台完成同一目标的 REST 调用序列，
  记录机器耗时；人工时间按每步 UI 操作（切页/筛选/读数）估算
  （默认 20s/步，可用 --human-step 调整），因为真实瓶颈是人不是接口。
- Agent 路径：向 agt_backend 发一句话，记录端到端墙钟时间
  （含 LLM 意图路由、工具循环、生成）。

用法：
    python scripts/time_compare.py
    python scripts/time_compare.py --human-step 15
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings

AGT_CHAT = "http://127.0.0.1:8080/v1/agent/chat"

# 三个对比场景：名称、一句话指令、传统后台需要的 REST 步骤序列
SCENARIOS = [
    {
        "name": "运营总览",
        "ask": "现在整体运营情况怎么样？",
        "manual_steps": [
            ("登录后台", "POST /v1/user/login"),
            ("打开运营总览页", "GET /v1/dashboard/summary"),
        ],
    },
    {
        "name": "设备/任务/告警综合检查",
        "ask": "现在有哪些设备？多少任务在跑？有多少告警没处理？",
        "manual_steps": [
            ("登录后台", "POST /v1/user/login"),
            ("切到设备管理页查设备", "GET /v1/drone/select"),
            ("切到任务管理页查任务", "GET /v1/task/select"),
            ("切到告警中心查未处置告警", "GET /v1/alarm/list"),
            ("回Excel/口头汇总三组数字", "-"),
        ],
    },
    {
        "name": "巡检媒体资料盘点",
        "ask": "现在有哪些任务产出过影像资料？各有多少文件？",
        "manual_steps": [
            ("登录后台", "POST /v1/user/login"),
            ("切到媒体中心按任务分组", "GET /v1/media/selectGroupedByTask"),
        ],
    },
]


async def manual_path(http: httpx.AsyncClient) -> dict:
    """逐接口复现传统操作，返回每步机器耗时。"""
    steps_ms = []
    # 1. 登录（操作员每天至少一次）
    t0 = time.perf_counter()
    r = await http.post(
        "/v1/user/login", json={"username": "admin", "password": "123456"}
    )
    token = r.json()["data"]["token"]
    steps_ms.append(("登录后台", (time.perf_counter() - t0) * 1000))
    headers = {"Authorization": f"Bearer {token}"}
    # 2. 依次完成各查询页
    for url in ["/v1/dashboard/summary", "/v1/drone/select", "/v1/task/list",
                "/v1/alarm/list", "/v1/media/selectGroupedByTask"]:
        t0 = time.perf_counter()
        await http.get(url, headers=headers)
        steps_ms.append((url, (time.perf_counter() - t0) * 1000))
    return {"steps": steps_ms}


async def agent_path(http: httpx.AsyncClient, agt_token: str, ask: str) -> dict:
    """Agent 一句话端到端耗时。"""
    t0 = time.perf_counter()
    r = await http.post(
        AGT_CHAT,
        headers={"Authorization": f"Bearer {agt_token}"},
        json={"message": ask, "userId": 1},
    )
    ms = (time.perf_counter() - t0) * 1000
    body = r.json()
    content = (body.get("data") or {}).get("content") or ""
    return {"ms": ms, "intent": (body.get("data") or {}).get("intent"), "content": content}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--human-step", type=float, default=20.0, help="传统 UI 每步人工耗时（秒）")
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=settings.jsdstu_base_url, timeout=30) as biz, \
               httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=180) as agt:
        # Agent 通道登录（代理登录）
        r = await agt.post("/v1/auth/login", json={"username": "admin", "password": "123456"})
        agt_token = r.json()["data"]["token"]

        print(f"\n## 传统后台 vs Agent 一句话（UI 每步人工耗时按 {args.human_step:.0f}s 估算）\n")
        print("| 场景 | 传统步骤数 | 传统机器耗时 | 传统人工耗时(估) | Agent 端到端 | 提速倍数(人工口径) |")
        print("|---|---|---|---|---|---|")
        for sc in SCENARIOS:
            m = await manual_path(biz)
            manual_machine = sum(ms for _, ms in m["steps"]) / 1000
            manual_human = len(m["steps"]) * args.human_step
            a = await agent_path(agt, agt_token, sc["ask"])
            agent_s = a["ms"] / 1000
            speedup = manual_human / agent_s if agent_s > 0 else 0
            print(
                f"| {sc['name']} | {len(m['steps'])} | {manual_machine:.2f}s "
                f"| {manual_human:.0f}s | {agent_s:.1f}s | **{speedup:.1f}x** |"
            )
            print(f"  - 问句：{sc['ask']}")
            print(f"  - 意图：{a['intent']} ｜ 回答前 80 字：{a['content'][:80]}")


if __name__ == "__main__":
    asyncio.run(main())

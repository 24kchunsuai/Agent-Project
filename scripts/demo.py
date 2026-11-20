#!/usr/bin/env python3
"""E-15 一键演示 / 回归脚本 —— 跑通核心链路：登录 → RAG 问答 → 业务工具 → 确认门 → 看板数据。

设计定位（规约 E-15）：
  * 演示用途：逐步打印每一步做了什么、看到了什么，适合给评审/验收方当面跑；
  * 回归用途：每步都有硬断言，全绿退出码 0，任一步红退出码 1；
  * 打真实三层（8080 → 8000 → 8060），不 import 项目代码 —— 与产品代码零耦合，
    交付包里单独拿这一个文件 + 一个带 httpx 的 python3 就能跑。

用法：
    python scripts/demo.py                      # 全部默认值（本机 8080 / 8060，admin）
    python scripts/demo.py --base URL --biz URL # 指定后端与业务系统地址
    python scripts/demo.py --user admin --password 123456

说明：
  * 确认门一节会真实创建一个「E15DEMO-」前缀的演示任务并在收尾删除（同样走确认流程）；
    取消路径用的是"删除设备"，只走到取消、绝不执行，不碰任何存量数据。
  * RAG 问句取自评测集 eval-002（实名登记平台），答案确定性高（UOM / uas.caac.gov.cn）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time

try:
    import httpx
except ImportError:
    print("缺少依赖 httpx：请先 pip install httpx（或使用项目 requirements 安装的环境）")
    sys.exit(2)

RAG_QUESTION = "无人机实名登记在哪个平台办理？网址是什么？"
RAG_NEEDLES = ("UOM", "uas.caac.gov.cn")
BIZ_QUESTION = "现在有哪些设备？"
CHAT_TIMEOUT = 180.0

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n[{title}]")


def one_line(text: str, limit: int = 70) -> str:
    return re.sub(r"\s+", " ", text)[:limit]


class Demo:
    def __init__(self, base: str, biz: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.biz = biz.rstrip("/")
        self.user = user
        self.password = password
        self.token = ""
        self.user_id = 1
        self.session = f"e15-demo-{int(time.time())}"
        self.task_name = f"E15DEMO-演示任务-{int(time.time())}"

    # ---------- 基础请求 ----------
    def chat(self, message: str, session: str | None = None) -> dict:
        r = httpx.post(
            f"{self.base}/v1/agent/chat",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"message": message, "sessionId": session or self.session,
                  "userId": self.user_id},
            timeout=CHAT_TIMEOUT,
        )
        body = r.json()
        return body.get("data") or {} if body.get("code") == 2000 else {"_code": body.get("code"),
                                                                        "_message": body.get("message"), "content": ""}

    def biz_task_exists(self, name: str) -> bool:
        """直查 jsdstu-3（8060）核验任务是否落库 —— 不信模型自述。"""
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{self.biz}/v1/user/login",
                       json={"username": self.user, "password": self.password})
            token = (r.json().get("data") or {}).get("token")
            if not token:
                return False
            r = c.get(f"{self.biz}/v1/task/list",
                      headers={"Authorization": f"Bearer {token}"}, params={"pageSize": 50})
            data = r.json().get("data") or {}
            rows = data.get("list") if isinstance(data, dict) else data
            return any(name in (t.get("taskName") or "") for t in (rows or []))

    # ---------- 六步 ----------
    def step1_login(self) -> bool:
        section("1/6 登录（8080 统一鉴权入口）")
        r = httpx.post(f"{self.base}/v1/auth/login",
                       json={"username": self.user, "password": self.password}, timeout=15)
        body = r.json()
        ok = body.get("code") == 2000
        data = body.get("data") or {}
        self.token = data.get("token") or ""
        self.user_id = data.get("userId") or data.get("id") or 1
        check("登录成功拿到 token", ok and bool(self.token),
              "" if ok else one_line(str(body)))
        return ok

    def step2_health(self) -> None:
        section("2/6 链路健康（8080 穿透 8000 + 全组件聚合）")
        r = httpx.get(f"{self.base}/v1/agent/health", timeout=15)
        body = r.json()
        check("8080 → 8000 穿透探活 code=2000", body.get("code") == 2000,
              one_line(str(body.get("data") or body)))
        r = httpx.get(f"{self.base}/v1/ops/health", timeout=15)
        body = r.json()
        data = body.get("data") or {}
        status = data.get("status")
        check("健康聚合 status ∈ {up, degraded}（组件级问题降级不算死）",
              body.get("code") == 2000 and status in ("up", "degraded"), f"status={status}")

    def step3_rag(self) -> None:
        section("3/6 RAG 知识问答（评测集 eval-002：实名登记平台）")
        d = self.chat(RAG_QUESTION, session=f"{self.session}-rag")
        content = d.get("content") or ""
        check("对话返回 code=2000 且有 traceId", bool(d.get("traceId")),
              f"traceId={d.get('traceId')} intent={d.get('intent')}")
        check("RAG 判定为 kept（证据充分放行生成）", d.get("ragDecision") == "kept",
              f"ragDecision={d.get('ragDecision')}")
        check("带引用来源（citations ≥ 1）", len(d.get("citations") or []) >= 1,
              f"citations={len(d.get('citations') or [])}")
        check("答案命中确定性要点（UOM 平台 / 官网）",
              any(n in content for n in RAG_NEEDLES), one_line(content, 90))

    def step4_tools(self) -> None:
        section("4/6 业务工具调用（设备查询，REACT 工具链）")
        d = self.chat(BIZ_QUESTION, session=f"{self.session}-biz")
        trace = d.get("toolTrace") or []
        check("识别为 business 意图", d.get("intent") == "business", f"intent={d.get('intent')}")
        check("业务工具真实调用（toolTrace 非空）", len(trace) >= 1,
              "toolTrace=" + one_line(str([(t.get("tool"), t.get("ok")) for t in trace])))
        check("回答包含设备数据", bool(d.get("content")), one_line(d.get("content") or "", 60))

    def step5_confirm_gate(self) -> None:
        section("5/6 写操作确认门（拦截 → 确认执行 → 取消 → 收尾自清理）")
        # 5a 拦截：挂起待确认，数据零改动
        d = self.chat(f"帮我创建一个飞行任务，名字叫「{self.task_name}」，优先级低")
        check("写操作被拦截转确认（系统话术）",
              "确认请回复" in (d.get("content") or ""), one_line(d.get("content") or ""))
        check("拦截后数据零改动（8060 直查无此任务）", not self.biz_task_exists(self.task_name))

        # 5b 确认执行：真正落库
        d = self.chat(f"帮我创建一个飞行任务，名字叫「{self.task_name}」，优先级低",
                      session=f"{self.session}-exec")
        if not check("再次挂起待确认", "确认请回复" in (d.get("content") or ""),
                     one_line(d.get("content") or "")):
            return
        d = self.chat("确认", session=f"{self.session}-exec")
        check("回复「确认」→ 系统回执已执行", "已执行" in (d.get("content") or ""),
              one_line(d.get("content") or ""))
        check("执行后任务真实落库（8060 直查）", self.biz_task_exists(self.task_name))

        # 5c 取消：只走到取消，不执行（删设备是危险操作，正好验证取消路径）
        d = self.chat("帮我把设备 1 删掉", session=f"{self.session}-cancel")
        check("危险操作同样被拦截", "确认" in (d.get("content") or ""),
              one_line(d.get("content") or ""))
        d = self.chat("取消", session=f"{self.session}-cancel")
        check("回复「取消」→ 不执行", "已执行" not in (d.get("content") or ""),
              one_line(d.get("content") or ""))

        # 5d 收尾：删除演示任务（同样走确认流程）
        if self.biz_task_exists(self.task_name):
            d = self.chat(f"把任务「{self.task_name}」删掉", session=f"{self.session}-cleanup")
            if "确认" in (d.get("content") or ""):
                d = self.chat("确认", session=f"{self.session}-cleanup")
                check("收尾：删除演示任务", "已执行" in (d.get("content") or ""),
                      one_line(d.get("content") or ""))
                check("收尾后无残留", not self.biz_task_exists(self.task_name))
            else:
                check("收尾：删除演示任务", False, "未能挂起删除确认：" + one_line(d.get("content") or ""))

    def step6_dashboard(self) -> None:
        section("6/6 看板数据（metrics 聚合 + 成本计量）")
        r = httpx.get(f"{self.base}/v1/agent/metrics/overview",
                      headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        body = r.json()
        check("总览 code=2000", body.get("code") == 2000,
              one_line(str(body.get("data") or body), 90))
        r = httpx.get(f"{self.base}/v1/agent/metrics/costs",
                      headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        body = r.json()
        data = body.get("data") or {}
        check("成本与配额 code=2000 且结构完整（totals / byModel）",
              body.get("code") == 2000 and isinstance(data.get("totals"), dict)
              and "byModel" in data, one_line(str(data), 90))
        print("\n  提示：浏览器打开前端（如 http://127.0.0.1:9090）可查看上述数据的可视化看板。")

    def run(self) -> int:
        print(f"=== drone-agent001 核心链路演示 ===")
        print(f"后端 {self.base} ｜ 业务系统 {self.biz} ｜ 会话 {self.session}")
        if not self.step1_login():
            print("\n登录失败：请确认四服务已启动（deploy/scripts/start-all.ps1）、账号密码正确。")
            return 1
        self.step2_health()
        self.step3_rag()
        self.step4_tools()
        self.step5_confirm_gate()
        self.step6_dashboard()
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        print(f"\n========== {passed}/{total} 通过 ==========")
        if passed < total:
            print("存在未通过项：对照 docs/验收清单.md 与 docs/部署手册.md §7 排查。")
        return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="drone-agent001 核心链路演示/回归")
    ap.add_argument("--base", default="http://127.0.0.1:8080", help="业务后端（8080）地址")
    ap.add_argument("--biz", default="http://127.0.0.1:8060", help="无人机主控（8060）地址")
    ap.add_argument("--user", default="admin", help="登录账号（默认 admin）")
    ap.add_argument("--password", default="123456", help="登录密码（默认 123456）")
    args = ap.parse_args()
    return Demo(args.base, args.biz, args.user, args.password).run()


if __name__ == "__main__":
    sys.exit(main())

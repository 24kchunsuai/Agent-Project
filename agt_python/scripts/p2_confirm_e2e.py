"""P2 二次确认流程端到端验证（走真实三层：9090 前端省略，直打 8080 → 8000 → 8060）。

覆盖场景：
1. 写操作被拦截：模型调用写工具 → 只回确认话术，数据零改动
2. 确认执行：回「确认」→ 真正落库（用创建任务验证）
3. 取消：回「取消」→ 不执行，数据零改动
4. 改问作废：挂起后问别的内容 → 动作作废，按新请求正常处理
5. 匿名：未登录说写操作 → 安全话术，不进确认流程
6. 收尾：把测试创建的任务删掉（同样走确认），保证环境干净

用法：python scripts/p2_confirm_e2e.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.test_guard import isolate  # noqa: E402
# 进程侧数据目录隔离（见 app/core/test_guard.py）。本脚本刻意打**真实三层**
# （8080 → 8000 → 8060）以验证二次确认链路，因此不做服务侧隔离断言——
# 它在知识库上没有写操作（只走对话与业务工具），残留面仅限会话记录。
DATA_DIR = isolate()

import httpx

from app.core.config import settings

AGT_URL = "http://127.0.0.1:8080"
TEST_SESSION = "p2-confirm-e2e"
TEST_TASK = "E2E-注册验证-勿留"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


async def ask(agt: httpx.AsyncClient, message: str, *, session: str = TEST_SESSION,
              token: str | None = None, user_id: int = 1) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await agt.post(
        "/v1/agent/chat",
        headers=headers,
        json={"message": message, "sessionId": session, "userId": user_id},
        timeout=180,
    )
    return r.json().get("data") or {}


async def task_exists(name: str) -> bool:
    """直查 jsdstu-3 REST 客观核验任务是否存在（不信模型自述）。"""
    async with httpx.AsyncClient(base_url=settings.jsdstu_base_url, timeout=15) as biz:
        r = await biz.post("/v1/user/login", json={"username": "admin", "password": "123456"})
        token = r.json()["data"]["token"]
        r = await biz.get(
            "/v1/task/list", headers={"Authorization": f"Bearer {token}"}, params={"pageSize": 50}
        )
        data = r.json().get("data") or {}
        records = data.get("list") if isinstance(data, dict) else data
        return any(name in (t.get("taskName") or "") for t in (records or []))


async def pilot_role_scenario(agt: httpx.AsyncClient) -> None:
    """真实非 admin 用户：写操作被角色校验拦住、查询仍可用（端到端）。

    做法：用 admin 直连 jsdstu-3 建一个 pilot 账号（默认密码 123456）→ 用该账号
    登录 agt_backend → 试写操作应被拒 → 试查询应正常 → 收尾删掉该账号。
    """
    uname = "e2e_pilot"
    async with httpx.AsyncClient(base_url=settings.jsdstu_base_url, timeout=20) as biz:
        r = await biz.post("/v1/user/login", json={"username": "admin", "password": "123456"})
        admin_t = r.json()["data"]["token"]
        ah = {"Authorization": f"Bearer {admin_t}"}

        async def find_uid() -> int | None:
            r = await biz.get("/v1/user/select", headers=ah, params={"username": uname})
            data = r.json().get("data") or {}
            rows = data.get("list") if isinstance(data, dict) else data
            return (rows or [{}])[0].get("id") if rows else None

        # 建号（同名残留先清掉，保证可重跑）
        existing = await find_uid()
        if existing:
            await biz.post(f"/v1/user/delete/{existing}", headers=ah)
        r = await biz.post("/v1/user/save", headers=ah, params={
            "username": uname, "email": f"{uname}@e2e.local", "phone": "13900000001",
            "age": 28, "gender": "男", "status": "1", "permission": "pilot",
        })
        created = r.json().get("code") == 2000
        uid = await find_uid()
        check("准备 pilot 账号（角色 data 前置）", created and uid is not None, f"uid={uid}")
        if not created or uid is None:
            return

        try:
            # 用 pilot 登录 agt_backend
            r = await agt.post("/v1/auth/login", json={"username": uname, "password": "123456"})
            pilot_t = (r.json().get("data") or {}).get("token")
            check("pilot 登录成功", bool(pilot_t))

            r = await ask(agt, "帮我把设备 1 删掉", token=pilot_t, session="p2-role", user_id=uid)
            content = r.get("content") or ""
            check("pilot 写操作被角色校验拒绝", "管理员权限" in content and "确认请回复" not in content,
                  content[:80].replace("\n", " "))

            r = await ask(agt, "现在有哪些设备？", token=pilot_t, session="p2-role-q", user_id=uid)
            content = r.get("content") or ""
            check("pilot 查询类仍可用（不影响只读业务）", "设备" in content, content[:60].replace("\n", " "))
        finally:
            await biz.post(f"/v1/user/delete/{uid}", headers=ah)
            check("收尾：删除 pilot 测试账号", await find_uid() is None)


async def main() -> None:
    async with httpx.AsyncClient(base_url=AGT_URL) as agt:
        login = await agt.post("/v1/auth/login", json={"username": "admin", "password": "123456"})
        token = login.json()["data"]["token"]
        print(f"已登录 admin，会话固定为 {TEST_SESSION}\n")

        # ---- 1. 写操作被拦截（只回确认话术） ----
        r1 = await ask(agt, f"帮我创建一个飞行任务，名字叫「{TEST_TASK}」，优先级低", token=token)
        blocked = "确认" in (r1.get("content") or "") and "写操作" in (r1.get("content") or "")
        check("写操作被拦截转确认", blocked, (r1.get("content") or "")[:60].replace("\n", " "))
        check("拦截后数据零改动", not await task_exists(TEST_TASK))

        # ---- 2. 改问作废（挂起后问别的） ----
        await ask(agt, f"帮我创建一个飞行任务，名字叫「{TEST_TASK}」，优先级低", token=token)
        r2 = await ask(agt, "现在有哪些空闲设备？", token=token)
        voided = "确认" not in (r2.get("content") or "") and (
            "设备" in (r2.get("content") or "") or "空闲" in (r2.get("content") or "")
        )
        check("挂起后改问 → 动作作废并按新问题回答", voided, (r2.get("content") or "")[:60].replace("\n", " "))

        # ---- 3. 确认执行 ----
        r3 = await ask(agt, f"帮我创建一个飞行任务，名字叫「{TEST_TASK}」，优先级低", token=token)
        # 严格断言：必须是系统确认话术（模型自己问"要执行吗"不算挂起）
        check("再次挂起待确认", "确认请回复" in (r3.get("content") or ""),
              (r3.get("content") or "")[:60].replace("\n", " "))
        r4 = await ask(agt, "确认", token=token)
        executed = "已执行" in (r4.get("content") or "")
        check("回复「确认」→ 真正执行（系统回执而非模型自述）", executed,
              (r4.get("content") or "")[:70].replace("\n", " "))
        check("执行后数据确实落库", await task_exists(TEST_TASK))

        # ---- 4. 取消（对设备删除做，避免动数据） ----
        r5 = await ask(agt, "帮我把设备 1 删掉", token=token, session="p2-cancel")
        check("删除设备被拦截转确认", "确认" in (r5.get("content") or ""), (r5.get("content") or "")[:60].replace("\n", " "))
        r6 = await ask(agt, "取消", token=token, session="p2-cancel")
        check("回复「取消」→ 不执行", "取消" in (r6.get("content") or "") and "已执行" not in (r6.get("content") or ""),
              (r6.get("content") or "")[:60].replace("\n", " "))

        # ---- 5. 匿名写操作：不进确认流程 ----
        r7 = await ask(agt, "帮我把设备 1 删掉", session="p2-anon", token=None)
        check("匿名写操作走安全话术（无待确认）", "确认请回复" not in (r7.get("content") or ""),
              (r7.get("content") or "")[:70].replace("\n", " "))

        # ---- 6. 收尾：删除测试任务（同样走确认） ----
        if await task_exists(TEST_TASK):
            r8 = await ask(agt, f"把任务「{TEST_TASK}」删掉", token=token)
            if "确认" in (r8.get("content") or ""):
                r9 = await ask(agt, "确认", token=token)
                check("收尾：删除测试任务", "已执行" in (r9.get("content") or ""),
                      (r9.get("content") or "")[:80].replace("\n", " "))
                check("收尾后无残留", not await task_exists(TEST_TASK))
            else:
                check("收尾：删除测试任务", False, "未能挂起删除确认：" + (r8.get("content") or "")[:80])

        # ---- 7. 真实非 admin 用户的角色校验 ----
        await pilot_role_scenario(agt)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n========== {passed}/{len(results)} 通过 ==========")


if __name__ == "__main__":
    asyncio.run(main())

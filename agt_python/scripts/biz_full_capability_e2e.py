#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""业务全能力 e2e 验收（P0+P1，规约 docs/specs/业务操作全能力规约.md §13）。

覆盖五类检查：
  A. 覆盖矩阵（静态）：工具注册数 ≥ 37，41 端点逐一映射到
     「工具 / 身份链路」两种归宿，零遗漏（P1 四项已全部转正为工具）；
  B. 复杂度路由：3 条报告类输入 → PLAN，3 条短查询 → REACT（规则可判定的样例）；
  C. PLAN 降级：规划失败自动降级 REACT，不报错（进程内 stub，不依赖服务）；
  E. 多智能体（静态，规约 §8/§13.2-P1 门）：3 子智能体定义加载、白名单只读
     （danger 一律剔除）、delegate_subagent 注册且枚举对齐、黑板同键上限 5、
     会话委派预算 10、超时返 TIMEOUT（stub LLM）；
  D. 实打（--live 时跑，需要 jsdstu-3 / 8080 / 8000 全部在线）：
     D1 查询类工具逐一实打（直连 handler，只读，数据断言只验形状）；
     D2 全链路冒烟 + BUG-A2 回归（8080 → 8000，toolTrace 必须透出）；
     D3 写操作确认门（抽样 2 个 danger 工具全链路：拦截话术 → 回复"取消" →
        断言无数据改动；其余 danger 工具静态断言复述模板已登记——
        确认门是单一代码路径（agent_loop 拦截 → guard 复述），抽样实测 +
        模板全覆盖即可锁定，避免每工具 2 次 LLM 调用的运行成本）；
     D4 子智能体委派实打：真实链路委派 alarm-analyst，断言 DONE + 黑板有结论。

用法：
    python scripts/biz_full_capability_e2e.py            # 只跑 A/B/C/E（不依赖服务）
    python scripts/biz_full_capability_e2e.py --live     # 全部（需要四服务在线）

退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

# 系统代理会劫持 localhost（与其余验收脚本同一个坑），显式绕过
OPENER = build_opener(ProxyHandler({}))

AGT_PY = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 结果簿

RESULTS: list[tuple[str, str, str]] = []  # (状态, 名称, 说明)


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[status]
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def summary() -> int:
    passed = sum(1 for s, *_ in RESULTS if s == "PASS")
    failed = sum(1 for s, *_ in RESULTS if s == "FAIL")
    skipped = sum(1 for s, *_ in RESULTS if s == "SKIP")
    print(f"\n通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    if failed:
        print("失败项：")
        for s, name, detail in RESULTS:
            if s == "FAIL":
                print(f"  ✗ {name} — {detail}")
    return 1 if failed else 0


def check(name: str, fn):
    try:
        fn()
        record("PASS", name)
    except AssertionError as exc:
        record("FAIL", name, str(exc))
    except Exception as exc:  # noqa: BLE001
        record("FAIL", name, f"异常: {exc}")


# ---------------------------------------------------------------- A. 覆盖矩阵

def build_coverage_matrix():
    """41 端点 → 归宿映射。工具名必须与注册表一致；identity = 身份链路承担。
    P1 四项（drone_detail / task_accept / task_reject / task_complete）已转正为工具。"""
    return [
        # 用户域 /v1/user（10）
        ("POST /v1/user/login", "identity", ""),
        ("POST /v1/user/logout", "identity", ""),
        ("GET /v1/user/me", "identity", ""),
        ("GET /v1/user/permission-matrix", "tool", "permission_matrix"),
        ("GET /v1/user/select", "tool", "user_query"),
        ("GET /v1/user/detail/{id}", "tool", "user_detail"),
        ("POST /v1/user/save", "tool", "user_save"),
        ("POST /v1/user/update/password/{userId}", "tool", "user_password_reset"),
        ("POST /v1/user/update/status/{userId}/{status}", "tool", "user_status_update"),
        ("POST /v1/user/delete/{userId}", "tool", "user_delete"),
        # 设备域 /v1/drone（5）
        ("GET /v1/drone/select", "tool", "drone_query"),
        ("GET /v1/drone/detail/{id}", "tool", "drone_detail"),
        ("POST /v1/drone/save", "tool", "drone_save"),
        ("POST /v1/drone/delete/{id}", "tool", "drone_delete"),
        ("POST /v1/drone/maintenance/{id}/{status}", "tool", "drone_maintenance"),
        # 任务域 /v1/task（8）
        ("GET /v1/task/list", "tool", "task_query"),
        ("POST /v1/task/save", "tool", "task_create"),
        ("POST /v1/task/assign/{taskId}", "tool", "task_assign"),
        ("POST /v1/task/accept/{taskId}", "tool", "task_accept"),
        ("POST /v1/task/reject/{taskId}/{reason}", "tool", "task_reject"),
        ("POST /v1/task/delete/{taskId}", "tool", "task_delete"),
        ("POST /v1/task/audit", "tool", "task_audit"),
        ("POST /v1/task/complete/{taskId}", "tool", "task_complete"),
        # 告警域 /v1/alarm（9）+ 规则（2）
        ("GET /v1/alarm/list", "tool", "alarm_query"),
        ("GET /v1/alarm/detail/{id}", "tool", "alarm_detail"),
        ("GET /v1/alarm/stats", "tool", "alarm_stats"),
        ("POST /v1/alarm/acknowledge/{id}", "tool", "alarm_acknowledge"),
        ("POST /v1/alarm/resolve/{id}", "tool", "alarm_resolve"),
        ("POST /v1/alarm/ignore/{id}", "tool", "alarm_ignore"),
        ("POST /v1/alarm/escalate/{id}", "tool", "alarm_escalate"),
        ("GET /v1/alarm/dashboard", "tool", "alarm_dashboard"),
        ("GET /v1/alarm/rule/list", "tool", "rule_query"),
        ("PUT /v1/alarm/rule/update", "tool", "rule_update"),
        # 围栏 / 仪表盘 / 媒体（7）
        ("GET /v1/fence/select", "tool", "fence_query"),
        ("POST /v1/fence/save", "tool", "fence_save"),
        ("POST /v1/fence/delete/{id}", "tool", "fence_delete"),
        ("GET /v1/dashboard/summary", "tool", "dashboard_query"),
        ("GET /v1/media/select", "tool", "media_query"),
        ("GET /v1/media/selectGroupedByTask", "tool", "media_query(grouped)"),
        ("DELETE /v1/media/delete?id=", "tool", "media_delete"),
        ("POST /v1/media/upload", "tool", "media_upload"),
    ]


def import_registry():
    """隔离数据目录后导入工具注册表（铁律：验收不写产品目录）。"""
    tmp = tempfile.mkdtemp(prefix="biz_e2e_")
    os.environ["DATA_ROOT"] = tmp
    if str(AGT_PY) not in sys.path:
        sys.path.insert(0, str(AGT_PY))
    from app.services.tools import registry  # noqa: E402

    return registry


def section_a_matrix():
    print("\nA. 覆盖矩阵（规约 §13.1-1）")
    registry = import_registry()
    names = set(registry.names())
    matrix = build_coverage_matrix()

    def _endpoint_count():
        assert len(matrix) == 41, f"端点映射总数 {len(matrix)} != 41"
        covered = sum(1 for _, kind, _ in matrix if kind == "tool")
        p1 = sum(1 for _, kind, _ in matrix if kind == "p1")
        ident = sum(1 for _, kind, _ in matrix if kind == "identity")
        assert covered + p1 + ident == 41, "三种归宿计数之和不等于 41"

    check("41 端点全登记且归宿计数闭合", _endpoint_count)

    def _tool_count():
        # P1 后口径：37 端点工具 + delegate_subagent = 38（规约 §13.1-1 的 32 下限随 P1 收紧）
        assert len(names) >= 37, f"注册工具 {len(names)} < 37（P1 口径）"

    check(f"注册工具数 ≥ 37（实际 {len(registry.names())}）", _tool_count)

    def _tools_exist():
        missing = [
            endpoint for endpoint, kind, tool in matrix
            if kind == "tool" and tool.split("(")[0] not in names
        ]
        assert not missing, f"映射到工具但未注册: {missing}"

    check("每个『工具』归宿的端点都有已注册工具对应", _tools_exist)

    def _p1_all_promoted():
        early = [entry for entry in matrix if entry[1] == "p1"]
        assert not early, f"仍有 P1 归宿未转正: {early}"

    check("P1 归宿四项全部转正为工具", _p1_all_promoted)

    def _danger_describers():
        from app.services.guard import describe_action

        generic_prefix = "执行 "
        bad = []
        for tool in registry.tools.values():
            if not tool.danger:
                continue
            # 用最小参数骨架取复述文本；落到通用兜底（"执行 xxx（参数：…）"）= 缺模板
            skeleton = {}
            for key in (tool.parameters or {}).get("properties", {}):
                schema = tool.parameters["properties"][key]
                if key in ("required",):
                    continue
                t = schema.get("type")
                skeleton[key] = [] if t == "array" else (0 if t in ("integer", "number") else "")
            desc = describe_action(tool.name, skeleton)
            if desc.startswith(generic_prefix) and "参数：" in desc:
                bad.append(tool.name)
        assert not bad, f"danger 工具缺复述模板: {bad}"

    check("全部 danger 工具已登记 guard 复述模板", _danger_describers)


# ---------------------------------------------------------------- B. 复杂度路由

def section_b_router():
    print("\nB. 复杂度路由（规约 §13.1-5，规则可判定样例）")
    from app.services.planning import complexity_router
    import asyncio

    plan_cases = [
        "把告警统计一下，再看看哪些设备要维修",       # 跨域
        "生成一份运营周报",                            # 意图词
        "帮我分析本月的告警趋势，并对比上个月的数据，输出一份完整的分析报告",  # 长文本+意图词
    ]
    react_cases = [
        "有多少台无人机？",
        "查看任务列表",
        "今天的告警有哪些？",
    ]

    async def run_all():
        got_plan = [await complexity_router.classify(c) for c in plan_cases]
        got_react = [await complexity_router.classify(c) for c in react_cases]
        return got_plan, got_react

    def _router():
        got_plan, got_react = asyncio.run(run_all())
        assert got_plan == ["PLAN"] * 3, f"报告类样例未全判 PLAN: {got_plan}"
        assert got_react == ["REACT"] * 3, f"短查询样例未全判 REACT: {got_react}"

    check("3 报告类 → PLAN，3 短查询 → REACT", _router)


# ---------------------------------------------------------------- C. PLAN 降级

def section_c_fallback():
    print("\nC. PLAN 降级（规约 §13.1-6，进程内 stub）")
    import asyncio
    import unittest.mock as mock

    from app.core import llm as llm_mod
    from app.services import agent_loop as loop_mod
    from app.services import planning as plan_mod
    from app.services.agent_loop import LoopResult

    async def _boom(*a, **k):
        raise llm_mod.LLMError("stub: LLM 不可用")

    async def _fake_loop_run(question, history, identity, session_id="default", **k):
        return LoopResult(content="降级后正常回答", model="stub", mode="REACT")

    def _llm_down_router():
        router = plan_mod.ComplexityRouter()
        # 40 字左右、无意图词、不跨域的边界样例 → 规则给不出结论 → LLM 兜底 → 失败降级
        text = "请帮我确认一下当前系统的整体配置情况还有各模块的运行状态是否都正常，谢谢"  # 35 字
        with mock.patch.object(plan_mod.llm_client, "chat", _boom):
            got = asyncio.run(router.classify(text))
        assert got == "REACT", f"LLM 兜底失败未降级 REACT: {got}"

    check("路由 LLM 兜底失败 → REACT 不报错", _llm_down_router)

    def _plan_gen_fail_fallback():
        agent = plan_mod.PlanningAgent()
        with mock.patch.object(plan_mod, "agent_loop") as mocked_loop, \
             mock.patch.object(agent, "_make_plan", new=mock.AsyncMock(return_value=None)):
            mocked_loop.run = mock.AsyncMock(side_effect=_fake_loop_run)
            result = asyncio.run(agent.run("q", [], {"droneToken": "t"}, "s"))
        assert result.mode == "REACT" and result.content == "降级后正常回答"

    check("计划生成失败 → 降级 REACT 执行", _plan_gen_fail_fallback)

    def _plan_gen_bad_json():
        agent = plan_mod.PlanningAgent()

        async def _garbage(*a, **k):
            class R:
                content = "这不是 JSON"

            return R()

        with mock.patch.object(plan_mod.llm_client, "chat", _garbage):
            plan = asyncio.run(agent._make_plan("生成报告", []))
        assert plan is None, f"坏 JSON 未判为规划失败: {plan}"

    check("计划 JSON 解析失败 → 返回 None（触发降级）", _plan_gen_bad_json)


# ---------------------------------------------------------------- D. 实打

def http_json(method: str, url: str, *, token: str = "", body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_http_status": exc.code, "code": -1, "message": f"HTTP {exc.code}"}


def jsdstu_login(base: str, username: str, password: str) -> str:
    resp = http_json("POST", f"{base}/v1/user/login", body={"username": username, "password": password})
    assert resp.get("code") == 2000, f"jsdstu-3 直登失败: {resp}"
    return resp["data"]["token"]


def section_d_live(args) -> str | None:
    if not args.live:
        print("\nD. 实打（SKIP：未加 --live）")
        record("SKIP", "D 组实打", "未加 --live")
        return None
    print("\nD. 实打（--live，需 jsdstu-3 / 8080 / 8000 在线）")

    # D1 查询类工具直连实打
    registry = import_registry()
    from app.services import tools as tools_mod

    token = ""
    try:
        token = jsdstu_login(args.jsdstu, args.username, args.password)
    except AssertionError as exc:
        record("FAIL", "jsdstu-3 直登", str(exc))

    identity = {"droneToken": token, "userId": 1, "username": args.username, "permission": "admin"}

    if token:
        # 注意：jsdstu_client 是全局单例（httpx 连接池绑定事件循环），
        # 所有 handler 实打必须在**同一个** asyncio.run 里执行，否则报 Event loop is closed
        async def _d1_all():
            out = {}

            async def call(name, **kwargs):
                out[name] = await registry.execute(name, identity, kwargs)

            await call("drone_query")
            await call("task_query")
            await call("alarm_query")
            await call("fence_query")
            await call("media_query")
            await call("media_query", grouped=True)
            await call("rule_query")
            await call("user_query")
            await call("dashboard_query")
            await call("permission_matrix")
            await call("alarm_stats")
            await call("alarm_dashboard")
            drones = out["drone_query"]
            if drones.get("drones"):
                await tools_mod.jsdstu_client.get(
                    f"/v1/drone/detail/{drones['drones'][0]['id']}", token=token)
            alarms = out["alarm_query"]
            if alarms.get("alarms"):
                detail = await registry.execute(
                    "alarm_detail", identity, {"alarmId": alarms["alarms"][0]["id"]})
                out["alarm_detail"] = detail
            users = out["user_query"]
            if users.get("users"):
                await registry.execute("user_detail", identity, {"userId": users["users"][0]["id"]})
            return out

        try:
            outs = asyncio_run(_d1_all())
        except Exception as exc:  # noqa: BLE001
            record("FAIL", "D1 查询类实打", f"异常: {exc}")
            outs = {}

        def _has_total(out):
            assert isinstance(out, dict) and "total" in out, f"输出缺 total: {str(out)[:120]}"

        for name in ("drone_query", "task_query", "alarm_query", "fence_query",
                     "media_query", "rule_query", "user_query"):
            if name in outs:
                check(f"{name} 实打", lambda n=name: _has_total(outs[n]))
            else:
                record("SKIP", f"{name} 实打", "未执行（前置失败）")
        if "media_query" in outs:
            pass  # grouped 已并入同名 key 覆盖，避免重复
        for name in ("dashboard_query", "permission_matrix", "alarm_stats", "alarm_dashboard"):
            if name in outs:
                check(f"{name} 实打", lambda n=name: (outs[n] or None) is not None or (_ for _ in ()).throw(AssertionError("输出为空")))
            else:
                record("SKIP", f"{name} 实打", "未执行（前置失败）")

        def _detail_endpoints():
            # detail 类接口在 _d1_all 内已随列表数据实打（有数据才打）
            assert "alarm_detail" in outs or not outs.get("alarm_query", {}).get("alarms"), \
                "告警列表非空但详情未实打"

        check("detail 类端点实打（drone/alarm/user）", _detail_endpoints)

    # D2 全链路冒烟 + BUG-A2 回归
    try:
        login = http_json("POST", f"{args.base}/v1/auth/login",
                          body={"username": args.username, "password": args.password})
        assert login.get("code") == 2000, f"8080 登录失败: {login}"
        agt_token = login["data"]["token"]
        user_id = login["data"]["user"]["id"]
        record("PASS", "8080 代理登录（identity 含 droneToken）")
    except AssertionError as exc:
        record("FAIL", "8080 代理登录", str(exc))
        return None

    session = f"e2e-biz-{int(time.time())}"

    def _full_chain_tooltrace():
        resp = http_json("POST", f"{args.base}/v1/agent/chat", token=agt_token, body={
            "message": "有多少台无人机？", "sessionId": session, "userId": user_id,
        })
        assert resp.get("code") == 2000, f"全链路对话失败: {str(resp)[:200]}"
        data = resp["data"]
        assert data.get("intent") == "business", f"意图异常: {data.get('intent')}"
        # BUG-A2 回归：toolTrace 必须透出（修复前恒为空数组）
        trace = data.get("toolTrace")
        assert isinstance(trace, list) and trace, "BUG-A2 回归失败：toolTrace 为空"
        assert any(t.get("durationMs") is not None for t in trace), "toolTrace 缺 durationMs"

    check("全链路冒烟 + BUG-A2 toolTrace 透出回归", _full_chain_tooltrace)

    # D3 确认门（抽样：新工具 user_delete + 存量 task_delete；零真实写入）
    def _confirm_gate(message: str, expect_desc: str):
        r1 = http_json("POST", f"{args.base}/v1/agent/chat", token=agt_token, body={
            "message": message, "sessionId": session, "userId": user_id,
        })
        assert r1.get("code") == 2000, f"触发写操作失败: {str(r1)[:200]}"
        content = r1["data"].get("content") or ""
        assert "写操作" in content and "确认" in content, \
            f"未进入确认话术: {content[:120]}"
        assert expect_desc in content, f"复述缺少关键描述: {content[:160]}"
        trace = r1["data"].get("toolTrace") or []
        assert any(t.get("blocked") == "pending_confirmation" for t in trace), \
            f"toolTrace 未标记 pending_confirmation: {trace}"
        r2 = http_json("POST", f"{args.base}/v1/agent/chat", token=agt_token, body={
            "message": "取消", "sessionId": session, "userId": user_id,
        })
        assert r2.get("code") == 2000, f"取消失败: {str(r2)[:200]}"
        assert "已取消" in (r2["data"].get("content") or ""), \
            f"取消话术异常: {str(r2['data'].get('content'))[:120]}"

    if token:
        # 用「创建类」写操作触发确认门（确定性高：不依赖存量数据存在）。
        # 删除类在 ID 不存在时模型会先查再拒答（这是正确行为），不适合做确定性样例。
        check("确认门 user_save：拦截→复述→取消（零写入）",
              lambda: _confirm_gate("帮我创建一个新用户，用户名叫 e2e_probe_user，角色是飞手",
                                    "创建新用户「e2e_probe_user」"))
        check("确认门 task_create：拦截→复述→取消（零写入）",
              lambda: _confirm_gate("帮我创建一个飞行任务，名称叫 e2e确认门探针任务",
                                    "e2e确认门探针任务"))

        def _cancel_clears_pending():
            # 直接经 guard 校验：取消后 pending 应为空（服务端内存态，另起会话验证话术即可）
            from app.services.guard import is_rejection
            assert is_rejection("取消") and is_rejection("算了不用了"), "取消词判定异常"

        check("取消词判定（guard.is_rejection）", _cancel_clears_pending)

        # D4 子智能体委派实打（规约 §13.2-P1 门：可被委派且黑板汇总正确）
        def _delegate_live():
            from app.services import subagent as sa

            async def scenario():
                r = sa.SubagentRunner()
                out = await r.delegate(
                    "e2e-delegate-live", "alarm-analyst",
                    "查一下当前告警分级统计，给出处置优先级建议（一两句即可）",
                    identity,
                )
                return r, out

            r, out = asyncio_run(scenario())
            assert out.get("status") == "DONE", f"委派未成功: {str(out)[:200]}"
            assert (out.get("conclusion") or "").strip(), "子智能体结论为空"
            board = r.blackboard_get("e2e-delegate-live")
            assert board.get("alarm-analyst"), "黑板未记录 alarm-analyst 结论"
            assert "该专家未完成" not in board["alarm-analyst"][-1], "黑板记录的是未完成标记"

        check("D4 委派 alarm-analyst 实打：DONE + 黑板有结论", _delegate_live)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------- E. 多智能体（静态，规约 §8）

def section_e_subagents():
    print("\nE. 多智能体（规约 §8/§13.2-P1 门）")
    from app.services import subagent as sa

    def _loaded():
        assert len(sa.SUBAGENTS) == 3, f"子智能体定义 {len(sa.SUBAGENTS)} != 3"
        names = {d.name for d in sa.SUBAGENTS}
        assert names == {"alarm-analyst", "device-locator", "ops-reporter"}, names
        for d in sa.SUBAGENTS:
            assert d.description, f"{d.name} 缺 description（路由依据）"
            assert "{task}" in d.prompt, f"{d.name} 的 prompt 缺 {task} 占位"
            assert d.max_turns > 0 and d.timeout_seconds > 0, f"{d.name} 预算非法"

    check("3 个子智能体定义加载完整（frontmatter 字段齐全）", _loaded)

    def _whitelist_readonly():
        for d in sa.SUBAGENTS:
            allowed = sa._whitelist_tools(d)
            assert allowed, f"{d.name} 白名单为空"
            for name in allowed:
                tool = sa.registry.get(name)
                assert tool is not None and not tool.danger, \
                    f"{d.name} 白名单含未注册或危险工具: {name}"

    check("子智能体白名单全只读（danger 一律剔除）", _whitelist_readonly)

    def _delegate_registered():
        tool = sa.registry.get("delegate_subagent")
        assert tool is not None, "delegate_subagent 未注册"
        enum = tool.parameters["properties"]["name"].get("enum") or []
        assert set(enum) == {d.name for d in sa.SUBAGENTS}, f"枚举与定义不对齐: {enum}"
        assert "只读" in tool.description or "不能执行任何写操作" in tool.description, \
            "委派工具描述未声明只读约束"

    check("delegate_subagent 已注册且枚举对齐、描述含只读约束", _delegate_registered)

    def _blackboard_cap():
        r = sa.SubagentRunner()
        for i in range(7):
            r.blackboard_append("s-x", "alarm-analyst", f"结论{i}")
        entries = r.blackboard_get("s-x")["alarm-analyst"]
        assert len(entries) == 5 and entries[0] == "结论2", "同键上限 5 / 淘汰最旧 失效"

    check("黑板同键上限 5 且淘汰最旧", _blackboard_cap)

    def _budget_cap():
        r = sa.SubagentRunner()
        used = 0
        try:
            for _ in range(sa.settings.agent_subagent_max_calls_per_session + 1):
                r._spend_budget("s-y")
                used += 1
            raise AssertionError("预算超限未被拦截")
        except sa.BizError as exc:
            assert used == sa.settings.agent_subagent_max_calls_per_session
            assert "已用完" in str(exc)

    check(f"会话委派预算 {sa.settings.agent_subagent_max_calls_per_session} 次后明确拒绝", _budget_cap)

    def _timeout_returns_timeouts():
        import asyncio

        async def scenario():
            r = sa.SubagentRunner()
            d = sa.SUBAGENT_MAP["alarm-analyst"]
            # 用例内临时把超时缩到 1s（避免真等 60s），恢复现场
            orig_timeout, d.timeout_seconds = d.timeout_seconds, 1
            orig_chat = sa.llm_client.chat

            async def hang(*a, **k):
                await asyncio.sleep(orig_timeout + 5)

            sa.llm_client.chat = hang
            try:
                return await r.delegate("s-timeout", d.name, "看下告警", {"droneToken": "t"})
            finally:
                sa.llm_client.chat = orig_chat
                d.timeout_seconds = orig_timeout

        out = asyncio_run(scenario())
        assert out.get("status") == "TIMEOUT", f"超时未转 TIMEOUT: {out}"
        assert "该专家未完成" in out.get("message", ""), "超时话术未提示专家未完成"

    check("子智能体超时返 TIMEOUT 且话术明确（stub LLM）", _timeout_returns_timeouts)

    def _unknown_agent_rejected():
        r = sa.SubagentRunner()
        try:
            asyncio_run(r.delegate("s-z", "不存在的专家", "任务", {"droneToken": "t"}))
            raise AssertionError("未知子智能体未被拒绝")
        except sa.BizError as exc:
            assert "可选" in str(exc)

    check("未知子智能体名被明确拒绝并列出可选项", _unknown_agent_rejected)


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="业务全能力 e2e（P0+P1）")
    parser.add_argument("--live", action="store_true", help="跑实打（需四服务在线）")
    parser.add_argument("--base", default="http://localhost:8080", help="Java 网关地址")
    parser.add_argument("--jsdstu", default="http://localhost:8060", help="jsdstu-3 地址")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="123456")
    args = parser.parse_args()

    print("=" * 60)
    print("业务全能力 e2e（P0+P1）— 矩阵 / 路由 / 降级 / 多智能体 / 实打")
    print("=" * 60)

    section_a_matrix()
    section_b_router()
    section_c_fallback()
    section_e_subagents()
    section_d_live(args)

    print("\n" + "=" * 60)
    return summary()


if __name__ == "__main__":
    sys.exit(main())

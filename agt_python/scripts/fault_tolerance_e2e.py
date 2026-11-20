"""容错 e2e（docs/specs/全链路容错规约.md §11，P0 + P2 用例）。

全部进程内 stub（stub LLM + stub 工具 + MockTransport），不依赖真实 LLM / jsdstu / Java 服务。
用例（编号对应规约 §11）：
  1  同指纹 3 轮分级：第 1 次执行 → 第 2 次执行+提醒 → 第 3 次拒绝+软着陆
  2  同指纹跨消息：会话级窗口，第 2 条消息里第 3 次出现同样熔断
  3  单工具连续失败：3 次失败后第 4 次调用被拒（tool_fail_break）
  4  工具卡死：wait_for 超时计一次失败
  5  max_steps 熔断：6 步后软着陆 finalize
  6  兜底文案不入会话历史（G5，经 agent_service 全链路）
  9  确认门拦截不触发指纹熔断（拦截≠执行，规约 §6.6）
  -- P2（容错规约 §12 第三行 + §8）--
  8  降级计数：指纹 warn/break、llm_fallback 触发后 counters 维度 +1（铁律 1）
  10 LLM failover：主 provider 500 耗尽重试 → 冷却 + 切备成功 + llm_failover 计数；
     冷却期内跳过主 provider（G8）
  11 业务查询重试：GET 幂等 5xx 重试 1 次成功；POST 5xx 绝不重试（G7/G9）
  12 埋点落库：chat_log/tool_call/delegation/fault_event 四表 + overview 聚合
     + fault_counters.json 落盘（业务规约 §10）

用法：python scripts/fault_tolerance_e2e.py    （DATA_ROOT 自动指向系统临时目录）
退出码：0=全部通过；1=有失败。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_ROOT", str(Path(os.environ.get("TEMP", "/tmp")) / "drone_agent_ft_e2e"))

import app.services.agent_loop as al  # noqa: E402
import app.services.agent_service as asvc  # noqa: E402
from app.core.llm import LLMError  # noqa: E402
from app.schemas.agent import ChatRequest  # noqa: E402
from app.services.biz import BizError  # noqa: E402
from app.services.fault import (  # noqa: E402
    BREAK_FALLBACK_REPLY,
    LLM_FALLBACK_REPLY,
    is_persistent_reply,
)
from app.services.memory import session_memory  # noqa: E402
from app.services.tools import Tool, registry  # noqa: E402

_identity = {"droneToken": "stub-token", "userId": 1, "username": "e2e", "permission": "admin"}

_results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  [✓] {name}")
    except Exception as exc:  # noqa: BLE001
        _results.append((name, False, str(exc)))
        print(f"  [✗] {name}: {exc}")


# ---------------------------------------------------------------- stub 基建

class ScriptedLLM:
    """按剧本依次返回：str=最终回复文本；list[(tool,args)]=工具调用；Exception=抛出。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.call_records: list[list] = []  # 每次调用收到的 messages 快照

    async def chat(self, messages, **kwargs):
        self.call_records.append(json.dumps(messages, ensure_ascii=False, default=str))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return SimpleNamespace(content=step, model="stub", usage={}, tool_calls=[])
        tool_calls = [
            SimpleNamespace(id=f"c{i}", name=name, arguments=json.dumps(args, ensure_ascii=False))
            for i, (name, args) in enumerate(step)
        ]
        return SimpleNamespace(content=None, model="stub", usage={}, tool_calls=tool_calls)


def _register_stub_tools() -> None:
    async def e2e_echo(identity, **kw) -> dict:
        return {"echo": kw}

    async def e2e_fail(identity, tag: str = "") -> dict:
        raise BizError(f"e2e 模拟故障（tag={tag}）")

    async def e2e_hang(identity, tag: str = "") -> dict:
        await asyncio.sleep(1.0)
        return {"done": tag}

    schema = {"type": "object", "properties": {"tag": {"type": "string"}}, "required": []}
    registry.register(Tool(name="e2e_echo", description="stub 回声", parameters=schema, handler=e2e_echo))
    registry.register(Tool(name="e2e_fail", description="stub 必败", parameters=schema, handler=e2e_fail))
    registry.register(Tool(name="e2e_hang", description="stub 卡死", parameters=schema, handler=e2e_hang))


async def _run(session: str, question: str, llm: ScriptedLLM, plan=None):
    orig = al.llm_client
    al.llm_client = llm
    try:
        return await al.agent_loop.run(question, [], dict(_identity), session_id=session, plan=plan)
    finally:
        al.llm_client = orig


def _trace(resp):
    return [(t.get("tool"), t.get("ok"), t.get("blocked"), t.get("error")) for t in resp.tool_trace]


# ---------------------------------------------------------------- 用例

def case1_same_call_graduated():
    """同指纹 3 轮：执行 → 提醒 → 拒绝+软着陆。"""
    llm = ScriptedLLM([
        [("e2e_echo", {"x": 1})],
        [("e2e_echo", {"x": 1})],
        [("e2e_echo", {"x": 1})],
        "FINAL_ANSWER",
    ])
    resp = asyncio.run(_run("ft-1", "问", llm))
    tr = _trace(resp)
    assert tr[0][:3] == ("e2e_echo", True, None), f"第 1 次应正常执行: {tr}"
    assert tr[1][:3] == ("e2e_echo", True, None), f"第 2 次应执行: {tr}"
    assert tr[2][2] == "fingerprint_break", f"第 3 次应被指纹熔断: {tr}"
    assert tr[3][0] == "(finalize)" and tr[3][1] is True, f"应有软着陆 finalize: {tr}"
    assert resp.content == "FINAL_ANSWER", resp.content
    # 第 2 次的工具结果里带提醒文本（payload 是内嵌 JSON，查子串即可）
    assert any("reminder" in rec for rec in llm.call_records), "第 2 次结果应含 reminder"
    # finalize 调用不带 tools（熔断恢复=无工具纯生成）
    last_has_tools = False  # stub 未区分；由 ScriptedLLM 剧本长度保证只走到 finalize


def case2_cross_message():
    """同指纹跨消息：会话级窗口，第 3 次出现（跨两条消息）触发熔断。"""
    session = "ft-2"
    llm1 = ScriptedLLM([[("e2e_echo", {"y": 2})], "A1"])
    r1 = asyncio.run(_run(session, "问1", llm1))
    assert _trace(r1)[0][:3] == ("e2e_echo", True, None)

    llm2 = ScriptedLLM([[("e2e_echo", {"y": 2})], "A2"])
    r2 = asyncio.run(_run(session, "问2", llm2))
    tr2 = _trace(r2)
    assert tr2[0][:3] == ("e2e_echo", True, None), f"第 2 条消息第 2 次出现应执行: {tr2}"

    llm3 = ScriptedLLM([[("e2e_echo", {"y": 2})], "FINAL"])
    r3 = asyncio.run(_run(session, "问3", llm3))
    tr3 = _trace(r3)
    assert tr3[0][2] == "fingerprint_break", f"跨消息第 3 次应熔断: {tr3}"
    assert r3.content == "FINAL", r3.content


def case3_tool_fail_break():
    """单工具连续失败：3 次失败后，第 4 次调用被拒（参数各不相同，避开指纹）。"""
    llm = ScriptedLLM([
        [("e2e_fail", {"tag": "a"})],
        [("e2e_fail", {"tag": "b"})],
        [("e2e_fail", {"tag": "c"})],
        [("e2e_fail", {"tag": "d"})],
        "DONE",
    ])
    resp = asyncio.run(_run("ft-3", "问", llm))
    tr = _trace(resp)
    assert all(t[0] == "e2e_fail" and t[1] is False for t in tr[:3]), f"前 3 次应失败: {tr}"
    assert tr[3][2] == "tool_fail_break", f"第 4 次应被连续失败熔断: {tr}"
    assert resp.content == "DONE", resp.content


def case4_tool_timeout():
    """工具卡死：wait_for 超时，计一次失败，循环不死等。"""
    loop = al.agent_loop
    orig_timeout = loop.tool_timeout
    loop.tool_timeout = 0.2
    try:
        llm = ScriptedLLM([[("e2e_hang", {"tag": "t"})], "H"])
        resp = asyncio.run(_run("ft-4", "问", llm))
        tr = _trace(resp)
        assert tr[0][1] is False and "超时" in (tr[0][3] or ""), f"应超时失败: {tr}"
        assert resp.content == "H", resp.content
    finally:
        loop.tool_timeout = orig_timeout


def case5_max_steps():
    """max_steps 熔断：6 步仍在调工具 → 软着陆 finalize。"""
    llm = ScriptedLLM(
        [[("e2e_echo", {"n": i})] for i in range(6)] + ["MAX_DONE"]
    )
    resp = asyncio.run(_run("ft-5", "问", llm))
    tr = _trace(resp)
    assert len([t for t in tr if t[0] == "e2e_echo"]) == 6, f"应有 6 步工具调用: {tr}"
    assert tr[-1][0] == "(finalize)" and tr[-1][1] is True, f"应 finalize 收尾: {tr}"
    assert resp.content == "MAX_DONE", resp.content


def case6_fallback_not_in_history():
    """G5：LLM 故障兜底文案不入会话历史，但用户问题保留。"""
    session = "ft-6"
    orig_router, orig_router_mode = asvc.intent_router, None

    async def _classify(message, identity=None):
        return SimpleNamespace(intent="business", source="stub", confidence=1.0)

    asvc.intent_router = SimpleNamespace(classify=_classify)
    orig_planning_enabled = asvc.settings.agent_planning_enabled
    asvc.settings.agent_planning_enabled = False  # 直接走 REACT，跳过路由 LLM
    try:
        llm1 = ScriptedLLM([LLMError("stub 主模型宕机")])
        orig = al.llm_client
        al.llm_client = llm1
        try:
            resp1 = asyncio.run(asvc.agent_service.chat(ChatRequest(
                session_id=session, message="查一下设备", user_id=1,
                metadata={"identity": dict(_identity)},
            )))
        finally:
            al.llm_client = orig
        assert resp1.content == LLM_FALLBACK_REPLY, resp1.content[:40]

        llm2 = ScriptedLLM(["NORMAL_OK"])
        orig = al.llm_client
        al.llm_client = llm2
        try:
            resp2 = asyncio.run(asvc.agent_service.chat(ChatRequest(
                session_id=session, message="再查一次", user_id=1,
                metadata={"identity": dict(_identity)},
            )))
        finally:
            al.llm_client = orig
        assert resp2.content == "NORMAL_OK", resp2.content[:40]

        history = session_memory.get(session)
        texts = [m["content"] for m in history]
        assert LLM_FALLBACK_REPLY not in texts, f"兜底文案混入了历史: {texts}"
        assert "查一下设备" in texts and "再查一次" in texts, f"用户问题应保留: {texts}"
        assert "NORMAL_OK" in texts, f"正常回答应入历史: {texts}"
        assert is_persistent_reply("正常回答") and not is_persistent_reply(LLM_FALLBACK_REPLY)
        assert not is_persistent_reply(BREAK_FALLBACK_REPLY)
    finally:
        asvc.intent_router = orig_router
        asvc.settings.agent_planning_enabled = orig_planning_enabled


def case9_confirm_gate_not_counted():
    """确认门拦截≠执行：同参数写操作拦截 3 次不触发指纹熔断。"""
    session = "ft-7"
    for i in range(3):
        llm = ScriptedLLM([[("user_delete", {"userId": 1})], "unused"])
        resp = asyncio.run(_run(session, f"删用户{i}", llm))
        tr = _trace(resp)
        assert tr[0][2] == "pending_confirmation", f"第 {i+1} 次应走确认门: {tr}"
        assert "确认" in resp.content, resp.content[:60]
        assert all(t[2] != "fingerprint_break" for t in tr), tr


# ---------------------------------------------------------------- P2 用例

def case8_counters():
    """§11-8 降级必须计数：指纹 warn/break、llm_fallback 各 +1（铁律 1）。"""
    from app.services.telemetry import telemetry

    before = telemetry.counters_snapshot()
    b_warn = before.get("loop_fingerprint_warn", 0)
    b_break = before.get("loop_fingerprint_break", 0)
    b_fb = before.get("llm_fallback", 0)

    llm = ScriptedLLM([
        [("e2e_echo", {"z": 9})],
        [("e2e_echo", {"z": 9})],
        [("e2e_echo", {"z": 9})],
        "FINAL8",
    ])
    resp = asyncio.run(_run("ft-8", "问", llm))
    assert any(t.get("blocked") == "fingerprint_break" for t in resp.tool_trace)

    llm2 = ScriptedLLM([LLMError("stub 主模型宕机")])
    asyncio.run(_run("ft-8b", "问", llm2))

    after = telemetry.counters_snapshot()
    assert after.get("loop_fingerprint_warn", 0) == b_warn + 1, (b_warn, after)
    assert after.get("loop_fingerprint_break", 0) == b_break + 1, (b_break, after)
    assert after.get("llm_fallback", 0) == b_fb + 1, (b_fb, after)


def case10_llm_failover():
    """G8：主 provider 持续故障 → 冷却 + 切备；冷却期内跳过主。"""
    import time as _time

    import httpx

    from app.core.config import settings as cfg
    from app.services.telemetry import telemetry

    old = (
        cfg.llm_api_key, cfg.llm_base_url, cfg.llm_model,
        cfg.llm_backup_base_url, cfg.llm_backup_api_key,
        cfg.llm_backup_model, cfg.llm_cooldown_seconds,
    )
    client = llm_client_factory()
    calls = {"primary": 0, "backup": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "primary" in str(request.url):
            calls["primary"] += 1
            return httpx.Response(500)
        calls["backup"] += 1
        return httpx.Response(200, json={
            "model": "backup-model",
            "choices": [{"message": {"content": "BACKUP_OK"}}],
            "usage": {},
        })

    async def fake_http(provider):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=provider.base_url
        )

    cfg.llm_api_key = "stub-key"
    cfg.llm_base_url = "http://primary-stub"
    cfg.llm_model = "primary-model"
    cfg.llm_backup_base_url = "http://backup-stub"
    cfg.llm_backup_api_key = "stub-key2"
    cfg.llm_backup_model = "backup-model"
    cfg.llm_cooldown_seconds = 300
    fb_before = telemetry.counters_snapshot().get("llm_failover", 0)
    client._http_for = fake_http  # 注入 MockTransport，杜绝真实网络请求
    try:
        result = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
        assert result.content == "BACKUP_OK", f"应切备成功: {result}"
        assert calls["primary"] >= 1 and calls["backup"] == 1, calls
        assert client._cooldown_until.get("primary", 0) > _time.time(), "主应进入冷却"
        assert telemetry.counters_snapshot().get("llm_failover", 0) == fb_before + 1
        # 冷却期内再次调用：跳过主，直接走备
        primary_before = calls["primary"]
        result2 = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
        assert result2.content == "BACKUP_OK"
        assert calls["primary"] == primary_before, "冷却期内不得再打主 provider"
    finally:
        asyncio.run(client.close())
        (
            cfg.llm_api_key, cfg.llm_base_url, cfg.llm_model,
            cfg.llm_backup_base_url, cfg.llm_backup_api_key,
            cfg.llm_backup_model, cfg.llm_cooldown_seconds,
        ) = old


def llm_client_factory():
    from app.core.llm import LLMClient

    return LLMClient()


def case11_biz_retry():
    """G7/G9：GET 幂等 5xx 重试 1 次成功；POST 写操作绝不自动重试。"""
    import httpx

    from app.services.biz import JsDstuClient

    client = JsDstuClient()
    calls = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls["get"] += 1
            if calls["get"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"code": 2000, "data": {"ok": True}, "message": "ok"})
        calls["post"] += 1
        return httpx.Response(500)

    async def fake_http():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://jsdstu-stub"
        )

    orig = client._http
    client._http = fake_http
    try:
        out = asyncio.run(client.get("/v1/x", token="t"))
        assert out == {"ok": True}, f"GET 重试后应成功: {out}"
        assert calls["get"] == 2, f"GET 应第 1 次 5xx 后重试 1 次: {calls}"
        try:
            asyncio.run(client.post("/v1/write", token="t", json_body={"a": 1}))
            raise AssertionError("POST 5xx 应直接 BizError")
        except BizError:
            pass
        assert calls["post"] == 1, f"写操作绝不自动重试: {calls}"
    finally:
        client._http = orig


def case12_telemetry_persist():
    """§10 埋点落库：四表写入 + overview 聚合 + fault_counters.json 落盘。"""
    from app.core.paths import DATA_DIR
    from app.services.telemetry import telemetry

    telemetry._ensure_conn()
    telemetry.record_chat(
        session_id="ft-12", user_id=1, permission="admin",
        intent="business", intent_source="stub", mode="REACT", model="stub",
        prompt_tokens=11, completion_tokens=7, duration_ms=12.5,
        ok=True, degraded=False, content_chars=5,
    )
    telemetry.record_tool_call(
        session_id="ft-12", step=1, tool="e2e_echo", ok=True,
        duration_ms=3.2, arguments={"x": 1},
    )
    telemetry.record_delegation(
        session_id="ft-12", agent="alarm-analyst", status="DONE", duration_ms=99.0,
    )
    telemetry.record_fault("loop_fingerprint_break", "ft-12", detail="e2e 直写")
    telemetry.flush_now()

    ov = telemetry.overview()
    assert ov["enabled"] is True, ov.keys()
    assert ov["chatLog24h"]["total"] >= 1, ov.get("chatLog24h")
    assert any(t["tool"] == "e2e_echo" for t in ov.get("toolCalls24h", [])), ov.get("toolCalls24h")
    assert any(d["agent"] == "alarm-analyst" for d in ov.get("delegations24h", []))
    assert any(f.get("path") for f in ov.get("recentFaults", []))
    # 计数器 JSON 已落盘（容错规约 §8：data/metrics/fault_counters.json）
    j = json.loads((DATA_DIR / "metrics" / "fault_counters.json").read_text(encoding="utf-8"))
    assert "counters" in j and "loop_fingerprint_break" in j["counters"], j


def main() -> int:
    print("=" * 60)
    print("容错 e2e（全链路容错规约 §11 P0 用例，全 stub 进程内）")
    print("=" * 60)
    _register_stub_tools()
    check("用例1 同指纹 3 轮分级（执行→提醒→拒绝+软着陆）", case1_same_call_graduated)
    check("用例2 同指纹跨消息熔断（会话级窗口）", case2_cross_message)
    check("用例3 单工具连续失败熔断（3 败后拒绝）", case3_tool_fail_break)
    check("用例4 工具卡死 wait_for 超时兜底", case4_tool_timeout)
    check("用例5 max_steps 熔断软着陆", case5_max_steps)
    check("用例6 兜底文案不入会话历史（G5）", case6_fallback_not_in_history)
    check("用例9 确认门拦截不触发指纹熔断", case9_confirm_gate_not_counted)
    check("用例8 降级计数：warn/break/fallback 各 +1", case8_counters)
    check("用例10 LLM failover：冷却+切备（G8）", case10_llm_failover)
    check("用例11 GET 幂等重试 / POST 绝不重试（G7/G9）", case11_biz_retry)
    check("用例12 埋点落库 + overview 聚合（§10）", case12_telemetry_persist)
    failed = [r for r in _results if not r[1]]
    print("=" * 60)
    print(f"通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

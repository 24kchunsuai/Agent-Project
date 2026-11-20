"""冒烟测试：确认服务可启动、核心接口可用、错误响应也走 JsonResult 约定。

LLM 调用一律 mock，不打真实 DeepSeek API（真实链路由联调脚本验证）。
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.core.response import StatusCode
from app.main import app
from app.prompts import load_prompt

client = TestClient(app)
# S-1 服务间信任边界：除探针/文档页外全部要求 X-Agent-Key，统一挂到 client 上，
# 免得每个用例都手写请求头（密钥由 tests/conftest.py 注入进程环境）。
client.headers["X-Agent-Key"] = os.environ["AGENT_SHARED_KEY"]

SUCCESS = int(StatusCode.OPERATION_SUCCESS)


def _mock_llm(
    monkeypatch: pytest.MonkeyPatch,
    content: str = "你好呀，我是小飞",
    classify_reply: str | None = '{"intent": "chitchat", "confidence": 0.9}',
    classify_raise: bool = False,
) -> None:
    """智能桩：意图分类调用回 classify_reply（classify_raise=True 则断言失败），
    闲聊生成调用回 content。"""

    async def fake_chat(messages, temperature=0.7, max_tokens=1024):
        from app.core.llm import ChatResult

        assert messages[-1]["role"] == "user"
        if "意图分类器" in messages[0]["content"]:
            assert not classify_raise, "不应触发 LLM 意图分类"
            return ChatResult(content=classify_reply or "", model="mock-model")
        assert messages[0]["role"] == "system" and messages[0]["content"]
        return ChatResult(
            content=content, model="mock-model", usage={"prompt_tokens": 10, "completion_tokens": 5}
        )

    from app.services import agent_service as svc

    monkeypatch.setattr(svc.llm_client, "chat", fake_chat)


def _mock_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(messages, temperature=0.7, max_tokens=1024):
        from app.core.llm import LLMError

        raise LLMError("大模型暂时不可用，请稍后重试")

    from app.services import agent_service as svc

    monkeypatch.setattr(svc.llm_client, "chat", fake_chat)


def test_health() -> None:
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == SUCCESS
    assert body["data"]["status"] == "up"


def test_chat_returns_llm_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_llm(monkeypatch, "你好呀，我是小飞")
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-001", "userId": 1, "message": "你好"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == SUCCESS
    data = body["data"]
    assert data["content"] == "你好呀，我是小飞"
    assert data["model"] == "mock-model"
    assert data["usage"]["promptTokens"] == 10
    assert data["usage"]["completionTokens"] == 5
    assert data["traceId"]


def test_chat_accepts_snake_case_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Java 侧用 camelCase，但内部 Python 代码用 snake_case，两种都要能收。"""
    _mock_llm(monkeypatch)
    resp = client.post(
        "/v1/agent/chat",
        json={"session_id": "s-002", "user_id": 2, "message": "hi"},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == SUCCESS


def test_chat_tolerates_explicit_null_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Java 侧未传 history 时会序列化成 null，不能让整单被拒。"""
    _mock_llm(monkeypatch)
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-003", "userId": 3, "message": "hi", "history": None, "metadata": None},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] == SUCCESS


def test_llm_failure_returns_business_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 挂了也必须 HTTP 200 + 3011，不抛堆栈。"""
    _mock_llm_error(monkeypatch)
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-004", "userId": 4, "message": "hi"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(StatusCode.AGENT_CALL_ERROR)
    assert "大模型" in body["message"]


def test_empty_message_returns_business_error() -> None:
    """参数校验失败也必须返回 HTTP 200 + 业务错误码，否则 Java 侧无法解析。"""
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-001", "userId": 1, "message": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(StatusCode.VALIDATE_ERROR)
    assert body["data"] is None


def test_unknown_path_returns_business_error() -> None:
    resp = client.get("/v1/not-exists")
    assert resp.status_code == 200
    assert resp.json()["code"] == int(StatusCode.DATA_UNEXISTS)


def test_chitchat_prompt_exists_and_non_empty() -> None:
    prompt = load_prompt("chitchat_system")
    assert "小飞" in prompt
    assert len(prompt) > 50


# ---- M2：意图路由 ----


def test_business_keyword_routes_to_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务关键词直接命中规则，返回占位话术，且全程不调 LLM（分类/生成都不会触发）。"""
    _mock_llm(monkeypatch, classify_raise=True)
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-biz", "userId": 1, "message": "帮我规划一条沿河巡检航线"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == SUCCESS
    data = body["data"]
    assert data["intent"] == "business"
    assert data["intentSource"] == "rule"
    assert "建设中" in data["content"]


def test_short_chitchat_keyword_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """短输入 + 闲聊关键词 → 规则直接放行，不花 LLM 分类钱。"""
    _mock_llm(monkeypatch, content="你好呀，我是小飞", classify_raise=True)
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-cc", "userId": 1, "message": "你好"},
    )
    data = resp.json()["data"]
    assert data["intent"] == "chitchat"
    assert data["intentSource"] == "rule"
    assert data["content"] == "你好呀，我是小飞"


def test_llm_classify_fallback_on_bad_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 分类输出非法 JSON → 兜底 chitchat，且闲聊生成仍正常。"""
    _mock_llm(monkeypatch, content="闲聊回复", classify_reply="这不是JSON")
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-fb", "userId": 1, "message": "今天天气怎么样呀朋友我们出来聊聊天吧"},
    )
    data = resp.json()["data"]
    assert data["intent"] == "chitchat"
    assert data["intentSource"] == "fallback"
    assert data["content"] == "闲聊回复"


def test_llm_classify_business(monkeypatch: pytest.MonkeyPatch) -> None:
    """无关键词命中、由 LLM 判定为业务 → business 占位。"""
    _mock_llm(monkeypatch, classify_reply='{"intent": "business", "confidence": 0.85}')
    resp = client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-lb", "userId": 1, "message": "无人机怎么备案登记呀"},
    )
    data = resp.json()["data"]
    assert data["intent"] == "business"
    assert data["intentSource"] == "llm"
    assert "建设中" in data["content"]


def _mock_llm_stream(monkeypatch: pytest.MonkeyPatch, chunks: list[str] | None = None, fail: bool = False) -> None:
    """流式桩：意图分类走 JSON 桩，chat_stream 按 chunks 逐段产出或抛错。"""

    async def fake_chat(messages, temperature=0.7, max_tokens=1024):
        from app.core.llm import ChatResult

        if "意图分类器" in messages[0]["content"]:
            return ChatResult(content='{"intent": "chitchat", "confidence": 0.9}', model="mock")
        raise AssertionError("stream mock 不应触发同步生成")

    async def fake_stream(messages, temperature=0.7, max_tokens=1024, usage_out=None):
        from app.core.llm import LLMError

        if fail:
            raise LLMError("大模型暂时不可用，请稍后重试")
        for chunk in chunks or []:
            yield chunk

    from app.services import agent_service as svc

    monkeypatch.setattr(svc.llm_client, "chat", fake_chat)
    monkeypatch.setattr(svc.llm_client, "chat_stream", fake_stream)


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            import json

            events.append(json.loads(line[len("data:"):]))
    return events


def test_chat_stream_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件顺序：meta -> delta* -> done，增量拼接 == 完整回复，且记忆落盘。"""
    _mock_llm_stream(monkeypatch, ["你", "好", "呀"])
    resp = client.post(
        "/v1/agent/chat/stream",
        json={"sessionId": "s-str", "userId": 1, "message": "你好"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events[0]["type"] == "meta"
    assert events[0]["intent"] == "chitchat" and events[0]["traceId"]
    deltas = [e for e in events if e["type"] == "delta"]
    assert "".join(e["content"] for e in deltas) == "你好呀"
    assert events[-1]["type"] == "done"

    from app.services.memory import session_memory

    assert session_memory.get("s-str")[-1] == {"role": "assistant", "content": "你好呀"}


def test_chat_stream_business(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务流式：meta(business) + 占位话术单段 delta + done。"""
    # business 走规则命中，不触达 LLM，直接用真实路由即可
    resp = client.post(
        "/v1/agent/chat/stream",
        json={"sessionId": "s-strb", "userId": 1, "message": "执行起飞"},
    )
    events = _parse_sse(resp.text)
    assert events[0]["type"] == "meta" and events[0]["intent"] == "business"
    deltas = [e for e in events if e["type"] == "delta"]
    assert "建设中" in "".join(e["content"] for e in deltas)
    assert events[-1]["type"] == "done"


def test_chat_stream_llm_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式中 LLM 失败：发 error 事件（HTTP 仍 200），不发半截内容。"""
    _mock_llm_stream(monkeypatch, fail=True)
    resp = client.post(
        "/v1/agent/chat/stream",
        json={"sessionId": "s-stre", "userId": 1, "message": "在吗"},
    )
    events = _parse_sse(resp.text)
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == int(StatusCode.AGENT_CALL_ERROR)


def test_intent_router_rule_level() -> None:
    """直接测路由器规则层：business 命中 / chitchat 短句。"""
    import asyncio

    from app.services.intent import intent_router

    result = asyncio.run(intent_router.classify("执行起飞动作"))
    assert result.intent == "business" and result.source == "rule"

    result = asyncio.run(intent_router.classify("你好"))
    assert result.intent == "chitchat" and result.source == "rule"


# ---- M3：会话记忆 ----


@pytest.fixture(autouse=True)
def _clean_session_memory():
    """每个用例前后清空全局记忆，避免用例间串味。"""
    from app.services.memory import session_memory

    session_memory.clear()
    yield
    session_memory.clear()


def test_multi_turn_uses_server_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 sessionId 第二轮请求，LLM 收到的上下文应包含第一轮的问答。"""
    captured: list[list[dict]] = []

    async def fake_chat(messages, temperature=0.7, max_tokens=1024):
        from app.core.llm import ChatResult

        if "意图分类器" in messages[0]["content"]:
            return ChatResult(content='{"intent": "chitchat", "confidence": 0.9}', model="mock")
        captured.append([dict(m) for m in messages])
        return ChatResult(content="记得住", model="mock", usage={})

    from app.services import agent_service as svc

    monkeypatch.setattr(svc.llm_client, "chat", fake_chat)

    client.post("/v1/agent/chat", json={"sessionId": "s-mem", "userId": 1, "message": "我叫小张"})
    client.post("/v1/agent/chat", json={"sessionId": "s-mem", "userId": 1, "message": "我叫什么？"})
    assert len(captured) == 2
    second = captured[1]
    # 第二轮应带上第一轮的 user + assistant（memory 优先于 req.history）
    assert {"role": "user", "content": "我叫小张"} in second
    assert {"role": "assistant", "content": "记得住"} in second


def test_different_sessions_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[dict]] = []

    async def fake_chat(messages, temperature=0.7, max_tokens=1024):
        from app.core.llm import ChatResult

        if "意图分类器" in messages[0]["content"]:
            return ChatResult(content='{"intent": "chitchat", "confidence": 0.9}', model="mock")
        captured.append([dict(m) for m in messages])
        return ChatResult(content="好", model="mock", usage={})

    from app.services import agent_service as svc

    monkeypatch.setattr(svc.llm_client, "chat", fake_chat)

    client.post("/v1/agent/chat", json={"sessionId": "s-a", "userId": 1, "message": "你好"})
    client.post("/v1/agent/chat", json={"sessionId": "s-b", "userId": 1, "message": "在吗"})
    # s-b 的第二轮上下文里不应混入 s-a 的内容
    assert {"role": "user", "content": "你好"} not in captured[1]


def test_business_turn_also_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """business 占位回合也写入记忆，跨意图上下文连续。"""
    _mock_llm(monkeypatch)
    client.post(
        "/v1/agent/chat",
        json={"sessionId": "s-biz2", "userId": 1, "message": "帮我规划巡检航线"},
    )
    from app.services.memory import session_memory

    history = session_memory.get("s-biz2")
    assert {"role": "user", "content": "帮我规划巡检航线"} in history
    assert any(m["role"] == "assistant" and "建设中" in m["content"] for m in history)


def test_memory_window_and_eviction() -> None:
    """会话淘汰策略（`window` 字段已随 M-2 移除，裁剪上移到 memory_trim）。"""
    from app.services.memory import MemoryConfig, SessionMemory

    # `window` 参数已删除：裁剪改由 memory_trim.trim_history() 按 token 预算 + 双锚点做，
    # 存储层只负责存取全量（两个实现行为必须一致，见 docs/specs/会话记忆规约.md §6 M-2）
    mem = SessionMemory(MemoryConfig(max_sessions=2, max_messages=4))
    for i in range(6):
        mem.record_turn("win", f"u{i}", f"a{i}")

    # 存储层不再按 window 截断；max_messages=4 才是存储层的上限（保留最近 4 条）
    history = mem.get("win")
    assert len(history) == 4
    assert history[0] == {"role": "user", "content": "u4"}  # 保留最近
    assert history[-1] == {"role": "assistant", "content": "a5"}

    mem.record_turn("s1", "hi", "yo")
    mem.record_turn("s2", "hi", "yo")
    mem.record_turn("s3", "hi", "yo")  # 触发 max_sessions=2 淘汰
    stats = mem.stats()
    assert stats["sessions"] == 2


def test_build_history_trims_by_token_budget(monkeypatch) -> None:
    """端到端：`_build_history` 必须真的按 token 预算裁剪（M-2 接入验证）。

    这是"规约落地"与"实现了但没接上"的分界断言——只测 trim_history 本身
    无法发现接线遗漏。
    """
    from app.core import config as config_mod
    from app.services import agent_service as asvc
    from app.schemas.agent import ChatRequest

    monkeypatch.setattr(config_mod.settings, "memory_token_budget", 3000)
    monkeypatch.setattr(config_mod.settings, "memory_head_turns", 2)
    monkeypatch.setattr(config_mod.settings, "memory_tail_turns", 6)

    sid = "trim-e2e"
    sess = asvc.session_memory
    # 全局 autouse 夹具已清空记忆，无需按会话清理（也不该摸实现内部 _store）
    # 80 字/轮：8 轮锚点约 1.4K token，3000 预算下仍有约束空间
    # （锚点优先于预算是设计意图，故用例必须让预算高于锚点自身所需）
    for i in range(30):
        sess.record_turn(sid, f"第{i}轮问题" + "字" * 80, f"第{i}轮回答" + "字" * 80)

    req = ChatRequest(session_id=sid, message="新问题", user_id=1)
    history = asvc.agent_service._build_history(req)

    from app.services.memory_token import token_estimator

    assert token_estimator.estimate_messages(history) <= 3000, "未按预算裁剪"
    contents = " ".join(m["content"] for m in history)
    assert "第0轮问题" in contents, "头部锚点丢失"
    assert "第29轮问题" in contents, "尾部锚点丢失"


def test_raw_history_not_trimmed(monkeypatch) -> None:
    """改写层用的 `_raw_history` 必须是**未裁剪**的（规约 §8 开放问题 4）。

    多轮改写依赖紧邻前一轮做指代消解，若被裁剪掉会退化为单轮检索。
    """
    from app.core import config as config_mod
    from app.services import agent_service as asvc
    from app.schemas.agent import ChatRequest

    monkeypatch.setattr(config_mod.settings, "memory_token_budget", 100)  # 极小预算

    sid = "raw-e2e"
    sess = asvc.session_memory
    # 全局 autouse 夹具已清空记忆，无需按会话清理（也不该摸实现内部 _store）
    for i in range(10):
        sess.record_turn(sid, f"问{i}" + "字" * 200, f"答{i}" + "字" * 200)

    req = ChatRequest(session_id=sid, message="新问题", user_id=1)
    raw = asvc.agent_service._raw_history(req)
    assert len(raw) == 20, "改写层历史不该被裁剪"


def test_build_history_filters_irrelevant_turns(monkeypatch) -> None:
    """接线验证：`_build_history` 真正经过相关性过滤（规约 §6 M-3）。

    这是"规约落地"与"实现了但没接上"的分界断言——只测 filter_history 本身
    无法发现接线遗漏。12 轮混合话题 + 登记类查询 → 炸机中间轮被淘汰，
    且 `_raw_history` 仍是全量（改写层不受影响）。
    """
    from app.core import config as config_mod
    from app.rag import history_filter as hf
    from app.services import agent_service as asvc
    from app.schemas.agent import ChatRequest

    # 12 轮：头 2（身份）+ 中间 4（炸机/炸机/登记/炸机）+ 尾 6（通用）
    bomb = "炸机"
    topics = (["身份"] * 2 + [bomb] * 2 + ["登记"] + [bomb]
              + ["通用"] * 6)
    sid = "filter-e2e"
    sess = asvc.session_memory
    for i, tp in enumerate(topics):
        sess.record_turn(sid, f"第{i}轮{tp}问题内容", f"第{i}轮{tp}回答内容")

    def _fake_embed(texts):
        out = []
        for t in texts:
            if bomb in t:
                out.append([1.0, 0.0, 0.0])
            elif "登记" in t:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out

    monkeypatch.setattr(hf, "_embed_batch", _fake_embed)

    req = ChatRequest(session_id=sid, message="实名登记需要什么材料", user_id=1)
    history = asvc.agent_service._build_history(req)
    contents = " ".join(m["content"] for m in history)

    assert "第2轮" not in contents and "第3轮" not in contents and "第5轮" not in contents, \
        "无关中间轮（炸机）应被过滤层淘汰"
    assert "第4轮登记问题内容" in contents, "相关中间轮必须保留"
    assert "第0轮身份问题内容" in contents and "第11轮通用回答内容" in contents, \
        "头尾锚点必须保留"

    # 改写层口径不受过滤影响：仍是全量 24 条
    assert len(asvc.agent_service._raw_history(req)) == 24, "_raw_history 必须保持全量"

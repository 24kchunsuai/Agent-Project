"""M-5 用户画像单测（docs/specs/会话记忆规约.md §6 M-5）。

覆盖验收草案的四条：
1. 抽取的偏好写入后，下一会话能读到（store 重开实例仍可读）；
2. 关闭开关后完全不抽取（schedule 三道闸门）；
3. 画像内容可查看、可删除（get_all / delete 语义）；
4. 抽取失败静默降级（LLM 抛异常 / 返回垃圾都不外抛不落库）。

注意：conftest 已把 DATA_ROOT 指到临时目录，本文件所有落盘自动隔离。
"""

import asyncio
import json

import pytest

from app.core.config import settings
from app.schemas.agent import ChatRequest
from app.services import user_profile as up
from app.services.user_profile import (
    UserProfileStore,
    _parse_preference,
    profile_store,
    schedule_profile_extraction,
)


@pytest.fixture()
def store(tmp_path):
    """每个用例独立库文件（本机安全删除钩子按回合共享配额，库文件极少、可接受）。"""
    return UserProfileStore(db_path=tmp_path / "user_profile.db")


# ---- ① 存储语义 ----

def test_upsert_get_delete_roundtrip(store):
    assert store.upsert(1, "preference_style", "简洁") is True
    assert store.upsert(1, "topics", json.dumps(["3号机", "考照"], ensure_ascii=False)) is True
    rows = store.get_all(1)
    assert {r["key"] for r in rows} == {"preference_style", "topics"}
    assert rows[0]["source"] == "agent" and rows[0]["confidence"] == pytest.approx(0.8)

    assert store.delete(1, "topics") == 1
    assert [r["key"] for r in store.get_all(1)] == ["preference_style"]
    assert store.delete(1, None) == 1          # 清空
    assert store.get_all(1) == []
    assert store.delete(1, None) == 0          # 幂等


def test_low_confidence_never_overwrites_high(store):
    """画像猜错会持续污染答案：用户显式设置（1.0）不可被 agent 猜测（0.8）冲掉。"""
    assert store.upsert(1, "preference_style", "用户亲口说的", confidence=1.0)
    assert store.upsert(1, "preference_style", "agent 猜的", confidence=0.8) is False
    assert store.get_all(1)[0]["value"] == "用户亲口说的"
    # 同置信度可以刷新值（agent 自己迭代；换一个 agent 置信度的 key 验证）
    assert store.upsert(1, "topics", '["a"]', confidence=0.8) is True
    assert store.upsert(1, "topics", '["a", "b"]', confidence=0.8) is True
    assert store.get_all(1, )[-1]["value"] == '["a", "b"]'
    # 更低置信度同样不能覆盖
    assert store.upsert(1, "topics", '["c"]', confidence=0.5) is False
    assert store.get_all(1)[-1]["value"] == '["a", "b"]'


def test_next_session_reads_persisted_profile(tmp_path):
    """验收①：写入后重开实例（=新会话/新进程）能读到。"""
    db = tmp_path / "user_profile.db"
    UserProfileStore(db_path=db).upsert(7, "preference_style", "简洁")
    reopened = UserProfileStore(db_path=db)
    assert reopened.as_prompt(7) == "preference_style=简洁"


def test_as_prompt_formats_topics(store):
    store.upsert(1, "preference_style", "简洁")
    store.upsert(1, "topics", json.dumps(["3号机", "考照"], ensure_ascii=False))
    line = store.as_prompt(1)
    assert "preference_style=简洁" in line
    assert "topics=3号机、考照" in line


# ---- ② 抽取解析（容错是验收④静默降级的前半段） ----

def test_parse_preference_happy_paths():
    assert _parse_preference('{"preference_style": "简洁", "topics": ["考照"]}') == {
        "preference_style": "简洁", "topics": '["考照"]'}
    fenced = '```json\n{"preference_style": "详细"}\n```'
    assert _parse_preference(fenced) == {"preference_style": "详细"}


def test_parse_preference_rejects_garbage():
    assert _parse_preference("null") is None
    assert _parse_preference("") is None
    assert _parse_preference("我觉得你说得对") is None      # 非 JSON
    assert _parse_preference('{"topics": "不是数组"}') is None  # 结构不符
    assert _parse_preference('{"topics": []}') is None        # 空 topics 无信息


def test_extract_silent_on_llm_error(monkeypatch):
    """验收④：LLM 抛异常 → 不外抛、不落库、不计数。"""
    from app.core import llm as llm_mod

    async def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_mod.llm_client, "chat", boom)
    asyncio.run(up._extract_and_save(1, "帮我查3号机的告警", "好的，已查询"))
    assert profile_store.get_all(1) == []


def test_extract_saves_parsed_preference(monkeypatch):
    """抽取成功路径：LLM 返回 JSON → 两个 key 落库。"""
    from app.core import llm as llm_mod

    class FakeResult:
        content = '{"preference_style": "简洁", "topics": ["3号机"]}'

    async def fake_chat(*a, **k):
        return FakeResult()

    monkeypatch.setattr(llm_mod.llm_client, "chat", fake_chat)
    asyncio.run(up._extract_and_save(2, "以后回答简洁点，我只看3号机", "明白"))
    rows = profile_store.get_all(2)
    assert {r["key"] for r in rows} == {"preference_style", "topics"}
    profile_store.delete(2, None)  # 清理单例库（已隔离到 tmp，仅防串用例）


# ---- ③ 开关与节流（验收②） ----

def test_schedule_disabled_no_task(monkeypatch):
    monkeypatch.setattr(settings, "profile_enabled", False)
    called = []

    async def spy(*a, **k):
        called.append(1)

    monkeypatch.setattr(up, "_extract_and_save", spy)
    schedule_profile_extraction(1, "这个问题足够长了吗", "答")
    assert called == []            # 开关关闭：连任务都不创建


def test_schedule_throttle_and_short_message(monkeypatch):
    monkeypatch.setattr(settings, "profile_enabled", True)
    monkeypatch.setattr(up, "_extract_and_save", lambda *a, **k: None)

    # 节流：预置 last_ts=now → 第二次在间隔内被拦
    import time as _t
    with up._extract_lock:
        up._last_extract_ts[9] = _t.time()
    schedule_profile_extraction(9, "这个问题足够长了吗", "答")  # 不应建任务（无事件循环/被节流）
    with up._extract_lock:
        del up._last_extract_ts[9]

    # 短问题闸门：在节流之前就返回
    assert len("在吗") < settings.profile_min_message_chars
    schedule_profile_extraction(9, "在吗", "答")


# ---- ④ 注入（agent_service._with_profile） ----

def _req(user_id=1):
    return ChatRequest(sessionId="s-ut", userId=user_id, message="在吗", history=[])


def test_with_profile_prepends_system(monkeypatch):
    from app.services.agent_service import AgentService

    monkeypatch.setattr(settings, "profile_enabled", True)
    monkeypatch.setattr(profile_store, "as_prompt", lambda uid: "preference_style=简洁")
    msgs = [{"role": "user", "content": "hi"}]
    out = AgentService()._with_profile(_req(), msgs)
    assert out[0]["role"] == "system" and out[0]["content"].startswith("[用户画像]")
    assert out[1] is msgs[0]       # 原历史原样保留在后


def test_with_profile_noop_when_disabled_or_empty(monkeypatch):
    from app.services.agent_service import AgentService

    msgs = [{"role": "user", "content": "hi"}]
    monkeypatch.setattr(settings, "profile_enabled", False)
    assert AgentService()._with_profile(_req(), msgs) is msgs

    monkeypatch.setattr(settings, "profile_enabled", True)
    monkeypatch.setattr(profile_store, "as_prompt", lambda uid: "")
    assert AgentService()._with_profile(_req(), msgs) is msgs

    # 画像库异常 → 原样返回（绝不影响主链路）
    def boom(uid):
        raise RuntimeError("db gone")

    monkeypatch.setattr(profile_store, "as_prompt", boom)
    assert AgentService()._with_profile(_req(), msgs) is msgs

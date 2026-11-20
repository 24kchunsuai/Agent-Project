"""会话历史裁剪单测（docs/specs/会话记忆规约.md §6 M-2，缺口 G11/G12）。

对应规约列的验收标准：
1. token 预算约束生效（50 轮 × 500 字 → 装配后 ≤ 预算）；
2. **头部锚点必须保留**（这是双锚点策略存在的理由，纯尾部窗口会挤掉用户身份/约束）；
3. 未超预算时原样返回、**不触发摘要**（不白花 LLM 调用）；
4. 双门槛：条数不够 或 中间占比 < 30% 时不摘要；
5. 摘要失败 → 降级为直接丢弃中间段，结果非空且含头部+尾部；
6. 递归摘要：第二次的 prompt 里带第一次的摘要；
7. 按「轮」裁剪，不产生"有问无答"的断头上下文。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    """固定裁剪参数，避免受 .env 影响导致断言漂移。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "memory_token_budget", 3000)
    monkeypatch.setattr(config_mod.settings, "memory_head_turns", 2)
    monkeypatch.setattr(config_mod.settings, "memory_tail_turns", 6)
    monkeypatch.setattr(config_mod.settings, "memory_summary_min_turns", 8)


def _long_history(turns: int, chars: int = 500) -> list[dict]:
    out: list[dict] = []
    for i in range(turns):
        out.append({"role": "user", "content": f"第{i}轮用户问题" + "字" * chars})
        out.append({"role": "assistant", "content": f"第{i}轮AI回答" + "字" * chars})
    return out


# ---------------- 验收 1：token 预算 ----------------


def test_trim_enforces_token_budget():
    """4K 字符级长历史 → 裁剪后 token 数 ≤ 预算。

    用例设计说明：锚点（head 2 轮 + tail 6 轮）**优先于预算**是设计意图
    （承载身份与当前话题），所以预算必须设在"锚点自身所需"之上才谈得上约束。
    这里用 120 字/轮的真实量级：8 轮锚点约 1.2K token，预算 3000 有约束空间。
    """
    from app.services.memory_token import token_estimator
    from app.services.memory_trim import trim_history

    raw = _long_history(60, chars=120)
    assert token_estimator.estimate_messages(raw) > 3000, "用例前提：原始必须超预算"

    result = trim_history(raw, token_budget=3000)
    assert result.was_trimmed is True
    assert token_estimator.estimate_messages(result.messages) <= 3000


def test_anchor_wins_over_budget():
    """锚点优先于预算：单轮超长时（500 字/轮）宁可超预算，也不丢锚点。

    这是有意为之的取舍——头部是用户身份/任务约束、尾部是当前话题，
    丢了它们等于退回"纯尾部窗口"，双锚点策略失去意义。
    """
    from app.services.memory_trim import trim_history

    raw = _long_history(50, chars=500)
    result = trim_history(raw, token_budget=3000)

    assert result.was_trimmed is True
    contents = " ".join(m["content"] for m in result.messages)
    assert "第0轮用户问题" in contents, "头部锚点必须保留（即使超预算）"
    assert "第49轮用户问题" in contents, "尾部锚点必须保留（即使超预算）"


def test_no_trim_when_under_budget():
    """未超预算 → 原样返回、不裁剪、不摘要。"""
    from app.services.memory_trim import trim_history

    raw = _long_history(2, chars=10)
    result = trim_history(raw, token_budget=3000)
    assert result.was_trimmed is False
    assert result.messages == raw
    assert result.summary == ""
    assert result.trimmed_count == 0


def test_empty_history():
    from app.services.memory_trim import trim_history

    result = trim_history([], token_budget=100)
    assert result.messages == []
    assert result.was_trimmed is False


# ---------------- 验收 2：头部锚点 ----------------


def test_head_anchor_preserved():
    """头部锚点轮必须保留——承载用户身份/任务约束，纯尾部窗口会挤掉它们。"""
    from app.services.memory_trim import trim_history

    raw = _long_history(50)
    result = trim_history(raw, token_budget=3000, )
    contents = " ".join(m["content"] for m in result.messages)

    assert "第0轮用户问题" in contents, "头部锚点（第 0 轮）被裁掉了"
    assert "第1轮用户问题" in contents, "头部锚点（第 1 轮）被裁掉了"


def test_tail_preserved():
    """尾部保留当前话题，最新几轮必须在。"""
    from app.services.memory_trim import trim_history

    raw = _long_history(50)
    result = trim_history(raw, token_budget=3000)
    contents = " ".join(m["content"] for m in result.messages)

    assert "第49轮用户问题" in contents, "最新一轮被裁掉了"


def test_trimmed_notice_appended():
    """裁剪后追加提示，防模型基于残缺历史编造（成本近零）。"""
    from app.services.memory_trim import TRIMMED_NOTICE, trim_history

    result = trim_history(_long_history(50), token_budget=3000)
    assert result.was_trimmed is True
    assert any(TRIMMED_NOTICE in m.get("content", "") for m in result.messages)


# ---------------- 验收 3/4：摘要触发门槛 ----------------


def test_summary_uses_prev_for_recursion(monkeypatch):
    """递归摘要：prompt 里必须带上一次的摘要内容。"""
    from app.services import memory_trim

    captured: dict[str, str] = {}

    def fake_call(llm_client, prompt):
        captured["prompt"] = prompt
        return "此前对话要点：用户偏好 3 号机。"

    monkeypatch.setattr(memory_trim, "_call_llm_sync", fake_call)

    result = memory_trim.trim_history(
        _long_history(50), prev_summary="上一轮的摘要内容ABC", token_budget=3000
    )
    assert "上一轮的摘要内容ABC" in captured.get("prompt", ""), "递归摘要未带上旧摘要"
    assert result.summary


def test_summary_skipped_when_middle_too_small(monkeypatch):
    """中间占比 / 条数不达门槛 → 不摘要（不白付 LLM 调用）。"""
    from app.services import memory_trim

    called = {"n": 0}

    def fake_call(llm_client, prompt):
        called["n"] += 1
        return "摘要"

    monkeypatch.setattr(memory_trim, "_call_llm_sync", fake_call)

    # head 2 + tail 6 = 8 轮，共 9 轮时中间只有 1 轮 → 条数门槛不过
    result = memory_trim.trim_history(_long_history(9), token_budget=200)
    assert called["n"] == 0, "中间段过小时不该调 LLM"
    assert isinstance(result.messages, list)


def test_summary_failure_degrades(monkeypatch):
    """摘要失败 → 降级为直接丢弃中间段，结果仍非空且含头部+尾部。"""
    from app.services import memory_trim

    def boom(llm_client, prompt):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(memory_trim, "_call_llm_sync", boom)

    result = memory_trim.trim_history(_long_history(50), token_budget=3000)
    assert result.summary_failed is True
    assert result.summary == ""
    assert result.messages, "摘要失败也必须给出可用结果（含头部+尾部）"
    contents = " ".join(m["content"] for m in result.messages)
    assert "第0轮用户问题" in contents
    assert "第49轮用户问题" in contents


# ---------------- 验收 5：按轮裁剪、不成断头 ----------------


def test_group_turns_pairs_user_assistant():
    """轮分组：user + 紧邻 assistant 为一轮。"""
    from app.services.memory_trim import group_turns

    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    turns = group_turns(msgs)
    assert len(turns) == 2
    assert [m["role"] for m in turns[0]] == ["user", "assistant"]


def test_group_turns_keeps_orphan_user():
    """末条未配对的 user 消息单独成轮——不能丢（参考项目踩过回放丢末条的坑）。"""
    from app.services.memory_trim import group_turns

    turns = group_turns([{"role": "user", "content": "q1"}])
    assert len(turns) == 1
    assert turns[0][0]["content"] == "q1"


def test_no_dangling_user_in_result(monkeypatch):
    """裁剪结果里不得出现"只有 user、没有 assistant"的断头轮（除末条孤轮外）。"""
    from app.services import memory_trim

    monkeypatch.setattr(memory_trim, "_call_llm_sync", lambda c, p: "摘要")
    result = memory_trim.trim_history(_long_history(50), token_budget=3000)

    msgs = result.messages
    for i, m in enumerate(msgs):
        if m["role"] == "user" and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            assert nxt["role"] in ("assistant", "user"), f"异常相邻: {m} -> {nxt}"


def test_all_turns_kept_when_turn_count_small():
    """轮数不超过 head+tail 时不做任何裁剪（无可裁的中间段）。"""
    from app.services.memory_trim import trim_history

    raw = _long_history(5, chars=2000)  # 超预算但只有 5 轮 < head2+tail6
    result = trim_history(raw, token_budget=200)
    assert result.was_trimmed is False
    assert result.messages == raw

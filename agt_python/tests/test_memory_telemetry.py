"""M-4 记忆埋点与 _build_history 接线单测（会话记忆规约 §6 G14）。

覆盖两件 pipeline 单测够不着的事：

1. `_build_history` 真实装配路径上 `note_memory_stats` 登记的数字正确
   （载入=raw 条数、注入=返回条数、压缩=trim 丢弃数；短会话无路径事件）；
2. 埋点整体 try 兜底：note/record 任何异常都不影响历史装配结果——
   这是验收标准「埋点写入整体 try 兜底」的直接断言。

本文件独立运行（规约验收提醒：本机安全删除钩子按回合共享 50 次配额）：
    python -m pytest tests/test_memory_telemetry.py -q
"""

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _req(n_msgs: int = 2):
    """构造 1 轮（2 条消息）的会话请求——低于 memory_filter_min_turns=8，
    过滤短路；远低于 token 预算，裁剪不触发。会话 id 每次随机，防单例串扰。"""
    from app.schemas.agent import ChatRequest

    hist: list[dict] = []
    for i in range(n_msgs // 2):
        hist += [
            {"role": "user", "content": f"问题{i}：无人机考照流程是什么"},
            {"role": "assistant", "content": f"回答{i}：报名、培训、考证。"},
        ]
    return ChatRequest(
        sessionId=f"m4-{uuid.uuid4().hex[:8]}", userId=1,
        message="刚才说到哪了", history=hist,
    )


@pytest.fixture()
def captured(monkeypatch):
    """拦截 telemetry 的两个写入口，避免污染真实埋点库。"""
    from app.services import agent_service as as_mod

    box: dict = {"stats": [], "faults": []}
    monkeypatch.setattr(
        as_mod.telemetry, "note_memory_stats",
        lambda **kw: box["stats"].append(dict(kw)))
    monkeypatch.setattr(
        as_mod.telemetry, "record_fault",
        lambda path, session_id="", detail="", **kw: box["faults"].append(path))
    return box


def test_build_history_notes_stats(captured):
    """装配 1 轮历史：载入=注入=2、压缩=0，且不产生任何路径事件。"""
    from app.services import agent_service as as_mod

    req = _req(2)
    msgs = as_mod.agent_service._build_history(req)
    assert len(msgs) == 2
    assert captured["stats"], "必须登记记忆数字（note_memory_stats）"
    s = captured["stats"][-1]
    assert s["total"] == 2 and s["used"] == 2 and s["trimmed"] == 0
    assert captured["faults"] == []  # 短会话：不过滤不裁剪，无路径事件


def test_build_history_empty_history_notes_zero(captured):
    """空历史早退分支也要登记 0——防御同一任务上下文里的陈旧值。"""
    from app.services import agent_service as as_mod

    req = _req(0)
    msgs = as_mod.agent_service._build_history(req)
    assert msgs == []
    assert captured["stats"] == [{"total": 0, "used": 0, "trimmed": 0}]


def test_telemetry_failure_never_blocks_build(captured, monkeypatch):
    """验收项「埋点写入整体 try 兜底」：埋点全炸，装配照常返回。"""
    from app.services import agent_service as as_mod

    def boom(*args, **kwargs):
        raise RuntimeError("埋点炸了")

    monkeypatch.setattr(as_mod.telemetry, "note_memory_stats", boom)
    monkeypatch.setattr(as_mod.telemetry, "record_fault", boom)
    msgs = as_mod.agent_service._build_history(_req(2))
    assert len(msgs) == 2

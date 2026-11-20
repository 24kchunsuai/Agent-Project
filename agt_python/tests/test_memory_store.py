"""会话记忆持久化单测（docs/specs/会话记忆规约.md §6 M-1，缺口 G10）。

覆盖规约列出的全部验收标准，其中两项直接对应参考项目踩过的坑：

1. **重启回放**：写入 → flush → 新建实例读同一库 → 内容与顺序一致；
2. **`synced_turn` 与库实际不一致时仍识别增量**——open-agent 的注释点名过这个坑：
   增量基准若依赖 SQL 聚合统计，一旦统计与实际不一致就会误判"无增量"从而永不写入；
3. **SQLite 不可写要降级**，不能让记忆层故障拖垮主链路（规约 §9 铁律 1）；
4. **不能重复写入**——本轮实测踩到的真实 bug：`_last_sync` 缺省值用 0.0 时，
   `time.time() - 0.0` 必然 ≥ 间隔阈值 → 首次 append 就落盘、第二次再落一次 → 消息重复。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """独立临时库的 SqliteMemoryStore（不碰产品 data 目录）。"""
    from app.core import config as config_mod
    from app.services.memory_sqlite import SqliteMemoryStore

    monkeypatch.setattr(config_mod.settings, "memory_sync_round_threshold", 10)
    monkeypatch.setattr(config_mod.settings, "memory_sync_interval_seconds", 60)
    monkeypatch.setattr(config_mod.settings, "memory_max_sessions", 500)
    s = SqliteMemoryStore(db_path=tmp_path / "mem.db")
    yield s
    s.close()


# ---------------- 验收 1：重启回放 ----------------


def test_persist_and_reload_after_restart(store, tmp_path):
    """写入 → flush → 新建实例（模拟重启）→ 内容与顺序一致。"""
    from app.services.memory_sqlite import SqliteMemoryStore

    store.record_turn("s1", "第一个问题", "第一个回答")
    store.record_turn("s1", "第二个问题", "第二个回答")
    store.flush()

    reborn = SqliteMemoryStore(db_path=store.db_path)
    try:
        history = reborn.get("s1")
        assert [m["content"] for m in history] == [
            "第一个问题", "第一个回答", "第二个问题", "第二个回答"
        ]
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    finally:
        reborn.close()


def test_reload_preserves_insert_order(store):
    """顺序必须按写入先后（靠自增 id 排序，不靠 ts——同秒写入 ts 会相同）。"""
    from app.services.memory_sqlite import SqliteMemoryStore

    for i in range(5):
        store.record_turn("s", f"问题{i}", f"回答{i}")
    store.flush()

    reborn = SqliteMemoryStore(db_path=store.db_path)
    try:
        contents = [m["content"] for m in reborn.get("s")]
        assert contents == [x for i in range(5) for x in (f"问题{i}", f"回答{i}")]
    finally:
        reborn.close()


def test_sessions_isolated(store):
    """不同会话互不串味。"""
    store.record_turn("a", "A问", "A答")
    store.record_turn("b", "B问", "B答")
    store.flush()

    assert [m["content"] for m in store.get("a")] == ["A问", "A答"]
    assert [m["content"] for m in store.get("b")] == ["B问", "B答"]


# ---------------- 验收 2：未达阈值也能落盘（关停路径） ----------------


def test_flush_without_threshold(store):
    """未达轮次阈值时 flush() 仍要落盘——这是服务关停路径，漏了就会丢数据。"""
    store.record_turn("s", "问题", "回答")  # 仅 1 轮，远低于阈值 10
    assert store._pending_turns.get("s", 0) == 1

    store.flush()
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM agent_memory_message WHERE session_id='s'"
    ).fetchone()
    assert rows[0] == 2


def test_flush_all_sessions(store):
    """flush(None) 冲刷全部待落盘会话（lifespan 关停调用形态）。"""
    store.record_turn("s1", "q1", "a1")
    store.record_turn("s2", "q2", "a2")
    store.flush()
    assert len(store.load_all_session_ids()) == 2


# ---------------- 验收 3：不重复写入（本轮踩到的真实 bug） ----------------


def test_no_duplicate_on_repeated_flush(store):
    """重复 flush 不得重复写入。

    ⚠️ 回归用例：`_last_sync` 缺省值曾用 0.0，导致
    `time.time() - 0.0 >= 60` 恒真 → 首次 append 就落盘，第二次再落一次，
    同一条消息被写两遍（实测库中出现 `[user Q1, user Q1, assistant A1]`）。
    """
    store.record_turn("s", "Q1", "A1")
    store.flush()
    store.flush()
    store.flush()

    rows = store._conn.execute(
        "SELECT role, content FROM agent_memory_message WHERE session_id='s'"
    ).fetchall()
    assert rows == [("user", "Q1"), ("assistant", "A1")], f"出现重复写入: {rows}"


def test_no_duplicate_when_threshold_triggers_midway(store, monkeypatch):
    """中途触发阈值落盘后，后续消息不得把已落盘的再写一遍。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "memory_sync_round_threshold", 2)
    store.record_turn("s", "q1", "a1")
    store.record_turn("s", "q2", "a2")  # 达阈值 → 自动落盘
    store.record_turn("s", "q3", "a3")
    store.flush()

    rows = store._conn.execute(
        "SELECT content FROM agent_memory_message WHERE session_id='s' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["q1", "a1", "q2", "a2", "q3", "a3"]


# ---------------- 验收 4：synced_turn 与实际不一致仍识别增量 ----------------


def test_incremental_baseline_survives_stale_index(store, monkeypatch):
    """基准被外部改乱后，仍能正确算出增量（不依赖 SQL 统计）。

    模拟 open-agent 描述的坑：索引计数与库内实际不一致时，
    若实现依赖"统计计数"，会误判为无增量 → 永不写入。
    """
    store.record_turn("s", "q1", "a1")
    store.flush()

    # 人为把自维护基准改小（模拟脏索引）
    store._synced_msgs["s"] = 0
    store._pending_turns["s"] = 1
    store.flush()

    # 基准脏导致重复写一遍是预期行为（幂等性由上层去重），
    # 但**绝不能因此不写**——"不写"才是致命故障
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM agent_memory_message WHERE session_id='s'"
    ).fetchone()
    assert rows[0] >= 2, "增量基准异常时仍必须写入，不能静默跳过"


def test_read_initializes_baseline_from_actual_rows(store):
    """重启后首次读取用库内实际行数初始化基准（不依赖任何索引文件）。"""
    store.record_turn("s", "q1", "a1")
    store.flush()

    from app.services.memory_sqlite import SqliteMemoryStore

    reborn = SqliteMemoryStore(db_path=store.db_path)
    try:
        assert "s" not in reborn._synced_msgs
        reborn.get("s")
        assert reborn._synced_msgs["s"] == 2  # 2 条消息（基准按条数维护）
    finally:
        reborn.close()


# ---------------- 验收 5：SQLite 不可用要降级 ----------------


def test_degrade_when_db_unwritable(tmp_path, monkeypatch):
    """库路径不可写 → 不抛异常、标记降级、**本轮仍能工作**（退回内存语义）。

    降级的目的不是"记忆失效"，而是"持久化失效但本轮对话照常"——
    所以断言降级后仍能读写会话历史（只是重启会丢）。
    """
    from app.services.memory_sqlite import SqliteMemoryStore

    # 用一个"父路径是文件"的非法路径，必定打开失败
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad = blocker / "sub" / "mem.db"

    s = SqliteMemoryStore(db_path=bad)
    assert s.degraded is True, "库打不开时应标记降级"
    # 降级后所有操作都安全，不抛异常
    s.record_turn("s", "q", "a")
    s.flush()
    # 内存模式仍可用（这正是"降级不中断"的含义）
    assert [m["content"] for m in s.get("s")] == ["q", "a"]
    # 但没有持久层可回放
    assert s.load_all_session_ids() == []
    s.close()


def test_degrade_does_not_raise_on_write(tmp_path):
    """降级后任何写操作都不得抛异常（主链路铁律：记忆故障不中断）。"""
    from app.services.memory_sqlite import SqliteMemoryStore

    blocker = tmp_path / "f"
    blocker.write_text("x", encoding="utf-8")
    s = SqliteMemoryStore(db_path=blocker / "x" / "m.db")
    try:
        s.record_turn("s", "q", "a")
        s.append("s", "user", "q2")
        s.flush()
        s.flush("s")
        s.save_summary("s", "摘要", 1)
        s.purge_expired()
        assert s.get_summary("s") == ""
    finally:
        s.close()


def test_append_ignores_invalid_role_and_empty(store):
    """非法 role / 空内容不得入库（沿用原内存实现的守卫）。"""
    store.append("s", "system", "系统消息")
    store.append("s", "user", "")
    store.flush()
    assert store.get("s") == []


# ---------------- 验收 6：TTL 清理 ----------------


def test_purge_expired(store, monkeypatch):
    """TTL 清理只删超期消息（规约 §5.1）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "memory_ttl_days", 30)
    store.record_turn("s", "旧问题", "旧回答")
    store.flush()

    # 把 ts 改成 40 天前
    import time as _t

    old = int(_t.time()) - 40 * 86400
    store._conn.execute("UPDATE agent_memory_message SET ts=?", (old,))
    store._conn.commit()

    removed = store.purge_expired()
    assert removed == 2
    assert store._conn.execute(
        "SELECT COUNT(*) FROM agent_memory_message"
    ).fetchone()[0] == 0


# ---------------- 验收 7：内存实现行为与 SQLite 一致 ----------------


def test_memory_store_returns_full_history(monkeypatch):
    """内存实现的 get() 也必须返回**全量**（裁剪上移到 trim 层）。

    两个实现行为必须一致，否则切换存储会改变上下文构成，排查时极易误判。
    """
    from app.services.memory import SessionMemory

    s = SessionMemory()
    for i in range(15):
        s.record_turn("x", f"q{i}", f"a{i}")
    assert len(s.get("x")) == 30, "内存实现不应再按 window 截断"


def test_memory_store_has_protocol_methods():
    """内存实现补齐 flush / load_all_session_ids，接口与 SQLite 版对齐。"""
    from app.services.memory import SessionMemory

    s = SessionMemory()
    s.flush()
    assert s.load_all_session_ids() == []
    assert s.stats()["sessions"] == 0

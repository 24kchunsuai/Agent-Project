"""E-13 成本计量与配额：价目 / 折算 / 配额 / DWS / 端点 五层单测。

企业级落地规约 E-13（判据 C7「可计量」）。分层理由与 test_client_error 相同——
它们坏起来时表现完全不同：

1. **价表层**（costing.parse_prices）：配置是给人填的，一定会填错。坏的形态是
   "启动报错"（不可接受，计量配置不该拦系统）或"负价算出倒贴钱"（更隐蔽）。
2. **折算层**（costing.model_cost）：成本数字的可信度全在这一行乘法。
   未知模型返回 0 而不是抛错——缺价 ≠ 免费照标，看板要单列 unpricedTokens。
3. **配额层**（telemetry.quota_check）：核心是**队列也算数**（record_chat 先进
   内存队列、5s 后才落库，只查 DB 会漏掉刚记的这轮——恰好最关键的一轮）与
   **每用户每日至多一条 fault**（超限后每轮都超，不去重会把 fault_event 刷满）。
4. **DWS 层**（recompute_daily）：tokens_prompt / tokens_completion 拆分——
   成本折算两侧单价不同，只有 tokens_total 就无从"与手工核算一致"。
5. **端点层**（GET /v1/agent/metrics/costs）：鉴权受 X-Agent-Key 保护（S-1）。

本文件独立运行（本机安全删除钩子按回合共享 50 次配额）：
    python -m pytest tests/test_cost_quota.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================ 1. 价表层


def test_parse_prices_happy_path():
    from app.services.costing import parse_prices

    table = parse_prices('{"deepseek-chat": {"prompt": 2.0, "completion": 8.0}}')
    assert table == {"deepseek-chat": {"prompt": 2.0, "completion": 8.0}}


def test_parse_prices_empty_and_blank_are_empty_table():
    from app.services.costing import parse_prices

    assert parse_prices("") == {}
    assert parse_prices(None) == {}
    assert parse_prices("   ") == {}


def test_parse_prices_invalid_json_converges_to_empty():
    """非法 JSON → 空表 + 不抛：计量配置坏了**不能拦着系统跑**（与 E-12 采样率同哲学）。"""
    from app.services.costing import parse_prices

    assert parse_prices("{not json") == {}
    assert parse_prices("[1,2,3]") == {}        # 顶层不是对象
    assert parse_prices("42") == {}


def test_parse_prices_drops_illegal_rows_and_clamps_negative():
    """行不合法丢弃；负价收敛为 0——**计量层不许出现"倒贴钱"**。"""
    from app.services.costing import parse_prices

    table = parse_prices(
        '{"ok": {"prompt": 1, "completion": 2},'
        ' "missing-completion": {"prompt": 1},'
        ' "not-dict": 3,'
        ' "negative": {"prompt": -5, "completion": 2}}'
    )
    assert "ok" in table and "negative" in table
    assert "missing-completion" not in table and "not-dict" not in table
    assert table["negative"]["prompt"] == 0.0   # 负 → 0
    assert table["negative"]["completion"] == 2.0


def test_parse_prices_non_numeric_sides_become_zero():
    from app.services.costing import parse_prices

    table = parse_prices('{"m": {"prompt": "abc", "completion": null}}')
    assert table["m"] == {"prompt": 0.0, "completion": 0.0}


# ============================================================ 2. 折算层


def test_model_cost_math_matches_manual_calculation():
    """判据要求"与手工核算一致"——这条就是那道手工核算：
    1,000,000 prompt × 2 元/M + 1,000,000 completion × 8 元/M = 10 元。"""
    from app.services.costing import model_cost, parse_prices

    prices = parse_prices('{"m": {"prompt": 2.0, "completion": 8.0}}')
    assert model_cost("m", 1_000_000, 1_000_000, prices) == pytest.approx(10.0)
    assert model_cost("m", 500_000, 0, prices) == pytest.approx(1.0)


def test_model_cost_unknown_model_is_zero_not_error():
    """未知模型返回 0（调用方负责把 unpricedTokens 单列）——缺价不阻断看板。"""
    from app.services.costing import model_cost

    assert model_cost("who-am-i", 999_999, 999_999, {}) == 0.0
    assert model_cost("", 10, 10, {"m": {"prompt": 1, "completion": 1}}) == 0.0


def test_quota_notice_message_is_human_readable_and_contains_numbers():
    """文案是给用户看的：必须含"提示"性质与两个数字，不能是一句吓唬人的报错。"""
    from app.services.costing import quota_notice

    out = quota_notice(used_tokens=12_345, cap_tokens=10_000)
    assert out["exceeded"] is True
    assert out["usedTokens"] == 12_345 and out["capTokens"] == 10_000
    assert "12,345" in out["message"] and "10,000" in out["message"]
    assert "仍可正常使用" in out["message"]   # 降级为提示而非拒绝（规约原文）


def test_quota_cap_clamps_negative_to_zero():
    from app.core import config as config_mod
    from app.services import costing

    original = config_mod.settings.quota_user_daily_tokens
    try:
        config_mod.settings.quota_user_daily_tokens = -100
        assert costing.quota_cap() == 0
    finally:
        config_mod.settings.quota_user_daily_tokens = original


# ============================================================ 3+4. 落库 / 配额 / DWS


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """一个指向临时库的真实 TelemetryStore（conftest 已把 DATA_ROOT 指向 tmp）。"""
    from app.core import config as config_mod
    from app.services.telemetry import TelemetryStore

    monkeypatch.setattr(config_mod.settings, "telemetry_db_path", str(tmp_path / "obs.db"))
    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", True)

    s = TelemetryStore()
    s._ensure_conn()
    yield s
    if s._conn is not None:
        s._conn.close()


def _chat(store, *, user_id=1, prompt=100, completion=50, model="m1", source="api"):
    store.record_chat(
        session_id="s1", user_id=user_id, intent="chitchat", mode="REACT",
        model=model, prompt_tokens=prompt, completion_tokens=completion,
        duration_ms=10.0, ok=True, usage_source=source,
    )


def test_usage_source_roundtrip_to_db(store):
    """usage_source 要原样落库——"这个数是量的还是估的"是成本口径的一半。"""
    _chat(store, source="api")
    _chat(store, source="estimate")
    store.flush_now()

    rows = store._conn.execute(
        "SELECT usage_source FROM agent_chat_log ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["api", "estimate"]


def test_user_tokens_today_counts_db_and_queue(store):
    """核心坑：record_chat 先进队列、后台才落库——只查 DB 会漏掉刚记的这轮。"""
    _chat(store, prompt=100, completion=50)   # 还在队列里（不 flush）
    assert store.user_tokens_today(1) == 150  # 队列里的也要算

    store.flush_now()
    _chat(store, prompt=10, completion=5)     # 新一轮又进队列
    assert store.user_tokens_today(1) == 165  # DB 150 + 队列 15


def test_user_tokens_today_ignores_other_users_and_zero_user(store):
    _chat(store, user_id=2, prompt=1000, completion=0)
    assert store.user_tokens_today(1) == 0
    assert store.user_tokens_today(0) == 0    # 匿名（user_id=0）不参与配额


def test_quota_check_under_cap_returns_none(store, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "quota_user_daily_tokens", 1000)
    _chat(store, prompt=100, completion=50)
    assert store.quota_check(1, "s1") is None


def test_quota_check_over_cap_notices_and_records_fault_once(store, monkeypatch):
    """超限 → 提示 + fault_event；**第二轮再超不再落第二条 fault**（按用户+日去重）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "quota_user_daily_tokens", 100)
    _chat(store, prompt=100, completion=50)   # 150 > 100

    notice = store.quota_check(1, "s1")
    assert notice is not None and notice["exceeded"] is True
    assert notice["usedTokens"] == 150 and notice["capTokens"] == 100
    store.flush_now()
    faults = store._conn.execute(
        "SELECT COUNT(*) FROM fault_event WHERE path = 'quota_exceeded'").fetchone()[0]
    assert faults == 1

    # 第二轮：仍在超限态 → 有提示，但 fault 不重复
    _chat(store, prompt=10, completion=5)
    notice2 = store.quota_check(1, "s1")
    assert notice2 is not None
    store.flush_now()
    faults2 = store._conn.execute(
        "SELECT COUNT(*) FROM fault_event WHERE path = 'quota_exceeded'").fetchone()[0]
    assert faults2 == 1


def test_quota_check_no_cap_or_anonymous_returns_none(store, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "quota_user_daily_tokens", 0)
    _chat(store)
    assert store.quota_check(1) is None          # 0 = 不限

    monkeypatch.setattr(config_mod.settings, "quota_user_daily_tokens", 1)
    _chat(store, user_id=0)
    assert store.quota_check(0) is None          # 匿名不配额


def test_costs_summary_math_and_unpriced_tokens(store, monkeypatch):
    """成本聚合全链路：两个模型（一个有价一个无价）+ 两个用户（一个有部门）。

    手工核算：m1 = 2,000,000×2 + 4,000,000×8 = 36 元；m2 无价 → 0 但 unpriced=1,000,000。
    """
    from app.core import config as config_mod

    monkeypatch.setattr(
        config_mod.settings, "llm_price_json",
        '{"m1": {"prompt": 2.0, "completion": 8.0}}')

    class _FakeProfile:
        def department_map(self):
            return {1: "飞行部"}

    monkeypatch.setattr(store, "_profile_service", lambda: _FakeProfile())

    _chat(store, user_id=1, prompt=1_000_000, completion=1_000_000, model="m1")
    _chat(store, user_id=1, prompt=1_000_000, completion=3_000_000, model="m1")
    _chat(store, user_id=2, prompt=500_000, completion=500_000, model="m2")
    store.flush_now()

    out = store.costs_summary(days=7)
    assert out["enabled"] is True and out["pricesConfigured"] is True
    assert out["totals"]["tokens"] == 7_000_000
    assert out["totals"]["costCny"] == pytest.approx(36.0)
    assert out["totals"]["unpricedTokens"] == 1_000_000

    by_model = {r["model"]: r for r in out["byModel"]}
    assert by_model["m1"]["costCny"] == pytest.approx(36.0)
    assert by_model["m2"]["costCny"] == 0.0
    assert by_model["m2"]["unpricedTokens"] == 1_000_000

    by_user = {r["userId"]: r for r in out["byUser"]}
    assert by_user[1]["costCny"] == pytest.approx(36.0)
    assert by_user[1]["dept"] == "飞行部"
    assert by_user[2]["dept"] == "unknown"

    by_dept = {r["dept"]: r for r in out["byDept"]}
    assert by_dept["飞行部"]["costCny"] == pytest.approx(36.0)
    assert by_dept["unknown"]["costCny"] == 0.0

    # 日趋势与总计同源：只有 m1 的 4 行计入成本
    assert out["daily"] and out["daily"][-1]["costCny"] == pytest.approx(36.0)


def test_costs_summary_untouched_when_disabled(tmp_path, monkeypatch):
    from app.core import config as config_mod
    from app.services.telemetry import TelemetryStore

    monkeypatch.setattr(config_mod.settings, "telemetry_db_path", str(tmp_path / "obs.db"))
    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", False)

    out = TelemetryStore().costs_summary(days=7)
    assert out["enabled"] is False and out["byModel"] == []


def test_recompute_daily_splits_prompt_and_completion(store):
    """DWS 拆分：成本折算两侧单价不同，tokens_total 一个数对不了账。"""
    _chat(store, prompt=300, completion=100, model="m1")
    store.flush_now()

    n = store.recompute_daily(store._conn.execute(
        "SELECT substr(created_at, 1, 10) FROM agent_chat_log LIMIT 1").fetchone()[0])
    assert n > 0
    rows = {(r[0], r[1]): r[2] for r in store._conn.execute(
        "SELECT metric, dim, value FROM metrics_daily WHERE dim_type = 'model'")}
    assert rows[("tokens_total", "m1")] == 400
    assert rows[("tokens_prompt", "m1")] == 300
    assert rows[("tokens_completion", "m1")] == 100


def test_schema_version_v8_registered(store):
    versions = {r[0] for r in store._conn.execute("SELECT version FROM schema_version")}
    assert 8 in versions


# ============================================================ 5. 端点层


def test_costs_endpoint_requires_agent_key():
    """与其它 metrics 读取端点同一把锁（S-1）：无 X-Agent-Key → 4010。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/v1/agent/metrics/costs")
    assert r.status_code == 200
    assert r.json()["code"] == 4010


def test_costs_endpoint_returns_envelope(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "telemetry_enabled", True)
    headers = {"X-Agent-Key": settings.agent_shared_key}
    with TestClient(app) as client:
        r = client.get("/v1/agent/metrics/costs?days=7", headers=headers)
    body = r.json()
    assert r.status_code == 200 and body["code"] == 2000
    data = body["data"]
    for key in ("enabled", "pricesConfigured", "quota", "totals",
                "byModel", "byUser", "byDept", "daily"):
        assert key in data, f"缺字段 {key}"
    assert data["quota"].get("capTokens", 0) >= 0


def test_schema_version_inserts_are_idempotent(store):
    """E-13 验收抓到的存量 bug：schema_version 表没有 UNIQUE 约束，
    `INSERT OR IGNORE` 从来不 IGNORE——每次启动都重复插一遍（存量库堆了数百行）。
    修复后：重复触发 _ensure_conn 也只保留一行，且历史重复被清理。"""
    store._ensure_conn()   # 再触发两次
    store._ensure_conn()

    rows = store._conn.execute(
        "SELECT version, COUNT(*) FROM schema_version GROUP BY version").fetchall()
    assert rows, "schema_version 不应有版本被清空"
    assert all(c == 1 for _, c in rows), f"仍有重复版本行: {rows}"
    assert {v for v, _ in rows} == set(range(1, 9))


def test_schema_version_inserts_are_idempotent(store):
    """E-13 验收抓到的存量 bug：schema_version 表没有 UNIQUE 约束，
    `INSERT OR IGNORE` 从来不 IGNORE——每次启动都重复插一遍（存量库堆了数百行）。
    修复后：重复触发 _ensure_conn 也只保留一行，且历史重复被清理。"""
    store._ensure_conn()   # 再触发两次
    store._ensure_conn()

    rows = store._conn.execute(
        "SELECT version, COUNT(*) FROM schema_version GROUP BY version").fetchall()
    assert rows, "schema_version 不应有版本被清空"
    assert all(c == 1 for _, c in rows), f"仍有重复版本行: {rows}"
    assert {v for v, _ in rows} == set(range(1, 9))

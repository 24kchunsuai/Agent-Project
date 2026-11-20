"""E-3 运维健康聚合单测（企业级落地规约 E-3 验收标准）。

覆盖：
1. 全绿 → `status=up`；
2. **停 Milvus → 整体 degraded，且 Milvus 组件自身是 `down` + 带原因**
   （规约原文写死的验收点：组件状态与整体状态是两件事）；
3. 单项 check **抛异常也不外抛**（对齐全链路容错规约——聚合器自己不能 500）；
4. 降级计数器非零**不改**整体状态（口径 2：计数是观测数据，不是当前故障）；
5. 所有数据依赖同时不可用 → `down`（服务实际已无能力）；
6. 8060 不可达 → degraded（外部系统不拖垮整体）；
7. scheduler 报错 → degraded；
8. 端点鉴权：无 key → 4010，带 key → 2000；
9. 3 秒内重复调用命中缓存。

全部用 monkeypatch 打桩，**不连真实 Milvus / 8060 / 数据库**（断网可跑）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import ops_health as oh

# 密钥由 tests/conftest.py 注入（早于任何 app.* 导入）
HEADERS = {"X-Agent-Key": settings.agent_shared_key}

client = TestClient(app)


# --------------------------------------------------------------- 打桩工具


def _comp(name: str, status: str, detail: str = "", *, required: bool = False) -> dict:
    return {"name": name, "status": status, "latencyMs": 0, "detail": detail, "required": required}


def _stub_all_up(monkeypatch, *, sqlite_status=oh.UP, data1_status=oh.UP) -> None:
    """把全部探针换成固定结果，让"归并逻辑"成为唯一被测对象。"""
    monkeypatch.setattr(oh, "_check_python", _async(_comp("python", oh.UP, "stub", required=True)))
    monkeypatch.setattr(oh, "_check_milvus", _async(_comp("milvus", oh.UP, "stub")))
    monkeypatch.setattr(oh, "_check_jsdstu", _async(_comp("jsdstu", oh.UP, "stub")))
    monkeypatch.setattr(
        oh, "_sqlite_targets",
        lambda: (
            [("sqlite:governance", "x"), ("sqlite:metrics", "x")],
            [],
        ),
    )
    monkeypatch.setattr(oh, "_check_sqlite", _async(_comp("sqlite", sqlite_status, "stub")))
    monkeypatch.setattr(oh, "_probe_scheduler", lambda: {"tasks": []})
    monkeypatch.setattr(
        oh, "_check_scheduler_state",
        lambda snap: _comp("scheduler", oh.UP, "stub"),
    )
    monkeypatch.setattr(oh, "_check_degrade", lambda: (_comp("degrade", oh.UP, "stub"), {}))


def _async(value):
    """把一个固定值包装成 async 函数（monkeypatch 需要可调用对象）。"""
    async def _fn(*args, **kwargs):  # noqa: ANN002, ANN003
        return value
    return _fn


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后清缓存：否则 3 秒 TTL 会把上一个用例的结果喂给下一个。"""
    oh.clear_cache()
    yield
    oh.clear_cache()


# --------------------------------------------------------------- 归并逻辑


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_all_up(monkeypatch):
    _stub_all_up(monkeypatch)
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.UP
    assert payload["summary"][oh.DOWN] == 0
    assert all(c["status"] == oh.UP for c in payload["components"])


def test_milvus_down_yields_degraded_but_component_is_down(monkeypatch):
    """E-3 的核心验收点：组件自称 down，整体只降级为 degraded。"""
    _stub_all_up(monkeypatch)
    monkeypatch.setattr(
        oh, "_check_milvus",
        _async(_comp("milvus", oh.DOWN, "ConnectionError: 连接被拒绝")),
    )
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.DEGRADED
    milvus = next(c for c in payload["components"] if c["name"] == "milvus")
    assert milvus["status"] == oh.DOWN
    assert "ConnectionError" in milvus["detail"]


def test_check_raising_does_not_propagate(monkeypatch):
    """单项抛异常 → 变成一条 down 组件，聚合器照常返回（绝不 500）。"""
    _stub_all_up(monkeypatch)

    async def boom():
        raise RuntimeError("探针自己写错了")

    monkeypatch.setattr(oh, "_check_milvus", boom)
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.DEGRADED
    assert any(c["status"] == oh.DOWN for c in payload["components"])


def test_degrade_counters_do_not_change_status(monkeypatch):
    """口径 2：计数器非零只是历史观测数据，不该让接口永远黄着。"""
    _stub_all_up(monkeypatch)
    monkeypatch.setattr(
        oh, "_check_degrade",
        lambda: (_comp("degrade", oh.UP, "累计 999 次"), {"rag_degraded": 999}),
    )
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.UP
    assert payload["degradeCounters"] == {"rag_degraded": 999}


def test_all_data_deps_down_yields_down(monkeypatch):
    """所有数据依赖同时不可用 = 服务已无能力，这才判 down。"""
    _stub_all_up(monkeypatch)
    monkeypatch.setattr(oh, "_check_milvus", _async(_comp("milvus", oh.DOWN, "x")))
    monkeypatch.setattr(oh, "_check_sqlite", _async(_comp("sqlite", oh.DOWN, "x")))
    monkeypatch.setattr(oh, "_check_jsdstu", _async(_comp("jsdstu", oh.DOWN, "x")))
    monkeypatch.setattr(
        oh, "_check_scheduler_state", lambda snap: _comp("scheduler", oh.DOWN, "x")
    )
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.DOWN


def test_external_jsdstu_down_is_only_degraded(monkeypatch):
    """外部系统（8060）不可达只降级，不把整套判死。"""
    _stub_all_up(monkeypatch)
    monkeypatch.setattr(
        oh, "_check_jsdstu",
        _async(_comp("jsdstu", oh.DEGRADED, "不可达：业务派活会失败")),
    )
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.DEGRADED


def test_scheduler_error_yields_degraded(monkeypatch):
    _stub_all_up(monkeypatch)
    monkeypatch.setattr(
        oh, "_check_scheduler_state",
        lambda snap: _comp("scheduler", oh.DEGRADED, "任务执行报错：reconcile"),
    )
    payload = _run(oh.collect_health(use_cache=False))
    assert payload["status"] == oh.DEGRADED


def test_never_ran_scheduler_is_degraded(monkeypatch):
    """"调度循环压根没挂载"是最难发现的故障，必须有观测点。"""
    monkeypatch.setattr(oh, "_PROCESS_START", 0.0)   # 假装进程已跑了很久
    comp = oh._check_scheduler_state({
        "tasks": [{
            "name": "reconcile", "enabled": True, "runs": 0,
            "initialDelaySec": 1, "intervalSec": 1, "lastError": None,
        }]
    })
    assert comp["status"] == oh.DEGRADED
    assert "从未执行" in comp["detail"]


def test_sqlite_missing_file_is_degraded_not_created(tmp_path):
    """探针只读：库不存在时报告 degraded，且**不创建文件**（零副作用）。"""
    missing = tmp_path / "nope" / "x.db"
    status, detail = oh._probe_sqlite("sqlite:x", missing)
    assert status == oh.DEGRADED
    assert "不存在" in detail
    assert not missing.exists()


# --------------------------------------------------------------- 缓存


def test_cache_hit_within_ttl(monkeypatch):
    _stub_all_up(monkeypatch)
    first = _run(oh.collect_health())
    assert first["cached"] is False
    second = _run(oh.collect_health())
    assert second["cached"] is True
    third = _run(oh.collect_health(use_cache=False))
    assert third["cached"] is False


# --------------------------------------------------------------- 端点


def test_endpoint_requires_shared_key():
    """E-3 与 S-1 的交叉验收：端点默认受保护，不在豁免清单里。"""
    resp = client.get("/v1/ops/health")
    assert resp.json()["code"] == 4010
    assert resp.status_code == 200


def test_endpoint_ok_with_key(monkeypatch):
    _stub_all_up(monkeypatch)
    resp = client.get("/v1/ops/health?fresh=true", headers=HEADERS)
    body = resp.json()
    assert body["code"] == 2000
    data = body["data"]
    assert data["status"] in {oh.UP, oh.DEGRADED, oh.DOWN}
    assert set(data) >= {"status", "checkedAt", "components", "summary"}
    for c in data["components"]:
        assert set(c) >= {"name", "status", "latencyMs", "detail"}
    assert "checkedAt" in data


def test_component_names_are_stable():
    """组件名是 8080 / 前端 的展示契约，改名要同步三端。"""
    assert "milvus" in oh.COMPONENT_ORDER
    assert "jsdstu" in oh.COMPONENT_ORDER
    assert len([n for n in oh.COMPONENT_ORDER if n.startswith("sqlite:")]) == 4

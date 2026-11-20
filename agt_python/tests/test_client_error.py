"""E-12 前端运行时报错：策略 / 落库 / 端点三层单测。

企业级落地规约 E-12（判据 C3）。分三层来测，因为它们在**坏起来时表现完全不同**：

1. **策略层**（`app/services/client_error.py`）纯函数：截断、页面清洗、来源归一、
   指纹、时间桶、采样。坏了的表现是"看板数据看着像对的、其实统计口径错了"——
   最难发现的一类。
2. **落库层**（`telemetry.record_client_error` + `flush_now` + `client_error_summary`）：
   核心是**合并写**（同错同桶只占一行、count 累加）。坏了的表现是"表被刷爆"
   或"次数严重偏低"。
3. **端点层**（`POST /v1/ops/client-error`）：鉴权（受 X-Agent-Key 保护）、
   **永远返回成功**、以及最关键的一条——**上报体里的 userId 必须被忽略**
   （身份只由上游以查询参数注入）。

本文件独立运行（本机安全删除钩子按回合共享 50 次配额）：
    python -m pytest tests/test_client_error.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================ 1. 策略层


def test_truncate_marks_truncation():
    """截断要留痕（`…`）：否则看的人会把被切掉的怪结尾当成真实报错文本。"""
    from app.services.client_error import _truncate

    assert _truncate("abcdef", 10) == "abcdef"
    assert _truncate("abcdef", 6) == "abcdef"
    assert _truncate("abcdef", 3) == "abc…"
    assert _truncate("", 3) == ""
    assert _truncate("abc", 0) == "abc"          # limit<=0 = 不截断
    assert _truncate("abcdef", 3).endswith("…")


def test_clean_page_strips_query_and_hash():
    """页面只留 path：`?query` 与 `#hash` 里常有 id / token，属"用户数据进观测表"。"""
    from app.services.client_error import _clean_page

    assert _clean_page("/dashboard/faults") == "/dashboard/faults"
    assert _clean_page("/kb?docId=42&x=1") == "/kb"
    assert _clean_page("/kb#/detail/42") == "/kb"
    assert _clean_page("/kb?docId=42#/d") == "/kb"   # 先剥 ? 后面的全部
    assert _clean_page(None) == ""
    assert len(_clean_page("/" + "a" * 500)) <= 201


def test_normalize_source_whitelist_keeps_unknown_not_drops():
    """白名单外的来源记 `unknown` 而**不是丢弃**：出现 unknown 本身就说明收口漏了场景。"""
    from app.services.client_error import _normalize_source

    assert _normalize_source("vue") == "vue"
    assert _normalize_source("VUE") == "vue"
    assert _normalize_source(" promise ") == "promise"
    assert _normalize_source("jquery") == "unknown"
    assert _normalize_source(None) == "unknown"


def test_fingerprint_stable_and_sensitive_to_each_part():
    from app.services.client_error import _fingerprint

    a = _fingerprint("/a", "vue", "boom")
    assert a == _fingerprint("/a", "vue", "boom")
    assert a != _fingerprint("/b", "vue", "boom")
    assert a != _fingerprint("/a", "window", "boom")
    assert a != _fingerprint("/a", "vue", "boom2")
    assert len(a) == 16


def test_fingerprint_ignores_message_tail_beyond_200_chars():
    """**这是刻意的口径，不是 bug**：指纹只用 message 前 200 字符。

    报错信息尾部常带随机 id（"row 5173 not found"），把整段算进指纹会让
    同一个错误每次算出不同指纹 → 去重彻底失效、表被刷爆。
    代价是"前 200 字相同、尾部不同"的两条错误会被合并——这是我们要的那一边。
    """
    from app.services.client_error import _fingerprint

    head = "x" * 200
    assert _fingerprint("/a", "vue", head + "TAIL-1") == _fingerprint("/a", "vue", head + "TAIL-2")
    # 但在 200 字符之内仍然敏感
    assert _fingerprint("/a", "vue", "x" * 199 + "1") != _fingerprint("/a", "vue", "x" * 199 + "2")


def test_bucket_is_deterministic_and_aligned():
    """固定桶：同一窗口内的时刻算出同一个键，跨边界则不同（刻意，见函数注释）。"""
    from app.services.client_error import _bucket

    assert _bucket(1_700_000_000.0, 60) == _bucket(1_700_000_030.0, 60)
    assert _bucket(1_700_000_000.0, 60) != _bucket(1_700_000_090.0, 60)
    # 窗口 <=0 时收敛为 1 秒，不炸（配错值不该让上报链路挂掉）
    assert _bucket(1_700_000_000.0, 0) == _bucket(1_700_000_000.0, -5)


def test_clamp_rate_converges_instead_of_rejecting():
    """采样率是观测旋钮，配错值应**收敛**而不是让服务起不来。"""
    from app.services.client_error import _clamp_rate

    assert _clamp_rate(0.5) == 0.5
    assert _clamp_rate(2) == 1.0
    assert _clamp_rate(-1) == 0.0
    assert _clamp_rate("bad") == 1.0     # 解析失败 = 别把数据丢光


# ============================================================ 2. report() 策略


class _FakeStore:
    """只记录调用，不落盘——把策略与存储分开测（存储另有专项）。"""

    def __init__(self):
        self.calls: list[dict] = []

    def record_client_error(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture()
def fake_store(monkeypatch):
    from app.services import client_error as ce

    store = _FakeStore()
    monkeypatch.setattr(ce, "telemetry", store)
    yield store
    store.calls.clear()


def test_report_happy_path_enqueues_cleaned_payload(fake_store):
    from app.services import client_error as ce

    out = ce.report({
        "page": "/dashboard/faults?docId=42#x", "source": "Vue",
        "message": "  TypeError: a is not a function  ",
        "stack": "at foo (main.js:1)", "appVersion": "1.0.0",
        "userAgent": "Mozilla/5.0",
    }, user_id=7)

    assert out["accepted"] is True
    assert out["fingerprint"] and out["bucket"]
    assert len(fake_store.calls) == 1
    call = fake_store.calls[0]
    assert call["page"] == "/dashboard/faults"      # query/hash 已剥
    assert call["source"] == "vue"                  # 已归一
    assert call["message"] == "TypeError: a is not a function"  # 已 strip
    assert call["user_id"] == 7
    assert call["app_version"] == "1.0.0"


def test_report_sanitizes_secrets_in_message_and_stack(fake_store, monkeypatch):
    """message / stack 会过 `redact()`：报错文本里可能带 Authorization 头或 token。"""
    from app.core import logging_setup
    from app.services import client_error as ce

    monkeypatch.setattr(logging_setup, "_secrets", {"super-secret-key-value"})
    ce.report({
        "page": "/a", "source": "vue",
        "message": "request failed key=super-secret-key-value",
        "stack": "Authorization: Bearer eyJhbGciOi.abc.def",
    })

    call = fake_store.calls[0]
    assert "super-secret-key-value" not in call["message"]
    assert "***" in call["message"]
    # Bearer 一律打码（正则规则，与日志出口同源）
    assert "eyJhbGciOi.abc.def" not in call["stack"]


def test_report_truncates_message_and_stack(fake_store, monkeypatch):
    from app.core import config as config_mod
    from app.services import client_error as ce

    monkeypatch.setattr(config_mod.settings, "client_error_max_message", 10)
    monkeypatch.setattr(config_mod.settings, "client_error_max_stack", 12)

    ce.report({"page": "/a", "message": "m" * 100, "stack": "s" * 100})

    call = fake_store.calls[0]
    assert len(call["message"]) == 11 and call["message"].endswith("…")
    assert len(call["stack"]) == 13 and call["stack"].endswith("…")


def test_report_empty_message_becomes_placeholder(fake_store):
    """空消息也要能入库：有些错误（被 Promise 拒绝的裸对象）确实没有 message，
    落一个占位符比让这行丢失更有价值——"这里炸过"本身就是信息。"""
    from app.services import client_error as ce

    ce.report({"page": "/a", "source": "promise"})

    assert fake_store.calls[0]["message"] == "(无消息)"


def test_report_disabled_switch_says_so(fake_store, monkeypatch):
    """关掉开关要**明确回答关掉了**，而不是静默丢弃：
    静默丢弃会让"看板怎么一直是空的"变成要翻配置才能回答的问题。"""
    from app.core import config as config_mod
    from app.services import client_error as ce

    monkeypatch.setattr(config_mod.settings, "client_error_enabled", False)

    out = ce.report({"page": "/a", "message": "x"})

    assert out["accepted"] is False and out["stored"] is False
    assert "disabled" in out["reason"]
    assert fake_store.calls == []


def test_report_rate_zero_drops_everything(fake_store, monkeypatch):
    from app.core import config as config_mod
    from app.services import client_error as ce

    monkeypatch.setattr(config_mod.settings, "client_error_sample_rate", 0.0)

    out = ce.report({"page": "/a", "message": "x"})

    assert out["accepted"] is False
    assert "sampled out" in out["reason"]
    assert fake_store.calls == []


def test_report_sampling_uses_random(fake_store, monkeypatch):
    """采样率 <1 时按 random 抽；这里把 random 钉死以断言两个分支都走对。"""
    from app.core import config as config_mod
    from app.services import client_error as ce

    monkeypatch.setattr(config_mod.settings, "client_error_sample_rate", 0.5)
    monkeypatch.setattr(ce.random, "random", lambda: 0.9)   # >= 0.5 → 丢
    assert ce.report({"page": "/a", "message": "x"})["accepted"] is False

    monkeypatch.setattr(ce.random, "random", lambda: 0.1)   # < 0.5 → 收
    assert ce.report({"page": "/a", "message": "x"})["accepted"] is True


def test_report_never_raises_even_if_store_explodes(monkeypatch):
    """**本模块最重要的一条**：上报链路上任何异常都必须在此终止。

    "错误上报失败"绝不能变成用户可见的错误——那等于用一个观测能力
    制造一个新的故障源。所以这里让存储层直接抛，断言 report 仍返回且不抛。
    """
    from app.services import client_error as ce

    class _Boom:
        def record_client_error(self, **kwargs):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(ce, "telemetry", _Boom())

    out = ce.report({"page": "/a", "message": "x"})   # 不抛 = 通过

    assert out["accepted"] is False
    assert out["reason"].startswith("internal:")
    assert "RuntimeError" in out["reason"]


def test_report_reports_stored_false_when_telemetry_off(fake_store, monkeypatch):
    """"收了但没存"要如实说：谎报 stored=true 会造出
    "接口说存了、库里没有"这类最难查的不一致。"""
    from app.core import config as config_mod
    from app.services import client_error as ce

    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", False)

    out = ce.report({"page": "/a", "message": "x"})

    assert out["accepted"] is True
    assert out["stored"] is False


# ============================================================ 3. 落库层


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


def _record(store, *, fingerprint="fp1", bucket="2026-01-01T00:00:00", page="/a",
            source="vue", message="boom", user_id=1):
    store.record_client_error(
        fingerprint=fingerprint, bucket=bucket, page=page, source=source,
        message=message, stack="", app_version="1.0.0", user_agent="UA", user_id=user_id,
    )


def test_same_error_same_bucket_merges_into_one_row(store):
    """合并写的核心断言：3 次上报 = 1 行、count=3。"""
    for _ in range(3):
        _record(store)
    store.flush_now()

    rows = store._conn.execute(
        "SELECT fingerprint, count FROM client_error").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 3

    summary = store.client_error_summary(hours=24, limit=10)
    assert summary["total"] == 3      # total 是 count 之和
    assert summary["distinct"] == 1   # distinct 才是"有多少类错"


def test_merge_happens_across_flushes(store):
    """跨两次 flush 的同一错误仍要合并 —— UPSERT 而不是"每批各写一行"。"""
    _record(store)
    store.flush_now()
    _record(store)
    store.flush_now()

    assert store.client_error_summary(hours=24, limit=10)["total"] == 2
    assert store._conn.execute("SELECT COUNT(*) FROM client_error").fetchone()[0] == 1


def test_different_bucket_creates_second_row(store):
    """跨时间桶不合并（固定桶的代价，写在设计注释里）。"""
    _record(store, bucket="2026-01-01T00:00:00")
    _record(store, bucket="2026-01-01T00:01:00")
    store.flush_now()

    summary = store.client_error_summary(hours=24, limit=10)
    assert summary["total"] == 2
    assert summary["distinct"] == 1     # 同一 fingerprint → 仍算一类错


def test_different_fingerprint_is_separate(store):
    _record(store, fingerprint="fp1", message="boom")
    _record(store, fingerprint="fp2", message="other")
    store.flush_now()

    assert store.client_error_summary(hours=24, limit=10)["distinct"] == 2


def test_aggregations_group_by_page_and_version(store):
    _record(store, fingerprint="a", page="/p1", message="m1")
    _record(store, fingerprint="a", page="/p1", message="m1")
    _record(store, fingerprint="b", page="/p2", message="m2", user_id=2)
    store.flush_now()

    s = store.client_error_summary(hours=24, limit=10)
    assert s["byPage"][0] == {"name": "/p1", "value": 2}   # 按 count 降序
    assert {r["name"] for r in s["byPage"]} == {"/p1", "/p2"}
    assert s["byVersion"] == [{"name": "1.0.0", "value": 3}]
    assert len(s["latest"]) == 2
    assert s["latest"][0]["userId"] in (1, 2)


def test_empty_page_shows_placeholder_not_empty_string(store):
    """分组值为空时给占位符：否则看板上会出现一行没有名字的分组，看不出是什么。"""
    _record(store, page="")
    store.flush_now()

    assert store.client_error_summary(hours=24, limit=10)["byPage"][0]["name"] == "(未知页面)"


def test_window_excludes_old_rows(store):
    """窗口按 `last_at` 过滤；**且比较的是同格式字符串**（T 分隔）。

    这条单测守着那个坑：若用 SQLite 的 `datetime('now',...)`（空格分隔）去比
    `2026-09-24T16:00:00`（T 分隔），第 11 位 `T` > 空格 → **同一天的行永远算在窗口内**，
    窗口被悄悄放宽最多 24 小时且不报错。
    """
    _record(store, fingerprint="new")
    store.flush_now()
    # 手工塞一条 100 天前的（正常路径不会产生，只能直写）
    store._conn.execute(
        "INSERT INTO client_error(fingerprint,bucket,page,source,message,stack,"
        "app_version,user_agent,user_id,trace_id,count,first_at,last_at) "
        "VALUES('old','2020-01-01T00:00:00','/a','vue','m','','','',0,'',1,"
        "'2020-01-01T00:00:00','2020-01-01T00:00:00')")
    store._conn.commit()

    assert store.client_error_summary(hours=24, limit=10)["total"] == 1   # 只算新的那条
    assert store.client_error_summary(hours=24 * 90, limit=10)["total"] == 1  # 上限 90 天，仍不含 2020 年


def test_trace_id_is_empty_without_request_context(store):
    """**不放假 traceId**：上下文里没有就存空串。

    若用会"凭空造 id"的 `current_trace_id()`，排查者会拿着一个在日志里
    查不到任何东西的 id 空转——比留空更坏。
    """
    _record(store)
    store.flush_now()

    assert store._conn.execute("SELECT trace_id FROM client_error").fetchone()[0] == ""


def test_summary_when_telemetry_disabled(tmp_path, monkeypatch):
    """埋点关闭时看板要能明确说"没开"，而不是显示 0（0 会被读成"前端没报错"）。"""
    from app.core import config as config_mod
    from app.services.telemetry import TelemetryStore

    monkeypatch.setattr(config_mod.settings, "telemetry_db_path", str(tmp_path / "obs.db"))
    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", False)

    s = TelemetryStore()
    out = s.client_error_summary(hours=24, limit=10)

    assert out["enabled"] is False
    assert out["reason"] == "telemetry disabled"


def test_schema_version_v7_registered(store):
    """建表要登记 schema_version（数据库规约 §2.6）。"""
    versions = {r[0] for r in store._conn.execute("SELECT version FROM schema_version")}
    assert 7 in versions


# ============================================================ 4. 端点层


def test_endpoint_requires_agent_key():
    """默认受保护（S-1）：无 X-Agent-Key 的上报请求被拒，且是 4010 而不是 500。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/v1/ops/client-error", json={"page": "/a", "message": "x"})
    assert r.status_code == 200
    assert r.json()["code"] == 4010


def test_endpoint_accepts_report_and_ignores_body_user_id(monkeypatch):
    """**身份只由上游注入**：body 里塞 userId 必须无效，实际值来自查询参数。

    这条是 S-1 铁律在本端点上的落地断言。若有人日后"顺手"把 body 里的
    userId 接进来，这里会立刻红。

    ⚠️ 断言必须看**库里的行**，不能看 `telemetry._q_client_error`。
    队列是 `flush_now` 的输入区，而后台 flush 循环（进程存活期间一直在跑）
    会在请求与断言之间把队列排空——"上报成功但队列为空"是**设计使然**，
    不是缺陷。曾按队列断言过一次，红得很有误导性（见 DEV_ISSUES 6.78）。
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app
    from app.services.telemetry import telemetry

    monkeypatch.setattr(settings, "client_error_enabled", True)
    monkeypatch.setattr(settings, "telemetry_enabled", True)

    with TestClient(app) as client:
        r = client.post(
            "/v1/ops/client-error?user_id=5",
            json={"page": "/dashboard/faults", "source": "vue", "message": "boom",
                  "appVersion": "9.9.9", "userAgent": "E12/UA-probe", "userId": 999},
            headers={"X-Agent-Key": settings.agent_shared_key},
        )
        body = r.json()
        # 落库在 with 块内断言：退出 TestClient 上下文会触发 shutdown，
        # 之后 `_enabled` 已复位，summary 会返回 "telemetry disabled" 而不是数据。
        telemetry.flush_now()
        latest = telemetry.client_error_summary(hours=24, limit=20)["latest"]
        # UA 不在 summary 里（看板用不到），但"别名没生效"会同时让 UA 也变空，
        # 所以直查一行把它一起钉住
        ua_row = telemetry._conn.execute(
            "SELECT user_agent FROM client_error WHERE page = '/dashboard/faults' "
            "ORDER BY id DESC LIMIT 1").fetchone()

    assert body["code"] == 2000
    assert body["data"]["accepted"] is True

    mine = [x for x in latest if x["page"] == "/dashboard/faults"]
    assert mine, "上报未落库（latest 里找不到本次上报的 page）"
    assert mine[0]["userId"] == 5, "userId 必须来自查询参数，而不是 body"
    assert 999 not in {x["userId"] for x in latest}, "body 里的 userId 绝不能进库"
    # ⚠️ 别名回归（实机抓到过一次）：`appVersion` / `userAgent` 是**别名**，
    # 走 `model_dump()` 而不带 `by_alias=True` 时键名是 `app_version` / `user_agent`，
    # 服务层按 camelCase 取键 → 两个字段静默变空串（接口照样 2000、库里照样有行，
    # 只有"按版本聚合"永远是「(未知版本)」）。这里钉住它。
    assert mine[0]["appVersion"] == "9.9.9", "appVersion 丢了：检查 model_dump(by_alias=True)"
    assert ua_row and ua_row[0] == "E12/UA-probe", "userAgent 丢了：检查 model_dump(by_alias=True)"


def test_endpoint_returns_2000_on_empty_body():
    """空体也不该 500：端点自身的健壮性属于"上报不能有前置条件"的一部分。

    两种空体都要过，因为它们是**不同的真实场景**：
    * 不带 `json=`（完全无体）——浏览器在页面卸载中发 `sendBeacon` 的常态；
    * `content=b""` 但带 `Content-Type: application/json`——`fetch` 被
      `keepalive` 截断、或调用方 `JSON.stringify(undefined)` 得到空串。

    若端点把请求体声明为必填，这两种都会吃 422 → `3001`，
    而它们**恰恰是最难复现的一类错误**（页面正在卸载，用户已经走了）。
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    with TestClient(app) as client:
        no_body = client.post("/v1/ops/client-error",
                              headers={"X-Agent-Key": settings.agent_shared_key})
        empty_json = client.post("/v1/ops/client-error", content=b"",
                                 headers={"X-Agent-Key": settings.agent_shared_key,
                                          "Content-Type": "application/json"})

    for r in (no_body, empty_json):
        assert r.status_code == 200
        assert r.json()["code"] == 2000


def test_query_endpoint_requires_agent_key_and_returns_shape():
    """看板读接口同样受保护；返回结构必须含 total/distinct/byPage/byVersion/latest。"""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    with TestClient(app) as client:
        unauth = client.get("/v1/agent/metrics/client-errors")
        authed = client.get("/v1/agent/metrics/client-errors",
                            headers={"X-Agent-Key": settings.agent_shared_key})

    assert unauth.json()["code"] == 4010
    data = authed.json()["data"]
    assert set(data) >= {"enabled", "total", "distinct", "byPage", "byVersion", "latest"}

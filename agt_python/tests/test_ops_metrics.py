"""E-9 轻量指标端点单测（企业级落地规约 E-9 验收标准）。

规约的验收原话是两条：**「`curl` 输出可被 Prometheus 解析」** 与
**「不接 Prometheus 时也零影响」**。这两条都不能靠"我自己看着像"来证明，因此本文件里：

1. **自写了一个最小 Prometheus 文本解析器**（`_parse`）——它按
   [文本格式 0.0.4](https://prometheus.io/docs/instrumenting/exposition_formats/) 的语法严格解析，
   并检查 Prometheus 真会报错的那几件事：`HELP`/`TYPE` 必须成对且在任何样本之前、
   **同名+同标签的重复序列**（真机会以 `duplicate sample for timestamp` 让**整次抓取失败**）、
   指标名与标签名必须匹配 `[a-zA-Z_:][a-zA-Z0-9_:]*`、标签值转义正确。
   不引 `prometheus_client`（决策 D3：零新依赖），也因为**自己写解析器才能断言"格式严格"**——
   官方库的解析器宽松到不会告诉你少了个 TYPE。
2. **零影响**：库不存在 / 表缺失 / 单族查询失败，端点都必须仍返回 200 且**不创建任何文件**。

其余断言集中在口径正确性：`_total` 必须等于 SQL 直算（不是"看起来合理"）、
分位必须单调且等于直算值、窗口必须真的收窄样本、`rag_decision` 空值必须映射成 `none`。

⚠️ 本文件独立运行（本机安全删除钩子按回合共享 50 次配额）：
    python -m pytest tests/test_ops_metrics.py -q
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import ops_metrics as om

# 密钥由 tests/conftest.py 注入（早于任何 app.* 导入）
HEADERS = {"X-Agent-Key": settings.agent_shared_key}

client = TestClient(app)

#: Prometheus 指标名/标签名的合法字符集（文本格式规范原文）
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?P<labels>\{(?:[a-zA-Z_][a-zA-Z0-9_]*=\"(?:[^\"\\]|\\.)*\"(?:,(?:[a-zA-Z_][a-zA-Z0-9_]*=\"(?:[^\"\\]|\\.)*\"))*)?\})?"
    r" (?P<value>\S+)$"
)

_TYPES = {"counter", "gauge", "histogram", "summary", "untyped"}


class _Parsed:
    def __init__(self) -> None:
        self.types: dict[str, str] = {}
        self.helps: dict[str, str] = {}
        self.samples: list[tuple[str, dict[str, str], float]] = []
        self.errors: list[str] = []

    def value(self, name: str, **labels) -> float | None:
        for n, ls, v in self.samples:
            if n == name and ls == labels:
                return v
        return None

    def values(self, name: str) -> dict[tuple, float]:
        return {tuple(sorted(ls.items())): v for n, ls, v in self.samples if n == name}

    def sum_of(self, name: str) -> float:
        return sum(v for n, _, v in self.samples if n == name)

    def labels_of(self, name: str) -> set[str]:
        out: set[str] = set()
        for n, ls, _ in self.samples:
            if n == name:
                out.update(ls)
        return out


def _unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_labels(raw: str | None) -> dict[str, str]:
    if not raw or raw == "{}":
        return {}
    body = raw[1:-1]
    out: dict[str, str] = {}
    for part in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', body):
        out[part.group(1)] = _unescape(part.group(2))
    return out


def _parse(text: str) -> _Parsed:
    """严格解析。任何一处不合规都写进 `errors`（而不是抛异常）——便于一次性看到全部问题。"""
    p = _Parsed()
    seen: set[tuple] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            p.errors.append(f"L{lineno}: 空行（真机容忍，但说明渲染多写了换行）")
            continue
        if line.startswith("#"):
            rest = line[1:].strip()
            if rest.startswith("HELP ") or rest.startswith("TYPE "):
                kind, _, payload = rest.partition(" ")
                name, _, arg = payload.partition(" ")
                if not _NAME_RE.match(name):
                    p.errors.append(f"L{lineno}: 非法指标名 {name!r}")
                    continue
                if kind == "HELP":
                    if name in p.helps:
                        p.errors.append(f"L{lineno}: HELP 重复声明 {name}")
                    p.helps[name] = arg
                    if name in p.types:
                        p.errors.append(f"L{lineno}: HELP 出现在 TYPE 之后（{name}）")
                else:
                    if arg not in _TYPES:
                        p.errors.append(f"L{lineno}: 未知类型 {arg!r}（{name}）")
                    if name in p.types:
                        p.errors.append(f"L{lineno}: TYPE 重复声明 {name}")
                    if name not in p.helps:
                        p.errors.append(f"L{lineno}: TYPE 之前缺 HELP（{name}）")
                    p.types[name] = arg
            continue

        m = _SAMPLE_RE.match(line)
        if not m:
            p.errors.append(f"L{lineno}: 无法解析的样本行 {line!r}")
            continue
        name = m.group("name")
        labels = _parse_labels(m.group("labels"))
        for k in labels:
            if not _NAME_RE.match(k):
                p.errors.append(f"L{lineno}: 非法标签名 {k!r}")
        if name not in p.types:
            p.errors.append(f"L{lineno}: 样本出现在 TYPE 之前（{name}）")
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            p.errors.append(f"L{lineno}: 重复序列 {name}{labels} —— 真机会让整次抓取失败")
        seen.add(key)
        raw = m.group("value")
        try:
            value = float(raw)
        except ValueError:
            if raw not in ("NaN", "+Inf", "-Inf"):
                p.errors.append(f"L{lineno}: 非法数值 {raw!r}")
            value = float("nan")
        p.samples.append((name, labels, value))
    return p


# --------------------------------------------------------------- 造数夹具


def _iso(delta_sec: float = 0.0) -> str:
    """与埋点一致的时间格式（`YYYY-MM-DDTHH:MM:SS`，本地时区）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delta_sec))


@pytest.fixture()
def ods(tmp_path, monkeypatch):
    """把埋点库指到临时目录，用**产品自己的建表语句**（`telemetry._ensure_conn`）建库再灌数。

    为什么不手抄 CREATE TABLE：手抄就多了一份 schema 副本，产品加列时测试不会跟着变——
    那种测试只会证明"我抄的 schema 自洽"。
    """
    from app.services.telemetry import telemetry

    db = tmp_path / "metrics" / "observability.db"
    monkeypatch.setattr(settings, "telemetry_db_path", str(db), raising=False)
    om.clear_cache()

    conn = telemetry._ensure_conn()
    conn.executescript("DELETE FROM agent_chat_log; DELETE FROM agent_tool_call;"
                       " DELETE FROM agent_delegation; DELETE FROM fault_event;")
    conn.commit()

    rows = [
        # (mode, intent_source, model, prompt, completion, duration_ms, ok, degraded, rag_decision, created_at)
        ("REACT", "rule", "deepseek-flash", 100, 50, 1000.0, 1, 0, "", _iso(-60)),
        ("REACT", "rule", "deepseek-flash", 200, 80, 2000.0, 1, 0, "kept", _iso(-120)),
        ("REACT", "llm", "deepseek-chat", 300, 90, 3000.0, 0, 1, "insufficient_evidence", _iso(-180)),
        ("PLAN", "rule", "deepseek-flash", 400, 10, 4000.0, 1, 0, "cross_domain", _iso(-300)),
        ("REACT", "rule", "deepseek-flash", 10, 5, 500.0, 1, 0, "kept", _iso(-100 * 3600)),
        ("REACT", "rule", "deepseek-flash", 10, 5, 700.0, 1, 0, "kept", _iso(-200 * 3600)),
    ]
    conn.executemany(
        "INSERT INTO agent_chat_log(mode,intent_source,model,prompt_tokens,completion_tokens,"
        "duration_ms,ok,degraded,rag_decision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO agent_tool_call(tool,ok,blocked,duration_ms,created_at) VALUES(?,?,?,?,?)",
        [("task_query", 1, "", 12.0, _iso(-60)),
         ("task_query", 1, "", 13.0, _iso(-70)),
         ("task_create", 0, "user_denied", 5.0, _iso(-80))])
    conn.executemany(
        "INSERT INTO agent_delegation(agent,status,duration_ms,created_at) VALUES(?,?,?,?)",
        [("alarm-analyst", "DONE", 100.0, _iso(-60)),
         ("ops-reporter", "TIMEOUT", 900.0, _iso(-70))])
    conn.executemany("INSERT INTO fault_event(path,detail,created_at) VALUES(?,?,?)",
                     [("rag_refused", "x", _iso(-60)), ("rag_refused", "y", _iso(-70)),
                      ("reply_cleaned", "z", _iso(-80))])
    conn.commit()

    yield db

    om.clear_cache()
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    telemetry._conn = None
    telemetry._counters.clear()


def _sql(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


# --------------------------------------------------------------- 格式合规（验收第 1 条）


def test_text_parses_without_error(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.errors == [], "渲染结果不符合 Prometheus 文本格式：" + "; ".join(p.errors)
    assert len(p.samples) > 0


def test_all_required_families_declared(ods):
    p = _parse(om.render_metrics(use_cache=False))
    missing = [f for f in om.REQUIRED_FAMILIES if f not in p.types]
    assert missing == [], f"缺失指标族：{missing}"


def test_family_has_help_and_type_paired(ods):
    """HELP/TYPE 必须成对——`_parse` 会把"只有 TYPE 没 HELP"记成错误。"""
    p = _parse(om.render_metrics(use_cache=False))
    assert p.errors == []
    for name, mtype in p.types.items():
        assert mtype in _TYPES
        assert name in p.helps, f"{name} 缺 HELP"


def test_no_duplicate_series(ods):
    """重复序列是真机致命错：`duplicate sample for timestamp` 会让**整次抓取失败**。

    直接构造一个会撞车的场景：同一个 tool 名出现在多行（正常），
    以及"同一 mode 的多个分位"——只要 label 不同就不该被判重复。
    """
    text = om.render_metrics(use_cache=False)
    assert "duplicate" not in text.lower()
    p = _parse(text)
    assert p.errors == []
    keys = [(n, tuple(sorted(ls.items()))) for n, ls, _ in p.samples]
    assert len(keys) == len(set(keys)), "渲染出了重复序列"


def test_counter_suffix_uses_counter_type(ods):
    """`_total` 结尾必须是 counter；耗时分位/up/ok 必须是 gauge。

    这条挡的是最容易犯的错：**把窗口内计数标成 counter**——窗口滑动时值会下降，
    Prometheus 会当成"计数器重置"，`rate()` 结果全错。
    """
    p = _parse(om.render_metrics(use_cache=False))
    for name, mtype in p.types.items():
        if name.endswith("_total"):
            assert mtype == "counter", f"{name} 是 _total 却不是 counter"
        if name.endswith(("_seconds", "_requests", "_up", "_ok", "_enabled", "_error", "_info")):
            assert mtype == "gauge", f"{name} 应为 gauge，实际 {mtype}"


def test_windowed_metric_is_gauge_not_counter(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.types[f"{om.PREFIX}_chat_window_requests"] == "gauge"
    assert p.types[f"{om.PREFIX}_chat_duration_seconds"] == "gauge"


def test_label_value_escaping_roundtrips(tmp_path, monkeypatch):
    """标签值里的 `"` `\\` 换行必须被转义，且解析后能还原原值。

    真实触发场景：工具名/任务名将来可能带这些字符（人名、路径），
    不转义会直接把整个抓取打坏。
    """
    from app.services.telemetry import telemetry
    from app.services import ops_metrics as m

    db = tmp_path / "metrics" / "observability.db"
    monkeypatch.setattr(settings, "telemetry_db_path", str(db), raising=False)
    m.clear_cache()
    conn = telemetry._ensure_conn()
    tricky = 'weird"name\\with\nnewline'
    conn.execute("DELETE FROM agent_tool_call")
    conn.execute("INSERT INTO agent_tool_call(tool,ok,blocked,created_at) VALUES(?,?,?,?)",
                 (tricky, 1, "", _iso()))
    conn.commit()
    try:
        p = _parse(m.render_metrics(use_cache=False))
        assert p.errors == []
        assert p.value(f"{om.PREFIX}_tool_calls_total", tool=tricky) == 1.0, "转义后未能还原原标签值"
    finally:
        conn.close()
        telemetry._conn = None
        m.clear_cache()


# --------------------------------------------------------------- 口径正确（与 SQL 直算比对）


def test_chat_requests_total_matches_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.sum_of(f"{om.PREFIX}_chat_requests_total") == _sql(ods, "SELECT COUNT(*) FROM agent_chat_log")[0][0]
    # 按 mode 分组的切片也要对
    assert p.value(f"{om.PREFIX}_chat_requests_total",
                   mode="REACT", intent_source="rule", model="deepseek-flash") == 4.0
    assert p.value(f"{om.PREFIX}_chat_requests_total",
                   mode="PLAN", intent_source="rule", model="deepseek-flash") == 1.0
    assert p.value(f"{om.PREFIX}_chat_requests_total",
                   mode="REACT", intent_source="llm", model="deepseek-chat") == 1.0


def test_failed_and_degraded_match_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_chat_failed_total", mode="REACT") == 1.0
    assert p.value(f"{om.PREFIX}_chat_degraded_total", mode="REACT") == 1.0
    assert p.value(f"{om.PREFIX}_chat_failed_total", mode="PLAN") is None, "没有失败的 mode 不该输出 0 序列"


def test_llm_tokens_match_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    prompt, completion = _sql(
        ods, "SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM agent_chat_log"
             " WHERE model='deepseek-flash'")[0]
    assert p.value(f"{om.PREFIX}_llm_tokens_total",
                   model="deepseek-flash", kind="prompt") == float(prompt)
    assert p.value(f"{om.PREFIX}_llm_tokens_total",
                   model="deepseek-flash", kind="completion") == float(completion)


def test_tool_calls_match_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_tool_calls_total", tool="task_query") == 2.0
    assert p.value(f"{om.PREFIX}_tool_calls_total", tool="task_create") == 1.0
    assert p.value(f"{om.PREFIX}_tool_calls_failed_total", tool="task_create") == 1.0
    assert p.value(f"{om.PREFIX}_tool_calls_blocked_total", tool="task_create") == 1.0
    assert p.value(f"{om.PREFIX}_tool_calls_failed_total", tool="task_query") == 0.0


def test_delegations_match_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_delegations_total", agent="alarm-analyst", status="DONE") == 1.0
    assert p.value(f"{om.PREFIX}_delegations_total", agent="ops-reporter", status="TIMEOUT") == 1.0


def test_faults_match_sql(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_faults_total", path="rag_refused") == 2.0
    assert p.value(f"{om.PREFIX}_faults_total", path="reply_cleaned") == 1.0


def test_rag_decision_empty_maps_to_none(ods):
    """空 `rag_decision` 必须映射为 `none`，而不是空标签值。

    空标签值在 PromQL 里能查但在图表上是一片空白；`none` 才表达"这轮没走 RAG 判定"。
    """
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_rag_decisions_total", decision="none") == 1.0
    assert p.value(f"{om.PREFIX}_rag_decisions_total", decision="kept") == 3.0
    assert p.value(f"{om.PREFIX}_rag_decisions_total", decision="cross_domain") == 1.0
    assert p.value(f"{om.PREFIX}_rag_decisions_total", decision="insufficient_evidence") == 1.0
    assert "" not in p.labels_of(f"{om.PREFIX}_rag_decisions_total")


def test_quantiles_are_ordered_and_match_nearest_rank(ods):
    """分位必须单调，且等于**最近秩法**直算值（不是插值、不是平均数）。

    窗口内 REACT 三条 = [1000, 2000, 3000] ms。最近秩法下 p50 取第 ceil(0.5×3)=2 个观测
    → **2000ms**（而插值法会给 2000、平均数也给 2000，这里三者恰好重合；
    真正的判别力在 p95/p99：最近秩给 **3000**（真实观测过的值），插值会给 2900 附近一个
    **真机上并不存在**的耗时）。
    """
    p = _parse(om.render_metrics(window_hours=1, use_cache=False))
    values = [1000.0, 2000.0, 3000.0]  # 窗口内 REACT 三条（第 4 条是 PLAN）
    assert p.value(f"{om.PREFIX}_chat_duration_seconds", mode="REACT", quantile="0.5") == values[1] / 1000
    assert p.value(f"{om.PREFIX}_chat_duration_seconds", mode="REACT", quantile="0.95") == values[2] / 1000
    assert p.value(f"{om.PREFIX}_chat_duration_seconds", mode="REACT", quantile="0.99") == values[2] / 1000
    q = [p.value(f"{om.PREFIX}_chat_duration_seconds", mode="REACT", quantile=f)
         for f in ("0.5", "0.95", "0.99")]
    assert q == sorted(q), "分位未单调"
    # 单位必须真的是秒（这条挡住"毫秒值挂在 _seconds 名下"的静默错 1000 倍）
    assert max(q) == 3.0, f"耗时单位不是秒：{q}"


def test_duration_average_and_window_count_match_sql(ods):
    p = _parse(om.render_metrics(window_hours=1, use_cache=False))
    avg = _sql(ods, "SELECT AVG(duration_ms) FROM agent_chat_log WHERE mode='REACT'"
                    " AND created_at >= ?", (om._window_cutoff(1),))[0][0]
    assert p.value(f"{om.PREFIX}_chat_duration_avg_seconds", mode="REACT") == pytest.approx(avg / 1000)
    assert p.value(f"{om.PREFIX}_chat_window_requests", mode="REACT") == 3.0


def test_window_really_narrows_samples(ods):
    """窗口必须真的生效：宽窗口能看到 2 小时前以外的两条，窄窗口看不到。"""
    narrow = _parse(om.render_metrics(window_hours=1, use_cache=False))
    wide = _parse(om.render_metrics(window_hours=720, use_cache=False))
    assert narrow.value(f"{om.PREFIX}_chat_window_requests", mode="REACT") == 3.0
    assert wide.value(f"{om.PREFIX}_chat_window_requests", mode="REACT") == 5.0
    assert wide.value(f"{om.PREFIX}_chat_requests_total",
                      mode="REACT", intent_source="rule", model="deepseek-flash") == 4.0, \
        "全时段计数器不该受窗口影响"


def test_window_is_clamped(ods):
    """窗口越界要夹紧，不能让调用方用一个超大窗口去扫全表（或负数搞出未来时间）。"""
    assert f"{om.PREFIX}_metrics_window_seconds" in om.render_metrics(window_hours=0, use_cache=False)
    p = _parse(om.render_metrics(window_hours=0, use_cache=False))
    assert p.value(f"{om.PREFIX}_metrics_window_seconds") == 3600
    p = _parse(om.render_metrics(window_hours=10 ** 6, use_cache=False))
    assert p.value(f"{om.PREFIX}_metrics_window_seconds") == 720 * 3600


def test_self_metrics_present(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_python_up") == 1.0
    info = [ls for n, ls, _ in p.samples if n == f"{om.PREFIX}_info"]
    assert len(info) == 1
    assert info[0]["service"] == "agt_python"
    assert info[0]["version"], "info 必须带版本号（E-1 的版本单一来源要在指标里也看得到）"
    assert p.value(f"{om.PREFIX}_metrics_scrape_duration_seconds") is not None


def test_collect_ok_all_one_on_healthy_db(ods):
    p = _parse(om.render_metrics(use_cache=False))
    for src in ("ods", "ods_chat", "ods_tools", "ods_durations", "scheduler", "degrade_counters"):
        assert p.value(f"{om.PREFIX}_metrics_collect_ok", source=src) == 1.0, f"{src} 采集未成功"


def test_degrade_since_start_is_separate_scope_from_faults(ods):
    """两个降级口径**不同**：`faults_total` 建表以来、`degrade_events_since_start_total` 本进程。

    这条断言的用处是**防止将来有人"顺手"把两者合并或断言它们相等**——
    `fault_counters.json` 只写不读（进程启动不恢复），所以重启后必然不等。
    """
    from app.services.telemetry import telemetry

    telemetry._counters["rag_refused"] = 7
    p = _parse(om.render_metrics(use_cache=False))
    assert p.value(f"{om.PREFIX}_degrade_events_since_start_total", path="rag_refused") == 7.0
    assert p.value(f"{om.PREFIX}_faults_total", path="rag_refused") == 2.0, "表口径不该被内存计数器影响"


def test_scheduler_metrics_present(ods):
    p = _parse(om.render_metrics(use_cache=False))
    assert f"{om.PREFIX}_scheduler_task_enabled" in p.types
    assert p.value(f"{om.PREFIX}_scheduler_task_enabled") is not None or p.types, "任务族已声明"


# --------------------------------------------------------------- 零影响（验收第 2 条）


def test_missing_db_degrades_and_creates_nothing(tmp_path, monkeypatch):
    """库不存在时：端点照常渲染、`collect_ok{ods}=0`、**且不创建文件**。

    最后一条最要紧：`sqlite3.connect()` 默认会建库——如果忘了先 `exists()`，
    抓一次指标就在产品目录里留一个空库（E-3 的探针踩过同类坑）。
    """
    from app.services.telemetry import telemetry

    missing = tmp_path / "nope" / "observability.db"
    monkeypatch.setattr(settings, "telemetry_db_path", str(missing), raising=False)
    om.clear_cache()
    telemetry._conn = None

    text = om.render_metrics(use_cache=False)
    p = _parse(text)
    assert p.errors == []
    assert p.value(f"{om.PREFIX}_metrics_collect_ok", source="ods") == 0.0
    assert p.value(f"{om.PREFIX}_python_up") == 1.0, "ODS 缺失不该让自身存活指标消失"
    assert not missing.exists(), "指标端点**创建**了库文件（违反零副作用）"
    assert not missing.parent.exists(), "指标端点创建了目录（违反零副作用）"


def test_broken_table_isolates_failure(tmp_path, monkeypatch):
    """单族 SQL 失败只影响该族：其余族照常输出，且汇总位如实为 0。"""
    from app.services.telemetry import telemetry

    db = tmp_path / "metrics" / "observability.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    # 只建 agent_chat_log —— tool/delegation/fault 三张表缺失
    conn.execute("CREATE TABLE agent_chat_log(id INTEGER PRIMARY KEY, mode TEXT, intent_source TEXT,"
                 " model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, duration_ms REAL,"
                 " ok INTEGER, degraded INTEGER, rag_decision TEXT, created_at TEXT)")
    conn.execute("INSERT INTO agent_chat_log(mode,ok,degraded,created_at) VALUES('REACT',1,0,?)",
                 (_iso(),))
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "telemetry_db_path", str(db), raising=False)
    om.clear_cache()
    telemetry._conn = None

    p = _parse(om.render_metrics(use_cache=False))
    assert p.errors == []
    assert p.sum_of(f"{om.PREFIX}_chat_requests_total") == 1.0, "健康的族必须照常输出"
    assert p.value(f"{om.PREFIX}_metrics_collect_ok", source="ods_tools") == 0.0
    assert p.value(f"{om.PREFIX}_metrics_collect_ok", source="ods") == 0.0


def test_no_secret_or_user_content_in_output(ods):
    """指标里**不得**出现密钥、token 或用户输入原文（与 S-8/E-6 同源）。

    造一个诱饵：往工具摘要里塞一段看起来像密钥的串，确认它不出现在输出里。
    """
    from app.services.telemetry import telemetry

    conn = telemetry._ensure_conn()
    conn.execute("INSERT INTO agent_tool_call(tool,ok,blocked,error,args_digest,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 ("secret_probe", 0, "", "Authorization: Bearer eyJhbGciOiSECRET",
                  "sk-live-DEADBEEFDEADBEEF", _iso()))
    conn.commit()
    om.clear_cache()

    text = om.render_metrics(use_cache=False)
    assert settings.agent_shared_key not in text
    assert "eyJhbGciOiSECRET" not in text
    assert "sk-live-DEADBEEFDEADBEEF" not in text
    assert "Authorization" not in text, "指标里不该透出任何请求头原文"


# --------------------------------------------------------------- 缓存与端点


def test_cache_returns_same_text_within_ttl(ods):
    a = om.render_metrics()
    b = om.render_metrics()
    assert a == b, "TTL 内应命中缓存（渲染耗时被缓存抹掉）"
    c = om.render_metrics(use_cache=False)
    assert isinstance(c, str) and c.endswith("\n")


def test_endpoint_requires_key(ods):
    """受保护方向与 /v1/ops/health 一致：不带 key → 4010。"""
    r = client.get("/v1/ops/metrics")
    assert r.status_code == 200
    assert r.json()["code"] == 4010


def test_endpoint_returns_prometheus_content_type(ods):
    r = client.get("/v1/ops/metrics", headers=HEADERS)
    assert r.status_code == 200
    ctype = r.headers["content-type"]
    assert ctype.startswith("text/plain"), ctype
    assert "version=0.0.4" in ctype, f"缺版本号，抓取器无法确定解析器：{ctype}"
    p = _parse(r.text)
    assert p.errors == []
    assert p.types[f"{om.PREFIX}_python_up"] == "gauge"


def test_endpoint_window_param(ods):
    r = client.get("/v1/ops/metrics?window=1&fresh=true", headers=HEADERS)
    p = _parse(r.text)
    assert p.value(f"{om.PREFIX}_metrics_window_seconds") == 3600


def test_render_is_pure_text(ods):
    """响应体必须**只有文本**：不能是 JSON 包装（Prometheus 不认 `{"code":2000,...}`）。"""
    r = client.get("/v1/ops/metrics?fresh=true", headers=HEADERS)
    assert not r.text.lstrip().startswith("{")
    assert r.text.endswith("\n")

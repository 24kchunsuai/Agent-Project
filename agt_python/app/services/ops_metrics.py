"""轻量指标端点（企业级落地规约 **E-9**，判据 C3「可观测」）。

## 这个模块回答什么

E-3 回答了"现在哪一环坏了"，E-6 回答了"这一次请求经过哪些环节"。两者都是**给人看**的。
本模块补上第三种读者：**监控系统**。它把系统里本来就有的数字，按
**Prometheus 文本格式**（`text/plain; version=0.0.4`）暴露出来，让"趋势"可被自动采集。

## 为什么手写而不引 `prometheus_client`

决策 D3 明确"可观测深度 = 轻量自建，不引外部组件"，且规约 E-9 原文写了"零新依赖手写即可"。
另外**我们的数据不在进程内存里**——它是 SQLite 里的 ODS 表（`agent_chat_log` 等），
不是"在代码里 `Counter().inc()`"那种客户端库擅长的场景。客户端库能省的只有"文本序列化"
这一小块，而那一块的复杂度恰好是 `_fmt_labels` 这十几行。引一个依赖换十几行代码，
还要为它维护版本上限（E-4 的锁）——不划算。

## 数据源：只读既有 ODS 表与既有计数器（**不新增埋点通道**）

| 指标族 | 数据源 | 口径 |
|---|---|---|
| `agt_chat_requests_total` | `agent_chat_log` | 按 `mode`（链路）/`intent_source`/`model` 分组 |
| `agt_chat_failed_total` | `agent_chat_log` | `ok=0` |
| `agt_chat_degraded_total` | `agent_chat_log` | `degraded<>0` |
| `agt_llm_tokens_total` | `agent_chat_log` | `prompt_tokens` / `completion_tokens` 分 `kind` |
| `agt_rag_decisions_total` | `agent_chat_log` | `rag_decision` 四值 + `none`（非 RAG 链路） |
| `agt_tool_calls_total` / `_failed_total` / `_blocked_total` | `agent_tool_call` | 按 `tool` |
| `agt_delegations_total` | `agent_delegation` | 按 `agent`/`status` |
| `agt_faults_total` | `fault_event` 表 | 按 `path`。**建表以来**累计（ODS 只增不改）→ 单调，适合 `rate()`/`increase()` |
| `agt_degrade_events_since_start_total` | 内存降级计数器（E-3 用的那个） | **本进程**累计。⚠️ **重启归零**，与上一行的"建表以来"**不同口径**，别当成一致性检查 |
| `agt_chat_duration_seconds` | `agent_chat_log` | **窗口内**分位（默认 24h），按 `mode` |
| `agt_scheduler_task_*` | `scheduler.status()` | 内存态 |

## 四条实现约束

1. **不新增埋点通道**：只读既有表与既有计数器，一个字段都不加（规约原文要求）。
2. **零副作用**：SQLite **只读**，库文件不存在时**跳过该族**（`sqlite3.connect` 会创建文件——
   必须先 `exists()` 判断，否则抓一次指标就在产品目录里留一个新库）。
3. **单项失败不炸**：任一族的 SQL 失败只把 `agt_metrics_collect_ok{source}` 置 0 并跳过该族，
   端点仍返回 200 与其余族——**指标端点本身不能成为新的故障点**。
4. **计数器全时段累计、只增不减**：`_total` 一律取**建表以来**的累计值（ODS 只增不改），
   满足 Prometheus 对 counter 的单调性要求；**绝不**把窗口内计数标成 counter
   （窗口滑动时值会下降，会被 Prometheus 误判成"计数器重置"，rate() 全错）。
   窗口相关的量一律用 gauge。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 指标名前缀（命名空间）。Java 侧另有 `agt_java_*`，两段合起来才是"整套系统"
PREFIX = "agt"

#: 默认窗口（小时）——分位类指标的统计范围
DEFAULT_WINDOW_HOURS = 24

#: 结果缓存 TTL（秒）。抓取通常 15~60s 一次，但"抓取器配错成 1s"是很常见的运维事故，
#: 缓存 2 秒足以挡住踩踏，又不会让数据显得滞后。
CACHE_TTL_SEC = 2.0

#: 分位定义（Prometheus 的 summary 也用这几个）
_QUANTILES: tuple[float, ...] = (0.5, 0.95, 0.99)

#: `rag_decision` 空值的对外名字——空串不是合法 label 语义（读者不知道它什么意思），
#: `none` 明确表示"这条链路没有走 RAG 判定"
_RAG_DECISION_NONE = "none"

_cache: dict[str, Any] = {"at": 0.0, "key": "", "text": ""}


# --------------------------------------------------------------------- 文本序列化


def _escape_label(value: Any) -> str:
    """转义 label 值。Prometheus 文本格式里只有三个必须转义：`\\\\` `"` 换行。"""
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    """HELP 文本只允许转义 `\\\\` 与换行（引号是合法的）。"""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _num(value: Any) -> str:
    """数值序列化：整数不带小数点（避免 Prometheus 抓成 1.0 那种"整数字段是浮点"的困惑），
    浮点保留 6 位且**避免科学计数法**（`1e-05` 虽合法但人读痛苦，也容易在文档里抄错）。"""
    if value is None:
        return "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "0"
    if f != f:  # NaN
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "+Inf" if f > 0 else "-Inf"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    # 定点输出，最多 6 位小数，去掉尾随 0
    out = f"{f:.6f}".rstrip("0").rstrip(".")
    return out or "0"


def _fmt_labels(labels: Iterable[tuple[str, Any]]) -> str:
    """`{a="1",b="x"}`；无标签返回空串（此时**不能**输出 `{}`——空花括号合法但难看）。"""
    pairs = [(k, v) for k, v in labels if v is not None]
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in pairs)
    return "{" + inner + "}"


class _Builder:
    """极小的文本构造器：负责 HELP/TYPE 成对、序列去重、结尾换行。

    **去重是必须的**：Prometheus 对同名同 label 的重复序列会直接报错
    （`duplicate sample for timestamp`），整个抓取失败 —— 一个重复就毁掉全部指标。
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()
        self._seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()

    def family(self, name: str, mtype: str, help_text: str) -> None:
        """声明一个指标族（HELP + TYPE）。同一族只声明一次，重复调用无副作用。"""
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f"# HELP {name} {_escape_help(help_text)}")
        self._lines.append(f"# TYPE {name} {mtype}")

    def sample(self, name: str, value: Any, labels: Iterable[tuple[str, Any]] = ()) -> None:
        """写一条序列。完全相同 (name, labels) 的重复写入会被忽略（见类 docstring）。"""
        pairs = tuple((k, v) for k, v in labels if v is not None)
        key = (name, pairs)
        if key in self._seen:
            return
        self._seen.add(key)
        self._lines.append(f"{name}{_fmt_labels(pairs)} {_num(value)}")

    def comment(self, text: str) -> None:
        """写注释行（Prometheus 解析器对 `#` 开头但不是 HELP/TYPE 的行按注释忽略）。"""
        for line in text.splitlines() or [""]:
            self._lines.append(f"# {line}".rstrip())

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


# --------------------------------------------------------------------- 数据访问


def _metrics_db_path() -> Path:
    """与埋点 store **同源**的库路径（不另写一份常量，否则会出现"指标读的库和埋点写的库不是同一个"）。"""
    from app.services.telemetry import telemetry

    return telemetry._db_path()


def _open_readonly(path: Path) -> sqlite3.Connection:
    """只读打开。**调用方必须先确认文件存在**（见模块约束 2）。

    `mode=ro` 是真保证（不会建库、不会写 `-wal`），但它在"库处于 WAL 模式且 `-shm`
    不可建"等少见情形下会直接失败。所以失败后退回普通连接——此时文件已确认存在，
    普通连接同样**不会创建任何东西**，最坏情况只是失去了"物理上写不进去"这层保险
    （而我们本来就只有 SELECT）。
    """
    uri = "file:///" + str(path).replace("\\", "/") + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:  # noqa: BLE001 —— 降级而非中断
        logger.warning("只读方式打开埋点库失败，退回普通连接 | %s: %s", type(exc).__name__, exc)
        return sqlite3.connect(str(path), timeout=2.0)


def _query(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return list(conn.execute(sql, args))


def _window_cutoff(hours: int) -> str:
    """窗口起点，格式与 `created_at` 一致（`YYYY-MM-DDTHH:MM:SS`）供字符串比较。

    为什么用字符串比较：`created_at` 一直是这个格式（埋点侧统一写），
    与 `datetime()` 混用会引入时区歧义；直接字典序比较反而确定。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - hours * 3600))


def _quantile(sorted_values: list[float], q: float) -> float | None:
    """最近秩法（nearest-rank）分位：取第 `ceil(q*n)` 个观测，索引从 0 起。

    为什么不用线性插值：观测是**离散的毫秒耗时**，插值出来的"第 95 百分位是 812.37ms"
    是虚构的精度——真机上并不存在这样一次请求。最近秩法给出的永远是**真实观测过的值**，
    这更符合"我要知道最慢的那批请求有多慢"的实际用法。
    """
    if not sorted_values:
        return None
    n = len(sorted_values)
    idx = max(0, min(n - 1, int(-(-q * n // 1)) - 1))  # ceil(q*n) - 1
    return sorted_values[idx]


def _ms_to_sec(value: float | None) -> float | None:
    """毫秒 → 秒。

    ⚠️ **这一步不能省**（本模块第一版就漏了，被单测抓住）：Prometheus 的约定是
    暴露**基础单位**，耗时的基础单位是秒。把毫秒值挂在 `_seconds` 名字下**不会报任何错**，
    但任何按 `_seconds` 写告警阈值的人都会静默差 1000 倍（"超过 2 秒告警"实际是 2 毫秒）。
    这类错误在图上完全看不出来——所以它必须由单测按"与 SQL 直算比对"来挡。
    """
    return None if value is None else value / 1000.0


# --------------------------------------------------------------------- 各族采集


def _collect_chat(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_chat_requests_total", "counter",
             "对话请求总数（按链路 mode、意图来源、模型分）。全时段累计。")
    for mode, source, model, n in _query(
            conn,
            "SELECT COALESCE(mode,''), COALESCE(intent_source,''), COALESCE(model,''), COUNT(*)"
            " FROM agent_chat_log GROUP BY 1,2,3 ORDER BY 1,2,3"):
        b.sample(f"{PREFIX}_chat_requests_total", n,
                 (("mode", mode), ("intent_source", source), ("model", model)))

    b.family(f"{PREFIX}_chat_failed_total", "counter", "对话失败总数（ok=0），按链路分。")
    for mode, n in _query(conn, "SELECT COALESCE(mode,''), COUNT(*) FROM agent_chat_log"
                                " WHERE COALESCE(ok,1)=0 GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_chat_failed_total", n, (("mode", mode),))

    b.family(f"{PREFIX}_chat_degraded_total", "counter",
             "发生降级处理的对话总数（degraded<>0），按链路分。")
    for mode, n in _query(conn, "SELECT COALESCE(mode,''), COUNT(*) FROM agent_chat_log"
                                " WHERE COALESCE(degraded,0)<>0 GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_chat_degraded_total", n, (("mode", mode),))


def _collect_tokens(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_llm_tokens_total", "counter",
             "LLM token 累计数（kind=prompt|completion），按模型分。口径为对话轮粒度。")
    for model, prompt, completion in _query(
            conn,
            "SELECT COALESCE(model,''), COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0) FROM agent_chat_log GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_llm_tokens_total", prompt, (("model", model), ("kind", "prompt")))
        b.sample(f"{PREFIX}_llm_tokens_total", completion, (("model", model), ("kind", "completion")))


def _collect_rag(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_rag_decisions_total", "counter",
             f"RAG 判定分布（kept/cross_domain/no_hit/insufficient_evidence/{_RAG_DECISION_NONE}）；"
             f"{_RAG_DECISION_NONE} = 该轮未走 RAG 判定。")
    for decision, n in _query(conn, "SELECT COALESCE(rag_decision,''), COUNT(*) FROM agent_chat_log"
                                    " GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_rag_decisions_total", n,
                 (("decision", decision or _RAG_DECISION_NONE),))


def _collect_tools(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_tool_calls_total", "counter", "工具调用总数，按工具名分。")
    b.family(f"{PREFIX}_tool_calls_failed_total", "counter", "工具调用失败总数（ok=0），按工具名分。")
    b.family(f"{PREFIX}_tool_calls_blocked_total", "counter",
             "被确认门拦下的工具调用总数（blocked 非空：用户拒绝或等待确认），按工具名分。")
    for tool, total, failed, blocked in _query(
            conn,
            "SELECT COALESCE(tool,''), COUNT(*),"
            " COALESCE(SUM(CASE WHEN COALESCE(ok,1)=0 THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(CASE WHEN COALESCE(blocked,'')<>'' THEN 1 ELSE 0 END),0)"
            " FROM agent_tool_call GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_tool_calls_total", total, (("tool", tool),))
        b.sample(f"{PREFIX}_tool_calls_failed_total", failed, (("tool", tool),))
        b.sample(f"{PREFIX}_tool_calls_blocked_total", blocked, (("tool", tool),))


def _collect_delegations(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_delegations_total", "counter", "专家委派总数，按子智能体与结果状态分。")
    for agent, status, n in _query(
            conn,
            "SELECT COALESCE(agent,''), COALESCE(status,''), COUNT(*)"
            " FROM agent_delegation GROUP BY 1,2 ORDER BY 1,2"):
        b.sample(f"{PREFIX}_delegations_total", n, (("agent", agent), ("status", status)))


def _collect_faults(b: _Builder, conn: sqlite3.Connection) -> None:
    b.family(f"{PREFIX}_faults_total", "counter",
             "降级/故障事件累计数（fault_event 表，ODS 唯一真相），按 path 分。")
    for path, n in _query(conn, "SELECT COALESCE(path,''), COUNT(*) FROM fault_event"
                                " GROUP BY 1 ORDER BY 1"):
        b.sample(f"{PREFIX}_faults_total", n, (("path", path),))


def _collect_degrade_counters(b: _Builder) -> None:
    """内存降级计数器（E-3 健康聚合展示的同一份数据）。

    ⚠️ **口径与 `agt_faults_total` 不同，不要当一致性检查**（这一点写文档时踩过一次）：
    它只累计**本进程**的降级次数，服务一重启就从 0 开始；`fault_counters.json` 是
    **只写不读**的快照（给人和脚本看的），进程启动时**不会**恢复它。
    而 `agt_faults_total` 来自 `fault_event` 表，是**建表以来**的累计。

    两个都留着是因为它们各有不可替代的用途：
    - 想算趋势/速率 → 用 `agt_faults_total`（跨重启连续，不能被重启锯断）；
    - 想知道"**这次上线以来**发生过哪些降级"、或埋点库被关掉（`telemetry_enabled=false`）
      时仍有数字可看 → 用本序列。
    """
    from app.services.telemetry import telemetry

    b.family(f"{PREFIX}_degrade_events_since_start_total", "counter",
             "降级计数器累计值（仅本进程，重启归零）。看跨重启趋势请用 agt_faults_total。")
    for path, n in sorted(telemetry.counters_snapshot().items()):
        b.sample(f"{PREFIX}_degrade_events_since_start_total", n, (("path", path),))


def _collect_durations(b: _Builder, conn: sqlite3.Connection, window_hours: int) -> None:
    cutoff = _window_cutoff(window_hours)
    rows = _query(
        conn,
        "SELECT COALESCE(mode,''), duration_ms FROM agent_chat_log"
        " WHERE created_at >= ? AND duration_ms IS NOT NULL AND duration_ms >= 0"
        " ORDER BY 1", (cutoff,))

    by_mode: dict[str, list[float]] = {}
    for mode, ms in rows:
        by_mode.setdefault(mode, []).append(float(ms))

    b.family(f"{PREFIX}_chat_duration_seconds", "gauge",
             f"对话耗时分位（秒），最近 {window_hours}h 窗口、最近秩法、按链路分。"
             "gauge 而非 summary：窗口滑动时它会下降，标成 counter 会被 Prometheus 误判为计数器重置。")
    b.family(f"{PREFIX}_chat_duration_avg_seconds", "gauge",
             f"对话平均耗时（秒），最近 {window_hours}h 窗口、按链路分。")
    b.family(f"{PREFIX}_chat_window_requests", "gauge",
             f"最近 {window_hours}h 窗口内的对话数——用它判断分位的样本量是否够看。")

    for mode, values in sorted(by_mode.items()):
        if not values:
            continue
        values.sort()
        for q in _QUANTILES:
            b.sample(f"{PREFIX}_chat_duration_seconds", _ms_to_sec(_quantile(values, q)),
                     (("mode", mode), ("quantile", _num(q))))
        b.sample(f"{PREFIX}_chat_duration_avg_seconds", _ms_to_sec(sum(values) / len(values)),
                 (("mode", mode),))
        b.sample(f"{PREFIX}_chat_window_requests", len(values), (("mode", mode),))


def _collect_scheduler(b: _Builder) -> None:
    from app.services.scheduler import scheduler

    tasks = scheduler.status()
    b.family(f"{PREFIX}_scheduler_task_enabled", "gauge", "常驻任务是否启用（1/0）。")
    b.family(f"{PREFIX}_scheduler_task_runs_total", "counter",
             "常驻任务累计执行次数（进程内计数，重启归零）。")
    b.family(f"{PREFIX}_scheduler_task_last_error", "gauge",
             "常驻任务最近一次是否报错（1=有 lastError）。这是「任务静默失效」的直接信号。")
    b.family(f"{PREFIX}_scheduler_task_last_run_timestamp_seconds", "gauge",
             "常驻任务最近一次开始时间（Unix 秒）。长期不更新 = 调度循环没在跑。")
    b.family(f"{PREFIX}_scheduler_task_interval_seconds", "gauge", "常驻任务配置的执行间隔（秒）。")
    b.family(f"{PREFIX}_scheduler_task_last_duration_seconds", "gauge",
             "常驻任务最近一次执行耗时（秒）。")
    for t in tasks:
        name = t.get("name", "")
        labels = (("task", name),)
        b.sample(f"{PREFIX}_scheduler_task_enabled", 1 if t.get("enabled") else 0, labels)
        b.sample(f"{PREFIX}_scheduler_task_runs_total", t.get("runs", 0), labels)
        b.sample(f"{PREFIX}_scheduler_task_last_error", 1 if t.get("lastError") else 0, labels)
        b.sample(f"{PREFIX}_scheduler_task_interval_seconds", t.get("intervalSec"), labels)
        last_ms = t.get("lastDurationMs")
        if last_ms is not None:
            b.sample(f"{PREFIX}_scheduler_task_last_duration_seconds",
                     float(last_ms) / 1000.0, labels)
        started = t.get("lastStartedAt")
        if started:
            # scheduler 给的是本地时间字符串；解析失败就不输出这条序列（缺序列比输出错时间好）
            try:
                ts = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
                b.sample(f"{PREFIX}_scheduler_task_last_run_timestamp_seconds", ts, labels)
            except (ValueError, OverflowError):
                pass


def _collect_self(b: _Builder, ok_sources: dict[str, bool], started_at: float,
                  window_hours: int) -> None:
    from app.core.build_info import get_build_info

    build = get_build_info()

    b.family(f"{PREFIX}_python_up", "gauge", "智能体（Python）进程存活并成功渲染本响应（1/0）。")
    b.family(f"{PREFIX}_info", "gauge",
             "版本信息（值恒为 1，看 label）。用 group_left 关联到其它序列上。")
    b.family(f"{PREFIX}_metrics_collect_ok", "gauge",
             "各数据源本次采集是否成功（1/0）。为 0 表示该族指标本次缺失，而非值为 0。")
    b.family(f"{PREFIX}_metrics_window_seconds", "gauge", "分位类指标使用的窗口长度（秒）。")
    b.family(f"{PREFIX}_metrics_scrape_duration_seconds", "gauge", "本次渲染耗时（秒）。")

    b.sample(f"{PREFIX}_python_up", 1)
    b.sample(f"{PREFIX}_info", 1, (("service", "agt_python"), ("version", build.version)))
    for source, ok in sorted(ok_sources.items()):
        b.sample(f"{PREFIX}_metrics_collect_ok", 1 if ok else 0, (("source", source),))
    b.sample(f"{PREFIX}_metrics_window_seconds", window_hours * 3600)
    b.sample(f"{PREFIX}_metrics_scrape_duration_seconds", time.perf_counter() - started_at)


# --------------------------------------------------------------------- 渲染入口


def _render(window_hours: int) -> str:
    started_at = time.perf_counter()
    b = _Builder()
    ok_sources: dict[str, bool] = {}

    # 内存态：不会失败到需要降级，但照样纳入 collect_ok 以便"缺了就看得见"
    for source, fn in (
            ("degrade_counters", lambda: _collect_degrade_counters(b)),
            ("scheduler", lambda: _collect_scheduler(b)),
    ):
        try:
            fn()
            ok_sources[source] = True
        except Exception as exc:  # noqa: BLE001 —— 单项失败只记录，绝不外抛
            ok_sources[source] = False
            logger.warning("指标采集失败：%s | %s: %s", source, type(exc).__name__, exc)

    # ODS 各族：一次连接读全部；库不存在直接标 false（**不创建库**）
    db_path = _metrics_db_path()
    ok_sources["ods"] = False
    if db_path.exists():
        try:
            conn = _open_readonly(db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("指标采集：只读打开埋点库失败 | %s: %s", type(exc).__name__, exc)
        else:
            try:
                for source, fn in (
                        ("ods_chat", lambda: _collect_chat(b, conn)),
                        ("ods_tokens", lambda: _collect_tokens(b, conn)),
                        ("ods_rag", lambda: _collect_rag(b, conn)),
                        ("ods_tools", lambda: _collect_tools(b, conn)),
                        ("ods_delegations", lambda: _collect_delegations(b, conn)),
                        ("ods_faults", lambda: _collect_faults(b, conn)),
                        ("ods_durations", lambda: _collect_durations(b, conn, window_hours)),
                ):
                    try:
                        fn()
                        ok_sources[source] = True
                    except Exception as exc:  # noqa: BLE001
                        ok_sources[source] = False
                        logger.warning("指标采集失败：%s | %s: %s", source, type(exc).__name__, exc)
                # 汇总位：**只看 ods_* 子项**（不能把 "ods" 自己算进去，否则恒 false）
                ok_sources["ods"] = all(
                    v for k, v in ok_sources.items() if k.startswith("ods_"))
            finally:
                conn.close()
    else:
        logger.warning("指标采集：埋点库不存在，ODS 相关指标本次缺失 | %s", db_path)

    _collect_self(b, ok_sources, started_at, window_hours)
    return b.render()


def render_metrics(*, window_hours: int = DEFAULT_WINDOW_HOURS, use_cache: bool = True) -> str:
    """渲染 Prometheus 文本（响应体）。

    `use_cache=False` 供验收/测试使用——**必须能拿到真实时间戳**，否则"抓了没变化"
    分不清是数据没变还是缓存没失效。
    """
    window_hours = max(1, min(int(window_hours), 24 * 30))
    key = f"w={window_hours}"
    now = time.time()
    if use_cache and _cache["text"] and _cache["key"] == key and (now - _cache["at"]) < CACHE_TTL_SEC:
        return _cache["text"]

    text = _render(window_hours)
    _cache.update({"at": time.time(), "key": key, "text": text})
    return text


def clear_cache() -> None:
    """清缓存（单测用）。"""
    _cache.update({"at": 0.0, "key": "", "text": ""})


#: Prometheus 文本格式的 Content-Type（**版本号不能省**：抓取器按它选解析器）
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: 供测试与文档引用的必需指标族清单（缺一个就该红——这是 E-9 与验收脚本的共同口径）
REQUIRED_FAMILIES: tuple[str, ...] = (
    f"{PREFIX}_chat_requests_total",
    f"{PREFIX}_chat_failed_total",
    f"{PREFIX}_chat_degraded_total",
    f"{PREFIX}_llm_tokens_total",
    f"{PREFIX}_rag_decisions_total",
    f"{PREFIX}_tool_calls_total",
    f"{PREFIX}_tool_calls_failed_total",
    f"{PREFIX}_tool_calls_blocked_total",
    f"{PREFIX}_delegations_total",
    f"{PREFIX}_faults_total",
    f"{PREFIX}_degrade_events_since_start_total",
    f"{PREFIX}_chat_duration_seconds",
    f"{PREFIX}_chat_duration_avg_seconds",
    f"{PREFIX}_chat_window_requests",
    f"{PREFIX}_scheduler_task_enabled",
    f"{PREFIX}_scheduler_task_runs_total",
    f"{PREFIX}_scheduler_task_last_error",
    f"{PREFIX}_scheduler_task_last_run_timestamp_seconds",
    f"{PREFIX}_python_up",
    f"{PREFIX}_info",
    f"{PREFIX}_metrics_collect_ok",
)

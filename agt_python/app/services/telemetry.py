"""数据中台埋点 + 降级计数（docs/specs/全链路容错规约.md §8、业务操作全能力规约 §10）。

两件事，一个模块：

1. **降级计数**（容错规约 §8 铁律 1：降级必须计数）
   - 进程内累计 + 定期落盘 `data/metrics/fault_counters.json`；
   - path 维度固定为规约列举的降级/熔断路径（PATH_* 常量）；
   - 每次计数同时 WARN 留痕（铁律 2）并向 fault_event 表投递一条明细（铁律 3 可查）。

2. **全链路埋点**（业务规约 §10 采集点①②③）
   - agent_chat_log（请求级）/ agent_tool_call（工具级）/ agent_delegation（委派）/
     fault_event（降级明细）四张 SQLite 表；
   - 内存队列 + 后台定时批量 INSERT（对齐语料治理的批量习惯）；
   - 任何埋点失败只打日志，**绝不阻塞主链路**（规约 §10 写入方式）；
   - 参数/内容只存摘要（≤200 字符），禁止存全量业务数据。

存储选型说明：业务规约 §10 建议 MySQL，但 Python 侧无 MySQL 依赖且 Java 不
应成为埋点写入的单点，故先落 SQLite（data/metrics/observability.db，与语料
治理账本同风格）；查询接口 /v1/agent/metrics/overview 由 Java 透传给前端。
迁 MySQL 时机：看板需要跨库联查业务数据时（已备注进容错规约 §13）。

trace_id 贯穿：ContextVar 存当前请求 trace_id，agent_service.chat/chat_stream
入口 set，深层的 subagent / telemetry 无需层层传参。
"""

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import trace as trace_ctx
from app.core.config import settings
from app.core.pii import redact_pii
from app.services import costing
from app.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# ---- 降级/熔断路径（容错规约 §8 维度表，P2 补 llm_failover）----
PATH_RAG_DEGRADED = "rag_degraded"
PATH_RAG_REFUSED = "rag_refused"
PATH_PLAN_DEGRADED = "plan_degraded"
PATH_PLAN_REPLAN = "plan_replan"
PATH_FP_WARN = "loop_fingerprint_warn"
PATH_FP_BREAK = "loop_fingerprint_break"
PATH_TOOL_FAIL_BREAK = "tool_fail_break"
PATH_SUBAGENT_TIMEOUT = "subagent_timeout"
PATH_SUBAGENT_BUDGET = "subagent_budget_exhausted"
PATH_LLM_FALLBACK = "llm_fallback"
PATH_LLM_FAILOVER = "llm_failover"          # P2：主模型故障切备
PATH_TOOL_TIMEOUT = "tool_timeout"          # 工具 wait_for 超时（计失败前的独立观察点）
PATH_REPLY_CLEANED = "reply_cleaned"        # P2+：正文开头漏出英文思考句，已确定性剥离
PATH_RAG_REWRITE_FAILED = "rag_rewrite_failed"  # P0：查询改写失败，已回退原文检索（增强层降级）
PATH_MEMORY_SQLITE_DEGRADED = "memory_sqlite_degraded"  # M-1：记忆库不可用，已退回内存（会话记忆规约）
PATH_MEMORY_FILTER_DEGRADED = "memory_filter_degraded"  # M-3：过滤层 embedding 失败，本轮返回全量历史（会话记忆规约）
# M-4（会话记忆规约 §6 G14）：后两个是**设计内行为**的路径事件——计数照记、
# 链路弹窗记忆段据此如实展示，但状态算 ok 不算 warn（裁剪/过滤不是降级）。
PATH_MEMORY_TRIMMED = "memory_trimmed"                  # M-4：历史超 token 预算被裁剪（M-2 双锚点），已压缩为摘要
PATH_MEMORY_FILTERED = "memory_filtered"                # M-4：相关性过滤淘汰了低相关轮次（M-3）
PATH_MEMORY_SUMMARY_FAILED = "memory_summary_failed"    # M-4：裁剪后摘要生成失败，被裁内容没有摘要补偿
# S-1 服务间信任边界（企业级落地规约）：Java→Python 的 X-Agent-Key 校验失败。
# 它**不是降级**而是安全事件（请求被拒绝、没有"退而求其次"的路径），
# 因此刻意不进 `_PATH_TO_STAGE` —— 它发生在请求入口，不属于任何会话阶段，
# 硬塞进链路视图只会让"执行流程"多出一个与问题无关的节点；
# 但照样计数 + 落 fault_event，看板的降级计数与故障明细里能查到。
PATH_AUTH_KEY_REJECTED = "auth_key_rejected"
# E-10 密钥轮换过渡窗口：请求用的是**上一把**密钥（`AGENT_SHARED_KEY_PREV`）而被放行。
# 它**不是故障也不是降级**——请求正常处理完了，只是"还有人拿旧钥匙"。
# 之所以要计数：它是**判断"能不能把 PREV 清掉"的唯一依据**。
# 不清 PREV 就一直有一把已停用的钥匙能开门（安全债）；凭感觉清掉又会打断
# 某个被遗忘的调用方（多半是某个运维脚本）。看到本计数**连续一段时间为 0**
# 才是清 PREV 的放行条件。
# 注意：走 `count_event` 只计数**不落 fault_event**——落进去会让看板的故障数
# 凭空变大，而"旧钥匙还在用"根本不是故障（与 PATH_AUTH_KEY_REJECTED 不同，
# 那个是真拒绝）。
PATH_AUTH_KEY_LEGACY_USED = "auth_key_legacy_used"

# E-13 成本计量与配额（判据 C7）：单用户日 token 用量触顶。
# 与 auth_key 系同理**不进 `_PATH_TO_STAGE`**：它发生在回答已完成之后，
# 不属于会话链路的任何一段，硬塞进"执行流程"只会多出一个莫名的节点。
# 但它是规约点名的"超限须留痕"事件——计数 + 落 fault_event（每用户每日
# 至多一条，防超限后的每轮对话把这张表刷满），且**不拒绝服务**（降级为提示）。
PATH_QUOTA_EXCEEDED = "quota_exceeded"

# ---- 链路阶段状态（v5：会话详情弹窗「执行流程」视图）----
#
# 三色语义（用户 2026-09-19 定的口径，第四色为如实拒答）：
#   ok    绿：正常执行
#   warn  黄：重试 / 降级 / 兜底——仍在为用户服务，只是路径不理想
#   gray  灰：如实拒答（insufficient_evidence / cross_domain）——设计内行为，不是故障
#   error 红：熔断 / 失败——**真故障**，用户拿不到答案
#
# ⚠️ 为什么拒绝把 rag_refused 一律算红：实测库里 78 条拒答（远超真故障数），
# 一律标红会让看板"满屏红"，把设计内的如实拒答误读成系统不可用。
# 灰/红拆开的依据是**处置动作不同**：灰=补语料，红=查系统。
_STAGE_STATUS_ORDER = {"ok": 0, "gray": 1, "warn": 2, "error": 3}

# fault_event.path → 该故障打在链路哪一段、算什么状态
# 键是 PATH_* 常量值；值为 (阶段 key, 状态)
_PATH_TO_STAGE = {
    PATH_RAG_DEGRADED: ("recall", "warn"),        # 检索挂了 → 召回段降级
    PATH_RAG_REFUSED: ("evaluate", "gray"),       # 如实拒答 → 评估段判未通过（设计内）
    PATH_RAG_REWRITE_FAILED: ("rewrite", "warn"),  # 改写失败回退原文 → 改写段降级但可用
    PATH_PLAN_DEGRADED: ("route", "warn"),        # 复杂度路由失败 → 退回 REACT
    PATH_PLAN_REPLAN: ("plan", "warn"),           # 规划重试
    PATH_FP_WARN: ("tools", "warn"),              # 指纹预警
    PATH_FP_BREAK: ("tools", "error"),            # 指纹熔断 → 真故障
    PATH_TOOL_FAIL_BREAK: ("tools", "error"),     # 工具连续失败熔断 → 真故障
    PATH_SUBAGENT_TIMEOUT: ("delegate", "warn"),
    PATH_SUBAGENT_BUDGET: ("delegate", "warn"),
    PATH_LLM_FALLBACK: ("answer", "error"),       # 兜底话术 = 没答上
    PATH_LLM_FAILOVER: ("answer", "warn"),        # 切备模型成功 = 降级但可用
    PATH_TOOL_TIMEOUT: ("tools", "warn"),
    PATH_REPLY_CLEANED: ("answer", "warn"),       # 剥离思考句：内容可用，路径不理想
    PATH_MEMORY_SQLITE_DEGRADED: ("memory", "warn"),  # M-1：记忆退回内存，会话不可恢复但本轮可用
    PATH_MEMORY_FILTER_DEGRADED: ("memory", "warn"),  # M-3：过滤降级返回全量，上下文未精选但可用
    # M-4：裁剪/过滤是设计内的预算与精选机制（green + 文案区分），不是降级——
    # 打黄会让长会话满屏黄，把正常机制误读成路径不理想；摘要失败才是真受损。
    PATH_MEMORY_TRIMMED: ("memory", "ok"),
    PATH_MEMORY_FILTERED: ("memory", "ok"),
    PATH_MEMORY_SUMMARY_FAILED: ("memory", "warn"),
}

# 摘要上限（规约 §10：参数摘要 ≤200 字符，禁止存全量）
_DIGEST_CHARS = 200

# traceId 的唯一持有者已迁到 app/core/trace.py（E-6：日志与埋点必须取**同一个** id，
# 否则"按 traceId 查日志"与"按 traceId 查埋点"会给出两套结果）。
# 这里保留 set/current 两个函数名，`agent_service` 与既有测试无需改动。
def set_trace_id(trace_id: str) -> None:
    trace_ctx.set_trace_id(trace_id)


def current_trace_id() -> str:
    """当前 traceId；上下文里没有就新造一个（埋点表 trace_id 是 NOT NULL 列）。"""
    return trace_ctx.get_trace_id() or trace_ctx.new_trace_id()


# M-4（会话记忆规约 §6）：本轮记忆装配数字「载入/注入/压缩」。
# 为什么用 ContextVar 而不是显式传参：装配发生在链路深处（_generate /
# _handle_business / 流式生成器里的 _build_history），而落库点统一在
# _record_metrics——显式穿层要动 5 个函数签名；ContextVar 与上面的 trace_id
# 同一套模式，每个请求独立任务上下文，互不串扰。
_memory_stats_cv: ContextVar[dict | None] = ContextVar("telemetry_memory_stats", default=None)


def note_memory_stats(*, total: int, used: int, trimmed: int) -> None:
    """登记本轮记忆装配数字，供 record_chat 落 agent_chat_log 三列（M-4）。

    单位统一为**消息条数**（raw / 过滤 / 裁剪三层都以条计），保证
    total = used + 过滤淘汰 + trimmed 的算术闭环（过滤数由 _pipeline 推导）。
    """
    _memory_stats_cv.set(
        {"total": int(total), "used": int(used), "trimmed": int(trimmed)})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _digest(value: Any, limit: int = _DIGEST_CHARS) -> str:
    """任意值 → 截断摘要文本（None/空 → 空串）。

    S-8：**先脱敏再截断**（不是先截断再脱敏）——截断可能把一个手机号/邮箱拦腰截断，
    剩下的半截既脱不掉、也不完整；先过 `redact_pii` 再取长度才保证入库摘要里无 PII。
    埋点四表所有文本（args_digest / error / detail）都经由此函数落库。
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return redact_pii(text)[:limit]


class TelemetryStore:
    """埋点存储：内存队列 + 后台批量写 SQLite + 计数器 JSON 落盘。"""

    def __init__(self) -> None:
        self._enabled = settings.telemetry_enabled
        self._counters_enabled = settings.fault_counters_enabled
        self._counters: dict[str, int] = {}
        self._counters_dirty = False
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self._recompute_task: asyncio.Task | None = None
        # 内存队列（list + 后台整体搬运；单进程 uvicorn 下足够）
        self._q_chat: list[tuple] = []
        self._q_tool: list[tuple] = []
        self._q_delegation: list[tuple] = []
        self._q_fault: list[tuple] = []
        # E-12 前端运行时报错（observability.db 第六张事件表）
        self._q_client_error: list[tuple] = []

    # ---- 生命周期（main.lifespan 调用）----

    def _db_path(self) -> Path:
        raw = settings.telemetry_db_path
        return Path(raw) if raw else DATA_DIR / "metrics" / "observability.db"

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            path = self._db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_chat_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT, session_id TEXT, user_id INTEGER, permission TEXT,
                    intent TEXT, intent_source TEXT, mode TEXT, model TEXT,
                    prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
                    duration_ms REAL, ok INTEGER DEFAULT 1, degraded INTEGER DEFAULT 0,
                    content_chars INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    created_at TEXT);
                CREATE TABLE IF NOT EXISTS agent_tool_call(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT, session_id TEXT, step INTEGER, tool TEXT,
                    ok INTEGER DEFAULT 1, blocked TEXT DEFAULT '', error TEXT DEFAULT '',
                    duration_ms REAL, args_digest TEXT DEFAULT '', created_at TEXT);
                CREATE TABLE IF NOT EXISTS agent_delegation(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT, session_id TEXT, agent TEXT, status TEXT,
                    duration_ms REAL, created_at TEXT);
                CREATE TABLE IF NOT EXISTS fault_event(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT, session_id TEXT, detail TEXT, created_at TEXT);
            """)
            # 演进迁移（数据库规约 §2.6：只加列；fault_event 补 trace_id 支撑 P0 trace 追溯）
            # 必须在 trace 类索引之前执行，否则存量旧库上建索引因列缺失而失败
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(fault_event)").fetchall()}
            if "trace_id" not in cols:
                self._conn.execute("ALTER TABLE fault_event ADD COLUMN trace_id TEXT DEFAULT ''")
            # v4 迁移（数据中台规约 §5）：RAG 链路判定落库，支撑「未命中分析」知识缺口视图。
            # 只加列不建表——判定维度本就属于「一次对话」的属性，独立成表会让会话轨迹被拆两处
            # （规约 §2.2 单一事实来源）。加列后未命中分析 = 对 agent_chat_log 的一个 GROUP BY，
            # 无需新增采集点。rag_docs 记命中文档 docId（逗号分隔，供 B 类「无人访问」巡检铺路）。
            chat_cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(agent_chat_log)").fetchall()}
            if "rag_decision" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN rag_decision TEXT DEFAULT ''")
            if "rag_docs" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN rag_docs TEXT DEFAULT ''")
            # v5 迁移（DEV_ISSUES 6.37）：查询改写来源落库，支撑「执行流程」弹窗
            # 的改写段着色（dict / dict_suffix / llm / llm+dict / none / failed）。
            if "rag_rewrite" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN rag_rewrite TEXT DEFAULT ''")
            # v6 迁移（会话记忆规约 §6 M-4）：记忆装配数字落库——「执行流程」弹窗
            # 记忆段的数据源 + M-3 阈值调参的分布积累（过滤/裁剪的真实幅度与频次）。
            # 列名沿用规约术语 turns，单位统一为**消息条数**（见 note_memory_stats）。
            if "memory_turns_total" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN memory_turns_total INTEGER DEFAULT 0")
            if "memory_turns_used" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN memory_turns_used INTEGER DEFAULT 0")
            if "memory_trimmed" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN memory_trimmed INTEGER DEFAULT 0")
            # v8 迁移（企业级落地规约 E-13，判据 C7）：本轮 token 数的来源标注。
            # E-13 之前流式链路 usage 恒为 0（上游没带 include_usage，也没人记）——
            # E-13 起流式能拿到真实 usage，拿不到（上游不支持）就本地估算。
            # 这一列就是"这个数是量的还是估的"的如实标注：看板据此区分"计量"与"估计"，
            # 手工核算时才知道哪些行可以拿去对账、哪些行只能看趋势。空串 = E-13 前的旧行。
            if "usage_source" not in chat_cols:
                self._conn.execute(
                    "ALTER TABLE agent_chat_log ADD COLUMN usage_source TEXT DEFAULT ''")
            self._conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_chat_created ON agent_chat_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_fault_created ON fault_event(created_at);
                CREATE INDEX IF NOT EXISTS idx_tool_created ON agent_tool_call(created_at);
                CREATE INDEX IF NOT EXISTS idx_delegation_created ON agent_delegation(created_at);
                CREATE INDEX IF NOT EXISTS idx_chat_trace ON agent_chat_log(trace_id);
                CREATE INDEX IF NOT EXISTS idx_tool_trace ON agent_tool_call(trace_id);
                CREATE INDEX IF NOT EXISTS idx_delegation_trace ON agent_delegation(trace_id);
                CREATE INDEX IF NOT EXISTS idx_fault_trace ON fault_event(trace_id);
                -- v4：知识缺口分析必须能直接筛 RAG 类会话，否则每次全表扫
                CREATE INDEX IF NOT EXISTS idx_chat_rag ON agent_chat_log(rag_decision);
                -- DWS 预聚合（数据库规约 §4；dim_type 为本项目对规约的登记扩展：标注 dim 的维度域，
                -- 使 timeseries 的 groupBy 可区分 intent/model 等同名值）
                CREATE TABLE IF NOT EXISTS metrics_daily(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    dim TEXT NOT NULL DEFAULT '',
                    dim_type TEXT NOT NULL DEFAULT '',
                    value REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    UNIQUE(day, metric, dim));
                CREATE INDEX IF NOT EXISTS idx_metrics_daily_day ON metrics_daily(day, metric);
                CREATE TABLE IF NOT EXISTS schema_version(
                    version INTEGER NOT NULL,
                    applied_at TEXT NOT NULL);
                -- P2 反馈闭环（数据中台规约 §6/§8，开放问题 2 拍板=二值评分）：
                -- agent_feedback=事件表（第五张，只存元数据）；
                -- feedback_content=独立内容表（规约 §4.2 口子：显式开关 + TTL，绝不并入四表）
                CREATE TABLE IF NOT EXISTS agent_feedback(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL, session_id TEXT DEFAULT '',
                    user_id INTEGER DEFAULT 0, rating TEXT NOT NULL,
                    comment TEXT DEFAULT '', created_at TEXT);
                CREATE TABLE IF NOT EXISTS feedback_content(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL, question TEXT DEFAULT '',
                    answer TEXT DEFAULT '', created_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_feedback_trace ON agent_feedback(trace_id);
                CREATE INDEX IF NOT EXISTS idx_feedback_created ON agent_feedback(created_at);
                -- E-12 前端运行时报错（第六张事件表）：
                -- **按「同错 + 同时间桶」合并成一行**，用 count 累加而不是每次插一行。
                -- 理由：前端一个渲染循环里的报错能在几秒内刷出上千条，逐条落库会让这张表
                -- 一天内失控，也让看板退化成"滚动字幕"（谁都读不出趋势）。
                -- UNIQUE(fingerprint, bucket) 是合并的实现手段：批量 INSERT 时用
                -- `ON CONFLICT ... DO UPDATE SET count = count + 1` 就地累加（见 flush_now）。
                -- ✂️ message / stack 都会**截断后**入库（长度在 config 里），因为完整栈常有数 KB。
                CREATE TABLE IF NOT EXISTS client_error(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    page TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    message TEXT DEFAULT '',
                    stack TEXT DEFAULT '',
                    app_version TEXT DEFAULT '',
                    user_agent TEXT DEFAULT '',
                    user_id INTEGER DEFAULT 0,
                    trace_id TEXT DEFAULT '',
                    count INTEGER NOT NULL DEFAULT 1,
                    first_at TEXT,
                    last_at TEXT,
                    UNIQUE(fingerprint, bucket));
                CREATE INDEX IF NOT EXISTS idx_client_error_last ON client_error(last_at);
                CREATE INDEX IF NOT EXISTS idx_client_error_page ON client_error(page);
            """)
            # schema_version 登记（数据库规约 §2.6）：v1=P0 索引与 fault.trace_id；v2=DWS 预聚合；
            # v3=P2 反馈两表；v4=RAG 判定落库（agent_chat_log.rag_decision / rag_docs）；
            # v5=查询改写来源（rag_rewrite，DEV_ISSUES 6.37 当时漏登记版本行，此处补齐）；
            # v6=记忆装配数字三列（会话记忆规约 §6 M-4）；
            # v7=前端运行时报错表 client_error（企业级落地规约 E-12，判据 C3）；
            # v8=token 来源标注 usage_source（企业级落地规约 E-13，判据 C7）
            #
            # ⚠️ E-13 真机验收抓到的存量 bug：这张表**没有 UNIQUE 约束**，
            # `INSERT OR IGNORE` 从来不 IGNORE——每次服务启动都把 v1..v7 再插一遍，
            # 存量库已堆积数百行重复（验收实测：v1 重复 54 行、v2 重复 95 行…）。
            # 修复 = ① 先清历史重复（保留最早 applied_at，即首次打补丁的时间）；
            #       ② 逐版本判存后再插（幂等）。不改表结构——加 UNIQUE 需重建表，
            #       而"插入幂等"已把问题根除，重建表的收益不值这个风险。
            self._conn.execute(
                "DELETE FROM schema_version WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM schema_version GROUP BY version)")
            for _v in range(1, 9):
                self._conn.execute(
                    "INSERT INTO schema_version(version, applied_at) "
                    "SELECT ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM schema_version WHERE version = ?)", (_v, _now(), _v))
            self._conn.commit()
        return self._conn

    async def start(self) -> None:
        if not self._enabled:
            logger.info("数据中台埋点未启用（telemetry_enabled=false）")
            return
        self._ensure_conn()
        self._task = asyncio.create_task(self._flush_loop())
        self._recompute_task = asyncio.create_task(self._recompute_loop())
        logger.info("数据中台埋点已启动 db=%s", self._db_path())

    async def stop(self) -> None:
        for attr in ("_task", "_recompute_task"):
            t = getattr(self, attr, None)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        self.flush_now()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _flush_loop(self) -> None:
        interval = max(1.0, float(settings.telemetry_flush_interval))
        while True:
            await asyncio.sleep(interval)
            self.flush_now()

    async def _recompute_loop(self) -> None:
        """DWS 定时重算（数据中台规约 §8 P1 / 数据库规约 §4）：近 2 天幂等重算。"""
        interval = max(60, int(settings.metrics_recompute_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                n = self.recompute_recent(days=2)
                if n:
                    logger.debug("DWS 重算完成: %s 行", n)
            except Exception as exc:  # 聚合失败只记日志，绝不影响业务
                logger.error("DWS 定时重算失败: %s", exc)
            # 反馈域 TTL 清理搭车执行（规约 §4.2：内容类与事件类都必须带定时清理）
            self._cleanup_expired()

    # ---- 采集入口（全部非阻塞、绝不抛异常）----

    def record_chat(
        self, *, session_id: str, user_id: int = 0, permission: str = "",
        intent: str = "", intent_source: str = "", mode: str = "", model: str = "",
        prompt_tokens: int = 0, completion_tokens: int = 0, duration_ms: float = 0,
        ok: bool = True, degraded: bool = False, content_chars: int = 0, error: str = "",
        rag_decision: str = "", rag_docs: Any = None, rag_rewrite: str = "",
        usage_source: str = "",
    ) -> None:
        """采集点②：请求级记录（业务规约 §10）。

        rag_decision/rag_docs 为 v4 新增（数据中台规约 §5 知识缺口）：
        - rag_decision 四值 kept / cross_domain / no_hit / insufficient_evidence，
          非知识问答链路（工具类/闲聊）留空串，**不能**当作缺口统计；
        - rag_docs 只记命中文档的 docId 列表，不记正文（埋点铁律：内容不落库）。

        rag_rewrite 为 v5 新增（DEV_ISSUES 6.37）：记录本轮查询改写的来源
        （dict / dict_suffix / llm / llm+dict / none / failed），供会话详情弹窗
        「执行流程」的改写段如实着色，**不记改写后文本**（同埋点铁律）。

        memory_* 三列为 v6 新增（会话记忆规约 §6 M-4）：本轮记忆装配数字
        「载入/注入/压缩」，来自 _build_history 经 note_memory_stats 登记——
        rag 链路直读全量历史（规约 §8 开放问题 4），不登记则落 0。

        usage_source 为 v8 新增（规约 E-13，判据 C7）：本轮 token 数的来源标注——
        'api' = 服务端返回的真实用量（可拿去对账）；'estimate' = 上游没给、本地估算
        （只可看趋势）；空串 = E-13 之前的旧行。**成本核算口径必须区分这两者**，
        把估算值当真值去对账必然对不上。
        """
        if not self._enabled:
            return
        m = _memory_stats_cv.get() or {}
        self._q_chat.append((
            current_trace_id(), session_id, user_id, permission[:32],
            intent, intent_source, mode, model[:64],
            prompt_tokens, completion_tokens, round(duration_ms, 1),
            1 if ok else 0, 1 if degraded else 0, content_chars, _digest(error),
            _now(),
            rag_decision[:32],
            self._join_docs(rag_docs),
            rag_rewrite[:32],
            int(m.get("total") or 0), int(m.get("used") or 0),
            int(m.get("trimmed") or 0),
            usage_source[:16],
        ))
        # 消费即清：一次请求只落一行，防同一任务内的陈旧值串到后续记录
        _memory_stats_cv.set(None)

    @staticmethod
    def _join_docs(docs: Any) -> str:
        """命中文档 docId 列表 → 逗号分隔串（去重、保序、限长）。

        限长理由：rag_docs 是明细字段，一条会话最多记前 20 篇——足够支撑「无人访问」
        巡检，又不会让单行膨胀；截断只影响极端长尾，不影响聚合口径。
        """
        if not docs:
            return ""
        try:
            if isinstance(docs, str):
                items = [docs]
            else:
                items = []
                for d in docs:
                    if isinstance(d, dict):
                        items.append(str(d.get("docId") or d.get("id") or d.get("doc_id") or ""))
                    else:
                        items.append(str(d))
            seen: list[str] = []
            for it in items:
                it = it.strip()[:64]
                if it and it not in seen:
                    seen.append(it)
            return ",".join(seen[:20])[:1000]
        except Exception:  # 埋点不许抛
            return ""

    def record_tool_call(
        self, *, session_id: str, step: int, tool: str, ok: bool,
        blocked: str = "", error: str = "", duration_ms: float = 0, arguments: Any = None,
    ) -> None:
        """采集点①：工具级记录（来自 tool_trace，参数只存摘要）。"""
        if not self._enabled:
            return
        self._q_tool.append((
            current_trace_id(), session_id, step, tool[:64],
            1 if ok else 0, blocked[:32], _digest(error),
            round(duration_ms, 1) if duration_ms is not None else None,
            _digest(arguments), _now(),
        ))

    def record_delegation(
        self, *, session_id: str, agent: str, status: str, duration_ms: float = 0,
    ) -> None:
        """采集点③：子智能体委派记录。"""
        if not self._enabled:
            return
        self._q_delegation.append((
            current_trace_id(), session_id, agent[:64], status[:16],
            round(duration_ms, 1), _now(),
        ))

    def record_fault(self, path: str, session_id: str = "", detail: str = "",
                     level: int = logging.WARNING) -> None:
        """降级/路径事件计数（铁律 1/2/3）：计数 + 留痕 + fault_event 明细。

        path 必须是 PATH_* 常量之一；detail 建议写成「本应 X，实际 Y（原因 Z）」。
        level（M-4 新增）：降级路径默认 WARN；设计内的路径事件（如 memory_trimmed
        裁剪）传 INFO——计数照记、弹窗照展示，但不把正常机制刷成告警级日志噪声。
        """
        if self._counters_enabled:
            with self._lock:
                self._counters[path] = self._counters.get(path, 0) + 1
                self._counters_dirty = True
            logger.log(level, "路径事件 [%s] session=%s %s", path, session_id, detail)
        if self._enabled:
            # trace_id 取 ContextVar 原值：聊天上下文内可关联回会话轨迹，
            # 上下文外（如启动期）为空串，不凭空造 id
            self._q_fault.append(
                (path, session_id[:64], _digest(detail), _now(), trace_ctx.get_trace_id()))

    def count_event(self, path: str, detail: str = "",
                    level: int = logging.INFO) -> None:
        """**只计数**的路径事件：不进 `fault_event` 表。

        与 `record_fault` 的分工是刻意的：
        - `record_fault` = 出了事（降级/失败）→ 计数 + `fault_event` 明细，看板按它算故障数；
        - `count_event` = 值得知道但**不是故障**（如 E-10 的"轮换期有人还在用旧密钥"）
          → 只计数。这类事件若也落 `fault_event`，会让看板的故障数凭空变大，
          而"旧钥匙还在用"本身完全正常。

        计数同样会出现在 `/v1/ops/metrics` 的
        `agt_degrade_events_since_start_total{path=...}` —— 这正是它存在的意义：
        给"能不能清掉上一把密钥"提供一个**机器可判**的放行条件。
        """
        if not self._counters_enabled:
            return
        with self._lock:
            self._counters[path] = self._counters.get(path, 0) + 1
            self._counters_dirty = True
        logger.log(level, "路径事件 [%s] %s", path, detail)

    # ---- E-13 成本计量与配额（企业级落地规约 E-13，判据 C7「可计量」）----
    #
    # 三个易踩点（都写在这里，调用方别自己另写）：
    # 1. **队列也算数**：`record_chat` 是先进内存队列、后台 5s 才落库的（规约 §10）。
    #    配额检查发生在"本轮刚记完"的下一瞬，只查 DB 会**漏掉刚记的这轮**——
    #    恰好是最关键的那一轮。所以 user_tokens_today 必须 DB + 队列两头加。
    # 2. **本地日口径**：与 DWS（metrics_daily）同一个 `_day_bounds`，别用 UTC 日——
    #    否则看板"今日成本"和配额"今日用量"会在 8 点前后各说各话。
    # 3. **每用户每日至多一条 fault**：超限后每轮对话都超，不去重的话 fault_event
    #    会被同一件事刷满（与 client_error 合并写是同一个道理，只是这里按"用户+日"）。

    def user_tokens_today(self, user_id: int) -> int:
        """某用户**今日**（本地日）token 累计（prompt+completion）。

        DB 已落库的行 + 尚在内存队列里的行（见上方易踩点 1）。未启用埋点返回 0。
        """
        if not self._enabled or not user_id:
            return 0
        day = datetime.now().strftime("%Y-%m-%d")
        lo, hi = self._day_bounds(day)
        total = 0
        try:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT SUM(prompt_tokens + completion_tokens) FROM agent_chat_log "
                "WHERE user_id = ? AND created_at BETWEEN ? AND ?",
                (int(user_id), lo, hi)).fetchone()
            total = int(row[0] or 0)
        except Exception as exc:  # noqa: BLE001 —— 配额检查失败不得影响主链路
            logger.debug("当日 token 查询失败 user=%s: %s", user_id, exc)
        # 队列里的行一定产生于本进程内：created_at 前缀等于今天才计入（跨天边缘情形）。
        # 元组下标：2=user_id, 8=prompt_tokens, 9=completion_tokens, 15=created_at。
        today_prefix = f"{day}T"
        for entry in list(self._q_chat):
            try:
                if (entry[2] == int(user_id)
                        and str(entry[15]).startswith(today_prefix)):
                    total += int(entry[8] or 0) + int(entry[9] or 0)
            except (IndexError, TypeError, ValueError):
                continue
        return total

    def quota_check(self, user_id: int, session_id: str = "") -> dict | None:
        """配额检查：超限返回提示 dict（含用户可见文案），未超限/未启用返回 None。

        调用时机 = **本轮 usage 已记录之后**（used 含本轮）。副作用：当天首次触顶
        落一条 `quota_exceeded` fault + 计数；后续轮次只提示不再落 fault。
        任何异常都吞掉返回 None——配额是运营能力，坏了不能挡用户。
        """
        try:
            cap = costing.quota_cap()
            if not cap or not user_id:  # 0=不限；user_id=0 是匿名（Java 没注入身份），不参与配额
                return None
            used = self.user_tokens_today(user_id)
            if used < cap:
                return None
            day = datetime.now().strftime("%Y-%m-%d")
            lo, hi = self._day_bounds(day)
            conn = self._ensure_conn()
            # detail 以「用户 <id> |」为稳定前缀做去重键（_digest 是截断不是哈希，前缀可查）
            already = conn.execute(
                "SELECT COUNT(*) FROM fault_event WHERE path = ? AND detail LIKE ? "
                "AND created_at BETWEEN ? AND ?",
                (PATH_QUOTA_EXCEEDED, f"用户 {int(user_id)} |%", lo, hi)).fetchone()
            if not already or not already[0]:
                self.record_fault(
                    PATH_QUOTA_EXCEEDED, session_id,
                    detail=f"用户 {int(user_id)} |日 token 用量触顶（used={used} cap={cap}）")
            return costing.quota_notice(used, cap)
        except Exception as exc:  # noqa: BLE001
            logger.debug("配额检查异常（已忽略）: %s", exc)
            return None

    def costs_summary(self, days: int = 7) -> dict[str, Any]:
        """成本聚合（E-13 看板数据源）：窗口内 ODS 明细按 当前价目表 折算。

        口径（与模块 `costing.py` 的三条设计决策一致，改一处必须同步另一处）：
        - **成本 = 查询期折算**（token × 当前单价），不在 DWS 冻结——供应商改价后
          历史口径跟着新价走（看"现在的钱"）；要对某张历史账单，得用当期价目重算；
        - **未知模型的 token 单列**（costCny=0 + unpricedTokens）——缺价 ≠ 免费照标；
        - **部门 = 画像里声明过的 `department` 键**（读取期派生，不回填），
          没声明的归 unknown——它回答"哪些人在用"，不是"哪个组织在用"。
        """
        data: dict[str, Any] = {
            "enabled": self._enabled,
            "days": days,
            "pricesConfigured": bool(costing.price_table()),
            "quota": {"capTokens": costing.quota_cap()},
            "totals": {"chats": 0, "promptTokens": 0, "completionTokens": 0,
                       "tokens": 0, "costCny": 0.0, "unpricedTokens": 0,
                       "estimatedRows": 0},
            "byModel": [], "byUser": [], "byDept": [], "daily": [],
        }
        if not self._enabled or self._conn is None:
            return data
        try:
            days = max(1, min(365, int(days or 7)))
            since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d") + "T00:00:00"
            conn = self._ensure_conn()
            prices = costing.price_table()

            # 按模型：tokens 与成本的一次折算点（byUser / byDept / daily 的成本同源）
            models: dict[str, dict[str, Any]] = {}
            for r in conn.execute(
                "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
                "SUM(CASE WHEN usage_source = 'estimate' THEN 1 ELSE 0 END) "
                "FROM agent_chat_log WHERE created_at >= ? GROUP BY model", (since,)):
                model = r[0] or "-"
                p, c = int(r[2] or 0), int(r[3] or 0)
                models[model] = {"chats": int(r[1] or 0), "promptTokens": p,
                                 "completionTokens": c, "tokens": p + c,
                                 "costCny": round(costing.model_cost(model, p, c, prices), 4),
                                 "estimatedRows": int(r[4] or 0),
                                 "unpricedTokens": (p + c) if model not in prices else 0}
            data["byModel"] = sorted(
                ({"model": m, **v} for m, v in models.items()),
                key=lambda x: -x["tokens"])
            for v in data["byModel"]:
                t = data["totals"]
                t["chats"] += v["chats"]
                t["promptTokens"] += v["promptTokens"]
                t["completionTokens"] += v["completionTokens"]
                t["tokens"] += v["tokens"]
                t["costCny"] = round(t["costCny"] + v["costCny"], 4)
                t["unpricedTokens"] += v["unpricedTokens"]
                t["estimatedRows"] += v["estimatedRows"]

            # 按用户（成本折算走 _user_cost 单点）：部门 = 画像声明的 department（读取期派生）
            profile = self._profile_service()
            dept_of: dict[int, str] = profile.department_map() if profile else {}
            users: list[dict[str, Any]] = []
            for r in conn.execute(
                "SELECT user_id, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) "
                "FROM agent_chat_log WHERE created_at >= ? AND user_id > 0 "
                "GROUP BY user_id ORDER BY SUM(prompt_tokens + completion_tokens) DESC",
                (since,)):
                uid = int(r[0] or 0)
                p, c = int(r[2] or 0), int(r[3] or 0)
                users.append({
                    "userId": uid, "chats": int(r[1] or 0),
                    "promptTokens": p, "completionTokens": c, "tokens": p + c,
                    "costCny": round(self._user_cost(conn, uid, since, prices), 4),
                    "dept": dept_of.get(uid, costing.DEPT_UNKNOWN)})
            data["byUser"] = users

            # 按部门：由 byUser 派生（部门成本 = 组员成本之和，同一价目表、不二次折算）
            depts: dict[str, dict[str, float]] = {}
            for u in users:
                d = depts.setdefault(u["dept"], {"chats": 0, "tokens": 0, "costCny": 0.0})
                d["chats"] += u["chats"]
                d["tokens"] += u["tokens"]
                d["costCny"] = round(d["costCny"] + u["costCny"], 4)
            data["byDept"] = sorted(
                ({"dept": k, **v} for k, v in depts.items()),
                key=lambda x: -x["costCny"])

            # 日趋势：按本地日 × 模型折算（不读 DWS——价格只在查询期应用，见模块决策 1）
            day_rows = conn.execute(
                "SELECT DISTINCT substr(created_at, 1, 10) AS day FROM agent_chat_log "
                "WHERE created_at >= ? ORDER BY day", (since,)).fetchall()
            for (day,) in day_rows:
                p = c = 0
                cost = 0.0
                for r in conn.execute(
                    "SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM agent_chat_log "
                    "WHERE substr(created_at, 1, 10) = ?", (day,)):
                    p, c = int(r[0] or 0), int(r[1] or 0)
                for r in conn.execute(
                    "SELECT model, SUM(prompt_tokens), SUM(completion_tokens) "
                    "FROM agent_chat_log WHERE substr(created_at, 1, 10) = ? GROUP BY model",
                    (day,)):
                    cost += costing.model_cost(r[0], int(r[1] or 0), int(r[2] or 0), prices)
                data["daily"].append({"day": day, "promptTokens": p,
                                      "completionTokens": c, "tokens": p + c,
                                      "costCny": round(cost, 4)})
            # 今日配额事件数（看板「今日 N 次触顶」）
            lo, hi = self._day_bounds(datetime.now().strftime("%Y-%m-%d"))
            row = conn.execute(
                "SELECT COUNT(*) FROM fault_event WHERE path = ? AND created_at BETWEEN ? AND ?",
                (PATH_QUOTA_EXCEEDED, lo, hi)).fetchone()
            data["quota"]["exceededEventsToday"] = int(row[0] or 0)
        except Exception as exc:  # noqa: BLE001 —— 成本查询失败返回空壳，不打挂看板
            logger.warning("成本聚合失败: %s", exc)
        return data

    def _user_cost(self, conn, user_id: int, since: str,
                   prices: dict[str, dict[str, float]]) -> float:
        """单用户成本：该用户各模型 token × 单价（所有成本折算都走 costing.model_cost 单点）。"""
        cost = 0.0
        for r in conn.execute(
            "SELECT model, SUM(prompt_tokens), SUM(completion_tokens) FROM agent_chat_log "
            "WHERE created_at >= ? AND user_id = ? GROUP BY model", (since, user_id)):
            cost += costing.model_cost(r[0], int(r[1] or 0), int(r[2] or 0), prices)
        return cost

    def _profile_service(self):
        """惰性取用户画像服务（函数内导入：telemetry 不应该在模块加载期拉起画像库）。"""
        try:
            from app.services.user_profile import profile_store
            return profile_store
        except Exception:  # noqa: BLE001
            return None

    def record_client_error(
        self, *, fingerprint: str, bucket: str, page: str, source: str, message: str,
        stack: str, app_version: str, user_agent: str, user_id: int = 0,
    ) -> None:
        """采集点④：前端运行时报错（E-12）。

        **入队即计数**（`client_error_ingested`），落库交给 `flush_now` 的批量 UPSERT：
        合并键是 `(fingerprint, bucket)`——同一错误在同一时间桶内**只占一行**、
        `count` 累加。因此本方法的入队量 ≠ 表里的行数，看板读的 `count` 才是真实次数。

        与其余采集点一样：**入队不阻塞、不抛异常**（埋点铁律）。前端上报接口
        绝不能因为"埋点写不进去"而给用户报错——那会把一个观测能力变成新的故障源。

        ⚠️ `trace_id` 用 `trace_ctx.get_trace_id()`（**只读，没有就存空串**），
        而不是 `current_trace_id()`。后者在上下文缺失时会**新造**一个 id
        （因为 `agent_chat_log.trace_id` 是 NOT NULL 列），而本表允许空串。
        在一个"以后要靠 traceId 去日志里找证据"的表里放假 id，
        会让排查者拿着一个查不到任何东西的 id 空转——比留空更坏。
        """
        if not self._enabled:
            return
        self._q_client_error.append((
            fingerprint, bucket, page, source, message, stack, app_version,
            user_agent, int(user_id or 0), trace_ctx.get_trace_id(), 1, _now(), _now(),
        ))

    # ---- 落盘 ----

    def note_memory_stats(self, *, total: int, used: int, trimmed: int) -> None:
        """登记本轮记忆装配数字（M-4）：实例便捷入口，委托到模块级 ContextVar。

        为什么要实例方法：各业务侧（agent_service 等）拿到的 `telemetry` 是
        TelemetryStore 单例，`telemetry.note_memory_stats(...)` 与
        `telemetry.record_fault(...)` 调用形态保持一致，避免「同名两物」踩坑。
        """
        note_memory_stats(total=total, used=used, trimmed=trimmed)

    def flush_now(self) -> None:
        """把队列批量写入 SQLite + 计数器落 JSON。失败降级为日志，不抛。"""
        try:
            if self._enabled and self._conn is not None:
                conn = self._ensure_conn()
                if self._q_chat:
                    conn.executemany(
                        "INSERT INTO agent_chat_log(trace_id,session_id,user_id,permission,"
                        "intent,intent_source,mode,model,prompt_tokens,completion_tokens,"
                        "duration_ms,ok,degraded,content_chars,error,created_at,"
                        "rag_decision,rag_docs,rag_rewrite,"
                        "memory_turns_total,memory_turns_used,memory_trimmed,usage_source) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self._q_chat)
                    self._q_chat = []
                if self._q_tool:
                    conn.executemany(
                        "INSERT INTO agent_tool_call(trace_id,session_id,step,tool,ok,"
                        "blocked,error,duration_ms,args_digest,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", self._q_tool)
                    self._q_tool = []
                if self._q_delegation:
                    conn.executemany(
                        "INSERT INTO agent_delegation(trace_id,session_id,agent,status,"
                        "duration_ms,created_at) VALUES(?,?,?,?,?,?)", self._q_delegation)
                    self._q_delegation = []
                if self._q_fault:
                    conn.executemany(
                        "INSERT INTO fault_event(path,session_id,detail,created_at,trace_id) "
                        "VALUES(?,?,?,?,?)", self._q_fault)
                    self._q_fault = []
                if self._q_client_error:
                    # 合并写：同一 (fingerprint, bucket) 已存在则**就地累加 count**，
                    # 而不是插第二行。用 UPSERT 而不是"先查再写"的理由是原子性——
                    # 先查再写要在两次语句之间持有状态，而批量入队时同键可能一次来好几条。
                    conn.executemany(
                        "INSERT INTO client_error(fingerprint,bucket,page,source,message,stack,"
                        "app_version,user_agent,user_id,trace_id,count,first_at,last_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(fingerprint,bucket) DO UPDATE SET "
                        "count = count + excluded.count, "
                        "last_at = excluded.last_at, "
                        "message = excluded.message",
                        self._q_client_error)
                    self._q_client_error = []
                conn.commit()
        except Exception as exc:  # 埋点永不阻塞主链路（规约 §10）
            logger.error("埋点落库失败（本批丢弃）: %s", exc)
        # 计数器 JSON（容错规约 §8：data/metrics/fault_counters.json）
        if self._counters_enabled:
            try:
                with self._lock:
                    dirty = self._counters_dirty
                    snapshot = dict(self._counters)
                    self._counters_dirty = False
                if dirty or not (DATA_DIR / "metrics" / "fault_counters.json").exists():
                    path = DATA_DIR / "metrics" / "fault_counters.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(
                        json.dumps(
                            {"updatedAt": _now(), "counters": snapshot},
                            ensure_ascii=False, indent=1,
                        ),
                        encoding="utf-8",
                    )
                    tmp.replace(path)
            except Exception as exc:
                logger.error("降级计数落盘失败: %s", exc)

    # ---- 查询（/v1/agent/metrics/overview 数据源）----

    def counters_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def overview(self) -> dict[str, Any]:
        """看板聚合：计数器 + 近 24h 四表聚合。埋点关闭时返回 enabled 标记。"""
        data: dict[str, Any] = {
            "enabled": self._enabled,
            "faultCounters": self.counters_snapshot(),
        }
        if not self._enabled or self._conn is None:
            return data
        try:
            conn = self._conn
            # created_at 是本地时间（isoformat），聚合窗口也用 localtime 基准
            day = "datetime('now','localtime','-1 day')"
            row = conn.execute(
                f"SELECT COUNT(*), SUM(ok=0), AVG(duration_ms), SUM(degraded=1) "
                f"FROM agent_chat_log WHERE created_at >= {day}"
            ).fetchone()
            # p95 近似值（nearest-rank，idx = ceil(0.95n)-1；指标字典注明"近似值"）
            p95: float | None = None
            if row and row[0]:
                durs = sorted(r[0] for r in conn.execute(
                    f"SELECT duration_ms FROM agent_chat_log "
                    f"WHERE created_at >= {day} AND duration_ms IS NOT NULL"))
                if durs:
                    p95 = round(durs[max(0, (95 * len(durs) + 99) // 100 - 1)], 1)
            data["chatLog24h"] = {
                "total": row[0] or 0,
                "failed": row[1] or 0,
                "avgDurationMs": round(row[2], 1) if row[2] is not None else None,
                "degraded": row[3] or 0,
                "p95DurationMs": p95,
            }
            data["chatByIntent"] = [
                {"intent": r[0], "count": r[1]} for r in conn.execute(
                    f"SELECT intent, COUNT(*) FROM agent_chat_log "
                    f"WHERE created_at >= {day} GROUP BY intent ORDER BY 2 DESC"
                ).fetchall()
            ]
            data["toolCalls24h"] = [
                {"tool": r[0], "total": r[1], "failed": r[2], "avgDurationMs": round(r[3], 1)}
                for r in conn.execute(
                    f"SELECT tool, COUNT(*), SUM(ok=0), AVG(duration_ms) "
                    f"FROM agent_tool_call WHERE created_at >= {day} "
                    f"GROUP BY tool ORDER BY 2 DESC LIMIT 15"
                ).fetchall()
            ]
            data["delegations24h"] = [
                {"agent": r[0], "status": r[1], "count": r[2]} for r in conn.execute(
                    f"SELECT agent, status, COUNT(*) FROM agent_delegation "
                    f"WHERE created_at >= {day} GROUP BY agent, status"
                ).fetchall()
            ]
            data["recentFaults"] = [
                {"path": r[0], "sessionId": r[1], "detail": r[2], "createdAt": r[3]}
                for r in conn.execute(
                    "SELECT path, session_id, detail, created_at FROM fault_event "
                    "ORDER BY id DESC LIMIT 20"
                ).fetchall()
            ]
            # P2：近 24h 反馈统计（指标字典：反馈满意率 = up/(up+down)，<80% 观察）
            stats = {r[0]: r[1] for r in conn.execute(
                f"SELECT rating, COUNT(*) FROM agent_feedback "
                f"WHERE created_at >= {day} GROUP BY rating").fetchall()}
            data["feedback24h"] = {"up": stats.get("up", 0), "down": stats.get("down", 0)}
        except Exception as exc:
            logger.error("埋点聚合查询失败: %s", exc)
        return data

    # ---- 查询（P0 看板：会话视图 + trace 追溯，数据中台开发规约 §6/§7）----

    @staticmethod
    def _chat_row(r: tuple) -> dict[str, Any]:
        return {
            "traceId": r[0], "sessionId": r[1], "userId": r[2], "permission": r[3],
            "intent": r[4], "intentSource": r[5], "mode": r[6], "model": r[7],
            "promptTokens": r[8], "completionTokens": r[9], "durationMs": r[10],
            "ok": bool(r[11]), "degraded": bool(r[12]), "contentChars": r[13],
            "error": r[14], "createdAt": r[15],
            "ragDecision": r[16] if len(r) > 16 else "",
            "ragDocs": (r[17].split(",") if len(r) > 17 and r[17] else []),
            "ragRewriteSource": r[18] if len(r) > 18 else "",
            # v6（会话记忆规约 §6 M-4）：记忆装配数字，「执行流程」记忆段的数据源
            "memoryTurnsTotal": r[19] if len(r) > 19 else 0,
            "memoryTurnsUsed": r[20] if len(r) > 20 else 0,
            "memoryTrimmed": r[21] if len(r) > 21 else 0,
        }

    # 会话明细列清单（列表与 trace 追溯共用，避免两处 SELECT 走偏）
    _CHAT_COLS = ("trace_id,session_id,user_id,permission,intent,intent_source,mode,model,"
                  "prompt_tokens,completion_tokens,duration_ms,ok,degraded,content_chars,error,"
                  "created_at,rag_decision,rag_docs,rag_rewrite,"
                  "memory_turns_total,memory_turns_used,memory_trimmed")

    def recent_chats(
        self, *, limit: int = 20, hours: int = 24, intent: str = "", session_id: str = "",
    ) -> dict[str, Any]:
        """会话视图列表：按时间倒序，支持 intent/session 过滤。

        参数边界（规约 §6）：limit ≤ 100，时间窗口 ≤ 90 天；埋点关闭返回 enabled:false。
        """
        data: dict[str, Any] = {"enabled": self._enabled, "chats": []}
        if not self._enabled or self._conn is None:
            return data
        limit = max(1, min(int(limit or 20), 100))
        hours = max(1, min(int(hours or 24), 24 * 90))
        sql = (f"SELECT {self._CHAT_COLS} "
               "FROM agent_chat_log WHERE created_at >= datetime('now','localtime',?)")
        args: list[Any] = [f"-{hours} hours"]
        if intent:
            sql += " AND intent = ?"
            args.append(intent[:32])
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id[:64])
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        try:
            data["chats"] = [self._chat_row(r) for r in self._conn.execute(sql, args).fetchall()]
        except Exception as exc:
            logger.error("会话列表查询失败: %s", exc)
        return data

    # ---- v5：链路阶段还原（会话详情弹窗「执行流程」视图）----
    #
    # 设计原则：**弹窗只呈现真实存在的阶段**。
    # （P0 更新，2026-09-19）此前本注释记录「RAG 链路含 query 改写但代码里不存在，
    # 故不画该节点」。查询改写已实现（app/rag/rewrite.py，DEV_ISSUES 6.37），
    # 该节点现在真实存在，因此纳入阶段序列。
    #
    # 两套链路（与 rag_service / agent_loop 的实际控制流对齐）：
    #   rag      : 意图识别 → 会话记忆(直读全量) → 查询改写 → 召回(BM25+向量双路) → 重排 → 精选 → 评估 → 回答
    #   business : 意图识别 → 会话记忆 → 复杂度路由 → 计划?(PLAN) → 委派? → 工具调用 → 回答
    #   chitchat : 意图识别 → 会话记忆 → 回答
    # M-4：三条链路都在意图识别后插入「会话记忆」节点——记忆装配确实发生在
    # 意图路由之后、生成之前（_handle_business:214 / _generate / 流式分支）。
    # rag 链路按规约 §8 开放问题 4 直读全量历史，不做过滤裁剪 → 渲染为 idle 不画假节点。

    # 阶段定义：(key, 中文名, 一句话说明)  —— 顺序即渲染顺序
    _RAG_STAGES = (
        ("intent", "意图识别", "判定问题属于知识问答链路"),
        ("memory", "会话记忆", "历史装配（本链路直读全量，不过滤不裁剪）"),
        ("rewrite", "查询改写", "口语术语规范化 + 结合上文的指代/省略补全（可跳过）"),
        ("recall", "召回", "BM25 关键词 + 稠密向量双路混合检索（RRF 融合）"),
        ("rerank", "重排", "rerank-pro 对候选全量重排打分"),
        ("select", "精选", "按排名与分数下限保留 3~8 条证据"),
        ("evaluate", "评估", "LLM judge 判断证据是否足以回答"),
        ("answer", "回答", "带证据生成，或如实拒答"),
    )
    _BIZ_STAGES = (
        ("intent", "意图识别", "判定问题属于业务操作链路"),
        ("memory", "会话记忆", "载入历史 → 相关性过滤 → token 预算裁剪（M-2 双锚点）"),
        ("route", "复杂度路由", "REACT / PLAN 执行模式判定"),
        ("plan", "任务规划", "PLAN 模式拆解执行步骤（REACT 跳过）"),
        ("delegate", "专家委派", "规则命中的设计用例钉死专家（可跳过）"),
        ("tools", "工具调用", "以用户身份调用真实业务工具，多步循环"),
        ("answer", "回答", "汇总工具结果生成结论"),
    )
    _CHITCHAT_STAGES = (
        ("intent", "意图识别", "判定为闲聊，不检索不调工具"),
        ("memory", "会话记忆", "载入历史 → 相关性过滤 → token 预算裁剪（M-2 双锚点）"),
        ("answer", "回答", "直接由大模型生成"),
    )

    @staticmethod
    def _stage_shell(key: str, name: str, note: str) -> dict[str, Any]:
        """阶段初始壳：未参与本轮链路的阶段保持 idle，前端据此不渲染连接线。"""
        return {"key": key, "name": name, "note": note, "status": "ok",
                "durationMs": None, "detail": "", "error": ""}

    @classmethod
    def _pipeline(cls, chat: dict[str, Any], tools: list[dict], delegations: list[dict],
                  faults: list[dict]) -> list[dict[str, Any]]:
        """把一次会话的四表数据还原成「链路阶段 + 状态」。

        状态优先级：error > warn > gray > ok（同一阶段多条痕迹时取最严重的）。
        """
        intent = (chat.get("intent") or "chitchat").lower()
        if intent == "rag":
            stages = [cls._stage_shell(*s) for s in cls._RAG_STAGES]
        elif intent == "business":
            stages = [cls._stage_shell(*s) for s in cls._BIZ_STAGES]
        else:
            stages = [cls._stage_shell(*s) for s in cls._CHITCHAT_STAGES]
        by_key = {s["key"]: s for s in stages}

        def hit(key: str, status: str, detail: str = "", error: str = "") -> None:
            """给某阶段打状态（取更严重者），detail 只补空位避免覆盖更精确的信息。"""
            st = by_key.get(key)
            if st is None:
                return
            if _STAGE_STATUS_ORDER.get(status, 0) > _STAGE_STATUS_ORDER.get(st["status"], 0):
                st["status"] = status
            if detail and not st["detail"]:
                st["detail"] = detail
            if error:
                st["error"] = error

        # ---- ① 故障事件 → 阶段状态（最主要的降级/熔断信号来源）----
        for f in faults:
            key, status = _PATH_TO_STAGE.get(f.get("path", ""), ("", ""))
            if not key:
                continue
            hit(key, status, detail=f.get("detail") or f.get("path", ""),
                error="" if status != "error" else (f.get("path") or ""))

        # ---- ② RAG 判定 → 改写/召回/评估/回答 各段 ----
        if intent == "rag":
            decision = (chat.get("ragDecision") or "").strip()
            docs = chat.get("ragDocs") or []
            n_docs = len(docs)
            # 改写段：命中实际生效的改写方式才标绿；none 说明这轮不需要改写
            # （已完整清晰的独立问题）——不标 idle 也不标灰，如实说明「无需改写」。
            rw_src = (chat.get("ragRewriteSource") or "none").strip()
            if rw_src in ("dict", "dict_suffix"):
                hit("rewrite", "ok", detail="口语术语已映射为规范表述（本地词典）")
            elif rw_src in ("llm", "llm+dict"):
                hit("rewrite", "ok", detail="已结合上文补全指代/省略后再检索")
            elif rw_src == "failed":
                hit("rewrite", "warn", detail="改写失败，已回退原文检索")
            else:
                hit("rewrite", "ok", detail="问题已足够清晰，未做改写")
            # 召回：命中条数（判定不是 degraded 就说明检索本身跑通了）
            if decision == "degraded" or not decision:
                hit("recall", "warn", detail="检索链路未产出判定（可能已降级）")
            elif decision == "no_hit":
                hit("recall", "gray", detail="双路召回均无候选命中")
            elif decision == "cross_domain":
                hit("recall", "gray", detail="候选中最高分低于跨域阈值，判为库外问题")
            else:
                hit("recall", "ok", detail="召回成功，进入重排")
            if decision == "kept":
                hit("rerank", "ok", detail="重排完成")
                hit("select", "ok", detail=f"精选 {n_docs} 篇文档作为证据" if n_docs
                    else "精选完成")
                hit("evaluate", "ok", detail="judge 判证据充分")
                hit("answer", "ok", detail="带证据生成回答")
            elif decision == "insufficient_evidence":
                # 两种情况：召回过少（检索层判）或 judge 判证据不足（生成层判）。
                # 前者连重排/精选都没走到——用是否有候选来区分，别把没发生的阶段标绿。
                has_cand = bool(n_docs)
                if has_cand:
                    hit("rerank", "ok", detail="重排完成")
                    hit("select", "ok", detail=f"精选出 {n_docs} 篇，但覆盖不足")
                else:
                    hit("rerank", "gray", detail="候选过少，未进入有效重排")
                    hit("select", "gray", detail="未达到最小保底条数")
                hit("evaluate", "gray", detail="judge 判证据不足，如实拒答")
                hit("answer", "gray", detail="如实拒答（知识库暂未收录）")
            elif decision in ("cross_domain", "no_hit"):
                hit("rerank", "gray", detail="无有效候选，未进入重排")
                hit("select", "gray", detail="无候选可精选")
                hit("evaluate", "gray", detail="未进入评估（问题不在知识域）")
                hit("answer", "gray", detail="如实拒答（问题超出知识库范围）")
            elif decision == "degraded":
                hit("answer", "warn", detail="检索故障，降级闲聊链路回答")

        # ---- ③ 业务链路：路由模式 / 计划 / 委派 / 工具 ----
        if intent == "business":
            mode = (chat.get("mode") or "").upper()
            if mode == "PLAN":
                hit("route", "ok", detail="判定为复杂任务，走 PLAN 规划模式")
                hit("plan", "ok", detail="已生成执行计划")
            elif mode == "REACT":
                hit("route", "ok", detail="判定为常规任务，走 REACT 循环模式")
                by_key["plan"]["status"] = "idle"
                by_key["plan"]["detail"] = "REACT 模式无需规划"
            else:
                hit("route", "warn", detail="未能判定执行模式")

            forced = [t for t in tools if t.get("forced")]
            if delegations or forced:
                names = [d.get("agent") for d in delegations if d.get("agent")] or \
                        [str((t.get("argsDigest") or ""))[:24] for t in forced]
                bad = [d for d in delegations if (d.get("status") or "").upper() != "DONE"]
                hit("delegate", "warn" if bad else "ok",
                    detail=f"委派 {len(delegations) or len(forced)} 次：{'、'.join(names[:3])}",
                    error="委派未正常完成" if bad else "")
            else:
                by_key["delegate"]["status"] = "idle"
                by_key["delegate"]["detail"] = "本轮无需委派"

            if tools:
                ok_n = sum(1 for t in tools if t.get("ok"))
                bad_tools = [t for t in tools if not t.get("ok")]
                retried = [t for t in bad_tools if (t.get("error") or "").endswith("_retry")]
                blocked = [t for t in bad_tools if t.get("blocked")]
                if blocked:
                    hit("tools", "warn",
                        detail=f"{len(tools)} 次调用，{len(blocked)} 次被权限拦截",
                        error=blocked[0].get("blocked") or "")
                elif retried:
                    hit("tools", "warn",
                        detail=f"{len(tools)} 次调用，{len(retried)} 次自愈重试后继续")
                elif bad_tools:
                    hit("tools", "error",
                        detail=f"{len(tools)} 次调用，{len(bad_tools)} 次失败",
                        error=bad_tools[0].get("error") or "工具调用失败")
                else:
                    hit("tools", "ok", detail=f"{ok_n} 次调用全部成功")

            # 回答段：可观测信号只覆盖异常路径（失败/降级/兜底），
            # 正常收尾时补一句与工具步数闭环的说明，免得该段空白像个缺失项。
            ans = by_key.get("answer")
            if ans is not None and ans["status"] == "ok" and not ans["detail"]:
                ans["detail"] = (f"汇总 {len(tools)} 步工具结果生成结论"
                                 if tools else "直接给出回答")

        # ---- ④ 会话级兜底：错误 / 兜底话术 ----
        if not chat.get("ok"):
            hit("answer", "error", detail="本轮未正常产出回答",
                error=chat.get("error") or "会话失败")
        if chat.get("degraded"):
            hit("answer", "warn", detail="本轮走了降级路径")

        # ---- ④.5 记忆段（M-4）：数字唯一来源是 agent_chat_log 三列 ----
        # 状态口径（验收「未裁剪/已裁剪/已过滤/降级」着色矩阵）：
        #   未裁剪 / 已裁剪 / 已过滤 = ok（设计内行为，靠 detail 文案区分，不打黄——
        #   打黄会让长会话满屏黄）；降级 = warn（由 ① 的 memory_*_degraded 事件打上，
        #   这里不覆盖）。数字推导：过滤淘汰 = total - used - trimmed（单位均为条）。
        mem = by_key.get("memory")
        if mem is not None:
            mem_total = int(chat.get("memoryTurnsTotal") or 0)
            mem_used = int(chat.get("memoryTurnsUsed") or 0)
            mem_trim = int(chat.get("memoryTrimmed") or 0)
            mem_filtered = max(0, mem_total - mem_used - mem_trim)
            if intent == "rag":
                # 知识问答直读全量历史（规约 §8 开放问题 4）：没有可展示的装配数字，
                # idle 不画假节点；若记忆库降级（①已打 warn）则保留 warn 不降级为 idle。
                if mem["status"] == "ok":
                    mem["status"] = "idle"
                    mem["detail"] = "知识问答直读全量历史，不做过滤与裁剪"
            elif mem_total <= 0:
                # 首轮无历史 / 未登录业务模板 / v6 之前的存量行
                if mem["status"] == "ok":
                    mem["status"] = "idle"
                    mem["detail"] = "本轮无历史记忆需要装配（或为 v6 前存量数据）"
            else:
                parts = [f"载入 {mem_total} 条"]
                if mem_filtered > 0:
                    parts.append(f"过滤 {mem_filtered} 条")
                if mem_trim > 0:
                    parts.append(f"压缩 {mem_trim} 条")
                parts.append(f"注入 {mem_used} 条")
                # detail 以三列数字为准（覆盖 ① 里 memory_trimmed/filtered 事件的
                # 占位 detail——数字口径唯一来源是 chat_log，事件只负责状态与留痕）
                mem["detail"] = " → ".join(parts)

        # ---- ⑤ 汇总：整体结论 + 每阶段耗时归集 ----
        worst = "ok"
        for s in stages:
            if _STAGE_STATUS_ORDER.get(s["status"], 0) > _STAGE_STATUS_ORDER.get(worst, 0):
                worst = s["status"]
        total_ms = chat.get("durationMs")
        return {"stages": stages, "worstStatus": worst, "totalMs": total_ms}

    def trace_detail(self, trace_id: str) -> dict[str, Any]:
        """trace 追溯核心（P0）：单次对话的 chat + 工具步骤 + 委派 + 故障时间线。

        故障关联回退：早期故障行 trace_id 为空，按 session_id 兜底匹配
        （chat_log 行的 session_id），保证存量数据可追溯。
        v5：额外返回 pipeline（链路阶段 + 三色状态），供会话详情弹窗渲染执行流程。
        """
        data: dict[str, Any] = {"enabled": self._enabled, "found": False,
                                "chat": None, "tools": [], "delegations": [], "faults": [],
                                "pipeline": None, "question": ""}
        if not self._enabled:
            return data
        # 格式非法直接返回 found=false（不查库，省一次无意义查询）
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", str(trace_id or "")):
            return data
        # ⚠️ 必须 _ensure_conn()：_conn 是懒初始化（首次写入时才建），
        # 进程刚起、还没写过任何埋点就来读 trace 时 _conn 仍是 None，
        # 原来直接判 `self._conn is None: return data` 会**静默返回 found=False**——
        # 表现为"接口 200 但查不到任何轨迹"，极易误判成数据丢失。
        conn = self._ensure_conn()
        try:
            row = conn.execute(
                f"SELECT {self._CHAT_COLS} "
                "FROM agent_chat_log WHERE trace_id = ? ORDER BY id DESC LIMIT 1",
                (trace_id,)).fetchone()
            if row is None:
                return data
            data["chat"] = self._chat_row(row)
            data["found"] = True
            session_id = row[1]
            data["tools"] = [
                {"step": r[0], "tool": r[1], "ok": bool(r[2]), "blocked": r[3],
                 "error": r[4], "durationMs": r[5], "argsDigest": r[6], "createdAt": r[7]}
                for r in conn.execute(
                    "SELECT step,tool,ok,blocked,error,duration_ms,args_digest,created_at "
                    "FROM agent_tool_call WHERE trace_id = ? ORDER BY step, id", (trace_id,)).fetchall()
            ]
            data["delegations"] = [
                {"agent": r[0], "status": r[1], "durationMs": r[2], "createdAt": r[3]}
                for r in conn.execute(
                    "SELECT agent,status,duration_ms,created_at FROM agent_delegation "
                    "WHERE trace_id = ? ORDER BY id", (trace_id,)).fetchall()
            ]
            data["faults"] = [
                {"path": r[0], "detail": r[1], "createdAt": r[2]}
                for r in conn.execute(
                    "SELECT path,detail,created_at FROM fault_event "
                    "WHERE trace_id = ? OR (trace_id = '' AND session_id = ?) ORDER BY id",
                    (trace_id, session_id)).fetchall()
            ]
            # v5：链路阶段还原（会话详情弹窗的「执行流程」视图）
            data["pipeline"] = self._pipeline(
                data["chat"], data["tools"], data["delegations"], data["faults"])
            # 用户问题原句：埋点不落正文（规约 §10），只能从 feedback_content
            # （点踩时捕获的那一句）取；取不到就留空，不编造。
            try:
                qrow = conn.execute(
                    "SELECT question FROM feedback_content WHERE trace_id = ? "
                    "AND question != '' ORDER BY id DESC LIMIT 1", (trace_id,)).fetchone()
                if qrow and qrow[0]:
                    # S-8：原句出库前过 PII 脱敏（库里是内容表原文，出口不留明文 PII）
                    data["question"] = redact_pii(qrow[0])[:300]
                    data["questionSource"] = "feedback"
            except Exception:
                pass
        except Exception as exc:
            logger.error("trace 追溯查询失败: %s", exc)
        return data

    # ---- DWS 预聚合与查询（P1：数据中台开发规约 §5/§6，数据库规约 §4）----

    # 指标白名单（口径唯一：改口径必须改数据中台规约 §5 指标字典并升版本）
    _METRIC_WHITELIST = (
        "chat_total", "chat_failed", "chat_degraded",
        "tool_total", "tool_failed",
        "delegation_total", "delegation_done",
        "fault_total",
        "duration_avg", "duration_p95",
        "tokens_total",
        # E-13（判据 C7）：token 按 prompt / completion 拆分——成本折算需要两侧单价不同，
        # 只有 tokens_total 的话"手工核算一致"就无从谈起
        "tokens_prompt", "tokens_completion",
    )
    # metric -> 允许的 dim_type（即 timeseries 的 groupBy 值域）
    _METRIC_DIM_TYPES = {
        "chat_total": ("", "intent"),
        "chat_failed": ("", "intent"),
        "chat_degraded": ("", "intent"),
        "duration_avg": ("", "intent"),
        "duration_p95": ("", "intent"),
        "tokens_total": ("", "model"),
        "tokens_prompt": ("", "model"),
        "tokens_completion": ("", "model"),
        "tool_total": ("", "tool"),
        "tool_failed": ("", "tool"),
        "delegation_total": ("", "agent"),
        "delegation_done": ("", "agent"),
        "fault_total": ("", "fault_path"),
    }

    @staticmethod
    def _day_bounds(day: str) -> tuple[str, str]:
        """本地日（YYYY-MM-DD）→ created_at 字符串比较用的闭区间。"""
        return f"{day}T00:00:00", f"{day}T23:59:59"

    def recompute_daily(self, day: str) -> int:
        """重算某本地日的 DWS 聚合（先删后插，幂等）。返回写入行数；异常上抛由调用方兜底。"""
        if not self._enabled or self._conn is None:
            return 0
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return 0
        conn = self._ensure_conn()
        lo, hi = self._day_bounds(day)
        now = _now()
        rows: list[tuple] = []  # (day, metric, dim, dim_type, value, updated_at)

        def add(metric: str, dim: str, dim_type: str, value) -> None:
            rows.append((day, metric, dim, dim_type, round(float(value), 4), now))

        # --- agent_chat_log：全量 + intent ---
        row = conn.execute(
            "SELECT COUNT(*), SUM(ok=0), SUM(degraded=1), AVG(duration_ms) "
            "FROM agent_chat_log WHERE created_at BETWEEN ? AND ?", (lo, hi)).fetchone()
        if row and row[0]:
            add("chat_total", "", "", row[0])
            add("chat_failed", "", "", row[1] or 0)
            add("chat_degraded", "", "", row[2] or 0)
            if row[3] is not None:
                add("duration_avg", "", "", row[3])
        for r in conn.execute(
            "SELECT intent, COUNT(*), SUM(ok=0), SUM(degraded=1), AVG(duration_ms) "
            "FROM agent_chat_log WHERE created_at BETWEEN ? AND ? "
            "GROUP BY intent", (lo, hi)).fetchall():
            if r[0]:
                add("chat_total", r[0], "intent", r[1])
                add("chat_failed", r[0], "intent", r[2] or 0)
                add("chat_degraded", r[0], "intent", r[3] or 0)
                if r[4] is not None:
                    add("duration_avg", r[0], "intent", r[4])
        # p95 近似值（指标字典注明）：nearest-rank，idx = ceil(0.95n)-1，纯整数运算
        dur = sorted(r[0] for r in conn.execute(
            "SELECT duration_ms FROM agent_chat_log "
            "WHERE created_at BETWEEN ? AND ? AND duration_ms IS NOT NULL", (lo, hi)))
        if dur:
            add("duration_p95", "", "", dur[max(0, (95 * len(dur) + 99) // 100 - 1)])
        # tokens：全量 + model。E-13 起按 prompt / completion 拆分（成本折算两侧单价不同；
        # tokens_total 保留以免既有看板断数）
        for r in conn.execute(
            "SELECT model, SUM(prompt_tokens + completion_tokens), SUM(prompt_tokens), "
            "SUM(completion_tokens) FROM agent_chat_log "
            "WHERE created_at BETWEEN ? AND ? GROUP BY model", (lo, hi)).fetchall():
            if r[0]:
                add("tokens_total", r[0], "model", r[1] or 0)
                add("tokens_prompt", r[0], "model", r[2] or 0)
                add("tokens_completion", r[0], "model", r[3] or 0)
        # --- agent_tool_call ---
        row = conn.execute(
            "SELECT COUNT(*), SUM(ok=0) FROM agent_tool_call "
            "WHERE created_at BETWEEN ? AND ?", (lo, hi)).fetchone()
        if row and row[0]:
            add("tool_total", "", "", row[0])
            add("tool_failed", "", "", row[1] or 0)
        for r in conn.execute(
            "SELECT tool, COUNT(*), SUM(ok=0) FROM agent_tool_call "
            "WHERE created_at BETWEEN ? AND ? GROUP BY tool", (lo, hi)).fetchall():
            if r[0]:
                add("tool_total", r[0], "tool", r[1])
                add("tool_failed", r[0], "tool", r[2] or 0)
        # --- agent_delegation ---
        row = conn.execute(
            "SELECT COUNT(*), SUM(status='DONE') FROM agent_delegation "
            "WHERE created_at BETWEEN ? AND ?", (lo, hi)).fetchone()
        if row and row[0]:
            add("delegation_total", "", "", row[0])
            add("delegation_done", "", "", row[1] or 0)
        for r in conn.execute(
            "SELECT agent, COUNT(*), SUM(status='DONE') FROM agent_delegation "
            "WHERE created_at BETWEEN ? AND ? GROUP BY agent", (lo, hi)).fetchall():
            if r[0]:
                add("delegation_total", r[0], "agent", r[1])
                add("delegation_done", r[0], "agent", r[2] or 0)
        # --- fault_event ---
        row = conn.execute(
            "SELECT COUNT(*) FROM fault_event WHERE created_at BETWEEN ? AND ?", (lo, hi)).fetchone()
        if row and row[0]:
            add("fault_total", "", "", row[0])
        for r in conn.execute(
            "SELECT path, COUNT(*) FROM fault_event "
            "WHERE created_at BETWEEN ? AND ? GROUP BY path", (lo, hi)).fetchall():
            if r[0]:
                add("fault_total", r[0], "fault_path", r[1])

        # 先删后插：维度当天归零时不留陈旧行，幂等可重跑
        conn.execute("DELETE FROM metrics_daily WHERE day = ?", (day,))
        if rows:
            conn.executemany(
                "INSERT INTO metrics_daily(day, metric, dim, dim_type, value, updated_at) "
                "VALUES(?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)

    def recompute_recent(self, days: int = 2) -> int:
        """定时重算入口：近 N 个本地日（含今天）。"""
        total = 0
        for i in range(max(1, days)):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            total += self.recompute_daily(d)
        return total

    def timeseries(
        self, *, metric: str, date_from: str, date_to: str, group_by: str = "",
    ) -> dict[str, Any]:
        """按天序列（读 DWS）。今天强制重算保证新鲜；历史天缺数且明细在则懒补算。"""
        data: dict[str, Any] = {"enabled": self._enabled, "metric": metric,
                                "from": date_from, "to": date_to, "groupBy": group_by, "series": []}
        if not self._enabled or self._conn is None:
            return data
        # 白名单校验（规约 §6：防注入，拼接前枚举校验）
        if metric not in self._METRIC_WHITELIST:
            data["reason"] = f"未知 metric: {metric}"
            return data
        allowed = self._METRIC_DIM_TYPES[metric]
        if group_by not in allowed:
            data["reason"] = f"metric {metric} 不支持 groupBy={group_by}（允许: {list(allowed)}）"
            return data
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from or "")
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to or "")):
            data["reason"] = "日期格式须为 YYYY-MM-DD"
            return data
        try:
            d0 = datetime.strptime(date_from, "%Y-%m-%d")
            d1 = datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            data["reason"] = "非法日期"
            return data
        if d0 > d1 or (d1 - d0).days > 92:
            data["reason"] = "时间区间非法（最长 92 天）"
            return data
        try:
            conn = self._ensure_conn()
            today = datetime.now().strftime("%Y-%m-%d")
            if date_to >= today:
                self.recompute_daily(today)  # 今天强制重算
            # 懒补算：历史天 DWS 无行但 ODS 明细仍在（TTL 90 天内）才补
            cur = d0
            while cur <= d1:
                d = cur.strftime("%Y-%m-%d")
                if d != today:
                    has_dws = conn.execute(
                        "SELECT 1 FROM metrics_daily WHERE day = ? LIMIT 1", (d,)).fetchone()
                    if not has_dws:
                        lo, hi = self._day_bounds(d)
                        has_ods = conn.execute(
                            "SELECT 1 FROM agent_chat_log WHERE created_at BETWEEN ? AND ? LIMIT 1",
                            (lo, hi)).fetchone()
                        if has_ods:
                            self.recompute_daily(d)
                cur += timedelta(days=1)
            data["series"] = [
                {"day": r[0], "dim": r[1], "value": r[2]}
                for r in conn.execute(
                    "SELECT day, dim, value FROM metrics_daily "
                    "WHERE metric = ? AND day BETWEEN ? AND ? AND dim_type = ? ORDER BY day, dim",
                    (metric, date_from, date_to, group_by)).fetchall()
            ]
        except Exception as exc:
            logger.error("timeseries 查询失败: %s", exc)
        return data

    def tools_top(self, *, order_by: str = "calls", limit: int = 10, hours: int = 24) -> dict[str, Any]:
        """工具 TopN（读 ODS 明细，窗口聚合：失败率需按 total 加权，DWS 单日行做不了跨天率）。"""
        data: dict[str, Any] = {"enabled": self._enabled, "orderBy": order_by, "rows": []}
        if not self._enabled or self._conn is None:
            return data
        if order_by not in ("calls", "fail_rate", "duration"):
            data["reason"] = f"orderBy 仅支持 calls/fail_rate/duration，收到 {order_by}"
            return data
        limit = max(1, min(int(limit or 10), 100))
        hours = max(1, min(int(hours or 24), 24 * 90))
        try:
            rows = [
                {"tool": r[0], "total": r[1], "failed": r[2] or 0,
                 "failRate": round((r[2] or 0) / r[1], 4), "avgDurationMs": round(r[3], 1) if r[3] is not None else None}
                for r in self._conn.execute(
                    "SELECT tool, COUNT(*), SUM(ok=0), AVG(duration_ms) FROM agent_tool_call "
                    "WHERE created_at >= datetime('now','localtime',?) GROUP BY tool",
                    (f"-{hours} hours",)).fetchall()
            ]
            key = {"calls": lambda x: x["total"], "fail_rate": lambda x: x["failRate"],
                   "duration": lambda x: x["avgDurationMs"] or 0}[order_by]
            rows.sort(key=key, reverse=True)
            data["rows"] = rows[:limit]
        except Exception as exc:
            logger.error("tools_top 查询失败: %s", exc)
        return data

    # ---- v4：知识缺口分析（数据中台规约 §5「未命中问题分析」）----

    # 「算知识缺口」的两类判定。kept=证据充足、cross_domain=问的根本不属知识域，
    # 两者都不是缺口；剩下两类才是"知识库里真该有、却答不上"的信号。
    GAP_DECISIONS = ("no_hit", "insufficient_evidence")

    # 停用词/疑问词：聚类的主题词须剔除，否则"怎么""为什么"会把所有问题聚成一坨
    _STOPWORDS = frozenset((
        "怎么", "如何", "为什么", "什么", "哪些", "哪个", "是否", "能否", "可以", "请问",
        "一下", "我们", "你们", "他们", "需要", "应该", "会不会", "有没有", "多少",
        "以及", "还有", "就是", "这个", "那个", "的话", "一个", "进行", "相关", "关于",
    ))

    # 切段用的单字虚词（"的/怎么/哪些"这类）。
    # 为什么必须做这一步：直接对整句取 bigram 会产出 `法怎`/`法的`/`的步` 这种
    # **跨词边界噪声**——它们把"查询方法的步骤"和"查询方法怎么用"的共同词元从 3 个
    # 稀释成"3 交集 / 5 并集"，同主题聚不起来（实测漏聚率约 25%）。
    # 先按虚词切成实词段，再对段取 bigram，噪声消失：同主题相似度升到 0.5~1.0，
    # 异主题降到 0.0，阈值判定变得极干净（见 `_cluster` 的实测数据）。
    _SEPARATORS = frozenset(
        "的了我是在和与及或对为把被就都也还很太更最不没要有会能可以这那哪什么怎如何"
        "谁时候吗呢吧啊呀个之其因此并但而且向从给让使将则并"
    )

    @classmethod
    def _keywords(cls, text: str) -> list[str]:
        """中文短句 → 主题关键词（规则法，零 token、结果可复现）。

        做法：切出 CJK 连续段与英文/数字串 → CJK 段先按单字虚词切成实词段 →
        实词段 ≤4 字整体保留，更长则展开为 bigram → 去停用词去重后取前若干。
        比 jieba 差，但对"聚类计数"这个用途足够——同一主题的不同问法会共享
        实词段（`查询方法`），异主题几乎不共享，这正是聚类需要的性质。
        """
        text = str(text or "")
        tokens: list[str] = []
        for seg in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]{2,}", text):
            if re.fullmatch(r"[A-Za-z0-9_]{2,}", seg):
                tokens.append(seg.lower())
                continue
            parts: list[str] = []
            cur = ""
            for ch in seg:
                if ch in cls._SEPARATORS:
                    if cur:
                        parts.append(cur)
                        cur = ""
                else:
                    cur += ch
            if cur:
                parts.append(cur)
            for p in parts:
                if len(p) < 2:
                    continue          # 单字实词信息量太低，且会放大噪声
                if len(p) <= 4:
                    tokens.append(p)
                else:
                    tokens.extend(p[i:i + 2] for i in range(len(p) - 1))
        seen: list[str] = []
        for t in tokens:
            if t in cls._STOPWORDS or t in seen:
                continue
            seen.append(t)
        return seen[:12]

    @classmethod
    def _similarity(cls, a: list[str], b: list[str]) -> float:
        """关键词集合的**重叠系数**（overlap coefficient），非 Jaccard。

        为什么不用 Jaccard：长句的独有词元会把分母撑大。`查询方法是什么`(1 词元) 与
        `查询方法有哪些参数`(2 词元) 用 Jaccard 只有 1/2=0.5，再加一个主题词就掉到线下；
        用 |A∩B| / min(|A|,|B|)：短句被长句覆盖时得 1.0，正好对应"同一主题的不同问法"。
        配合 `_keywords` 的实词切段，实测同主题 0.5~1.0 / 异主题 0.0。
        """
        if not a or not b:
            return 0.0
        sa, sb = set(a), set(b)
        return len(sa & sb) / min(len(sa), len(sb))

    @classmethod
    def _cluster(cls, items: list[dict], threshold: float = 0.5) -> list[dict]:
        """把缺口问题按关键词相似度聚成主题（单遍贪心，O(n·k)）。

        threshold=0.5 的来历（按 `_keywords` 的虚词切段法实测）：
        「同一主题换个问法」落 0.5~0.88，「不同主题」≈0.0~0.14
        （唯一 0.14 是"反重力/无人机"共享了"多久"这种问法词，不达阈值）。

        ⚠️ 簇代表怎么存（实测踩坑）：**簇的关键词取"首次建簇时那一句"，不取并集**。
        若并集，代表集会随成员增加而膨胀——3 条反重力问题的并集有 13 个词，
        新成员（11 词）与它的交集只有 5 个 → 0.45 < 0.5 被漏掉，
        于是「本该 3 条一簇」变成「2 条一簇 + 1 条独立」。
        改存首句后，新成员永远与一个**短且干净的**代表集比，实测三条稳定合一。
        代价：首个样本若本身偏离主题，簇会偏；单遍贪心本来就不回溯，这点可接受
        （且样本顺序是 id DESC，即最新的问法优先当代表，通常更贴近真实用语）。
        """
        clusters: list[dict] = []
        for it in items:
            kws = it["keywords"]
            best, best_score = None, 0.0
            for c in clusters:
                score = cls._similarity(kws, c["keywords"])
                if score > best_score:
                    best, best_score = c, score
            if best is not None and best_score >= threshold:
                best["count"] += 1
                best["decisions"][it["decision"]] = best["decisions"].get(it["decision"], 0) + 1
                best["samples"].append(it["question"])
                best["traceIds"].append(it["traceId"])
            else:
                clusters.append({
                    "keywords": list(kws),
                    "count": 1,
                    "decisions": {it["decision"]: 1},
                    "samples": [it["question"]],
                    "traceIds": [it["traceId"]],
                })
        return clusters

    @staticmethod
    def _suggestion(decision_mix: dict, count: int, min_occ: int) -> tuple[str, str]:
        """主题 → (补充建议, 优先级)。优先级三档，与前端色板对齐。

        - 高：证据不足（insufficient_evidence）——库里**有**相关内容但不够支撑回答，
              补一篇就能救活，性价比最高；
        - 中：完全无召回（no_hit）——库里是真没有，要新增文档；
        - 观察：次数不到阈值，或两类混杂——先别动手，攒样本。
        """
        insuff = decision_mix.get("insufficient_evidence", 0)
        no_hit = decision_mix.get("no_hit", 0)
        if count < min_occ:
            return ("观察（样本不足，累计出现后再评估补库）", "低")
        if insuff and not no_hit:
            return ("紧急补库：库内有相关内容但证据不足，建议补充/细化该主题的专项文档", "高")
        if no_hit and not insuff:
            return ("建议补库：库内完全无召回，需新增该主题的文档", "中")
        return ("建议补库：本主题同时存在无召回与证据不足，建议先补文档、再细化已有文档", "高")

    def unanswered(self, *, hours: int = 0, limit: int = 50) -> dict[str, Any]:
        """知识缺口分析（数据中台规约 §5）：未命中问题聚类 + 补库建议。

        口径要点（写进指标字典，改口径必须同步改规约）：
        - **只统计 no_hit / insufficient_evidence**；kept（答上了）与 cross_domain
          （问的根本不属知识域）都不算缺口——把后者算进来会让视图被"今天天气"淹掉；
        - 问题原文**可能缺失**：埋点四表不存正文（规约 §10），原句只能从会话记忆
          （易失）或 feedback_content 捞。捞不到就只出关键词与计数，**不伪造原句**；
        - 缺口的"主题"由关键词聚类得出，不调 LLM（零 token、可复现、夜间批跑也便宜）。
        """
        data: dict[str, Any] = {
            "enabled": self._enabled, "hours": hours, "total": 0, "gapTotal": 0,
            "distinctTopics": 0, "byDecision": {}, "topics": [], "suggestions": [],
            "questionAvailable": False, "reason": "",
        }
        if not self._enabled or self._conn is None:
            data["reason"] = "telemetry disabled"
            return data
        hours = max(1, min(int(hours or settings.kb_gap_hours_default), 24 * 90))
        limit = max(1, min(int(limit or 50), 200))
        max_q = max(1, int(settings.kb_gap_max_questions))
        min_occ = max(2, int(settings.kb_gap_min_occurrences))
        data["hours"] = hours
        try:
            conn = self._ensure_conn()
            self.flush_now()   # 队列里可能还有刚产生的会话，先落库再统计，否则看板滞后一轮
            where = "WHERE created_at >= datetime('now','localtime',?)"
            args: list[Any] = [f"-{hours} hours"]
            # ① 分布：四类判定各多少（kept/cross_domain 也要出，便于看板说明"为何不算缺口"）
            data["byDecision"] = {
                r[0] or "": r[1] for r in conn.execute(
                    f"SELECT rag_decision, COUNT(*) FROM agent_chat_log {where} "
                    "AND rag_decision != '' GROUP BY rag_decision", args).fetchall()}
            data["total"] = sum(data["byDecision"].values())
            gap_rows = conn.execute(
                f"SELECT trace_id, session_id, rag_decision, created_at "
                f"FROM agent_chat_log {where} AND rag_decision IN (?,?) "
                "ORDER BY id DESC LIMIT ?",
                args + [self.GAP_DECISIONS[0], self.GAP_DECISIONS[1], max_q]).fetchall()
            data["gapTotal"] = len(gap_rows)
            data["truncated"] = len(gap_rows) >= max_q
            if not gap_rows:
                return data

            # ② 原句回捞：会话记忆（进程内，近期）优先，feedback_content 兜底。
            #    两处都没有就留空——宁可只给关键词，也不编一句假问题。
            #
            #    ⚠️ 对齐陷阱（实测踩到）：记忆只保留"最近 window 条"，且**只有一份**——
            #    同一 session 里有 N 条会话行时，若都调 `_last_turn_of(sid)`，会拿到
            #    同一句（最后一次提问），于是 4 条不同问题的样例全变成同一句、
            #    关键词也被污染成同一个主题。
            #    正确做法：缺口行按 id DESC 已是"新→旧"，记忆里 user 消息的倒序也是
            #    "新→旧"，**同序号对齐**即可（第 1 条缺口行 ↔ 倒数第 1 句提问）。
            #    记忆被截断（消息数上限 40 / window 10）时对齐会错位，故只在
            #    "缺口行数 ≤ 记忆里 user 消息数"时启用对齐，否则退回一句都不给——
            #    错位的原句比没有原句更坏（会把 A 问题的原文挂在 B 主题下）。
            questions: dict[str, str] = {}
            try:
                from app.services.memory import session_memory  # 延迟导入防环
                # session → 该 session 在缺口行里的出现顺序（新→旧）
                order: dict[str, list[str]] = {}
                for tid, sid, _dec, _c in gap_rows:
                    order.setdefault(sid or "", []).append(tid)
                for sid, tids in order.items():
                    if not sid:
                        continue
                    users = [m["content"] for m in session_memory.get(sid)
                             if m["role"] == "user"]
                    users.reverse()          # 新→旧，与 tids 顺序一致
                    if len(users) < len(tids):
                        continue             # 记忆不足以对齐全部轮次 → 放弃该 session（防错位）
                    for i, tid in enumerate(tids):
                        if users[i]:
                            # S-8：展示侧原句先过 PII 脱敏（防 PII 经看板二次扩散）
                            questions[tid] = redact_pii(users[i])
            except Exception:
                pass
            try:
                # feedback_content 以 trace_id 为键，精确对齐（点踩时捕获的那一句），
                # 优先级低于记忆（记忆是即时对齐，feedback 可能隔很久才点）
                for r in conn.execute(
                    "SELECT trace_id, question FROM feedback_content "
                    "WHERE question != '' ORDER BY id DESC LIMIT ?", (max_q,)).fetchall():
                    if r[0] and r[0] not in questions and r[1]:
                        questions[r[0]] = redact_pii(r[1])
            except Exception:
                pass
            data["questionAvailable"] = bool(questions)
            if not questions:
                data["reason"] = "问题原文不可得（会话记忆已淘汰且无反馈捕获），仅输出主题聚类与计数"

            # ③ 构造待聚类条目 + 聚类（原句按 traceId 对齐，捞不到就只留占位词）
            #    占位词用**整个 sessionId** 而非尾号：同一 session 的多条无原句行
            #    会因此归到同一占位簇（能看出"某会话连续问了 N 次都没答上"），
            #    用尾号会因相似度不足散成多个假主题。
            items: list[dict] = []
            for tid, sid, decision, _created in gap_rows:
                q = questions.get(tid or "", "")
                kws = self._keywords(q) if q else []
                items.append({"traceId": tid, "question": q, "decision": decision,
                              "keywords": kws or [f"sess-{(sid or 'unknown')[:24]}"]})
            clusters = self._cluster(items)
            clusters.sort(key=lambda c: (-c["count"], -len(c["keywords"])))

            # ④ 出主题视图 + 补库建议（三档优先级）
            suggestions: list[dict] = []
            for c in clusters[:limit]:
                text, priority = self._suggestion(c["decisions"], c["count"], min_occ)
                # samples 去重保序：同句重复出现（用户复述）对"看样例"没价值，
                # 但它仍**计入 count**（复述就是高频信号，不能因为重复而减权）
                seen_s: list[str] = []
                for s in c["samples"]:
                    if s and s not in seen_s:
                        seen_s.append(s)
                samples = seen_s[:3]
                topics_row = {
                    "keywords": c["keywords"][:8],
                    "count": c["count"],
                    "decisions": c["decisions"],
                    "samples": samples,
                    "traceIds": c["traceIds"][:5],
                    "suggestion": text,
                    "priority": priority,
                }
                data["topics"].append(topics_row)
                suggestions.append({
                    "topic": " / ".join(c["keywords"][:4]) or "(无原句主题)",
                    "count": c["count"],
                    "priority": priority,
                    "suggestion": text,
                    "sampleQuestions": samples,
                })
            data["distinctTopics"] = len(clusters)
            # 建议按优先级排序：高 > 中 > 低，同级按出现次数
            rank = {"高": 0, "中": 1, "低": 2}
            suggestions.sort(key=lambda s: (rank.get(s["priority"], 9), -s["count"]))
            data["suggestions"] = suggestions[:limit]
        except Exception as exc:
            logger.error("知识缺口分析失败: %s", exc)
            data["reason"] = f"分析失败: {exc}"
        return data

    # ---- v4：文档使用度（B 类「无人访问文档」巡检的数据源）----

    def doc_usage(self, *, hours: int = 0, limit: int = 200) -> dict[str, Any]:
        """命中文档使用度：每个 docId 被 RAG 引用了多少次、最近一次何时。

        这是 B 类巡检（"哪些文档从没被访问过"）的**已访问侧**：
        与知识库文档清单做差集 = 从未命中过的文档。
        注意口径边界（规约必须写明）：只统计 **kept**（真的答上了）的引用，
        拒答会话即使召回到文档也不算——拒答说明那份文档答不了这个问题。

        已知限制：rag_docs 截断到前 20 篇/行，长尾引用统计会偏低；
        对"是否从未被访问"这个二值判断无影响（从未访问仍会显示为 0）。
        """
        data: dict[str, Any] = {"enabled": self._enabled, "hours": hours,
                                "rows": [], "docTotal": 0, "reason": ""}
        if not self._enabled or self._conn is None:
            data["reason"] = "telemetry disabled"
            return data
        hours = max(1, min(int(hours or settings.kb_gap_hours_default), 24 * 90))
        limit = max(1, min(int(limit or 200), 2000))
        data["hours"] = hours
        try:
            conn = self._ensure_conn()
            stats: dict[str, dict[str, Any]] = {}
            for docs, created in conn.execute(
                "SELECT rag_docs, created_at FROM agent_chat_log "
                "WHERE rag_decision = 'kept' AND rag_docs != '' "
                "AND created_at >= datetime('now','localtime',?)", (f"-{hours} hours",)).fetchall():
                for did in str(docs).split(","):
                    did = did.strip()
                    if not did:
                        continue
                    s = stats.setdefault(did, {"docId": did, "hits": 0, "lastHitAt": ""})
                    s["hits"] += 1
                    if (created or "") > s["lastHitAt"]:
                        s["lastHitAt"] = created or ""
            rows = sorted(stats.values(), key=lambda x: (-x["hits"], x["docId"]))
            data["docTotal"] = len(rows)
            data["rows"] = rows[:limit]
        except Exception as exc:
            logger.error("文档使用度查询失败: %s", exc)
            data["reason"] = f"查询失败: {exc}"
        return data

    # ---- E-12：前端运行时报错（企业级落地规约 E-12，判据 C3）----

    def client_error_summary(self, *, hours: int = 24, limit: int = 20) -> dict[str, Any]:
        """前端报错看板数据源：总量 + 按页面 / 按版本聚合 + 最近明细（E-12）。

        口径说明写在这里，免得看板注释与后端各写一份（本项目铁律 7：判定只许有一份）：

        * `total` 是 **`count` 之和，不是行数** —— 同一错误在同一时间桶里只占一行，
          行数会严重低估"炸了多少次"（合并写的目的，见建表注释）；
        * `distinct` 才是不同错误的**类数**（约等于有多少种错）；
        * 聚合按 count 求和降序 —— 一个高频小错会排在偶发大错前面。这是刻意的：
          看板回答的是"现在什么最吵"，而不是"哪个最严重"（前端给不出严重度）。
        """
        data: dict[str, Any] = {
            "enabled": self._enabled, "hours": hours, "total": 0, "distinct": 0,
            "byPage": [], "byVersion": [], "latest": [], "reason": "",
        }
        if not self._enabled or self._conn is None:
            data["reason"] = "telemetry disabled"
            return data
        hours = max(1, min(int(hours or 24), 24 * 90))
        limit = max(1, min(int(limit or 20), 200))
        data["hours"] = hours
        # 窗口起点**在 Python 侧算好**，不用 SQLite 的 `datetime('now',...)`。
        # 理由：`_now()` 写的是 `2026-09-24T16:40:12`（T 分隔），而 SQLite 的
        # `datetime()` 返回 `2026-09-24 16:40:12`（空格分隔）。两者做**字符串**比较时，
        # 第 11 位 `T`(0x54) > 空格(0x20) —— 结果是"同一天的行无论如何都算在窗口内"，
        # 窗口实际被放宽最多 24 小时，且不报错。与 ops_metrics._window_cutoff 同一口径。
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        try:
            conn = self._ensure_conn()
            # ⚠️ `distinct` 数的是 **fingerprint 去重后的类数**，不是行数。
            # 行数是"同错 × 时间桶"的组合数（固定桶每跨一分钟就多一行），
            # 拿它当"有多少种错"会随观察窗口长短自己变大——看板上会出现
            # "刷新一下错误种类就多了几种"这种没法解释的现象。
            row = conn.execute(
                "SELECT COALESCE(SUM(count),0), COUNT(DISTINCT fingerprint) "
                "FROM client_error WHERE last_at >= ?",
                (cutoff,)).fetchone()
            data["total"] = int(row[0] or 0)
            data["distinct"] = int(row[1] or 0)
            data["byPage"] = [
                {"name": p or "(未知页面)", "value": int(c or 0)}
                for p, c in conn.execute(
                    "SELECT page, SUM(count) AS c FROM client_error WHERE last_at >= ? "
                    "GROUP BY page ORDER BY c DESC LIMIT ?", (cutoff, limit)).fetchall()]
            data["byVersion"] = [
                {"name": v or "(未知版本)", "value": int(c or 0)}
                for v, c in conn.execute(
                    "SELECT app_version, SUM(count) AS c FROM client_error WHERE last_at >= ? "
                    "GROUP BY app_version ORDER BY c DESC LIMIT ?", (cutoff, limit)).fetchall()]
            data["latest"] = [
                {"fingerprint": r[0], "page": r[1], "source": r[2], "message": r[3],
                 "stack": r[4], "appVersion": r[5], "userId": int(r[6] or 0),
                 "count": int(r[7] or 0), "firstAt": r[8], "lastAt": r[9],
                 "traceId": r[10] or ""}
                for r in conn.execute(
                    "SELECT fingerprint,page,source,message,stack,app_version,user_id,"
                    "count,first_at,last_at,trace_id FROM client_error WHERE last_at >= ? "
                    "ORDER BY last_at DESC, count DESC LIMIT ?", (cutoff, limit)).fetchall()]
        except Exception as exc:
            logger.error("前端报错查询失败: %s", exc)
            data["reason"] = f"查询失败: {exc}"
        return data

    # ---- P2：feedback 反馈闭环（数据中台规约 §6/§8；验收=按 trace 关联轨迹 + 金标集候选）----

    # 二值评分（规约开放问题 2 拍板：up/down，与评测 pass/fail 对齐）
    _RATINGS = ("up", "down")

    def submit_feedback(
        self, *, trace_id: str, rating: str, session_id: str = "",
        user_id: int = 0, comment: str = "",
    ) -> dict[str, Any]:
        """提交反馈：必须挂在真实会话轨迹上（trace 校验），重复反馈=新事件（只增不改）。

        内容捕获走独立表 feedback_content（规约 §4.2 口子：显式开关 + TTL，绝不并入四表），
        原文取自会话记忆的最后一轮；记忆被淘汰/重启丢失时留空，不伪造。
        """
        data: dict[str, Any] = {"recorded": False}
        rating = str(rating or "").lower().strip()
        if rating not in self._RATINGS:
            data["reason"] = "rating 须为 up/down（二值评分）"
            return data
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", str(trace_id or "")):
            data["reason"] = "traceId 格式非法"
            return data
        if not self._enabled or self._conn is None:
            data["reason"] = "telemetry disabled"
            return data
        try:
            self.flush_now()  # 把队列里的会话行先落库，保证 trace 可校验、反馈可关联
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT session_id FROM agent_chat_log WHERE trace_id = ? LIMIT 1",
                (trace_id,)).fetchone()
            if row is None:
                data["reason"] = "trace 不存在（反馈必须挂在真实会话轨迹上）"
                return data
            session_id = session_id or row[0] or ""
            conn.execute(
                "INSERT INTO agent_feedback(trace_id, session_id, user_id, rating, comment, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (trace_id, session_id[:64], int(user_id or 0), rating, _digest(comment), _now()))
            if settings.feedback_capture_content:
                question, answer = self._last_turn_of(session_id)
                conn.execute(
                    "INSERT INTO feedback_content(trace_id, question, answer, created_at) "
                    "VALUES(?,?,?,?)", (trace_id, question, answer, _now()))
            conn.commit()
            data["recorded"] = True
        except Exception as exc:
            logger.error("反馈落库失败: %s", exc)
            data["reason"] = "落库失败"
        return data

    @staticmethod
    def _last_turn_of(session_id: str) -> tuple[str, str]:
        """取该会话记忆中最后一轮（用户问题, 助手回复），供金标集候选参考。"""
        if not session_id:
            return "", ""
        try:
            from app.services.memory import session_memory  # 延迟导入防环
            msgs = session_memory.get(session_id)
            question = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
            answer = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant"), "")
            return question[:2000], answer[:2000]
        except Exception:
            return "", ""

    def feedback_list(
        self, *, hours: int = 24, limit: int = 50, rating: str = "",
    ) -> dict[str, Any]:
        """反馈列表：按 trace 关联会话轨迹摘要（时间线走既有 /metrics/traces/{id}）。"""
        data: dict[str, Any] = {"enabled": self._enabled, "rows": [], "up": 0, "down": 0}
        if not self._enabled or self._conn is None:
            return data
        hours = max(1, min(int(hours or 24), 24 * 90))
        limit = max(1, min(int(limit or 50), 200))
        sql = (
            "SELECT f.trace_id, f.session_id, f.user_id, f.rating, f.comment, f.created_at, "
            "c.intent, c.mode, c.ok, c.duration_ms, c.content_chars "
            "FROM agent_feedback f LEFT JOIN agent_chat_log c ON c.trace_id = f.trace_id "
            "WHERE f.created_at >= datetime('now','localtime',?)")
        args: list[Any] = [f"-{hours} hours"]
        if rating in self._RATINGS:
            sql += " AND f.rating = ?"
            args.append(rating)
        sql += " ORDER BY f.id DESC LIMIT ?"
        args.append(limit)
        try:
            stats = {r[0]: r[1] for r in self._conn.execute(
                "SELECT rating, COUNT(*) FROM agent_feedback "
                "WHERE created_at >= datetime('now','localtime',?) GROUP BY rating",
                (f"-{hours} hours",)).fetchall()}
            data["up"] = stats.get("up", 0)
            data["down"] = stats.get("down", 0)
            data["rows"] = [
                {"traceId": r[0], "sessionId": r[1], "userId": r[2], "rating": r[3],
                 "comment": r[4], "createdAt": r[5], "intent": r[6], "mode": r[7],
                 "chatOk": bool(r[8]) if r[8] is not None else None,
                 "durationMs": r[9], "contentChars": r[10]}
                for r in self._conn.execute(sql, args).fetchall()
            ]
        except Exception as exc:
            logger.error("反馈列表查询失败: %s", exc)
        return data

    def eval_candidates(self, *, hours: int = 24 * 90, limit: int = 500) -> dict[str, Any]:
        """评测金标集候选（P2 验收）：点踩/点赞会话 → JSONL 行，qid=fb-<traceId>。

        question/reference_answer 来自 feedback_content（捕获开关开且记忆尚在时），
        expected_* 字段留待人工审核补充——候选池是起点，不是自动金标。
        """
        data: dict[str, Any] = {"enabled": self._enabled, "count": 0, "jsonl": ""}
        if not self._enabled or self._conn is None:
            return data
        hours = max(1, min(int(hours or 24 * 90), 24 * 90))
        limit = max(1, min(int(limit or 500), 2000))
        try:
            # 同一 trace 多次反馈取最新一条；内容取该 trace 最新一行
            rows = self._conn.execute(
                "SELECT f.trace_id, f.session_id, f.rating, f.comment, f.created_at, "
                "c.intent, c.mode FROM agent_feedback f "
                "LEFT JOIN agent_chat_log c ON c.trace_id = f.trace_id "
                "WHERE f.id IN (SELECT MAX(id) FROM agent_feedback "
                "GROUP BY trace_id) "
                "AND f.created_at >= datetime('now','localtime',?) "
                "ORDER BY f.id DESC LIMIT ?", (f"-{hours} hours", limit)).fetchall()
            lines: list[str] = []
            for r in rows:
                content = self._conn.execute(
                    "SELECT question, answer FROM feedback_content WHERE trace_id = ? "
                    "ORDER BY id DESC LIMIT 1", (r[0],)).fetchone()
                cand = {
                    "qid": f"fb-{r[0]}",
                    "source": "feedback",
                    "traceId": r[0],
                    "sessionId": r[1],
                    "rating": r[2],
                    "comment": r[3],
                    "createdAt": r[4],
                    "intent": r[5] or "",
                    "mode": r[6] or "",
                    "question": (content[0] if content else "") or "",
                    "reference_answer": (content[1] if content else "") or "",
                    "category": "feedback_candidate",
                    "design_branch": "数据中台反馈闭环候选，待人工补 expected_* 字段后入评测集",
                }
                lines.append(json.dumps(cand, ensure_ascii=False))
            data["count"] = len(lines)
            data["jsonl"] = "\n".join(lines)
        except Exception as exc:
            logger.error("金标集候选导出失败: %s", exc)
        return data

    def _cleanup_expired(self) -> None:
        """反馈域两张表的 TTL 清理（规约 §4.2：内容类与事件类都必须带定时清理）。

        两表期限不同、语义也不同，因此**分开配置、统一入口**：
        - `feedback_content`（内容表，含问答原文）：默认 30 天——它含正文，是最该短命的一张；
        - `agent_feedback`（事件表，只存元数据）：默认 90 天——它是评测金标集的起点，
          候选池要活得更久，且不含正文，风险面小得多。

        失败只记日志：清理是维护动作，绝不能因为它挂掉影响埋点主链路。
        """
        if not self._enabled or self._conn is None:
            return
        # (表名, 保留天数)——新增表只需在此加一行
        plans = (
            ("feedback_content", settings.feedback_content_ttl_days),
            ("agent_feedback", settings.feedback_ttl_days),
        )
        for table, raw_days in plans:
            try:
                days = max(1, int(raw_days))
                cur = self._conn.execute(
                    f"DELETE FROM {table} "  # noqa: S608 —— 表名为内部字面量，非用户输入
                    "WHERE created_at < datetime('now','localtime',?)", (f"-{days} days",))
                if cur.rowcount:
                    self._conn.commit()
                    logger.info("%s TTL 清理: 删除 %s 行（>%s 天）", table, cur.rowcount, days)
            except Exception as exc:
                logger.error("%s TTL 清理失败: %s", table, exc)

    # ---- P2：告警处置链路联动（口径见数据中台规约 §5 指标字典，2026-09-18 登记）----

    async def alarm_link(
        self, *, hours: int = 24, token: str | None = None, window_minutes: int = 30,
    ) -> dict[str, Any]:
        """委派(alarm-analyst) ↔ jsdstu 告警 联动视图。

        两级关联，口径已在指标字典登记：
        1. 精确：委派 ↔ 同 trace 的故障事件（trace_id join）；
        2. 近似：委派 ↔ jsdstu 告警，triggeredAt 落在委派时间 ±window_minutes 内
          （委派表无 alarm_id，此为登记过的近似口径，前端必须标注"近似关联"）。
        token 用看板访问者自己的身份透传，不引入系统账号。
        """
        data: dict[str, Any] = {
            "enabled": self._enabled, "hours": hours, "windowMinutes": window_minutes,
            "linked": False, "delegations": [], "alarms": [], "reason": "",
        }
        if not self._enabled or self._conn is None:
            data["reason"] = "telemetry disabled"
            return data
        hours = max(1, min(int(hours or 24), 24 * 90))
        window_minutes = max(1, min(int(window_minutes or 30), 240))
        try:
            delegations = [
                {"traceId": r[0], "sessionId": r[1], "agent": r[2], "status": r[3],
                 "durationMs": r[4], "createdAt": r[5]}
                for r in self._conn.execute(
                    "SELECT trace_id, session_id, agent, status, duration_ms, created_at "
                    "FROM agent_delegation WHERE agent = 'alarm-analyst' "
                    "AND created_at >= datetime('now','localtime',?) "
                    "ORDER BY id DESC LIMIT 100", (f"-{hours} hours",)).fetchall()
            ]
            for d in delegations:
                d["faults"] = [
                    {"path": fr[0], "detail": fr[1]} for fr in self._conn.execute(
                        "SELECT path, detail FROM fault_event WHERE trace_id = ? LIMIT 10",
                        (d["traceId"],)).fetchall()
                ]
            data["delegations"] = delegations
            if not token:
                data["reason"] = "未登录（需要 jsdstu 身份才能拉取告警列表）"
                return data
            from app.services.biz import jsdstu_client  # 延迟导入防环
            from datetime import datetime as _dt
            # 委派时间窗口 ±window：只拉这段范围，避免全量拉取
            times = [_dt.strptime(d["createdAt"].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                     for d in delegations if d.get("createdAt")]
            alarms: list[dict[str, Any]] = []
            seen_ids: set[Any] = set()

            def _collect(records: list[dict[str, Any]], *, linked: bool) -> None:
                """收编一批 jsdstu 告警；linked 标记它是否落在委派时间窗内。

                `source` 字段是 B1（看板处置入口）的必要信息：
                - "linked"     = 与某次委派时间相近，属**关联分析**结果；
                - "pending"    = 独立拉取的**待处置**告警，与委派无关。
                前端据此分组展示，避免把两类语义不同的行混在一起。
                """
                for r in records:
                    rid = r.get("id")
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    trig = str(r.get("triggeredAt") or "")
                    matched: list[str] = []
                    try:
                        t = _dt.strptime(trig.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        t = None
                    if t is not None:
                        for d, dt in zip(delegations, times):
                            if abs((t - dt).total_seconds()) <= window_minutes * 60:
                                matched.append(d["traceId"])
                    alarms.append({
                        "id": rid, "title": r.get("title"),
                        "severity": r.get("severity"), "status": r.get("status"),
                        "droneCode": r.get("droneCode"), "triggeredAt": trig,
                        "matchedTraceIds": matched,
                        "source": "linked" if matched else ("window" if linked else "pending"),
                    })

            if times:
                lo = (min(times) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
                hi = (max(times) + timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
                payload = await jsdstu_client.get(
                    "/v1/alarm/list", token=token,
                    params={"startTime": lo, "endTime": hi, "pageSize": 100})
                records = payload.get("list") if isinstance(payload, dict) else payload
                _collect(records or [], linked=True)

            # 独立拉取「待处置」告警：不能被委派窗口绑住。
            # 理由：看板 B1 的定位是**处置入口**——只要系统里还有 pending 告警，
            # 看板就该看得到并可发起处置，与"最近有没有触发过 alarm-analyst 委派"无关。
            # 原实现挂在 `if times:` 之下，导致无委派时明明有待处置告警也显示 0 条。
            pending = await jsdstu_client.get(
                "/v1/alarm/list", token=token,
                params={"status": "pending", "pageSize": 100})
            precords = pending.get("list") if isinstance(pending, dict) else pending
            _collect(precords or [], linked=False)

            data["alarms"] = alarms
            data["linked"] = True
        except Exception as exc:
            logger.error("告警联动查询失败: %s", exc)
            data["reason"] = f"告警联动失败: {exc}"
        return data

    # ---- P2：CSV 报表导出（规约 §8：导出属 ADS 消费，只读既有查询，不写新 SQL 规则）----

    _EXPORT_VIEWS = {
        "chats": ("会话明细", [
            ("traceId", "trace_id"), ("sessionId", "session_id"), ("intent", "intent"),
            ("mode", "mode"), ("ok", "ok"), ("degraded", "degraded"),
            ("durationMs", "duration_ms"), ("promptTokens", "prompt_tokens"),
            ("completionTokens", "completion_tokens"), ("createdAt", "created_at"),
        ]),
        "tools": ("工具调用", [
            ("tool", "tool"), ("total", "调用次数"), ("failed", "失败次数"),
            ("failRate", "失败率"), ("avgDurationMs", "平均耗时ms"),
        ]),
        "delegations": ("子智能体委派", [
            ("agent", "agent"), ("status", "status"), ("count", "次数"),
        ]),
        "faults": ("故障明细", [
            ("path", "path"), ("sessionId", "session_id"), ("detail", "detail"),
            ("createdAt", "created_at"),
        ]),
        "feedback": ("反馈明细", [
            ("traceId", "trace_id"), ("rating", "rating"), ("intent", "intent"),
            ("mode", "mode"), ("comment", "comment"), ("createdAt", "created_at"),
        ]),
        # v4：知识缺口主题表（补库清单可直接给知识库同学当工单）
        "unanswered": ("知识缺口主题", [
            ("topic", "主题"), ("count", "出现次数"), ("priority", "优先级"),
            ("suggestion", "补库建议"), ("sampleQuestions", "样例问题"),
        ]),
        "doc_usage": ("文档命中使用度", [
            ("docId", "docId"), ("hits", "命中次数"), ("lastHitAt", "最近命中时间"),
        ]),
    }

    def export_csv(self, *, view: str, hours: int = 24) -> dict[str, Any]:
        """CSV 报表导出（复用既有查询，utf-8 带 BOM 便于 Excel 直开）。"""
        data: dict[str, Any] = {"filename": "", "content": ""}
        if view not in self._EXPORT_VIEWS:
            data["reason"] = f"view 须为 {'/'.join(self._EXPORT_VIEWS)}"
            return data
        name, cols = self._EXPORT_VIEWS[view]
        hours = max(1, min(int(hours or 24), 24 * 90))
        if view == "tools":
            rows = self.tools_top(order_by="calls", limit=100, hours=hours)["rows"]
        elif view == "delegations":
            rows = self.overview().get("delegations24h", [])
        elif view == "faults":
            rows = self.overview().get("recentFaults", [])
        elif view == "feedback":
            rows = self.feedback_list(hours=hours, limit=200)["rows"]
        elif view == "unanswered":
            rows = self.unanswered(hours=hours, limit=200)["suggestions"]
        elif view == "doc_usage":
            rows = self.doc_usage(hours=hours, limit=2000)["rows"]
        else:
            rows = self.recent_chats(limit=100, hours=hours)["chats"]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([label for _, label in cols])
        for row in rows:
            out: list[Any] = []
            for key, _ in cols:
                v = row.get(key, "")
                if isinstance(v, bool):
                    v = 1 if v else 0
                elif isinstance(v, (list, tuple)):
                    v = " | ".join(str(x) for x in v)  # 样例问题/列表字段落地成可读文本
                elif isinstance(v, dict):
                    v = json.dumps(v, ensure_ascii=False)
                out.append(v)
            writer.writerow(out)
        day = datetime.now().strftime("%Y%m%d")
        data["filename"] = f"metrics_{view}_{day}.csv"
        # \ufeff BOM：Excel 识别 utf-8
        data["content"] = "\ufeff" + buf.getvalue()
        return data


telemetry = TelemetryStore()


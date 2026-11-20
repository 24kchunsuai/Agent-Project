"""SQLite 会话记忆存储（docs/specs/会话记忆规约.md §5.1 / §6 M-1，缺口 G10）。

为什么需要：原实现是纯内存 dict，`uvicorn` 一重启全部会话历史归零——
用户上一轮说的"3号无人机""刚才那个任务"下一轮模型完全不知道。

为什么用 SQLite 而不是参考项目的 `memory.md`：
open-agent-master 用「Markdown 正文 + `.idx` 索引」的方案，需要自己处理
索引一致性、按日期滚动、按大小拆分、并发 append 加锁四件事。本项目已有
`observability.db` 那套成熟的「建表 + 幂等迁移 + 后台批量落库」模式可复用，
用 SQLite 能省掉前三件麻烦事，且天然多进程安全（WAL）。

关键设计（照抄参考项目踩过的坑）：
1. **增量基准自维护**：每会话维护 `synced_turn`，**不依赖 SQL 聚合统计**。
   open-agent 的注释指出过真实坑——索引计数与实际数据不一致时会误判
   "无增量"从而永不写入。故本实现把基准放在内存 dict，重启时用实际查询初始化。
2. **三条件 OR 触发落盘**：累计轮次 ≥ 阈值 / 距上次落盘 ≥ 间隔。
   既不会每轮写盘（IO 放大），也不会长时间不落盘（丢数据窗口可控）。
3. **降级不中断**：SQLite 打开/写入异常 → record_fault + 自动退回内存，
   主链路绝不因记忆故障失败（规约 §9 铁律 1）。

线程模型：FastAPI 单进程 + 多线程（sync 端点跑在线程池），故用
`check_same_thread=False` + `threading.Lock` 串行化写操作；读走同一连接。
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

from app.core.config import settings
from app.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory_message(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    turn_index  INTEGER NOT NULL,
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_session_ts ON agent_memory_message(session_id, ts);

CREATE TABLE IF NOT EXISTS agent_memory_summary(
    session_id   TEXT PRIMARY KEY,
    summary      TEXT NOT NULL,
    covered_turn INTEGER NOT NULL,
    updated_ts   INTEGER NOT NULL
);
"""


class MemoryUnavailable(RuntimeError):
    """SQLite 记忆库不可用（由调用方决定降级策略）。"""


class SqliteMemoryStore:
    """SQLite 持久化记忆实现。接口与 `SessionMemory` 完全一致（可互换）。

    额外提供 `flush()` / `load_all_session_ids()` 供 lifespan 的启动回放与
    关闭冲刷使用。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or self._default_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        # 每会话**已落盘的消息条数**（自维护增量基准，见模块 docstring 设计点 1）。
        #
        # ⚠️ 为什么基准按「条数」而非「轮数」：早期实现按轮数（消息数//2）维护，
        # 而 record_turn 是「先 append(user) 再 append(assistant)」两次调用——
        # 第一次 append 后待落盘轮数就可能达阈值并立即落盘，此时 assistant 还没写；
        # 落盘把 synced 推进到 N 轮，第二次 append 的 assistant 就被算进第 N+1 轮，
        # 与下一条 user 错位（实测库里出现 [q1,a1,q2,q2,q3,a3]，a2 被覆盖）。
        # 改为按条数维护后，基准与切片单位一致，不存在错位。
        self._synced_msgs: dict[str, int] = {}
        # 每会话待落盘的**轮数**（三条件判据之一，按成对写入计）
        self._pending_turns: dict[str, int] = {}
        self._last_sync: dict[str, float] = {}
        # 会话内最近消息缓存（读路径避免每轮查库）
        self._cache: dict[str, list[dict]] = {}
        # 超过内部缓存上限的会话数（防内存膨胀，与旧实现的 max_sessions 语义对齐）
        self._cache_order: list[str] = []
        self._degraded = False
        self._open()

    # ---- 生命周期 ----

    def _default_path(self) -> Path:
        return DATA_DIR / "memory" / "session_memory.db"

    def _open(self) -> None:
        """打开连接并建表；失败则标记降级（不抛给调用方）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            # WAL：读写不互斥，掉电恢复更好
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
            logger.info("会话记忆库已就绪 db=%s", self._path)
        except Exception as exc:  # noqa: BLE001 —— 记忆故障绝不阻断主链路
            self._degraded = True
            self._conn = None
            logger.warning("会话记忆库打开失败，降级为内存模式: %s", exc)
            self._record_fault("打开失败", str(exc))

    @property
    def degraded(self) -> bool:
        """是否已降级为仅内存（观测用）。"""
        return self._degraded or self._conn is None

    @property
    def db_path(self) -> Path:
        return self._path

    def _record_fault(self, stage: str, detail: str) -> None:
        """降级计数（规约 §9 铁律 3：降级必须计数）。import 放函数内避免循环依赖。"""
        try:
            from app.services.telemetry import PATH_MEMORY_SQLITE_DEGRADED, telemetry

            telemetry.record_fault(
                PATH_MEMORY_SQLITE_DEGRADED, detail=f"记忆库{stage}：{detail}"
            )
        except Exception as exc:  # noqa: BLE001 —— 埋点自身失败也不能影响记忆
            logger.debug("记忆降级计数写入失败: %s", exc)

    # ---- 读 ----

    def get(self, session_id: str) -> list[dict[str, str]]:
        """取该会话全部历史（时间升序）。

        注意：**返回全量**，不再做 `window` 裁剪——裁剪已上移到
        `memory_trim.trim_history()`（按 token 预算 + 双锚点），
        存储层只管存取，职责单一。
        """
        cached = self._cache.get(session_id)
        if cached is not None:
            return list(cached)

        if self.degraded:
            return []

        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT role, content FROM agent_memory_message "
                    "WHERE session_id=? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话记忆读取失败 session=%s: %s", session_id, exc)
            self._record_fault("读取失败", str(exc))
            return []

        messages = [{"role": r[0], "content": r[1]} for r in rows]
        self._put_cache(session_id, messages)
        # 用实际行数初始化增量基准（重启后的第一次读取，见设计点 1）
        self._synced_msgs.setdefault(session_id, len(messages))
        return list(messages)

    def has(self, session_id: str) -> bool:
        return bool(self.get(session_id))

    def get_summary(self, session_id: str) -> str:
        """取该会话的累积摘要（递归压缩用）；无则空串。"""
        if self.degraded:
            return ""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT summary FROM agent_memory_summary WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            return row[0] if row else ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("摘要读取失败 session=%s: %s", session_id, exc)
            return ""

    # ---- 写 ----

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息到**内存缓存**并计入待落盘（真正的写库在 flush）。

        注意：**单条 append 不触发落盘**——落盘判定只在 `record_turn` 的轮边界做，
        否则 user/assistant 之间落盘会导致基准错位（见 `__init__` 的踩坑注释）。
        """
        if role not in ("user", "assistant") or not content:
            return
        messages = self._cache.get(session_id)
        if messages is None:
            # 首次写入：先尝试从库回放，保证"读改写"语义正确
            messages = self.get(session_id)
            self._put_cache(session_id, messages)
            self._synced_msgs.setdefault(session_id, len(messages))

        messages.append({"role": role, "content": content})

    def record_turn(self, session_id: str, user_message: str, assistant_reply: str) -> None:
        """把一轮完整对话（用户 + 助手）成对写入，并在**轮边界**判断是否落盘。"""
        self.append(session_id, "user", user_message)
        self.append(session_id, "assistant", assistant_reply)

        # 轮边界才更新待落盘轮数并判阈值——保证不会在 user/assistant 之间切分
        messages = self._cache.get(session_id) or []
        synced = self._synced_msgs.get(session_id, 0)
        pending = max(0, (len(messages) - synced) // 2)
        if pending:
            self._pending_turns[session_id] = pending

        if self._should_flush(session_id):
            self.flush(session_id)

    # ---- 落盘 ----

    def _should_flush(self, session_id: str) -> bool:
        """三条件 OR：轮次达阈值 / 距上次落盘达间隔。

        ⚠️ 踩坑记录：`_last_sync` 缺省值**必须是"当前时刻"而非 0.0**。
        用 0.0 时 `time.time() - 0.0` 是巨大正数，必然 ≥ 间隔阈值，
        导致首次 append 就立即落盘；随后 `_synced_msgs` 推进，
        而第二次 append 又触发一次落盘 → **同一条消息被写两遍**。
        """
        if self._pending_turns.get(session_id, 0) >= int(settings.memory_sync_round_threshold):
            return True
        last = self._last_sync.get(session_id)
        if last is None:
            # 本会话首次写入：记下时刻，本轮不因"间隔条件"落盘
            self._last_sync[session_id] = time.time()
            return False
        return (time.time() - last) >= int(settings.memory_sync_interval_seconds)

    def flush(self, session_id: str | None = None) -> None:
        """把待落盘的增量写入 SQLite。

        Args:
            session_id: 指定会话；None = 冲刷全部待落盘会话（关停路径用）
        """
        if self.degraded:
            return
        if session_id is not None:
            self._flush_one(session_id)
            return
        for sid in list(self._pending_turns.keys()):
            self._flush_one(sid)

    def _flush_one(self, session_id: str) -> None:
        """落盘单会话的增量。

        增量基准取 `_synced_msgs`（自维护、按条数）而非 SQL 计数——
        这是参考项目明确踩过的坑，见模块 docstring 设计点 1。
        """
        if self.degraded:
            return
        messages = self._cache.get(session_id)
        if not messages:
            return

        synced = self._synced_msgs.get(session_id, 0)
        if synced >= len(messages):
            self._pending_turns[session_id] = 0
            return

        new_msgs = messages[synced:]
        now = int(time.time())
        try:
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO agent_memory_message(session_id, role, content, turn_index, ts) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (session_id, m["role"], m["content"], (synced + i) // 2, now)
                        for i, m in enumerate(new_msgs)
                    ],
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001 —— 落盘失败只降级，不影响已服务的请求
            logger.warning("会话记忆落盘失败 session=%s: %s", session_id, exc)
            self._record_fault("落盘失败", str(exc))
            return

        self._synced_msgs[session_id] = len(messages)
        self._pending_turns[session_id] = 0
        self._last_sync[session_id] = time.time()
        logger.debug("会话记忆落盘 session=%s 新增 %d 条", session_id, len(new_msgs))

    def save_summary(self, session_id: str, summary: str, covered_turn: int) -> None:
        """保存累积摘要（递归压缩的载体）。"""
        if self.degraded or not summary:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO agent_memory_summary(session_id, summary, covered_turn, updated_ts) "
                    "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                    "summary=excluded.summary, covered_turn=excluded.covered_turn, "
                    "updated_ts=excluded.updated_ts",
                    (session_id, summary, covered_turn, int(time.time())),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("摘要保存失败 session=%s: %s", session_id, exc)

    # ---- 启动回放 / 清理 ----

    def load_all_session_ids(self, limit: int = 0) -> list[str]:
        """列出库中所有会话 id（按最近活动倒序）。

        启动回放用。**只返回 id，不加载全部消息**——回放时按 token 预算
        只取最近若干轮，避免启动阶段把全库读进内存。
        """
        if self.degraded:
            return []
        try:
            with self._lock:
                sql = (
                    "SELECT session_id, MAX(ts) AS t FROM agent_memory_message "
                    "GROUP BY session_id ORDER BY t DESC"
                )
                if limit:
                    sql += f" LIMIT {int(limit)}"
                rows = self._conn.execute(sql).fetchall()
            return [r[0] for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("列出记忆会话失败: %s", exc)
            return []

    def purge_expired(self, ttl_days: int | None = None) -> int:
        """清理超过 TTL 的消息，返回删除条数。

        只由产品内定时任务调用（规约 §9 铁律 4：不擅自清理数据）。
        """
        if self.degraded:
            return 0
        days = ttl_days if ttl_days is not None else int(settings.memory_ttl_days)
        cutoff = int(time.time()) - days * 86400
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM agent_memory_message WHERE ts < ?", (cutoff,)
                )
                self._conn.commit()
                removed = cur.rowcount or 0
            if removed:
                logger.info("会话记忆 TTL 清理: 删除 %d 条（早于 %d 天前）", removed, days)
            return removed
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话记忆 TTL 清理失败: %s", exc)
            return 0

    # ---- 缓存管理 ----

    def _put_cache(self, session_id: str, messages: list[dict]) -> None:
        """写入读缓存，超上限淘汰最久未访问的会话。"""
        if session_id not in self._cache:
            self._cache_order.append(session_id)
        self._cache[session_id] = messages
        limit = max(1, int(settings.memory_max_sessions))
        while len(self._cache_order) > limit:
            oldest = self._cache_order.pop(0)
            if oldest != session_id:
                # 淘汰前先落盘，避免缓存里的增量丢失
                self._flush_one(oldest)
                self._cache.pop(oldest, None)
                self._pending_turns.pop(oldest, None)
                self._synced_msgs.pop(oldest, None)
                self._last_sync.pop(oldest, None)

    def stats(self) -> dict[str, int]:
        return {"sessions": len(self._cache), "mode": 0 if not self.degraded else 1}

    def clear(self) -> None:
        """清空全部会话（测试隔离用；不属业务路径）。

        ⚠️ 只删 DB 行不够——`_synced_msgs`/`_pending_turns` 等基准若不重置，
        后续写入会被"已同步"状态吞掉，表现为 clear 之后再写也不落盘。
        """
        if self._conn is not None:
            try:
                self._conn.execute("DELETE FROM agent_memory_message")
                self._conn.execute("DELETE FROM agent_memory_summary")
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                self._record_fault("清空失败", str(exc))
                return
        self._synced_msgs.clear()
        self._pending_turns.clear()
        self._last_sync.clear()
        self._cache.clear()
        self._cache_order.clear()

    def close(self) -> None:
        """关闭连接（lifespan 关停路径调用，先 flush 再 close）。"""
        self.flush()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


# 模块级单例（懒加载：由 memory.get_memory_store() 决定是否实例化）
_store: SqliteMemoryStore | None = None
_store_lock = threading.Lock()


def get_sqlite_store() -> SqliteMemoryStore:
    """取全局 SQLite 记忆实例（进程内单例）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = SqliteMemoryStore()
        return _store

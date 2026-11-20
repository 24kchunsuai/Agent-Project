"""治理元数据存储适配层。

定位（spec §13.5 强制设计约束）
------------------------------
业务逻辑**不直接拼 SQL**，只调本模块暴露的仓储方法。将来治理域满足迁移触发条件
（Java 要直查 / 与中台数据 join / Python 多实例 / 治理动作转高频）时，只需替换本模块
的实现（SQLite → MySQL），service 层与 API 层零改动。

因此本模块的两条自我约束：
1. 表结构与字段命名**按 MySQL 规范预留**：文本长度显式声明、时间统一 ISO 字符串存 TEXT、
   自增主键写法与 MySQL 对齐（`INTEGER PRIMARY KEY AUTOINCREMENT` → `INT AUTO_INCREMENT`）
2. 对外暴露的方法签名只用通用类型（str/int/float/list/dict），不泄漏 sqlite3 细节

并发模型
--------
单机场景：一个进程内共享一条连接（`check_same_thread=False`）+ RLock 串行化，
配合 SQLite `WAL` 模式。知识库写操作本身已有 `KbService._write_lock` 串行化，
定时任务与 API 写又都经过本模块的同一把锁，不会出现多写者竞争。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

from app.core.paths import DATA_DIR                 # noqa: E402  数据根目录（DATA_ROOT 可覆盖）

ROOT = Path(__file__).resolve().parents[2]          # agt_python/
# 治理账本默认路径：随数据根目录走（测试隔离见 app/core/paths.py）
DEFAULT_DB_PATH = DATA_DIR / "governance/governance.db"

# ---------------------------------------------------------------- 表结构
# 与 spec §3 一一对应；tickets 属 G4，此处先建表（幂等），避免后续再加迁移
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS docs (
      doc_id          TEXT    PRIMARY KEY,
      doc_name        TEXT    NOT NULL DEFAULT '',
      file_hash       TEXT,
      version_no      INTEGER NOT NULL DEFAULT 1,
      owner           TEXT    NOT NULL DEFAULT '',
      category        TEXT    NOT NULL DEFAULT '',
      visibility      TEXT    NOT NULL DEFAULT 'public',
      state           TEXT    NOT NULL DEFAULT 'ACTIVE',
      vector_present  INTEGER NOT NULL DEFAULT 1,
      chunk_count     INTEGER NOT NULL DEFAULT 0,
      char_count      INTEGER NOT NULL DEFAULT 0,
      created_at      TEXT    NOT NULL DEFAULT '',
      updated_at      TEXT    NOT NULL DEFAULT '',
      recycled_at     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_docs_file_hash ON docs(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_docs_state ON docs(state)",
    """
    CREATE TABLE IF NOT EXISTS chunks (
      chunk_hash  TEXT    NOT NULL,
      doc_id      TEXT    NOT NULL,
      chunk_index INTEGER NOT NULL,
      char_len    INTEGER NOT NULL DEFAULT 0,
      created_at  TEXT    NOT NULL DEFAULT '',
      PRIMARY KEY (chunk_hash, doc_id, chunk_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(chunk_hash)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         TEXT    NOT NULL,
      operator   TEXT    NOT NULL,
      action     TEXT    NOT NULL,
      doc_id     TEXT,
      doc_name   TEXT,
      version_no INTEGER,
      detail     TEXT,
      result     TEXT    NOT NULL DEFAULT 'SUCCESS'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)",
    "CREATE INDEX IF NOT EXISTS idx_audit_doc ON audit_log(doc_id)",
    """
    CREATE TABLE IF NOT EXISTS tickets (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      ticket_type      TEXT    NOT NULL,
      doc_id           TEXT    NOT NULL,
      doc_name         TEXT,
      similarity_score REAL,
      matched_doc_id   TEXT,
      status           TEXT    NOT NULL DEFAULT 'OPEN',
      note             TEXT,
      handled_at       TEXT,
      created_at       TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_doc ON tickets(doc_id)",
)

# --------------------------------------------------------- v5（E-8 操作审计留痕）
# 审计表补齐**统一维度**。规约要求的 `actor / action / target / result / ip / traceId / at`
# 与既有表的对应关系：
#   actor ≡ operator（历史命名；改名会破坏既有查询与前端，故保留并在此声明等价）
#   at    ≡ ts；action / result 同名
# 缺的是 **target / ip / trace_id**，用 ALTER TABLE 补列（**只加列不重建**，见数据库规约 §2.6）。
#
# `target` 是给"非文档类动作"用的统一指向（此前只有 doc_id/doc_name，登录、备份这类
# 动作无处可放）。约定写法：
#   doc:<docId> / tool:<工具名> / user:<用户名> / backup / config:<配置项> / service:<服务名>
# 有了它，登录、确认门、备份才能和文档治理动作放在同一张表里按时间序查。
AUDIT_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("target", "TEXT"),
    ("ip", "TEXT"),
    ("trace_id", "TEXT"),
)

AUDIT_EXTRA_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target)",
    "CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id)",
)

# `target` 的**读取期派生**表达式（E-8）。
#
# 为什么要派生：E-8 之前的审计行 `target` 是 NULL（那时还没有这一列），
# 但它们都有 `doc_id`——语义上等价于 `doc:<doc_id>`。直接 `UPDATE` 回填历史数据
# 会动到"只增不改"的审计表，代价远大于收益；改成**读取时算**：
#   - 库里仍是 NULL（历史数据一个字节没动，可解释、可回滚）
#   - 显示与筛选**用同一个表达式**，不会出现"筛出来了但对象列写着别的"
# 老库的真实表现：475 行历史数据里，有 doc_id 的那些现在也能被 `target=doc:` 筛到。
_AUDIT_TARGET_EXPR = (
    "COALESCE(target, CASE WHEN doc_id IS NOT NULL AND doc_id <> '' "
    "THEN 'doc:' || doc_id END)"
)

# 显式列清单（不用 `SELECT *`）：① 需要给 target 起别名派生；
# ② 表会随版本加列，显式列出才能保证"返回结构"是刻意决定的而不是随表漂移
_AUDIT_SELECT = (
    "SELECT id, ts, operator, action, doc_id, doc_name, version_no, detail, result, "
    f"{_AUDIT_TARGET_EXPR} AS target, ip, trace_id FROM audit_log"
)


def now_iso() -> str:
    """当前时间（ISO 8601，秒级）——统一时间格式，便于迁 MySQL 时无需转换。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def days_ago_iso(days: int) -> str:
    """N 天前的时间戳（与 `now_iso` 同格式）。

    格式统一带来一个实用性质：**ISO 秒级字符串的字典序即时间先后**，
    于是 SQL 里可以直接 `recycled_at <= ?` / `updated_at <= ?` 做时间过滤，
    不必引入 datetime 列类型（迁 MySQL 时这层写法保持不变）。
    """
    return (datetime.now() - timedelta(days=max(0, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


@runtime_checkable
class GovernanceRepository(Protocol):
    """治理仓储接口。

    业务层只依赖本协议；`SqliteGovernanceStore` 是当前唯一实现。
    """

    def ensure_schema(self) -> None: ...

    def get_doc(self, doc_id: str) -> dict[str, Any] | None: ...

    def find_doc_by_hash(self, file_hash: str, exclude_doc_id: str | None = None) -> dict[str, Any] | None: ...

    def register_upload(self, **kwargs: Any) -> dict[str, Any]: ...

    def set_state(self, doc_id: str, state: str, *, vector_present: bool,
                  recycled_at: str | None = None) -> bool: ...

    def set_version(self, doc_id: str, version_no: int) -> bool: ...

    def set_vector_expectation(self, doc_id: str, present: bool) -> bool: ...

    def delete_doc(self, doc_id: str) -> None: ...

    def list_docs(self, *, state: str | None = None, limit: int = 50,
                  offset: int = 0) -> tuple[list[dict[str, Any]], int]: ...

    def count_docs_by_state(self) -> dict[str, int]: ...

    def list_doc_briefs(self) -> list[dict[str, Any]]: ...

    def replace_chunks(self, doc_id: str, rows: Sequence[tuple[int, str, int]]) -> int: ...

    def delete_chunks(self, doc_id: str) -> int: ...

    def find_duplicate_chunks(self, hashes: Sequence[str],
                              exclude_doc_id: str) -> dict[str, tuple[str, int]]: ...

    def chunk_stats(self) -> dict[str, Any]: ...

    def top_duplicate_hashes(self, limit: int = 10) -> list[dict[str, Any]]: ...

    def log_audit(self, *, operator: str, action: str, doc_id: str | None = None,
                  doc_name: str | None = None, version_no: int | None = None,
                  detail: str | None = None, result: str = "SUCCESS",
                  target: str | None = None, ip: str | None = None,
                  trace_id: str | None = None) -> None: ...

    def query_audit(self, *, operator: str | None = None, action: str | None = None,
                    doc_id: str | None = None, target: str | None = None,
                    from_ts: str | None = None,
                    to_ts: str | None = None, limit: int = 20,
                    offset: int = 0) -> tuple[list[dict[str, Any]], int]: ...

    def recent_audit(self, limit: int = 10) -> list[dict[str, Any]]: ...

    # ---- G4：工单与巡检/TTL 的候选查询 ----

    def create_ticket(self, *, ticket_type: str, doc_id: str, doc_name: str | None = None,
                      similarity_score: float | None = None,
                      matched_doc_id: str | None = None,
                      note: str | None = None) -> int | None: ...

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None: ...

    def list_tickets(self, *, status: str | None = None, ticket_type: str | None = None,
                     limit: int = 50, offset: int = 0
                     ) -> tuple[list[dict[str, Any]], int]: ...

    def count_tickets_by_status(self) -> dict[str, int]: ...

    def update_ticket(self, ticket_id: int, *, status: str | None = None,
                      note: str | None = None, handled_at: str | None = None) -> bool: ...

    def last_handled_ticket(self, *, ticket_type: str, doc_id: str,
                            status: str) -> dict[str, Any] | None: ...

    def list_recycled_expired(self, *, before_ts: str,
                              limit: int = 50) -> list[dict[str, Any]]: ...

    def list_stale_docs(self, *, before_ts: str,
                        limit: int = 50) -> list[dict[str, Any]]: ...

    def find_low_quality_docs(self, *, min_chars: int,
                              limit: int = 50) -> list[dict[str, Any]]: ...


class SqliteGovernanceStore:
    """SQLite 实现（进程本地单文件库）。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ 基础设施

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connection(self) -> sqlite3.Connection:
        """懒加载连接（首次调用时建目录 + 建表）。

        `check_same_thread=False` 是必需的：FastAPI 的同步端点跑在线程池里，
        连接会被不同线程使用；并发安全由 `self._lock` 保证。
        """
        if self._conn is not None:
            return self._conn
        with self._lock:
            if self._conn is None:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self._db_path), timeout=10.0,
                                       check_same_thread=False)
                conn.row_factory = sqlite3.Row
                # WAL：读不阻塞写；治理域是低并发小表，足够
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._conn = conn
                self.ensure_schema()
                logger.info("治理库就绪 | path=%s", self._db_path)
        return self._conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn or sqlite3.connect(
                str(self._db_path), timeout=10.0, check_same_thread=False)
            try:
                # 直接调本方法（未经 `_connection()`）时新建的连接默认是 tuple 行，
                # 而下面的 `_ensure_audit_columns` 要按列名取值 → 必须显式对齐。
                # 这个分支真实存在：迁移脚本 / 预检会先建表再读数据。
                if self._conn is None:
                    conn.row_factory = sqlite3.Row
                for stmt in SCHEMA_STATEMENTS:
                    conn.execute(stmt)
                self._ensure_audit_columns(conn)
                for stmt in AUDIT_EXTRA_INDEXES:
                    conn.execute(stmt)
                conn.commit()
            finally:
                if self._conn is None:
                    conn.close()

    @staticmethod
    def _ensure_audit_columns(conn: sqlite3.Connection) -> None:
        """给**已存在**的 `audit_log` 补 v5 新列（幂等）。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表不会加列，所以升级路径必须显式
        `ALTER TABLE ADD COLUMN`。先 `PRAGMA table_info` 查再补，重复执行安全。
        """
        # 按下标取列名（`table_info` 的第 2 列），不依赖 row_factory——
        # 本方法可能被"自建连接"的调用方复用，行对象形态不保证
        existing = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")}
        for col, decl in AUDIT_EXTRA_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col} {decl}")

    @staticmethod
    def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------ docs

    def get_doc(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._connection().execute(
                "SELECT * FROM docs WHERE doc_id = ?", (doc_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def find_doc_by_hash(self, file_hash: str,
                         exclude_doc_id: str | None = None) -> dict[str, Any] | None:
        """按文件指纹找"内容已存在"的文档。

        只比对非回收站文档（RECYCLED 的内容已从向量库物理移除，
        重新上传应被允许）。`state != 'RECYCLED'` 还能保证命中的文档确实在库里。
        """
        if not file_hash:
            return None
        sql = ("SELECT * FROM docs WHERE file_hash = ? AND state <> 'RECYCLED'")
        args: list[Any] = [file_hash]
        if exclude_doc_id:
            sql += " AND doc_id <> ?"
            args.append(exclude_doc_id)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        with self._lock:
            row = self._connection().execute(sql, args).fetchone()
            return dict(row) if row else None

    def register_upload(
        self,
        *,
        doc_id: str,
        doc_name: str,
        file_hash: str | None,
        owner: str,
        category: str = "",
        visibility: str = "public",
        chunk_rows: Sequence[tuple[int, str, int]] = (),
        chunk_count: int = 0,
        char_count: int = 0,
        state: str = "ACTIVE",
        vector_present: bool = True,
        version_no: int | None = None,
        bump_version: bool = False,
    ) -> dict[str, Any]:
        """登记/更新一篇文档及其切片指纹（同一事务）。

        - 文档行按 doc_id upsert；切片行**先清后插**（同名覆盖时旧版指纹被替换，
          避免用过期指纹误拦新上传）
        - 版本号（G3）三选一：`version_no` 显式指定 > `bump_version` 递增 > 保留旧值。
          默认保留旧值，于是"恢复/回滚回灌"这类**不是新内容**的写入不会凭空涨版本号；
          只有真正的"同名上传新内容"才 `bump_version=True`
        """
        ts = now_iso()
        with self._lock:
            conn = self._connection()
            with conn:  # 事务：失败整体回滚，不留半截指纹
                # 版本号必须在**锁内**读旧值再算：两个并发上传若各自先读后写，
                # 会算出同一个目标版本号，用户看到"传了两次却只有 v2"
                row = conn.execute(
                    "SELECT version_no FROM docs WHERE doc_id = ?", (doc_id,)).fetchone()
                if version_no is not None:
                    target = int(version_no)
                elif row is None:
                    target = 1
                elif bump_version:
                    target = int(row["version_no"] or 1) + 1
                else:
                    target = int(row["version_no"] or 1)
                conn.execute(
                    """
                    INSERT INTO docs (doc_id, doc_name, file_hash, version_no, owner,
                                      category, visibility, state, vector_present,
                                      chunk_count, char_count, created_at, updated_at, recycled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(doc_id) DO UPDATE SET
                      doc_name       = excluded.doc_name,
                      file_hash      = excluded.file_hash,
                      version_no     = excluded.version_no,
                      owner          = excluded.owner,
                      category       = excluded.category,
                      visibility     = excluded.visibility,
                      state          = excluded.state,
                      vector_present = excluded.vector_present,
                      chunk_count    = excluded.chunk_count,
                      char_count     = excluded.char_count,
                      updated_at     = excluded.updated_at,
                      recycled_at    = NULL
                    """,
                    (doc_id, doc_name, file_hash, target, owner, category, visibility,
                     state, 1 if vector_present else 0, chunk_count, char_count,
                     ts, ts),
                )
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                if chunk_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO chunks "
                        "(chunk_hash, doc_id, chunk_index, char_len, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(h, doc_id, idx, clen, ts) for idx, h, clen in chunk_rows],
                    )
        return self.get_doc(doc_id) or {}

    def set_state(self, doc_id: str, state: str, *, vector_present: bool,
                  recycled_at: str | None = None) -> bool:
        """更新治理状态。`vector_present` 是**对账标记**：SQLite 认为向量库是否应有该文档。"""
        with self._lock:
            conn = self._connection()
            with conn:
                cur = conn.execute(
                    "UPDATE docs SET state = ?, vector_present = ?, recycled_at = ?, "
                    "updated_at = ? WHERE doc_id = ?",
                    (state, 1 if vector_present else 0, recycled_at, now_iso(), doc_id),
                )
            return cur.rowcount > 0

    def set_version(self, doc_id: str, version_no: int) -> bool:
        """显式设置版本号（G3 回滚：回滚版升为"历史最大值 + 1"）。

        与 `set_state` 分开，是因为回滚要写一个**既不是"旧值"也不是"旧值+1"**的版本号
        （目标可能是 v1 而当前是 v4，新版本号必须是 max(v1..v4)+1 = 5）——
        把这种跳跃逻辑塞进 set_state 的隐式推断里，早晚会有人误用。
        """
        with self._lock:
            conn = self._connection()
            with conn:
                cur = conn.execute(
                    "UPDATE docs SET version_no = ?, updated_at = ? WHERE doc_id = ?",
                    (int(version_no), now_iso(), doc_id),
                )
            return cur.rowcount > 0

    def set_vector_expectation(self, doc_id: str, present: bool) -> bool:
        """只翻转"期望向量存在"标记，**不触碰状态与时间戳**（返回是否有行被更新）。

        用途（G2）：向量写入/删除之前先落"期望"，让对账任务的判据先于事实变化——
        否则"向量已写、期望仍为无"的窗口里，对账会把刚写好的向量当残留删掉（实测踩到）。
        行不存在时返回 False（新文档还没有账本，对账会把它算作"未知"而只告警不删）。
        """
        with self._lock:
            conn = self._connection()
            with conn:
                cur = conn.execute(
                    "UPDATE docs SET vector_present = ? WHERE doc_id = ?",
                    (1 if present else 0, doc_id),
                )
            return cur.rowcount > 0

    def delete_doc(self, doc_id: str) -> None:
        """彻底删除：文档登记行与切片指纹一并清除（审计日志永不删除）。"""
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def list_docs(self, *, state: str | None = None, limit: int = 50,
                  offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where, args = "", []
        if state:
            where = " WHERE state = ?"
            args.append(state)
        with self._lock:
            conn = self._connection()
            total = conn.execute(f"SELECT COUNT(*) AS c FROM docs{where}", args).fetchone()["c"]
            rows = self._rows(conn.execute(
                f"SELECT * FROM docs{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                [*args, limit, offset]))
        return rows, int(total)

    def count_docs_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._rows(self._connection().execute(
                "SELECT state, COUNT(*) AS c FROM docs GROUP BY state"))
        return {str(r["state"]): int(r["c"]) for r in rows}

    def list_doc_briefs(self) -> list[dict[str, Any]]:
        """全部文档的轻量视图（不给正文/指纹）：对账任务做"期望 vs 实际"集合比对用。"""
        with self._lock:
            return self._rows(self._connection().execute(
                "SELECT doc_id, doc_name, state, vector_present, chunk_count, "
                "updated_at FROM docs"))

    # ------------------------------------------------------------ chunks

    def replace_chunks(self, doc_id: str, rows: Sequence[tuple[int, str, int]]) -> int:
        ts = now_iso()
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO chunks "
                        "(chunk_hash, doc_id, chunk_index, char_len, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(h, doc_id, idx, clen, ts) for idx, h, clen in rows],
                    )
        return len(rows)

    def delete_chunks(self, doc_id: str) -> int:
        with self._lock:
            conn = self._connection()
            with conn:
                cur = conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            return cur.rowcount

    def find_duplicate_chunks(self, hashes: Sequence[str],
                              exclude_doc_id: str) -> dict[str, tuple[str, int]]:
        """L2：给定切片指纹，找出**其它文档**中已存在的那些。

        返回 `{chunk_hash: (源 doc_id, 源 chunk_index)}`，供调用方按
        `chunk_id = f"{doc_id}#{index}"` 回捞源切片的向量，实现"跳过 embedding"。

        两点排除：
        - 同一 doc_id（自身旧版）不算重复——同名覆盖是正常更新
        - RECYCLED 文档的指纹不算——其内容已从向量库物理移除，且无法回捞向量

        排序：**优先返回切片序号有效的行**（`chunk_index >= 0`）。回填来的历史文档若
        切片数与当前切片器不一致，其指纹以 `chunk_index = -1` 登记（只参与检测、不参与
        向量复用），把它们排在后面可以优先拿到"可用于复用"的源切片。
        """
        uniq = [h for h in dict.fromkeys(hashes) if h]
        if not uniq:
            return {}
        found: dict[str, tuple[str, int]] = {}
        with self._lock:
            conn = self._connection()
            for i in range(0, len(uniq), 200):     # 分块，避免 SQL 变量过多
                part = uniq[i : i + 200]
                marks = ",".join("?" * len(part))
                rows = self._rows(conn.execute(
                    f"""
                    SELECT c.chunk_hash AS h, c.doc_id AS d, c.chunk_index AS i
                    FROM chunks c
                    JOIN docs   x ON x.doc_id = c.doc_id
                    WHERE c.chunk_hash IN ({marks})
                      AND c.doc_id <> ?
                      AND x.state <> 'RECYCLED'
                    ORDER BY CASE WHEN c.chunk_index >= 0 THEN 0 ELSE 1 END,
                             c.created_at ASC
                    """,
                    [*part, exclude_doc_id],
                ))
                for r in rows:
                    found.setdefault(str(r["h"]), (str(r["d"]), int(r["i"])))
        return found

    def chunk_stats(self) -> dict[str, Any]:
        """切片指纹统计：总行数 / 唯一指纹数 → 冗余率。"""
        with self._lock:
            conn = self._connection()
            total = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
            uniq = conn.execute(
                "SELECT COUNT(DISTINCT chunk_hash) AS c FROM chunks").fetchone()["c"]
            dup_docs = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                  SELECT chunk_hash FROM chunks GROUP BY chunk_hash
                  HAVING COUNT(DISTINCT doc_id) > 1
                )
                """).fetchone()["c"]
        total, uniq = int(total), int(uniq)
        return {
            "chunkRows": total,
            "uniqueHashes": uniq,
            "duplicateRows": total - uniq,
            "duplicateRate": round((total - uniq) / total, 4) if total else 0.0,
            "crossDocHashes": int(dup_docs),
        }

    def top_duplicate_hashes(self, limit: int = 10) -> list[dict[str, Any]]:
        """跨文档重复最严重的切片指纹（冗余率报告用）。

        返回 `[{chunkHash, docCount, docIds}]`，按涉及文档数降序。
        """
        with self._lock:
            conn = self._connection()
            rows = self._rows(conn.execute(
                """
                SELECT chunk_hash AS h, COUNT(DISTINCT doc_id) AS d,
                       GROUP_CONCAT(DISTINCT doc_id) AS docs
                FROM chunks
                GROUP BY chunk_hash
                HAVING COUNT(DISTINCT doc_id) > 1
                ORDER BY d DESC, h ASC
                LIMIT ?
                """, (limit,)))
        return [
            {"chunkHash": r["h"], "docCount": int(r["d"]),
             "docIds": str(r["docs"] or "").split(",")[:10]}
            for r in rows
        ]

    # ------------------------------------------------------------ audit

    def log_audit(self, *, operator: str, action: str, doc_id: str | None = None,
                  doc_name: str | None = None, version_no: int | None = None,
                  detail: str | None = None, result: str = "SUCCESS",
                  target: str | None = None, ip: str | None = None,
                  trace_id: str | None = None) -> None:
        # target 缺省时用 doc_id 兜底：治理域的老调用点不必逐个改造，
        # 非文档类动作（登录/确认门/备份）显式传 target（见 AUDIT_EXTRA_COLUMNS 注释）
        resolved_target = target or (f"doc:{doc_id}" if doc_id else None)
        with self._lock:
            conn = self._connection()
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, operator, action, doc_id, doc_name, "
                    "version_no, detail, result, target, ip, trace_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_iso(), operator or "system", action, doc_id, doc_name,
                     version_no, detail, result, resolved_target, ip, trace_id),
                )

    def query_audit(self, *, operator: str | None = None, action: str | None = None,
                    doc_id: str | None = None, target: str | None = None,
                    from_ts: str | None = None,
                    to_ts: str | None = None, limit: int = 20,
                    offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        conds: list[str] = []
        args: list[Any] = []
        if operator:
            conds.append("operator = ?"); args.append(operator)
        if action:
            conds.append("action = ?"); args.append(action)
        if doc_id:
            conds.append("doc_id = ?"); args.append(doc_id)
        if target:
            # 前缀匹配（`doc:` 一次捞出所有文档类动作），且**与投影用同一个表达式**：
            # 筛选和显示如果不一致，用户会看到"筛出来了但对象列写着别的"，这是最坏的不一致
            conds.append(f"{_AUDIT_TARGET_EXPR} LIKE ?"); args.append(f"{target}%")
        if from_ts:
            conds.append("ts >= ?"); args.append(from_ts)
        if to_ts:
            conds.append("ts <= ?"); args.append(to_ts)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        with self._lock:
            conn = self._connection()
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM audit_log{where}", args).fetchone()["c"]
            rows = self._rows(conn.execute(
                f"{_AUDIT_SELECT}{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset]))
        return rows, int(total)

    def recent_audit(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return self._rows(self._connection().execute(
                f"{_AUDIT_SELECT} ORDER BY id DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------ tickets（G4）

    def create_ticket(self, *, ticket_type: str, doc_id: str,
                      doc_name: str | None = None, similarity_score: float | None = None,
                      matched_doc_id: str | None = None,
                      note: str | None = None) -> int | None:
        """新建工单；**同类型同文档已有 OPEN 工单时不插入**，返回 None。

        幂等必须落在仓储层的这一把锁里，不能靠调用方"先查后插"：
        巡检是每小时一轮的常驻任务，两处判定之间的窗口足以让同一问题堆出多张工单
        （实测：先查后插的写法在"手动触发 + 定时触发"重叠时重复开单）。
        """
        with self._lock:
            conn = self._connection()
            with conn:
                exists = conn.execute(
                    "SELECT id FROM tickets WHERE ticket_type = ? AND doc_id = ? "
                    "AND status = 'OPEN' LIMIT 1",
                    (ticket_type, doc_id),
                ).fetchone()
                if exists:
                    return None
                cur = conn.execute(
                    "INSERT INTO tickets (ticket_type, doc_id, doc_name, similarity_score, "
                    "matched_doc_id, status, note, handled_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'OPEN', ?, NULL, ?)",
                    (ticket_type, doc_id, doc_name, similarity_score, matched_doc_id,
                     note, now_iso()),
                )
                return int(cur.lastrowid or 0)

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection().execute(
                "SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone()
            return dict(row) if row else None

    def list_tickets(self, *, status: str | None = None, ticket_type: str | None = None,
                     limit: int = 50, offset: int = 0
                     ) -> tuple[list[dict[str, Any]], int]:
        conds: list[str] = []
        args: list[Any] = []
        if status:
            conds.append("status = ?"); args.append(status)
        if ticket_type:
            conds.append("ticket_type = ?"); args.append(ticket_type)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        with self._lock:
            conn = self._connection()
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tickets{where}", args).fetchone()["c"]
            rows = self._rows(conn.execute(
                # OPEN 优先、再按新→旧：待办永远排在最前（前端角标与列表直接用 ORDER 结果）
                f"SELECT * FROM tickets{where} "
                "ORDER BY CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END, id DESC "
                "LIMIT ? OFFSET ?",
                [*args, limit, offset]))
        return rows, int(total)

    def count_tickets_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._rows(self._connection().execute(
                "SELECT status, COUNT(*) AS c FROM tickets GROUP BY status"))
        return {str(r["status"]): int(r["c"]) for r in rows}

    def update_ticket(self, ticket_id: int, *, status: str | None = None,
                      note: str | None = None, handled_at: str | None = None) -> bool:
        """改工单状态/备注。note 传 None 表示不动原值（**不覆盖已有备注**）。

        执行化的失败原因要能追加到已有 note 上，直接覆盖会把"当初为什么开这张单"
        的上下文弄丢——那张信息是人工判断该不该处理它的唯一依据。
        """
        sets: list[str] = []
        args: list[Any] = []
        if status is not None:
            sets.append("status = ?"); args.append(status)
        if note is not None:
            sets.append("note = ?"); args.append(note)
        if handled_at is not None:
            sets.append("handled_at = ?"); args.append(handled_at)
        if not sets:
            return False
        args.append(int(ticket_id))
        with self._lock:
            conn = self._connection()
            with conn:
                cur = conn.execute(
                    f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", args)
            return cur.rowcount > 0

    def last_handled_ticket(self, *, ticket_type: str, doc_id: str,
                            status: str) -> dict[str, Any] | None:
        """该文档该类型最近一次"已处理"（DISMISSED/CONFIRMED/DONE）的工单。

        巡检据此实现**静默期**：用户已忽略的问题，只要文档内容没再变过，
        就不应该下一轮又冒出来——否则"忽略"按钮等于没用，工单列表会变成噪音墙。
        """
        with self._lock:
            row = self._connection().execute(
                "SELECT * FROM tickets WHERE ticket_type = ? AND doc_id = ? "
                "AND status = ? AND handled_at IS NOT NULL "
                "ORDER BY handled_at DESC, id DESC LIMIT 1",
                (ticket_type, doc_id, status),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------ G4 候选查询

    def list_recycled_expired(self, *, before_ts: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        """回收站中**已超过 TTL** 的文档（TTL 清理的唯一候选来源）。

        三个条件缺一不可：
        - `state = 'RECYCLED'`：停用区（DISABLED）**永不参与 TTL**——
          停用没有"过期"语义，且启用的唯一数据来源就是那份备份
        - `recycled_at IS NOT NULL`：没有删除时间的行不能凭"看起来老"就删
        - `recycled_at <= before_ts`：ISO 字符串比较即时间比较（格式统一，见 now_iso）

        只走**账本**、不扫目录：删除动作必须有账本依据（与对账任务同一条铁律）。
        目录里有备份而账本无记录的情形由调用方按"孤儿"另行告警，不在此自动清理。
        """
        with self._lock:
            return self._rows(self._connection().execute(
                "SELECT * FROM docs WHERE state = 'RECYCLED' "
                "AND recycled_at IS NOT NULL AND recycled_at <= ? "
                "ORDER BY recycled_at ASC LIMIT ?",
                (before_ts, limit),
            ))

    def list_stale_docs(self, *, before_ts: str,
                        limit: int = 50) -> list[dict[str, Any]]:
        """长期没有变化的 ACTIVE 文档（STALE 巡检候选）。

        `before_ts` 由调用方按"当前时间 - gov_stale_days"算好传入（时钟口径留在上层，
        存储层不掺业务时间语义）。排序用 `updated_at ASC`：最旧的先被巡检到。
        """
        with self._lock:
            return self._rows(self._connection().execute(
                "SELECT * FROM docs WHERE state = 'ACTIVE' AND updated_at <= ? "
                "ORDER BY updated_at ASC LIMIT ?",
                (before_ts, limit),
            ))

    def find_low_quality_docs(self, *, min_chars: int,
                              limit: int = 50) -> list[dict[str, Any]]:
        """含**过短切片**的 ACTIVE 文档（LOW_QUALITY 巡检候选）。

        阈值取 `< min_chars`（默认 10 字符）：这类切片几乎不携带语义，
        进向量库只会稀释检索质量。返回 `shortChunks` / `shortChars` 供工单备注说明。
        """
        with self._lock:
            return self._rows(self._connection().execute(
                """
                SELECT d.doc_id, d.doc_name, d.owner, d.updated_at,
                       COUNT(*) AS shortChunks, SUM(c.char_len) AS shortChars
                FROM chunks c
                JOIN docs d ON d.doc_id = c.doc_id
                WHERE d.state = 'ACTIVE' AND c.char_len < ?
                GROUP BY d.doc_id, d.doc_name, d.owner, d.updated_at
                ORDER BY shortChunks DESC, d.doc_id ASC
                LIMIT ?
                """,
                (int(min_chars), limit),
            ))


# ---------------------------------------------------------------- 工厂

_store: SqliteGovernanceStore | None = None
_store_lock = threading.Lock()


def get_store() -> SqliteGovernanceStore:
    """进程级单例（与 `kb_service` 同样的共享方式）。

    数据库路径由 `settings.gov_db_path` 决定，为空时用 `data/governance/governance.db`。
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from app.core.config import settings
                path = (settings.gov_db_path or "").strip() or str(DEFAULT_DB_PATH)
                _store = SqliteGovernanceStore(path)
    return _store


def utc_now_iso() -> str:
    """UTC ISO（保留给需要时区语义的场景）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

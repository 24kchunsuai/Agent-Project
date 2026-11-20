"""用户级长期记忆（画像）——docs/specs/会话记忆规约.md §6 M-5（缺口 G15）。

与 M-1 会话记忆的区别：会话记忆是「本会话说过什么」，画像是「这个人一直
偏好什么」（如"我只看 3 号机""回答要简洁"），跨会话生效。规约 §7 定位：
P3 增强能力，**不是主链路**——抽取失败静默降级（logger.debug、不计数），
关闭开关后完全不抽取。

设计来源（借鉴 AgentCraft memory_agent.py，并按本项目规约收紧）：
1. LLM 从问答中抽偏好（`_extract_user_preference` 的提示词思路），
   返回 `{"preference_style": "简洁/详细", "topics": [...]}` 或 null；
2. 落 SQLite `agent_memory_profile(user_id, key, value, source, confidence,
   updated_ts)`，保留 **来源与置信度** 字段——画像猜错会持续污染答案，
   比会话记忆危险，所以每条都带出身；
3. 覆盖规则：**高置信度才覆盖**（confidence >= 既有值）。当前只有 agent
   抽取（0.8）；将来用户显式设置（source="user", confidence=1.0）不会被
   agent 的猜测冲掉；
4. 可人工关闭（settings.profile_enabled）+ 可查看可删除（/v1/profile API，
   对应用户的知情权，规约验收明确要求）。

为什么单独建库而不进 session_memory.db：会话消息有 TTL 清理（purge_expired
按 created_ts 删），画像没有 TTL——混在一个库里，将来清理逻辑互相掣肘；
分库后两边生命周期各自独立。

线程模型与 memory_sqlite 一致：check_same_thread=False + Lock 串行写。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time

from app.core.config import settings
from app.core.paths import DATA_DIR
from app.services.costing import DEPT_KEY

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory_profile(
    user_id    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'agent',
    confidence REAL    NOT NULL DEFAULT 0.8,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

_EXTRACT_PROMPT = """分析以下问答，判断用户是否有明显的偏好特征。

用户问题：{question}
AI回答：{answer}

如果有，返回 JSON：{{"preference_style": "简洁/详细", "topics": ["主题1"]}}
如果没有明显偏好，返回：null
只返回 JSON 或 null，不要解释。"""


class UserProfileStore:
    """画像 KV 存储。任何异常只记日志不外抛（增强能力不阻断主链路）。"""

    def __init__(self, db_path=None) -> None:
        self._path = db_path or (DATA_DIR / "memory" / "user_profile.db")
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._open()

    # ---- 生命周期 ----

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
            logger.info("用户画像库已就绪 db=%s", self._path)
        except Exception as exc:  # noqa: BLE001 —— 画像库坏了不影响主链路
            self._conn = None
            logger.warning("用户画像库打开失败，画像功能停用: %s", exc)

    @property
    def available(self) -> bool:
        return self._conn is not None

    # ---- 读写 ----

    def get_all(self, user_id: int) -> list[dict]:
        """该用户的全部画像条目（知情权：可查看）。"""
        if self._conn is None:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT key, value, source, confidence, updated_ts "
                    "FROM agent_memory_profile WHERE user_id=? ORDER BY key",
                    (int(user_id),),
                ).fetchall()
            return [
                {"key": r[0], "value": r[1], "source": r[2],
                 "confidence": r[3], "updatedTs": r[4]}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("画像读取失败 user=%s: %s", user_id, exc)
            return []

    def department_map(self) -> dict[int, str]:
        """全量 `user_id -> department` 映射（E-13 成本看板的部门维度）。

        ⚠️ 口径（与 `costing.py` / `telemetry.costs_summary` 一致）：本系统**没有
        组织架构**，部门 = 画像 KV 里 key=`department` 的条目（用户聊到过才会落）。
        读取期全表派生、不回填任何业务表；没声明的用户在调用方归 `unknown`。
        画像库不可用返回空 dict（成本看板全部归 unknown，不出错）。
        """
        if self._conn is None:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT user_id, value FROM agent_memory_profile WHERE key = ?",
                    (DEPT_KEY,)).fetchall()
            return {int(r[0]): str(r[1]) for r in rows if r[0] and r[1]}
        except Exception as exc:  # noqa: BLE001
            logger.debug("部门映射读取失败: %s", exc)
            return {}

    def upsert(self, user_id: int, key: str, value: str,
               source: str = "agent", confidence: float = 0.8) -> bool:
        """写入/更新一条画像。**低置信度不覆盖高置信度**（见模块注释 3）。"""
        if self._conn is None:
            return False
        key = (key or "").strip()[:64]
        value = (value or "").strip()[:2000]
        if not key or not value:
            return False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT confidence FROM agent_memory_profile "
                    "WHERE user_id=? AND key=?", (int(user_id), key)).fetchone()
                if row is not None and confidence < row[0]:
                    return False
                self._conn.execute(
                    "INSERT INTO agent_memory_profile"
                    "(user_id, key, value, source, confidence, updated_ts) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET "
                    "value=excluded.value, source=excluded.source, "
                    "confidence=excluded.confidence, updated_ts=excluded.updated_ts",
                    (int(user_id), key, value, source[:32],
                     float(confidence), int(time.time())))
                self._conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("画像写入失败 user=%s key=%s: %s", user_id, key, exc)
            return False

    def delete(self, user_id: int, key: str | None = None) -> int:
        """删除画像（key=None 清空该用户全部）。返回删除条数。"""
        if self._conn is None:
            return 0
        try:
            with self._lock:
                if key is None:
                    cur = self._conn.execute(
                        "DELETE FROM agent_memory_profile WHERE user_id=?",
                        (int(user_id),))
                else:
                    cur = self._conn.execute(
                        "DELETE FROM agent_memory_profile "
                        "WHERE user_id=? AND key=?", (int(user_id), key))
                self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("画像删除失败 user=%s key=%s: %s", user_id, key, exc)
            return 0

    # ---- 注入 ----

    def as_prompt(self, user_id: int) -> str:
        """画像的注入形态：一行人类可读文本；无画像返回空串。

        放进 system 消息而不是拼进用户历史——画像不是"说过的话"，
        是给模型的背景设定，用 system 语义才对。
        """
        entries = self.get_all(user_id)
        if not entries:
            return ""
        parts = []
        for e in entries:
            value = e["value"]
            if e["key"] == "topics":
                try:
                    topics = json.loads(value)
                    value = "、".join(str(t) for t in topics) if topics else ""
                except (ValueError, TypeError):
                    pass
            if value:
                parts.append(f"{e['key']}={value}")
        return "；".join(parts)


profile_store = UserProfileStore()

# 同一用户的抽取节流（进程内即可——重启丢节流状态无实害）
_last_extract_ts: dict[int, float] = {}
_extract_lock = threading.Lock()


def _parse_preference(text: str) -> dict | None:
    """解析 LLM 返回的偏好 JSON。容错：代码块包裹 / null / 垃圾输入。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.lower() == "null":
        return None
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        data = json.loads(cleaned.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    style = data.get("preference_style")
    if isinstance(style, str) and style.strip():
        out["preference_style"] = style.strip()
    topics = data.get("topics")
    if isinstance(topics, list):
        topics = [str(t).strip() for t in topics if str(t).strip()]
        if topics:
            out["topics"] = json.dumps(topics, ensure_ascii=False)
    return out or None


async def _extract_and_save(user_id: int, question: str, answer: str) -> None:
    """跑一次抽取并落库。全程静默降级（规约：logger.debug、不计数）。"""
    from app.core.llm import llm_client  # 局部导入避免环

    try:
        prompt = _EXTRACT_PROMPT.format(question=question[:1500],
                                        answer=answer[:1500])
        result = await llm_client.chat([{"role": "user", "content": prompt}])
        preference = _parse_preference(result.content if hasattr(
            result, "content") else str(result))
        if not preference:
            return
        saved = [k for k, v in preference.items()
                 if profile_store.upsert(user_id, k, v)]
        if saved:
            logger.info("用户画像已更新 user=%s keys=%s", user_id, saved)
    except Exception as exc:  # noqa: BLE001 —— 增强能力静默失败
        logger.debug("用户画像抽取失败 user=%s: %s", user_id, exc)


def schedule_profile_extraction(user_id: int, question: str,
                                answer: str) -> None:
    """在轮次结束后挂一个 fire-and-forget 抽取任务。

    三道闸门（规约要求可关、成本可控）：
    1. settings.profile_enabled 总开关——关闭后完全不抽取；
    2. 节流：同一用户两次抽取至少隔 profile_extract_interval_seconds；
    3. 短问题跳过（"在吗"之类没有可抽信息）。

    抽取在独立 asyncio 任务里跑，不 await——绝不让画像拖慢回答。
    """
    if not settings.profile_enabled or not user_id:
        return
    if len((question or "").strip()) < settings.profile_min_message_chars:
        return
    now = time.time()
    with _extract_lock:
        last = _last_extract_ts.get(int(user_id), 0.0)
        if now - last < settings.profile_extract_interval_seconds:
            return
        _last_extract_ts[int(user_id)] = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环（理论不应发生——调用点都在 async 上下文）
    loop.create_task(_extract_and_save(int(user_id), question, answer))

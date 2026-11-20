"""会话记忆（docs/specs/会话记忆规约.md）。

分层（规约 §3）：
- L1 存储：本模块。`SessionMemory`=内存实现，`SqliteMemoryStore`=持久化实现；
- L2 窗口 / L3 持久化 / L5 可靠性：分别见 memory_trim.py / memory_sqlite.py；
- 工厂 `get_memory_store()` 按 `settings.memory_store` 返回实现，调用方无感。

设计：
- 以 sessionId 为键存消息列表，闲聊/业务回合都记，保证跨意图上下文连续；
- **存储层不做裁剪**：`get()` 返回全量，裁剪上移到 `memory_trim.trim_history()`
  （按 token 预算 + 双锚点，见规约 §6 M-2）。职责单一，且持久化后旧窗口会截断
  掉本可用的早期上下文；
- 会话数与会话内消息数都有上限：内存版防膨胀，超限淘汰最旧；
- 暴露 get / append / record_turn / stats / flush / load_all_session_ids，
  `agent_service` 只依赖这套接口。
"""

import logging
import threading
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class MemoryConfig:
    max_sessions: int = 500      # 最多保留多少个会话（内存版上限）
    max_messages: int = 40       # 每个会话最多保留多少条消息（内存版上限）


class SessionMemory:
    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig()
        self._store: dict[str, list[dict[str, str]]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> list[dict[str, str]]:
        """取该会话**全部**历史（时间升序）。

        注意：不再按 `window` 截断——裁剪已上移到 `memory_trim.trim_history()`
        （token 预算 + 双锚点）。两个实现（内存/SQLite）行为必须一致，
        否则切换存储会改变上下文构成，排查时极易误判。
        """
        with self._lock:
            return list(self._store.get(session_id, []))

    def has(self, session_id: str) -> bool:
        return bool(self._store.get(session_id))

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息；顺带做会话数与消息数上限控制。"""
        if role not in ("user", "assistant") or not content:
            return
        with self._lock:
            messages = self._store.get(session_id)
            if messages is None:
                # 会话数超限：淘汰最旧的会话（内存版的简单策略）
                if len(self._store) >= self.config.max_sessions:
                    oldest = next(iter(self._store))
                    self._store.pop(oldest, None)
                    logger.info("会话数超限，淘汰最旧会话 %s", oldest)
                messages = self._store[session_id] = []
            messages.append({"role": role, "content": content})
            if len(messages) > self.config.max_messages:
                del messages[: len(messages) - self.config.max_messages]

    def record_turn(self, session_id: str, user_message: str, assistant_reply: str) -> None:
        """把一轮完整对话（用户 + 助手）成对写入。"""
        self.append(session_id, "user", user_message)
        self.append(session_id, "assistant", assistant_reply)

    def flush(self, session_id: str | None = None) -> None:
        """内存实现无需落盘，空实现以对齐 `MemoryStore` 协议。"""

    def load_all_session_ids(self) -> list[str]:
        """内存实现没有持久层可回放，返回空表。"""
        return []

    def clear(self) -> None:
        """清空全部会话（测试隔离用；不属业务路径）。

        两个实现都必须提供：测试夹具只依赖这个公开方法，
        不允许摸 `_store` 之类的实现内部——否则切换存储实现即崩。
        """
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"sessions": len(self._store), "mode": 0}


#: 纯内存实现（降级兜底 / `memory_store=memory` 时使用）。
#: ⚠️ 不要直接 import 它当全局记忆用——请用 `session_memory`（见下方说明）。
_memory_store = SessionMemory()


def get_memory_store():
    """按配置返回记忆实现（进程内单例）。

    `memory_store=sqlite`（默认）走持久化；任何初始化问题都退化为内存版，
    **绝不因记忆层故障让服务起不来**（规约 §9 铁律 1）。
    """
    mode = (settings.memory_store or "memory").strip().lower()
    if mode == "sqlite":
        try:
            from app.services.memory_sqlite import get_sqlite_store

            return get_sqlite_store()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLite 记忆初始化失败，回退内存实现: %s", exc)
    return _memory_store


#: 全局记忆实例——**唯一入口**，由配置决定实现（SQLite 持久化 / 纯内存）。
#
# ⚠️ 设计踩坑（务必保留此写法）：
# 早期版本这里写 `session_memory = SessionMemory()`，工厂另起一个函数返回 SQLite，
# 结果 `from app.services.memory import session_memory` 的调用方拿到的是**内存实例**，
# 持久化被静默绕过（写进了内存但重启即失，且从代码上看不出来）。
# 现在名字直接绑定工厂结果，import 谁都是配置指定的实现，不存在"绕过"路径。
session_memory = get_memory_store()

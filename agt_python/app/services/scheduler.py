"""常驻定时任务调度（G2）——FastAPI lifespan + asyncio，零新依赖。

为什么不用调度框架
------------------
drone 的 Python 侧是一个单进程 FastAPI 服务，需要周期的只有治理域的几个任务
（对账 / 回收站 TTL 清理 / 巡检）加一条会话记忆 TTL 清理（会话记忆规约 §5.1）。
为此引入 APScheduler / Celery 意味着多一个依赖、
一套持久化配置与一类新的失败模式（调度器挂了没人知道），收益不成比例。
spec §7 的结论：**用 lifespan 内的 `asyncio.create_task` + `while: sleep` 常驻循环**。

三条工程约束（都是踩过坑才写的）
--------------------------------
1. **任务体是同步阻塞代码**（pymilvus / sqlite3 都是同步库），必须丢进
   `asyncio.to_thread` 执行，否则一轮对账就把事件循环卡住、所有 HTTP 请求排队
2. **异常必须就地吞掉并记录**：任务循环抛出一次异常就再也不跑了，
   那是最难发现的故障（服务看着正常，治理静默失效）
3. **任务幂等 + 可手动触发**：服务重启丢一轮无所谓，下轮补齐；
   同时所有任务都能通过 API 手动跑（验收与排障都要用）

G4 的 TTL 清理与巡检扫描就是直接往 `default_tasks()` 里加条目；E-7 的数据备份
同样只是加一条（开关 / 异常吞掉 / 手动触发 / 状态上报全部复用）——本模块的机制
（开关 / 异常吞掉 / 手动触发 / 状态上报）一行都不用动，这是当初选"任务表数据化"
而不是"写死两个循环"的收益。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.config import settings
from app.services.backup_service import run_backup
from app.services.inspection_service import inspection_service
from app.services.memory_sqlite import get_sqlite_store
from app.services.reconcile_service import reconcile_service
from app.services.ttl_service import ttl_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeriodicTask:
    """一个常驻周期任务。"""

    name: str
    run: Callable[[], Any]
    interval_sec: int
    initial_delay_sec: int = 60
    enabled: Callable[[], bool] = lambda: True
    describe: str = ""


@dataclass
class TaskRunState:
    """运行态（内存，不落库——重启即重置，因为它只是观测信息）。"""

    runs: int = 0
    last_started_at: float | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    last_skipped: str | None = None
    history: list[str] = field(default_factory=list)


def default_tasks() -> list[PeriodicTask]:
    """默认任务表：G2 对账 + G4 回收站 TTL 清理 / 巡检扫描 + 记忆 TTL 清理 + 数据备份。

    五个任务都遵守同一条约定：**任务体只做正事，不在里面判断"要不要跑"**——
    开与不开由 `enabled` 决定，于是 `/governance/tasks` 能一眼看出"哪些任务被开关关掉了"，
    而不是让人去猜为什么某一轮没有动作。
    """
    return [
        PeriodicTask(
            name="reconcile",
            describe="向量对账：清理回收站/停用文档的残留向量，报告应存在却缺失的文档",
            interval_sec=int(settings.gov_reconcile_interval_sec),
            initial_delay_sec=int(settings.gov_reconcile_initial_delay_sec),
            enabled=lambda: bool(settings.gov_enabled and settings.gov_reconcile_enabled),
            run=lambda: reconcile_service.run(operator="system"),
        ),
        PeriodicTask(
            name="ttl_purge",
            describe="回收站 TTL：超期未恢复的文档走彻底删除（三清 + TTL_PURGE 审计）",
            interval_sec=int(settings.gov_ttl_interval_sec),
            initial_delay_sec=int(settings.gov_ttl_initial_delay_sec),
            enabled=lambda: bool(settings.gov_enabled and settings.gov_ttl_enabled),
            run=lambda: ttl_service.run(operator="system"),
        ),
        PeriodicTask(
            name="inspection_scan",
            describe="巡检扫描：近重复 / 陈旧 / 劣质切片 → 工单（幂等，已忽略的不重复开）",
            interval_sec=int(settings.gov_inspection_interval_sec),
            initial_delay_sec=int(settings.gov_inspection_initial_delay_sec),
            enabled=lambda: bool(settings.gov_enabled and settings.gov_inspection_enabled),
            run=lambda: inspection_service.scan(operator="system"),
        ),
        PeriodicTask(
            name="memory_ttl_purge",
            describe="会话记忆 TTL：删除超期会话消息（memory_ttl_days 天前；"
                     "规约 §5.1——清理必须走产品内定时任务，不手工删文件）",
            interval_sec=int(settings.memory_ttl_interval_seconds),
            initial_delay_sec=120,
            enabled=lambda: settings.memory_store == "sqlite" and int(settings.memory_ttl_days) > 0,
            run=lambda: get_sqlite_store().purge_expired(),
        ),
        PeriodicTask(
            name="db_backup",
            describe="数据备份：四库（治理/埋点/会话记忆/画像）一致快照 + integrity_check，"
                     "落 data/backups/<date>/，滚动保留 14 份（E-7，判据 C4）",
            interval_sec=int(settings.backup_interval_sec),
            initial_delay_sec=int(settings.backup_initial_delay_sec),
            enabled=lambda: bool(settings.backup_enabled),
            run=lambda: run_backup(operator="system"),
        ),
    ]


class TaskScheduler:
    """asyncio 常驻任务调度器（进程级单例见模块底部 `scheduler`）。"""

    def __init__(self, tasks: list[PeriodicTask] | None = None) -> None:
        self._tasks: list[PeriodicTask] = list(tasks) if tasks is not None else default_tasks()
        self._handles: list[asyncio.Task] = []
        self._states: dict[str, TaskRunState] = {t.name: TaskRunState() for t in self._tasks}
        self._running = False

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """挂载常驻任务。重复调用是幂等的（uvicorn reload 会重新走 lifespan）。

        `gov_scheduler_enabled=false` 时**只不挂载循环**，任务表照样保留——
        `/governance/tasks` 仍能看到三个任务与各自开关状态，只是 runs 停在 0，
        不会出现"任务列表空了、不知道是被关了还是没有"的困惑。
        """
        if self._running:
            return
        self._running = True
        if not settings.gov_scheduler_enabled:
            logger.warning("常驻任务总闸关闭（gov_scheduler_enabled=false）："
                           "不挂载定时循环，手动触发仍可用 | %s",
                           ", ".join(t.name for t in self._tasks) or "无")
            return
        for task in self._tasks:
            self._handles.append(
                asyncio.create_task(self._loop(task), name=f"gov-task:{task.name}")
            )
        logger.info("治理常驻任务已启动 | %s",
                    ", ".join(f"{t.name}/{t.interval_sec}s" for t in self._tasks) or "无")

    async def stop(self) -> None:
        self._running = False
        for h in self._handles:
            h.cancel()
        if self._handles:
            await asyncio.gather(*self._handles, return_exceptions=True)
        self._handles.clear()
        logger.info("治理常驻任务已停止")

    async def _loop(self, task: PeriodicTask) -> None:
        try:
            await asyncio.sleep(max(1, task.initial_delay_sec))
            while True:
                # 同步阻塞任务体 → 线程池执行，别占住事件循环
                await asyncio.to_thread(self.execute, task)
                await asyncio.sleep(max(5, task.interval_sec))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  兜底：循环本身崩了要能看见
            logger.exception("常驻任务循环异常退出 | task=%s", task.name)

    # ------------------------------------------------------------ 执行

    def execute(self, task: PeriodicTask) -> Any:
        """同步执行一轮（定时循环与手动触发共用同一条路径）。"""
        state = self._states.setdefault(task.name, TaskRunState())
        if not task.enabled():
            state.last_skipped = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.debug("任务已跳过（开关关闭）| task=%s", task.name)
            return None

        t0 = time.time()
        state.runs += 1
        state.last_started_at = t0
        try:
            result = task.run()
            state.last_error = None
            return result
        except Exception as e:  # noqa: BLE001  任务失败不能中断循环
            state.last_error = f"{type(e).__name__}: {e}"
            logger.error("[GOV-WARN] 任务执行失败，下轮重试 | task=%s err=%s", task.name, e)
            return None
        finally:
            state.last_duration_ms = round((time.time() - t0) * 1000)

    def run_once(self, name: str) -> Any:
        """手动触发指定任务（同步执行，供 API 直接调用）。"""
        for task in self._tasks:
            if task.name == name:
                return self.execute(task)
        raise KeyError(f"未知任务：{name}")

    def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._tasks:
            st = self._states.get(t.name, TaskRunState())
            out.append({
                "name": t.name,
                "describe": t.describe,
                "intervalSec": t.interval_sec,
                "initialDelaySec": t.initial_delay_sec,
                "enabled": bool(t.enabled()),
                "runs": st.runs,
                "lastStartedAt": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.last_started_at))
                                  if st.last_started_at else None),
                "lastDurationMs": st.last_duration_ms,
                "lastError": st.last_error,
            })
        return out


scheduler = TaskScheduler()

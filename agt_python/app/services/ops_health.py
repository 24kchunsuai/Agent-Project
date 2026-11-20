"""运维健康聚合（企业级落地规约 **E-3**，判据 C3「可观测」）。

## 这个模块回答什么

`/v1/health` 只回答"Python 进程活着吗"。运维真正要问的是另一个问题：

> **这套系统现在健康吗？哪一环坏了？**

本模块把 Python 侧能真实观测到的每一环聚合到**一个 URL**，返回固定的
`{status, checkedAt, components:[{name,status,latencyMs,detail}]}`。

## 两条口径（先看这里，否则会误读 status）

### 1. 组件 `status` 是"组件自身"的状态，整体 `status` 是"按影响归并"

停掉 Milvus 时，`components` 里 Milvus 项必须是 **`down`**（它真的挂了），
而整体是 **`degraded`**（知识库不可用，但对话/画像/看板仍能跑）。
两个字段各说各的事，所以组件项额外带 `required` 标明影响权重：

| 组件 | `required` | 自身 down 时的整体影响 |
|---|---|---|
| `python`（本进程） | ✅ | down —— 但本接口能返回就说明它在跑 |
| `milvus` | ❌ | degraded（知识库/检索降级） |
| `sqlite:*` ×4 | ❌ | degraded（对应能力降级：记忆/画像/埋点/治理） |
| `scheduler` | ❌ | degraded（治理任务静默失效） |
| `degrade` | ❌ | 不参与归并（只作观测数据） |
| `jsdstu`（8060，外部系统） | ❌ | degraded（业务派活不可用） |

**本接口正常不会返回 `down`**：能返回就意味着进程活着，而单点数据依赖失败
只降级。唯一的 `down` 路径是"所有数据依赖同时不可用"（服务实际已无能力）。
真正的 `down` 出现在 **8080 侧**：Java 连不上 8000 → 核心链路整体不可用。
这是刻意的语义分工，不是漏实现。

### 2. 降级计数器**不参与**状态归并

`degrade` 组件恒 `up`（= "计数已采集"）。计数器非零表示"历史发生过降级处理"，
是**观测数据**不是**当前故障**——把它算进健康状态会让接口永远黄着，
运维很快就不再看了（告警疲劳）。计数趋势归 E-9 指标端点负责。

## 三条实现约束

- **单项失败不抛异常**（对齐全链路容错规约）：每个 check 自己 try/except 成
  `down` 组件，`gather(return_exceptions=True)` 再兜一层，保证聚合器本身不会 500。
- **探针零副作用**：SQLite 只读（文件不存在时**不创建**、报告 degraded），
  Milvus 只 `list_collections`，绝不在产品数据目录留任何东西。
- **短 TTL 缓存**：前端顶栏按秒轮询，Milvus 连接是百毫秒级。缓存 3 秒，
  避免"看健康的人把被看的服务压垮"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 组件状态取值（字符串常量，与 Java 侧 / 文档同一套词表）
UP = "up"
DEGRADED = "degraded"
DOWN = "down"

#: 状态严重度（归并时取最差）
_SEVERITY = {UP: 0, DEGRADED: 1, DOWN: 2}

#: 聚合结果缓存 TTL（秒）——前端轮询友好，见模块 docstring
CACHE_TTL_SEC = 3.0

#: 单项探针超时（秒）。取值依据：本机回环调用，正常在 10ms 量级；
#: 超过这个数就是"不通"，再等只会拖垮整个聚合（验收要求 < 2s）。
_TIMEOUT_MILVUS = 3.0
_TIMEOUT_SQLITE = 2.0
_TIMEOUT_JSDSTU = 2.0

#: 进程启动时刻（模块 import 时记录）——用于判断"调度器是不是压根没跑起来"
_PROCESS_START = time.time()

_cache: dict[str, Any] = {"at": 0.0, "payload": None}


# --------------------------------------------------------------------- 工具


def _iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uptime_human() -> str:
    sec = int(time.time() - _PROCESS_START)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60}s"
    return f"{sec // 3600}h{(sec % 3600) // 60}m"


def _component(
    name: str,
    status: str,
    latency_ms: int,
    detail: str = "",
    *,
    required: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "latencyMs": latency_ms,
        "detail": detail,
        "required": required,
    }


def _merge_status(components: list[dict[str, Any]]) -> str:
    """按影响归并整体状态（规则见模块 docstring 表）。"""
    # 必需组件挂了 → down
    if any(c["status"] == DOWN and c["required"] for c in components):
        return DOWN
    data_components = [c for c in components if not c["required"] and c["name"] != "degrade"]
    # 所有数据依赖同时不可用 → 服务实际已无能力
    if data_components and all(c["status"] == DOWN for c in data_components):
        return DOWN
    if any(c["status"] != UP for c in components):
        return DEGRADED
    return UP


# --------------------------------------------------------------------- 各组件探针


async def _check_python() -> dict[str, Any]:
    """自身。能跑到这里就说明进程活着——这一项是"聚合器别忘了自己"的锚点。"""
    from app.core.build_info import get_build_info

    build = get_build_info()
    import sys

    return _component(
        "python",
        UP,
        0,
        f"{settings.app_name} v{build.version} / py{sys.version.split()[0]} / 已运行 {_uptime_human()}",
        required=True,
    )


def _probe_milvus() -> tuple[str, str]:
    """真实连接 Milvus（不是 ping）：列一次 collection 名。

    为什么不做成 `GET /healthz`：Milvus 的 HTTP 探针在依赖栈半死时仍可能 200，
    而 `list_collections` 走的是真实的 gRPC 元数据通道——**探测的判别力 =
    它对"半死"状态的敏感度**（E-2 的假阳性教训同源）。
    """
    from pymilvus import MilvusClient

    client = MilvusClient(uri=settings.milvus_uri, timeout=_TIMEOUT_MILVUS)
    names = client.list_collections()
    target = settings.milvus_collection
    if target not in names:
        return DEGRADED, f"连通但集合缺失：{target}（现有 {len(names)} 个）"
    return UP, f"连接正常，集合 {target} 在位"


def _probe_sqlite(label: str, path) -> tuple[str, str]:
    """只读探测：库可打开 + 目录可写。**不创建文件**（避免污染产品目录）。"""
    if not path.exists():
        return DEGRADED, f"库文件不存在：{path}（首次启动会创建；若长期如此请查 DATA_ROOT）"
    conn = sqlite3.connect(str(path), timeout=_TIMEOUT_SQLITE)
    try:
        # schema_version 是纯读操作，能返回即证明文件是合法 SQLite 且可读
        ver = conn.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        conn.close()
    if not os.access(str(path.parent), os.W_OK):
        return DEGRADED, f"库可读但目录不可写（schema v{ver}）：{path.parent}"
    return UP, f"可读可写（schema v{ver}）"


def _sqlite_targets() -> tuple[list[tuple[str, Any]], list[dict[str, Any]]]:
    """四个库的 (标签, 路径) + 路径解析失败项。

    路径必须与各 store 实际使用的**同源**（不在这里另写一份常量），否则会出现
    "健康检查说好、业务读写报错"的割裂。所以这里读的是各模块自己的解析结果。
    代价是读了几处私有属性/方法——因此整体包在 try 里：**store 内部改名时
    健康检查要老实报"路径解析失败"，而不是自己炸掉**。
    """
    from app.governance.store import DEFAULT_DB_PATH
    from app.services.memory_sqlite import get_sqlite_store
    from app.services.telemetry import telemetry
    from app.services.user_profile import profile_store

    probe: list[tuple[str, Any]] = []
    failed: list[dict[str, Any]] = []

    def take(label: str, fn: Callable[[], Any]) -> None:
        try:
            probe.append((label, fn()))
        except Exception as exc:  # noqa: BLE001 —— 解析失败只降级
            failed.append(_component(label, DEGRADED, 0, f"路径解析失败：{type(exc).__name__}: {exc}"))

    take("sqlite:governance", lambda: DEFAULT_DB_PATH)
    take("sqlite:metrics", lambda: telemetry._db_path())
    take("sqlite:profile", lambda: profile_store._path)
    take("sqlite:session_memory", lambda: get_sqlite_store().db_path)
    return probe, failed


async def _check_sqlite(label: str, path) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        status, detail = await asyncio.wait_for(
            asyncio.to_thread(_probe_sqlite, label, path), timeout=_TIMEOUT_SQLITE + 1.0
        )
    except asyncio.TimeoutError:
        status, detail = DOWN, f"探测超时（>{_TIMEOUT_SQLITE}s）：{path}"
    except Exception as exc:  # noqa: BLE001 —— 单项失败只降级，绝不外抛
        status, detail = DOWN, f"{type(exc).__name__}: {exc}"
    return _component(label, status, int((time.perf_counter() - t0) * 1000), detail)


async def _check_milvus() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        status, detail = await asyncio.wait_for(
            asyncio.to_thread(_probe_milvus), timeout=_TIMEOUT_MILVUS + 1.0
        )
    except asyncio.TimeoutError:
        status, detail = DOWN, f"连接超时（>{_TIMEOUT_MILVUS}s）：{settings.milvus_uri}"
    except Exception as exc:  # noqa: BLE001
        status, detail = DOWN, f"{type(exc).__name__}: {exc}"
    return _component("milvus", status, int((time.perf_counter() - t0) * 1000), detail)


async def _check_jsdstu() -> dict[str, Any]:
    """8060 可达性（外部系统，不可达只降级不 down）。"""
    import httpx

    t0 = time.perf_counter()
    url = f"{settings.jsdstu_base_url.rstrip('/')}/"
    try:
        # trust_env=False：探测目标是本机，必须绕开系统代理。
        # 否则设了 HTTP_PROXY 的机器上，回环请求会被代理吞掉变成 502，
        # 健康检查就会把"活着的 8060"报成不可达（curl --noproxy '*' 同源教训）。
        async with httpx.AsyncClient(timeout=_TIMEOUT_JSDSTU, trust_env=False) as client:
            resp = await client.get(url)
        # 8060 未登录会回 401/403 或带 code 的 JSON，只要"应答了"就算可达；
        # 判别"是不是本系统"交给 E-2 的 preflight（那里探的是响应体特征）。
        return _component(
            "jsdstu", UP, int((time.perf_counter() - t0) * 1000),
            f"可达（HTTP {resp.status_code}）：{settings.jsdstu_base_url}",
        )
    except Exception as exc:  # noqa: BLE001
        return _component(
            "jsdstu", DEGRADED, int((time.perf_counter() - t0) * 1000),
            f"不可达（{type(exc).__name__}）：{settings.jsdstu_base_url} —— 业务派活/登录会失败",
        )


def _probe_scheduler() -> dict[str, Any]:
    """常驻任务态。异常吞在这里（它是内存状态，不该抛）。"""
    from app.services.scheduler import scheduler

    try:
        return {"tasks": scheduler.status()}
    except Exception as exc:  # noqa: BLE001
        return {"tasks": [], "error": f"{type(exc).__name__}: {exc}"}


def _check_scheduler_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把任务快照归纳成一个组件的状态（明细另挂顶层 `scheduler` 字段）。"""
    tasks = snapshot.get("tasks") or []
    if snapshot.get("error"):
        return _component("scheduler", DOWN, 0, f"任务表读取失败：{snapshot['error']}")
    if not tasks:
        return _component("scheduler", DEGRADED, 0, "无任务（配置异常？）")

    failed = [t["name"] for t in tasks if t.get("lastError")]
    if failed:
        return _component(
            "scheduler", DEGRADED, 0,
            "任务执行报错：" + "; ".join(f"{n}" for n in failed) + "（明细见 scheduler 字段）",
        )

    # "调度器压根没跑起来"是最难发现的故障：服务看着正常，治理静默失效。
    # 判据：进程已运行超过 (initialDelay + interval) 但该任务 runs 仍为 0。
    uptime = time.time() - _PROCESS_START
    never_ran = [
        t["name"] for t in tasks
        if t.get("enabled") and t.get("runs", 0) == 0
        and uptime > (t.get("initialDelaySec", 60) + t.get("intervalSec", 300))
    ]
    if never_ran:
        return _component(
            "scheduler", DEGRADED, 0,
            "任务已过首轮时间却从未执行（调度循环可能未挂载）：" + ", ".join(never_ran),
        )

    off = [t["name"] for t in tasks if not t.get("enabled")]
    note = f"{len(tasks)} 个任务已挂载" + (f"，其中 {len(off)} 个被开关关闭：{', '.join(off)}" if off else "")
    return _component("scheduler", UP, 0, note)


def _check_degrade() -> tuple[dict[str, Any], dict[str, int]]:
    """降级计数器：只作观测数据，恒 up（理由见模块 docstring 口径 2）。"""
    from app.services.telemetry import telemetry

    try:
        counters = telemetry.counters_snapshot()
    except Exception as exc:  # noqa: BLE001
        return _component("degrade", DEGRADED, 0, f"计数器读取失败：{exc}"), {}
    nonzero = {k: v for k, v in counters.items() if v}
    top = sorted(nonzero.items(), key=lambda kv: kv[1], reverse=True)[:3]
    detail = (
        "本轮无降级记录" if not nonzero
        else f"累计 {sum(nonzero.values())} 次 / {len(nonzero)} 类；Top: "
             + ", ".join(f"{k}={v}" for k, v in top)
    )
    return _component("degrade", UP, 0, detail), counters


# --------------------------------------------------------------------- 聚合入口


async def _collect() -> dict[str, Any]:
    """并发采集全部组件（总耗时 ≈ 最慢一项，而非各项之和）。"""
    t0 = time.perf_counter()

    sqlite_items, sqlite_failed = _sqlite_targets()   # 同步、纯内存，先取路径
    sched_snapshot = _probe_scheduler()
    degrade_comp, counters = _check_degrade()

    results = await asyncio.gather(
        _check_python(),
        _check_milvus(),
        _check_jsdstu(),
        *(_check_sqlite(label, path) for label, path in sqlite_items),
        return_exceptions=True,
    )

    components: list[dict[str, Any]] = list(sqlite_failed)
    for item in results:
        if isinstance(item, BaseException):
            # 兜底：检查函数自己写错也不能让聚合接口 500
            logger.error("运维健康检查项异常", exc_info=item)
            components.append(_component("unknown", DOWN, 0, f"{type(item).__name__}: {item}"))
        else:
            components.append(item)

    components.append(_check_scheduler_state(sched_snapshot))
    components.append(degrade_comp)

    return {
        "status": _merge_status(components),
        "checkedAt": _iso_now(),
        "durationMs": int((time.perf_counter() - t0) * 1000),
        "summary": {
            UP: sum(1 for c in components if c["status"] == UP),
            DEGRADED: sum(1 for c in components if c["status"] == DEGRADED),
            DOWN: sum(1 for c in components if c["status"] == DOWN),
        },
        "components": components,
        # ---- 明细附加区（组件列表保持一行一项，细节放这里更易读）----
        "scheduler": sched_snapshot.get("tasks") or [],
        "degradeCounters": counters,
    }


async def collect_health(*, use_cache: bool = True) -> dict[str, Any]:
    """聚合入口（带短 TTL 缓存）。`use_cache=False` 强制重新采集（验收/排障用）。"""
    now = time.time()
    if use_cache and _cache["payload"] is not None and (now - _cache["at"]) < CACHE_TTL_SEC:
        payload = dict(_cache["payload"])
        payload["cached"] = True
        payload["cacheAgeMs"] = int((now - _cache["at"]) * 1000)
        return payload

    payload = await _collect()
    payload["cached"] = False
    _cache["at"] = time.time()
    _cache["payload"] = payload
    return payload


def clear_cache() -> None:
    """清缓存（单测用）。"""
    _cache["at"] = 0.0
    _cache["payload"] = None


#: 供 8080 侧复用的组件名清单（Java 侧只用它做展示分组，不参与逻辑）
COMPONENT_ORDER: tuple[str, ...] = (
    "python", "milvus",
    "sqlite:governance", "sqlite:metrics", "sqlite:profile", "sqlite:session_memory",
    "scheduler", "degrade", "jsdstu",
)

#: 类型别名（供调用方标注）
Component = dict[str, Any]
HealthFn = Callable[[], Any]

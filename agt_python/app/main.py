"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --port 8000
文档：http://localhost:8000/docs
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.auth import SharedKeyMiddleware, assert_shared_key_configured
from app.core.build_info import get_build_info
from app.core.config import settings
from app.core.exception import register_exception_handlers
from app.core.llm import llm_client
from app.core.logging_setup import setup_logging
from app.core.trace import TraceIdMiddleware
from app.services.scheduler import scheduler
from app.services.telemetry import telemetry

# 结构化日志（E-6）：控制台人读 + deploy/logs 下 JSON lines 轮转。
# ⚠️ 必须在其它模块 import **之前**装配吗？不必——logging 的 handler 是运行期读的，
# 但**越早越好**：这样连 import 期的告警也进得了结构化文件。
setup_logging()
logger = logging.getLogger(__name__)

# 服务间信任边界（企业级落地规约 S-1）：密钥未配置 → 拒绝启动（fail-closed）。
# ⚠️ 刻意放在**模块级**而非 lifespan 内：uvicorn 加载 `app.main:app` 时就会失败，
# 比"服务起来了、首个请求才 4010"更早、更明确地暴露部署错误。
assert_shared_key_configured()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动阶段：初始化长生命周期资源 ----
    # 后续在这里挂载 LLM 客户端、向量库连接池、LangGraph 编译产物
    logger.info("agent 服务启动中 | app=%s build=%s", settings.app_name,
                get_build_info().describe())
    # 会话记忆：回放持久化历史（规约 docs/specs/会话记忆规约.md §6 M-1）。
    # 只加载会话 id，消息按需读——避免启动阶段把全库读进内存
    try:
        from app.services.agent_service import session_memory as _mem

        ids = _mem.load_all_session_ids()
        logger.info("会话记忆已加载: %d 个会话（存储=%s）", len(ids), settings.memory_store)
    except Exception as exc:  # noqa: BLE001 —— 记忆回放失败不能让服务起不来
        logger.warning("会话记忆回放失败（不影响启动）: %s", exc)
    # 治理常驻任务（对账 / G4 的 TTL 与巡检）：与 HTTP 服务同生命周期，
    # 零新依赖（asyncio 常驻循环），任务幂等且可手动触发
    await scheduler.start()
    # 数据中台埋点：内存队列 + 定时批量落 SQLite（容错规约 §8 / 业务规约 §10）
    await telemetry.start()
    # 配置变更留痕（E-8）：本系统无运行时改配置入口，"配置被动过"只能在启动时观测到。
    # 指纹比对放线程池——读 137 个字段 + 一次小文件 IO，不该占住事件循环
    try:
        from app.services.config_audit import audit_config_change  # noqa: PLC0415

        report = await asyncio.to_thread(audit_config_change)
        if report.get("audited"):
            logger.info("配置变更已留痕 | changed=%s keys=%s",
                        report.get("changed"), report.get("changedKeys"))
        elif report.get("firstRun"):
            logger.info("配置快照首次建立（未写审计）| fingerprint=%s",
                        report.get("fingerprint", "")[:12])
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不能让服务起不来
        logger.warning("配置变更留痕失败（不影响启动）: %s", exc)

    yield

    # ---- 关闭阶段：释放资源 ----
    # 会话记忆：冲刷未达阈值的增量，保证重启丢数据窗口收敛到 0（规约 §6 M-1）
    try:
        from app.services.agent_service import session_memory as _mem

        _mem.flush()
        logger.info("会话记忆已冲刷落盘")
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话记忆冲刷失败: %s", exc)
    await telemetry.stop()
    await scheduler.stop()
    await llm_client.close()
    logger.info("agent 服务已关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Drone Agent 智能体服务，由 Java 后端通过 HTTP 调用",
    lifespan=lifespan,
)

# 统一异常响应格式，保证任何错误都返回 JsonResult 结构
register_exception_handlers(app)

# 服务间鉴权（S-1）：除 /v1/health、/ 与文档页外，全部要求 X-Agent-Key。
# ⚠️ 必须写在 CORSMiddleware **之前**：Starlette 的 add_middleware 是"后加的在更外层"，
# 这样 CORS 包住本中间件直接返回的 4010 响应，浏览器直连 8000 调试时也能拿到 CORS 头。
app.add_middleware(SharedKeyMiddleware, shared_key=settings.agent_shared_key,
                   prev_key=settings.agent_shared_key_prev)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 链路上下文（E-6）：采纳 Java 传来的 X-Trace-Id / X-User-Id，回写响应头。
# ⚠️ **最后添加 = 最外层**（Starlette 里后加的在更外层）：这样连 SharedKeyMiddleware
# 直接返回的 4010 也带得上 traceId —— 鉴权失败恰恰是最需要 traceId 的场景。
app.add_middleware(TraceIdMiddleware)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": f"{settings.api_prefix}/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)

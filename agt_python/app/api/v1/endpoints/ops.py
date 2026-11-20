"""运维健康聚合端点（企业级落地规约 E-3，判据 C3）+ 数据备份端点（E-7，判据 C4）
+ 指标端点（E-9，判据 C3）+ 前端报错上报（E-12，判据 C3）。

`GET /v1/ops/health` —— **一个 URL 回答"整套系统现在健康吗"**。

## 鉴权：刻意**不放**白名单

本端点暴露的是**内部组件拓扑 + 故障原因文本**（库路径、外部系统地址、
任务名），信息量远大于 `/v1/health` 的"进程活着"。因此它受 S-1 的
`X-Agent-Key` 保护（`app/core/auth.py` 的豁免清单里没有它）——这是刻意的
默认方向：**新增端点默认受保护**（E-2 已验证）——管理员必须登录（或本机带 key）才能看到组件级原因。

E-7 的备份端点沿用同一方向：`POST /v1/ops/backup/run` 会**写盘**，更不该匿名可触发；
`GET /v1/ops/backup/status` 虽只读，但泄露的是数据目录与库清单，同样不放行。

E-9 的指标端点同理：它暴露的是工具名、任务名、模型名、故障路径这类**内部情报**。
注意这条与 E-3 的取舍**不同**——健康灯是给人看的，所以 8080 侧刻意匿名；
而指标是给**监控系统**看的，Prometheus 完全有能力带 header，因此没有理由匿名放行。
（8080 侧的转发端点沿用既有匿名口径，见 `docs/权限矩阵.md` §3.1 / §3.2。）

E-12 的前端报错上报沿用"默认受保护"：它受 `X-Agent-Key` 保护。
⚠️ 但这**不等于"浏览器要持有密钥"**——浏览器打的是 8080 的转发端点
（`POST /v1/ops/client-error`），由 Java 带密钥转发进来。见下方端点注释。

运维直接自查：
```bash
curl -s --noproxy '*' http://127.0.0.1:8000/v1/ops/health \\
  -H "X-Agent-Key: $(cat deploy/secrets/agent.key)"
```
前端顶栏的状态灯走 8080 的转发端点（浏览器不可能持有服务间密钥）。
"""

import asyncio
import logging

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.response import ApiResponse, ok
from app.services import client_error as client_error_service
from app.services.backup_service import list_backups, run_backup
from app.services.ops_health import collect_health
from app.services.ops_metrics import CONTENT_TYPE, DEFAULT_WINDOW_HOURS, render_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/health", response_model=ApiResponse, summary="运维健康聚合（含组件明细）")
async def ops_health(
    fresh: bool = Query(False, description="true = 跳过 3s 缓存强制重新采集（排障用）"),
) -> ApiResponse:
    return ok(await collect_health(use_cache=not fresh))


# ---------------------------------------------------------------- E-9 指标


@router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus 文本格式指标")
async def ops_metrics(
    window: int = Query(DEFAULT_WINDOW_HOURS,
                        description="分位类指标的统计窗口（小时，1~720）"),
    fresh: bool = Query(False, description="true = 跳过 2s 缓存强制重新渲染（验收用）"),
) -> PlainTextResponse:
    """输出 Prometheus 文本格式（`text/plain; version=0.0.4`）。

    数据全部来自**既有 ODS 表与既有降级计数器**，本端点不新增任何埋点通道、不写任何数据。

    **必须丢到线程池**：SQLite 是同步阻塞调用，直接在协程里跑会卡住事件循环
    （与备份端点、常驻任务同一条理由）。
    """
    text = await asyncio.to_thread(render_metrics, window_hours=window, use_cache=not fresh)
    return PlainTextResponse(content=text, media_type=CONTENT_TYPE)


# ---------------------------------------------------------------- E-7 备份


@router.post("/backup/run", response_model=ApiResponse, summary="手动触发一轮数据备份")
async def ops_backup_run(
    operator: str = Query("manual", description="触发人（记进报告，便于追责）"),
    force: bool = Query(False, description="true = 强制重备当天（默认每日幂等，已有好备份就复用）"),
) -> ApiResponse:
    """手动跑一轮备份（验收、排障、换机器前"先备一份"都用它）。

    默认**每日幂等**：当天已有一份校验通过的备份就直接复用，不重写、也不动
    `drill-report.json`。要覆盖它（例如刚发生了重要数据变更、想立刻把新状态备下来）
    得显式 `force=true`——**这一步刻意不做成默认**，因为覆盖会让当天的演练报告失效。

    **必须丢到线程池**：备份是同步阻塞的 sqlite3 调用，直接在协程里跑会把事件循环
    卡住几十毫秒到几秒——与常驻任务同一条理由（见 `services/scheduler.py` 约束 1）。
    """
    report = await asyncio.to_thread(run_backup, operator, force=force)
    return ok(report)


@router.get("/backup/status", response_model=ApiResponse, summary="备份清单与保留策略")
async def ops_backup_status() -> ApiResponse:
    return ok(list_backups())


# ---------------------------------------------------------------- E-12 前端报错


class ClientErrorReport(BaseModel):
    """前端上报体（E-12）。

    ⚠️ **刻意没有 `userId` 字段** —— 身份只由上游（Java）以 `user_id` 查询参数注入。
    这样"前端传的 userId 一律不采信"就是**结构性**的（字段根本不存在），
    而不是一条要靠人记住的约定。与 S-1「身份只由 Java 注入」同一条铁律。
    """

    model_config = ConfigDict(populate_by_name=True)

    page: str = Field(default="", description="页面 path（服务端会剥掉 ?query 与 #hash）")
    source: str = Field(default="", description="收口来源：vue / window / promise")
    message: str = Field(default="", description="错误消息（服务端截断并脱敏）")
    stack: str = Field(default="", description="错误栈（服务端截断并脱敏）")
    app_version: str = Field(default="", alias="appVersion", description="前端版本（/version.json 的 version）")
    user_agent: str = Field(default="", alias="userAgent", description="浏览器 UA（服务端只留前 200 字符）")


@router.post("/client-error", response_model=ApiResponse, summary="前端运行时报错上报（E-12）")
async def ops_client_error(
    report: ClientErrorReport | None = None,
    user_id: int = Query(0, description="上报人 userId —— **由 Java 从登录态注入**，不接受前端自报"),
) -> ApiResponse:
    """接收前端三处收口（Vue errorHandler / window.onerror / unhandledrejection）上报的错误。

    ## 几个刻意的设计

    **① 永远返回成功**：上报链路上任何异常都在 `client_error.report` 内部消化。
    给前端返回失败没有意义——它既不能重试（会放大风暴），也无法把失败呈现给用户
    （上报是后台行为）。**"错误上报失败"绝不能变成用户可见的错误**，
    否则等于用一个观测能力制造一个新的故障源。

    **② 是否入库如实说**：`data.stored=false` + `reason` 说明为什么没存
    （开关关闭 / 采样丢弃 / 埋点未启用）。静默丢弃会让"看板怎么一直是空的"
    变成一个要翻配置才能回答的问题。

    **③ `user_id` 从查询参数来，不从 body 来**：见 `ClientErrorReport` 的说明。

    **④ body 声明为可选（`ClientErrorReport | None = None`）**，不是遗漏。
    若写成必填的 `report: ClientErrorReport`，FastAPI 会把请求体标成 required，
    于是 `POST` 一个空体（浏览器在"页面已在卸载中"时常见：`sendBeacon` 只带
    Content-Type 不保证带体、或 `fetch` 被 `keepalive` 截断）会直接吃一个
    422 → 被全局处理器转成 `3001 参数校验失败`。**上报端点一旦有前置条件，
    就有一整类错误永远收不到**（恰恰是最难复现的那类）。空体一律按 `{}` 处理。

    **⑤ 丢线程池**：入队虽是内存操作，但落库在 `flush_now` 里做；
    本端点自身很轻，故直接同步处理（无 sqlite 调用）——不需要 `asyncio.to_thread`。
    真正要防的是"报错风暴"：合并窗口（同错同窗口只占一行）才是那条防线，
    见 `app/services/client_error.py` 的 `_bucket`。
    """
    # ⚠️ `by_alias=True` 不是可选项，是必需的：pydantic v2 的 `model_dump()`
    # 默认输出**字段名**（`app_version` / `user_agent`），而服务层 `client_error.report`
    # 按**上报体（wire format，camelCase）**取键 `appVersion` / `userAgent`。
    # 少了这个参数，前端报上来的版本号与 UA 会**静默变成空串**——接口返回 2000、
    # 库里也有行，只有"按版本聚合"这一列永远是「(未知版本)」。
    # （实机验证抓到：E-12 首轮真机验收时 appVersion 全为空，见 DEV_ISSUES。）
    payload = report.model_dump(by_alias=True) if report is not None else {}
    result = client_error_service.report(payload, user_id=user_id)
    return ok(result)


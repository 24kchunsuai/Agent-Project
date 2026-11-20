"""Agent 相关接口。"""

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.core.response import ApiResponse, ok
from app.schemas.agent import ChatRequest, ChatResponse, FeedbackRequest
from app.services import client_error as client_error_service
from app.services.agent_service import agent_service
from app.services.telemetry import telemetry

router = APIRouter(tags=["agent"])

# SSE 心跳间隔（秒）：biz/rag/delta 事件之间的静默期超过该值就发 ping，
# 防中间层（代理/网关）判空闲断流，也让前端"思考中"状态可判定（规约 §6.7 G10）
_SSE_HEARTBEAT_SEC = 15.0


@router.post("/chat", response_model=ApiResponse[ChatResponse], summary="Agent 对话（非流式）")
async def chat(req: ChatRequest) -> ApiResponse:
    data = await agent_service.chat(req)
    return ok(data)


async def _with_heartbeat(gen):
    """SSE 心跳包装：超过 _SSE_HEARTBEAT_SEC 没有真实事件就发 {"type":"ping"}。

    前端/Java 对未知事件类型一律忽略，ping 不影响既有协议（meta/rag/biz/delta/done/error）。
    """
    iterator = gen.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=_SSE_HEARTBEAT_SEC)
        except asyncio.TimeoutError:
            yield {"type": "ping"}
            continue
        except StopAsyncIteration:
            break
        yield event


@router.post("/chat/stream", summary="Agent 对话（SSE 流式）")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 事件协议：meta（意图/traceId）-> ping*（心跳）-> rag?/biz? -> delta* -> done | error。

    恒返 HTTP 200，业务失败通过 error 事件表达，与 JsonResult 的约定思路一致。
    """

    async def event_source():
        async for event in _with_heartbeat(agent_service.chat_stream(req)):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 防 nginx 等代理缓冲
        },
    )


@router.get("/metrics/overview", summary="数据中台可观测总览（降级计数 + 近 24h 聚合）")
async def metrics_overview() -> ApiResponse:
    """容错规约 §8 / 业务规约 §10：降级计数快照 + 四表近 24h 聚合。"""
    return ok(telemetry.overview())


@router.get("/metrics/chats", summary="会话明细列表（看板会话视图数据源，P0）")
async def metrics_chats(
    limit: int = 20, hours: int = 24, intent: str = "", sessionId: str = "",
) -> ApiResponse:
    """数据中台开发规约 §6：按时间倒序的会话列表，点击行进 trace 追溯。

    参数边界在 telemetry.recent_chats 内收口（limit≤100、hours≤90 天）。
    """
    return ok(telemetry.recent_chats(limit=limit, hours=hours, intent=intent, session_id=sessionId))


@router.get("/metrics/traces/{trace_id}", summary="单次对话全轨迹（chat+工具+委派+故障时间线，P0）")
async def metrics_trace(trace_id: str) -> ApiResponse:
    """trace 追溯核心：四表按 trace_id 聚合为时间线；格式非法或不存在返回 found:false。"""
    return ok(telemetry.trace_detail(trace_id))


@router.get("/metrics/timeseries", summary="按天聚合序列（读 DWS 预聚合，P1）")
async def metrics_timeseries(
    metric: str,
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    groupBy: str = "",
) -> ApiResponse:
    """数据中台开发规约 §6：metric/from/to/groupBy 白名单校验后读 metrics_daily。"""
    return ok(telemetry.timeseries(metric=metric, date_from=date_from, date_to=date_to, group_by=groupBy))


@router.get("/metrics/tools/top", summary="工具 TopN（calls/fail_rate/duration 排序，P1）")
async def metrics_tools_top(
    orderBy: str = "calls", limit: int = 10, hours: int = 24,
) -> ApiResponse:
    """窗口内工具聚合排行（读 ODS：跨天失败率需按 total 加权，单日 DWS 行做不了）。"""
    return ok(telemetry.tools_top(order_by=orderBy, limit=limit, hours=hours))


# ---- v4：知识缺口分析（数据中台规约 §5「未命中问题分析」）----


@router.get("/metrics/unanswered", summary="知识缺口分析：未命中聚类 + 补库建议（v4）")
async def metrics_unanswered(hours: int = 0, limit: int = 50) -> ApiResponse:
    """只统计 no_hit / insufficient_evidence；cross_domain 排除（问的不属知识域）。

    问题原文可能不可得（埋点不存正文，记忆易失）——此时只出关键词聚类与计数，
    接口用 questionAvailable=false + reason 如实标注，不伪造原句。
    """
    return ok(telemetry.unanswered(hours=hours, limit=limit))


@router.get("/metrics/docs/usage", summary="命中文档使用度（B 类无人访问巡检的已访问侧，v4）")
async def metrics_docs_usage(hours: int = 0, limit: int = 200) -> ApiResponse:
    """只统计 kept 会话的引用；与知识库文档清单做差集 = 从未被命中的文档。"""
    return ok(telemetry.doc_usage(hours=hours, limit=limit))


# ---- E-12：前端运行时报错（企业级落地规约 E-12，判据 C3）----


@router.get("/metrics/client-errors", summary="前端运行时报错（看板「前端错误」卡数据源，E-12）")
async def metrics_client_errors(hours: int = 24, limit: int = 20) -> ApiResponse:
    """前端报错聚合：总量 + 按页面 / 按版本分组 + 最近明细。

    口径要点（免得看板注释与后端各写一份）：
    * `total` 是 `count` **之和**而非行数——同错同窗口只占一行（合并写）；
    * `distinct` 才是"有多少类错误"；
    * 分组按 count 降序，所以高频小错会排在偶发大错前面：看板回答的是"现在什么最吵"。

    写入侧在 8080 的 `POST /v1/ops/client-error`（浏览器无法持有服务间密钥）。
    """
    return ok(client_error_service.summary(hours=hours, limit=limit))


@router.get("/metrics/costs", summary="LLM 成本与配额聚合（E-13，判据 C7）")
async def metrics_costs(days: int = 7) -> ApiResponse:
    """成本 = ODS 明细按**当前价目表**查询期折算（token × 单价，元/百万 token）。

    口径（与 costing.py / telemetry.costs_summary 三处一致，改一处同步另一处）：
    - 成本**不在 DWS 冻结**：改价后历史按新价重算——看"现在的钱"；对历史账单须用当期价目；
    - 未知模型的 token 单列 `unpricedTokens`（缺价 ≠ 免费照标）；
    - 部门 = 用户画像里**声明过的** `department` 键（读取期派生），没声明的归 unknown；
    - `usage_source='estimate'` 的行是本地估算，看趋势可以、对账不行（totals.estimatedRows 提示）。
    """
    return ok(telemetry.costs_summary(days=days))


# ---- P2：feedback 反馈闭环 + 告警处置联动 + CSV 报表导出（数据中台规约 §6/§8）----


@router.post("/metrics/feedback", summary="提交会话反馈（二值 up/down，挂 trace，P2）")
async def metrics_feedback_submit(req: FeedbackRequest) -> ApiResponse:
    """反馈必须挂在真实会话轨迹上（trace 校验），重复反馈=新事件（只增不改）。

    内容捕获走独立表 + 开关 + TTL（规约 §4.2 口子），原文来自会话记忆。
    """
    return ok(telemetry.submit_feedback(
        trace_id=req.trace_id, rating=req.rating,
        session_id=req.session_id, comment=req.comment))


@router.get("/metrics/feedback", summary="反馈列表（按 trace 关联会话轨迹摘要，P2）")
async def metrics_feedback_list(
    hours: int = 24, limit: int = 50, rating: str = "",
) -> ApiResponse:
    return ok(telemetry.feedback_list(hours=hours, limit=limit, rating=rating))


@router.get("/metrics/feedback/export", summary="评测金标集候选导出（JSONL 文本，P2 验收项）")
async def metrics_feedback_export(hours: int = 2160, limit: int = 500) -> ApiResponse:
    """点踩/点赞会话 → JSONL 候选行（qid=fb-<traceId>），expected_* 留人工审核。"""
    return ok(telemetry.eval_candidates(hours=hours, limit=limit))


@router.get("/metrics/export", summary="CSV 报表导出（view=chats/tools/delegations/faults/feedback，P2）")
async def metrics_export(view: str = "chats", hours: int = 24) -> ApiResponse:
    """返回 {filename, content}，前端 Blob 落盘（Java 纯透传、前端不直连明细）。"""
    return ok(telemetry.export_csv(view=view, hours=hours))


@router.get("/metrics/alarms/link", summary="告警处置链路联动（委派↔jsdstu 告警近似关联，P2）")
async def metrics_alarms_link(request: Request, hours: int = 24) -> ApiResponse:
    """两级关联：同 trace 故障（精确）+ jsdstu 告警 ±30 分钟（近似，口径见指标字典）。

    token 由 Java 侧以看板访问者身份经 Authorization: Bearer 注入（内网网关到
    Python 一跳，与业务工具的 Bearer 约定一致），Python 不落地该 token。
    """
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return ok(await telemetry.alarm_link(hours=hours, token=token or None))

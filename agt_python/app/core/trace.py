"""链路上下文（traceId / userId）的唯一来源（企业级落地规约 E-6）。

字段与链路规则见 `docs/日志规范.md` §2 / §6。

## 三条设计约定

1. **traceId 由 Java(8080) 生成，Python 只采纳。**
   本模块的 `ensure_trace_id()` 在上下文里已有值时**原样返回**，只有
   "压根不在 HTTP 请求里"（定时任务、单测、脚本）才自造一个。
   如果把 Java 传来的 id 丢掉、自己再造一个，那"一次 grep 串三端"就直接失效——
   而失效方式是"每端都有 traceId，但两边的值不同"，很难发现。

2. **用 ContextVar 而不是显式传参。**
   取用点太深（agent_service → rag/retriever → llm），显式穿层要改十几个函数签名。
   `asyncio.to_thread` 会复制当前上下文，所以线程池里的活儿也拿得到同一个 id。

3. **用纯 ASGI 中间件，不用 `BaseHTTPMiddleware`。**
   与 `app/core/auth.py` 同一个理由：后者会为下游复制 contextvars，
   对本项目的 SSE 流式接口（`/v1/agent/chat/stream`）有已知副作用。
   纯 ASGI 里 set/reset 都在同一个上下文，最不容易串号。

## 安全：入站值必须校验后才采用

`X-Trace-Id` / `X-User-Id` 都是**外部输入**，直接塞进日志会造成
**日志伪造（log forging）**：`"` 能破坏 JSON 行结构、`\\n` 能凭空造出一条记录。
所以只接受白名单字符集，不合法就"忽略该输入"（traceId 重造、userId 置空），
而不是"转义后使用"——后者仍会把垃圾带进日志。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable

#: 入站请求头名（ASGI scope 里是小写 bytes）
TRACE_HEADER = "x-trace-id"
USER_HEADER = "x-user-id"

#: 回给出站/前端的响应头名（HTTP 头大小写不敏感，此处按习惯写驼峰）
TRACE_RESPONSE_HEADER = "X-Trace-Id"

#: traceId 白名单：十六进制或常见 id 字符，4~64 位。
#: 只收十六进制会让"别的系统传 `req-abc123`"被丢弃——那本意是复用上游 id，丢掉更亏；
#: 所以放宽到 `[0-9A-Za-z_-]`，但仍拒绝引号/换行/空格等可破坏日志结构的字符。
_TRACE_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}$")

#: userId 只收十进制（Java 侧是 Long，`LoginUser.userId`）
_USER_RE = re.compile(r"^[0-9]{1,20}$")

_trace_id: ContextVar[str] = ContextVar("agt_trace_id", default="")
_user_id: ContextVar[str] = ContextVar("agt_user_id", default="")


def new_trace_id() -> str:
    """造一个新的 traceId（16 位十六进制 = 64 bit，碰撞概率可忽略）。"""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    """当前上下文的 traceId；**不在请求里时为 `""`**（不去造值，见日志规范 §2）。"""
    return _trace_id.get()


def get_user_id() -> str:
    """当前上下文的 userId（十进制字符串）；未知为 `""`。"""
    return _user_id.get()


def set_trace_id(trace_id: str) -> None:
    """显式绑定 traceId（埋点侧沿用，保持 `telemetry.set_trace_id` 的对外语义）。"""
    _trace_id.set(trace_id or "")


def ensure_trace_id() -> str:
    """取当前 traceId；没有就新建并绑定。

    这是**业务代码该用的那个**：`agent_service` 在入口调用它，
    于是"Java 传来的 id"被采纳、"没有 id 的场景"（单测/定时任务）也能自洽。
    """
    tid = _trace_id.get()
    if tid:
        return tid
    tid = new_trace_id()
    _trace_id.set(tid)
    return tid


def bind(trace_id: str, user_id: str = "") -> tuple[Token, Token]:
    """绑定上下文并返回用于还原的 token（中间件按请求成对使用 `bind`/`unbind`）。"""
    return _trace_id.set(trace_id or ""), _user_id.set(user_id or "")


def unbind(tokens: tuple[Token, Token] | None) -> None:
    """还原上下文。**必须放在 finally 里**：否则同一个上下文被复用时（如测试、
    常驻任务的循环）会带着上一个请求的 id，日志开始"张冠李戴"。"""
    if not tokens:
        return
    trace_token, user_token = tokens
    try:
        _trace_id.reset(trace_token)
        _user_id.reset(user_token)
    except ValueError:               # token 来自另一个上下文（不同 task）——忽略即可
        _trace_id.set("")
        _user_id.set("")


def read_trace_header(scope: dict[str, Any]) -> str:
    """从 ASGI scope 取并校验 `X-Trace-Id`；不合法返回 `""`（调用方自行决定是否重造）。"""
    return _read(scope, TRACE_HEADER, _TRACE_RE)


def read_user_header(scope: dict[str, Any]) -> str:
    """从 ASGI scope 取并校验 `X-User-Id`；不合法返回 `""`。

    ⚠️ **只用于日志，不做任何鉴权判定**：身份以 Java 侧会话为准，
    这个头只是"把已知身份同步过来，便于按人检索日志"。
    """
    return _read(scope, USER_HEADER, _USER_RE)


def _read(scope: dict[str, Any], name: str, pattern: re.Pattern[str]) -> str:
    target = name.encode("ascii")
    for header, value in scope.get("headers") or ():
        if header == target:
            try:
                text = value.decode("latin-1").strip()
            except (UnicodeDecodeError, AttributeError):   # pragma: no cover —— 防御式
                return ""
            return text if pattern.match(text) else ""
    return ""


class TraceIdMiddleware:
    """纯 ASGI 中间件：采纳/生成 traceId + userId，回写响应头，并按请求记一条
    `http.request` 日志（带 `durationMs`）。

    **顺序**：在 `main.py` 中**最后**添加（Starlette 里后加的在外层），
    这样鉴权拒绝（4010）这类响应也带得上 traceId —— 排障恰恰最需要它。
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = read_trace_header(scope)
        trace_id = incoming or new_trace_id()
        user_id = read_user_header(scope)
        tokens = bind(trace_id, user_id)
        status = 0

        # 回写响应头 + 顺手记下状态码。
        # 一个响应只会 start 一次（SSE 也一样：start 之后全是 body 帧），
        # 所以这里直接改这一条消息是安全的。
        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                headers.append((TRACE_RESPONSE_HEADER.lower().encode("ascii"),
                                trace_id.encode("ascii")))
                message = dict(message, headers=headers)
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            record_http_request(scope, elapsed_ms, status)
            unbind(tokens)


def record_http_request(scope: dict[str, Any], duration_ms: int, status: int) -> None:
    """记一条 `http.request`（`docs/日志规范.md` §3）。

    ✅ 刻意**只记方法与路径，不记查询串**：查询串里常有 `?key=...`、用户输入，
    记了就得依赖正则脱敏，而"不记"比"脱敏"更可靠（日志规范 §7）。
    任何异常都被吞掉：埋点绝不能让一个已经成功的请求变成 500（埋点铁律）。
    """
    try:
        logger = logging.getLogger("app.http")
        path = scope.get("path", "")
        method = scope.get("method", "")
        # 探针每 30s 一次，全量记会把结构化日志冲成噪音；只在非 2xx 时记，
        # 或对探针降级为 DEBUG。健康探针是 uptime 这类指标的来源，走指标不靠日志。
        if path in _QUIET_PATHS and 200 <= status < 300:
            return
        logger.info(
            "%s %s -> %s",
            method, path, status or "-",
            extra={"event": "http.request", "durationMs": duration_ms},
        )
    except Exception:  # noqa: BLE001 —— 埋点失败不影响请求
        pass


#: 高频探测/上报路径：**成功时**不写结构化日志，失败时仍会记 ——
#: 健康探针（每 30s 一次）与前端报错上报（E-12，报错风暴时一次会话能来几十条）
#: 全量记会把结构化日志冲成噪音，而它们各自有专门的观测出口
#: （E-3 健康聚合 / E-9 指标 / E-12 `client_error` 表）。
#: 判据只看成功码：`/v1/ops/client-error` 自己返回 2000，所以"上报接口报错"这件事
#: 要靠 5xx/4xx 才记——那种情况下这里会照常留痕，不会被静音。
_QUIET_PATHS = frozenset({
    "/v1/health", "/v1/ops/health", "/v1/ops/metrics", "/", "/v1/ops/client-error",
})

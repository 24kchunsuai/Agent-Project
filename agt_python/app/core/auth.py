"""服务间信任边界：Java(8080) → Python(8000) 共享密钥校验（企业级落地规约 §S-1）。

## 为什么需要
Python 侧原本**零鉴权**：`ChatRequest.userId` 由请求体声明即信、`/v1/profile/*`
可直调，而调用者身份与角色都由 Java 层注入。于是任何**网络可达者**都能伪装任意
用户（含 admin 的写操作）。企业交付时这是不可接受的。

S-1 立两道门，缺一不可：
1. **网络层**：8000 只绑 `127.0.0.1`（外网不可达）；
2. **应用层**（本模块）：本机其他进程也不能冒充 Java —— 所有请求必须带
   `X-Agent-Key`，值与 `settings.agent_shared_key` 全等。

## 三条硬约束
- **fail-closed**（规约 §6 铁律 1）：密钥未配置 → 服务**拒绝启动**；校验不通过 →
  返回 4010，不放行、不降级。鉴权是唯一允许 fail-closed 的地方。
- **纯 ASGI 中间件**，不用 `BaseHTTPMiddleware`：本项目有 SSE 流式接口
  （`/v1/agent/chat/stream`），后者会包一层 anyio 流并复制 contextvars，对流式
  响应有已知副作用；纯 ASGI 只读 `scope["headers"]` 后原样转发，对 SSE 零干扰。
- **埋点不得影响判定**：校验失败要落 `fault_event`（可查账），但埋点自身异常
  绝不能让"该拒绝的请求"变成放行或 500 —— 所以那段用 try/except 包住。

## E-10 双密钥过渡窗口（轮换不停服）
本服务是**非 reload** 部署，改密钥必须重启；Java 侧同理。若只认一把密钥，
轮换时必然出现"一边已换、一边未换"的窗口，那个窗口里全链路 4010。
因此轮换期同时接受 `AGENT_SHARED_KEY`（新）与 `AGENT_SHARED_KEY_PREV`（旧）：

- **顺序：先消费者、后生产者**。本服务先两把都收 → 再改 Java → 最后清 PREV。
- **旧密钥放行不是故障**：只计数（`count_event`）**不落 `fault_event`**，
  否则看板故障数会凭空变大。计数走
  `agt_degrade_events_since_start_total{path="auth_key_legacy_used"}`，
  它是"能不能清掉 PREV"的**机器可判**放行条件。
- **`PREV` 与当前密钥同值 → 拒绝启动**：那是"以为在轮换其实没换"，
  会让人看到计数恒为 0 而误判可以停用旧密钥。

## 豁免清单（只放"不含业务/用户数据"的探针与文档页）
`/v1/health`、`/`、`/docs`、`/redoc`、`/openapi.json`。
⚠️ 其余全部要 key —— 包括 `/v1/profile/*`（含用户隐私）与 `/v1/kb/governance/*`
（治理写操作）。新增端点时**默认受保护**，这是刻意的默认方向。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.response import StatusCode, fail

logger = logging.getLogger(__name__)

#: 请求头名（HTTP 头大小写不敏感，比对前统一小写）
HEADER_NAME = "x-agent-key"

#: 豁免路径（精确匹配）。判定按"必须精确相等"，避免 `/v1/health` 前缀
#: 顺带放行 `/v1/healthz` 这类未来新增的端点。
_EXEMPT_EXACT = frozenset({"/", "/docs", "/redoc", "/openapi.json"})

#: 豁免路径（带尾斜杠的容错形式 + 文档页的重定向子路径）
_EXEMPT_EXTRA = frozenset({"/docs/", "/redoc/", "/docs/oauth2-redirect"})


def accepted_keys(current: str | None = None, prev: str | None = None) -> tuple[str, ...]:
    """本服务当前接受的密钥，**顺序即优先级**（下标 0 = 当前密钥）。

    E-10 密钥轮换：轮换期 `agent_shared_key_prev` 非空，两把都收；
    轮换结束后清空该项，旧密钥立刻失效。

    去重与去空的顺序有讲究：先过滤空值再判重，避免"两把都空"被当成"已配置"。
    """
    cur = (settings.agent_shared_key if current is None else current) or ""
    old = (settings.agent_shared_key_prev if prev is None else prev) or ""
    keys: list[str] = []
    for k in (cur, old):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return tuple(keys)


def assert_shared_key_configured(key: str | None = None, prev: str | None = None) -> None:
    """fail-closed：密钥未配置就拒绝启动（规约 S-1 验收标准之一）。

    传入 `key` / `prev` 便于单测直接验证；默认取 `settings`。

    调用点在 `app/main.py` **模块级**（不是 lifespan 内）——这样 uvicorn 加载
    `app.main:app` 时就会失败，比"起来了但在首个请求才发现"更早暴露部署错误。

    E-10 补一条：**`AGENT_SHARED_KEY_PREV` 若与当前密钥同值则拒绝启动**。
    这不是洁癖——如果两把一样，说明操作者以为自己在轮换其实没换：
    他会在"观察 legacy 计数"那一步看到 0（因为命中的永远是当前密钥），
    于是得出"可以清 PREV 了"的结论，而真实情况是**旧密钥从未被停用**。
    静默的安全债比启动失败危险得多。
    """
    cur = (settings.agent_shared_key if key is None else key) or ""
    old = (settings.agent_shared_key_prev if prev is None else prev) or ""
    if not cur.strip():
        raise RuntimeError(
            "AGENT_SHARED_KEY 未配置：服务拒绝启动（企业级落地规约 S-1，fail-closed）。\n"
            "  生成密钥：python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  写入位置：agt_python/.env 的 AGENT_SHARED_KEY；\n"
            "  Java 侧用同名环境变量注入**同一个值**（两侧不一致会被 4010 拒绝）。"
        )
    if old.strip() and old.strip() == cur.strip():
        raise RuntimeError(
            "AGENT_SHARED_KEY_PREV 与 AGENT_SHARED_KEY 同值：服务拒绝启动。\n"
            "  两把相同的密钥不构成轮换——请把 PREV 设为**真正要被停用的那把旧密钥**，\n"
            "  轮换结束后**清空** AGENT_SHARED_KEY_PREV（而不是填成当前值）。\n"
            "  开始轮换的方法见 docs/密钥与配置管理.md。"
        )


def _is_exempt(path: str) -> bool:
    """是否豁免鉴权（探针 / 文档页）。"""
    if path in _EXEMPT_EXACT or path in _EXEMPT_EXTRA:
        return True
    return path in {f"{settings.api_prefix}/health", f"{settings.api_prefix}/health/"}


def _read_header(scope: dict[str, Any]) -> bytes:
    """从 ASGI scope 取 X-Agent-Key 的**原始字节**（缺失返回 `b""`）。

    为什么返回 bytes 而不是 str：`hmac.compare_digest` 对 str 要求**双方都是 ASCII**，
    否则抛 `TypeError`。请求头是攻击者可控的——发一个含 `0xFF` 的头就能让
    "该 4010" 变成 500（既污染看板，又给了一个廉价的探测信号）。
    直接比字节则对任意输入都安全，且与解码环节解耦。
    """
    for name, value in scope.get("headers") or ():
        if name == b"x-agent-key":
            return value if isinstance(value, bytes) else bytes(value or b"")
    return b""


def _report_reject(scope: dict[str, Any], had_key: bool) -> None:
    """落一条 fault_event（可查账）；任何失败都不得影响拒绝本身。"""
    path = scope.get("path", "")
    client = (scope.get("client") or ("unknown", 0))[0]
    reason = "X-Agent-Key 不匹配" if had_key else "X-Agent-Key 缺失"
    logger.warning("服务间鉴权拒绝 path=%s client=%s reason=%s", path, client, reason)
    try:
        from app.services.telemetry import PATH_AUTH_KEY_REJECTED, telemetry

        telemetry.record_fault(
            PATH_AUTH_KEY_REJECTED, "", f"{reason}（path={path} client={client}）"
        )
    except Exception:  # noqa: BLE001 —— 埋点失败绝不影响"拒绝"这个决定
        logger.debug("鉴权拒绝埋点写入失败（忽略）", exc_info=True)


#: "旧密钥被使用"的 WARNING 是否已喊过（每进程一次，避免刷屏）
_legacy_warned = False


def _report_legacy_used(scope: dict[str, Any]) -> None:
    """放行，但用的是**上一把**密钥（E-10 轮换期）。

    刻意与"拒绝"分开：
    - 只 `count_event`（计数），**不落 `fault_event`** —— 请求成功处理完毕，
      把它计入故障数会让看板说谎；
    - 日志**每条进程只喊一次 WARNING**，之后静默。轮换窗口可能持续数天，
      高频接口上逐条 WARN 会把这个信号淹掉；长期趋势由指标负责
      （`agt_degrade_events_since_start_total{path="auth_key_legacy_used"}`）。
    """
    global _legacy_warned

    path = scope.get("path", "")
    try:
        from app.services.telemetry import PATH_AUTH_KEY_LEGACY_USED, telemetry

        telemetry.count_event(
            PATH_AUTH_KEY_LEGACY_USED, f"轮换期使用上一把密钥放行（path={path}）"
        )
    except Exception:  # noqa: BLE001 —— 埋点失败绝不影响"放行"这个决定
        logger.debug("旧密钥放行埋点写入失败（忽略）", exc_info=True)
    if not _legacy_warned:
        _legacy_warned = True
        logger.warning(
            "服务间鉴权：请求使用的是**上一把**密钥（AGENT_SHARED_KEY_PREV）并已放行。"
            "这属轮换过渡窗口的正常现象；确认指标 "
            "agt_degrade_events_since_start_total{path=\"auth_key_legacy_used\"} "
            "连续一段时间不再增长后，清空 AGENT_SHARED_KEY_PREV 并重启即可停用旧密钥。"
            "（本条每进程只提示一次，逐次计数见指标）"
        )


class SharedKeyMiddleware:
    """纯 ASGI 中间件：除豁免路径外，全部要求 `X-Agent-Key` 命中**已接受的密钥之一**。

    接受集合 = `(当前密钥, 上一把密钥)`（E-10），顺序即优先级；轮换期两把都收。

    为什么不用 `app.middleware("http")` / `BaseHTTPMiddleware`：见模块 docstring。
    """

    def __init__(self, app: Callable[..., Awaitable[None]], *, shared_key: str,
                 prev_key: str = "") -> None:
        self.app = app
        self._keys = accepted_keys(shared_key, prev_key)
        # 预编码成 bytes：与 `_read_header` 的原始字节同构，避免 str/bytes 混比
        self._key_bytes = tuple(k.encode("utf-8") for k in self._keys)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # 非 HTTP（lifespan / websocket）直接放行：lifespan 必须能跑，否则服务起不来
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_exempt(path):
            await self.app(scope, receive, send)
            return

        provided = _read_header(scope)
        # 常量时间比较：字符串 != 比较会因前缀匹配长度不同而泄耗时（时序侧信道）。
        # 逐个都比、**不提前 break**：否则"第一把就命中"与"第二把才命中"耗时不同，
        # 等于把"哪一把在轮换"透出去。
        hit_current = bool(provided) and hmac.compare_digest(provided, self._key_bytes[0])
        hit_legacy = False
        for kb in self._key_bytes[1:]:
            hit_legacy = hmac.compare_digest(provided, kb) or hit_legacy
        if hit_current:
            await self.app(scope, receive, send)
            return
        if hit_legacy:
            # 走到这里必然不是当前密钥（上面已 return），故命中即"上一把"。
            # 只支持一对 (current, prev)，不引入"第三把"——轮换期两把足够，
            # 而多把并存会让"何时能停用"变成一道猜谜题。
            _report_legacy_used(scope)
            await self.app(scope, receive, send)
            return

        _report_reject(scope, had_key=bool(provided))
        # HTTP 恒 200 + body 里带 code：与 Java 侧 JsonResult 约定一致
        # （Java RestClient 只需一套解析逻辑；4010 前端已有"重新登录"处理）
        response = JSONResponse(
            status_code=200,
            content=fail(StatusCode.UNAUTHORIZED).model_dump(),
        )
        await response(scope, receive, send)

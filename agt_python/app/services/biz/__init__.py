"""jsdstu-3（无人机业务后端）HTTP 客户端。

设计要点：
- Python 侧不做登录。Bearer token 由 Java 层鉴权后经 metadata.identity 注入，
  本客户端只负责"带 token 发请求"——用户身份优先，绝不偷偷用系统账号；
- 4010（未登录/过期）归一化为 BizAuthError，由 AgentLoop 转成
  "请重新登录"的固定话术；其余非 2000 归一化为 BizError（message 原样透传）；
- 只封装 GET 查询类调用；写操作（POST）在 P2 落地时走独立方法，
  便于加危险白名单与二次确认拦截。
"""

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services.fault import AUTH_EXPIRED_REPLY

logger = logging.getLogger(__name__)


class BizError(Exception):
    """业务接口返回非 2000（或网络故障），message 可直接展示给用户。"""


class BizAuthError(BizError):
    """业务系统鉴权失败（4010）：token 缺失/过期，需引导用户重新登录。"""


class JsDstuClient:
    """访问 jsdstu-3 的唯一出口，约定同 AgentClient（Java 侧）。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.jsdstu_base_url,
                timeout=httpx.Timeout(settings.jsdstu_timeout, connect=5),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        read_timeout: float | None = None,
        retries: int = 0,
    ) -> Any:
        """统一请求：带 Bearer token，解包 JsonResult，错误归一化。

        data=表单字段（仅 multipart 上传的普通字段用；FORM 绑定的写接口
        走 params=query string，见 fence_save/user_save 的既有约定）；
        files=多部分文件；read_timeout=本次请求的读超时覆盖（上传用）。

        幂等重试（容错规约 §6.5，G7/G9）：只有调用方显式传 retries>0 的
        **幂等查询（GET）**才会对网络错误/5xx 重试；写操作（POST/PUT/DELETE）
        的 retries 恒为 0——非幂等重试等于制造事故，宁可贵一点。
        """
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        attempts = max(1, retries + 1)
        resp = None
        for attempt in range(attempts):
            try:
                http = await self._http()
                timeout = httpx.Timeout(read_timeout or settings.jsdstu_timeout, connect=5)
                resp = await http.request(
                    method, path, params=params, json=json_body, data=data,
                    files=files, headers=headers, timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < attempts - 1:
                    logger.warning(
                        "业务接口网络异常（第 %d 次，将重试）%s %s: %s",
                        attempt + 1, method, path, exc,
                    )
                    await asyncio.sleep(settings.jsdstu_retry_backoff)
                    continue
                logger.warning("业务接口网络异常 %s %s: %s", method, path, exc)
                raise BizError("业务系统暂时联系不上，请稍后重试") from exc
            if resp.status_code >= 500 and attempt < attempts - 1:
                # 5xx 是上游瞬时故障，幂等请求可安全重发
                logger.warning(
                    "业务接口上游 %d（第 %d 次，将重试）%s %s",
                    resp.status_code, attempt + 1, method, path,
                )
                await asyncio.sleep(settings.jsdstu_retry_backoff)
                continue
            break

        if resp.status_code != 200:
            logger.warning("业务接口 HTTP %d: %s %s", resp.status_code, method, path)
            raise BizError("业务系统响应异常，请稍后重试")

        data = resp.json()
        code = data.get("code")
        if code == 4010:
            raise BizAuthError(AUTH_EXPIRED_REPLY)
        if code != 2000:
            message = str(data.get("message") or "业务操作失败")
            logger.info("业务接口返回 %s: %s %s", code, method, path)
            raise BizError(message)
        return data.get("data")

    # ---- 查询类（P1 起供各业务工具使用） ----

    async def get(self, path: str, *, token: str | None, params: dict[str, Any] | None = None) -> Any:
        # GET 幂等：网络错误/5xx 自动重试 1 次（规约 §6.5 G7/G9）
        return await self._request(
            "GET", path, token=token, params=params, retries=settings.jsdstu_query_retry
        )

    # ---- 写操作类（P2 起，调用前必须经 AgentLoop 危险白名单拦截） ----

    async def post(
        self,
        path: str,
        *,
        token: str | None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """POST 写操作。json_body=JSON 请求体；data=表单（jsdstu-3 部分老接口
        未标 @RequestBody，走 form/query 绑定，如 fence save、user save——
        **表单字段统一走 params（query string）**，见规约 §5.1）。"""
        return await self._request("POST", path, token=token, json_body=json_body, params=data)

    async def put(
        self,
        path: str,
        *,
        token: str | None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """PUT 写操作（如告警规则更新 /v1/alarm/rule/update，见规约 §3.4）。"""
        return await self._request("PUT", path, token=token, json_body=json_body)

    async def delete(
        self,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """DELETE 写操作（如媒体删除 /v1/media/delete，id 在 query，见规约 §3.5）。"""
        return await self._request("DELETE", path, token=token, params=params)

    async def upload(
        self,
        path: str,
        *,
        token: str | None,
        fields: dict[str, Any],
        files: list[tuple[str, tuple[str, bytes, str]]],
    ) -> Any:
        """multipart 文件上传（如媒体上传 /v1/media/upload，taskId+files+description）。

        files 元素形如 ("files", ("a.jpg", b"...", "image/jpeg"))；
        读超时用上传专用配置（文件可达 100MB）。"""
        return await self._request(
            "POST", path, token=token, data=fields, files=files,
            read_timeout=float(settings.jsdstu_upload_timeout),
        )

    async def health(self) -> bool:
        """业务系统连通性探测（不走鉴权，仅用于健康页展示）。"""
        try:
            http = await self._http()
            resp = await http.get("/v1/user/login", headers={"Authorization": ""})
            # 登录页 GET 405 也算"活着"
            return resp.status_code < 500
        except Exception:
            return False


jsdstu_client = JsDstuClient()

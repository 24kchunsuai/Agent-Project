"""全局异常处理。

目的是让**所有**错误响应都符合 JsonResult 约定：
HTTP 状态码恒为 200，业务结果由 body 里的 code 表达。
这样 Java 侧只需要一套解析逻辑，不会因为 422/500 而解析失败。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.response import ApiResponse, StatusCode, fail, MESSAGES

logger = logging.getLogger(__name__)


class ServiceException(Exception):
    """业务异常：message 面向用户可读，code 走 3000 段。"""

    def __init__(self, status_code: StatusCode = StatusCode.OPERATION_FAILED, message: str | None = None) -> None:
        self.status_code = status_code
        self.message = message or MESSAGES.get(int(status_code), "操作失败")
        super().__init__(self.message)


def _payload(status_code: StatusCode, message: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=200, content=fail(status_code, message).model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceException)
    async def _handle_service(request: Request, exc: ServiceException) -> JSONResponse:
        logger.warning("业务异常 path=%s code=%s msg=%s", request.url.path, exc.status_code, exc.message)
        return _payload(exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
        detail = f"{loc}: {first.get('msg', '')}".strip(": ") or "参数校验失败"
        logger.warning("参数校验失败 path=%s detail=%s", request.url.path, detail)
        return _payload(StatusCode.VALIDATE_ERROR, detail)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        logger.warning("HTTP 异常 path=%s status=%s detail=%s", request.url.path, exc.status_code, exc.detail)
        if exc.status_code == 404:
            return _payload(StatusCode.DATA_UNEXISTS, "请求数据不存在")
        return _payload(StatusCode.OPERATION_FAILED, str(exc.detail))

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常 path=%s", request.url.path)
        return _payload(StatusCode.OPERATION_FAILED, "服务内部错误")

"""统一响应体。

与 Java 侧 JsonResult 完全一致：code / message / data。
成功 2000，失败 3000 段，前端只需处理一套解包逻辑。
"""

from enum import IntEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class StatusCode(IntEnum):
    """与 Java 侧 cn.tedu.agt.base.response.StatusCode 保持同一编码段。"""

    OPERATION_SUCCESS = 2000
    OPERATION_FAILED = 3000
    VALIDATE_ERROR = 3001
    DATA_UNEXISTS = 3002
    AGENT_UNAVAILABLE = 3010
    AGENT_CALL_ERROR = 3011
    # 服务间鉴权失败（S-1）：编码与文案与 Java 侧 StatusCode.UNAUTHORIZED 完全对齐，
    # 前端因此可以直接复用现成的"未登录/重新登录"处理，不需要新增分支。
    UNAUTHORIZED = 4010


MESSAGES: dict[int, str] = {
    StatusCode.OPERATION_SUCCESS: "操作成功",
    StatusCode.OPERATION_FAILED: "操作失败",
    StatusCode.VALIDATE_ERROR: "参数校验失败",
    StatusCode.DATA_UNEXISTS: "请求数据不存在",
    StatusCode.AGENT_UNAVAILABLE: "智能体服务不可用，请稍后重试",
    StatusCode.AGENT_CALL_ERROR: "智能体调用失败",
    StatusCode.UNAUTHORIZED: "未登录或登录已过期，请重新登录",
}


class ApiResponse(BaseModel, Generic[T]):
    code: int = int(StatusCode.OPERATION_SUCCESS)
    message: str = MESSAGES[StatusCode.OPERATION_SUCCESS]
    data: T | None = None


def ok(data: Any = None, message: str | None = None) -> ApiResponse:
    """成功响应。"""
    return ApiResponse(
        code=int(StatusCode.OPERATION_SUCCESS),
        message=message or MESSAGES[StatusCode.OPERATION_SUCCESS],
        data=data,
    )


def fail(status_code: StatusCode = StatusCode.OPERATION_FAILED, message: str | None = None) -> ApiResponse:
    """失败响应，code 与 Java 侧错误码体系对齐。"""
    return ApiResponse(
        code=int(status_code),
        message=message or MESSAGES.get(int(status_code), MESSAGES[StatusCode.OPERATION_FAILED]),
        data=None,
    )

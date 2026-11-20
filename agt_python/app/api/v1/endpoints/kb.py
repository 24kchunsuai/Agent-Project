"""知识库管理接口。

POST   /v1/kb/documents              上传并入库单篇文档（切片 → embedding → Milvus）
GET    /v1/kb/documents              文档列表（按 doc_id 聚合，分页）
DELETE /v1/kb/documents/{id}         移入回收站（软删除，可恢复）
GET    /v1/kb/recycle                回收站列表（分页）
POST   /v1/kb/recycle/{id}/restore   从回收站恢复
DELETE /v1/kb/recycle/{id}           彻底删除（不可恢复）
GET    /v1/kb/stats                  知识库统计

调用链：Vue(9090) → Java agt_backend(8080) → 本服务(8000)。
owner 一律由 Java 层从登录态注入，前端传的不作数（与 agent 链路同一约定）；
直接调本接口调试时可手动传 owner / scope。

请求体用 JSON 传正文（不用 multipart），原因：
  1. 当前只支持 md/txt 文本格式，正文直接放 JSON 更简单；
  2. Java 侧无需处理 multipart 转发，也省掉 python-multipart 依赖。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from app.core.response import ApiResponse, StatusCode, fail, ok
from app.rag.glm import GlmApiError
from app.services.kb_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    KbConfirmRequired,
    KbDuplicateError,
    KbError,
    kb_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["kb"])

PAGE_DESC = f"页码，从 1 起（默认 1）"
PAGE_SIZE_DESC = f"每页条数（默认 {DEFAULT_PAGE_SIZE}，上限 {MAX_PAGE_SIZE}）"


class KbUploadRequest(BaseModel):
    fileName: str = Field(min_length=1, max_length=255, description="文件名（含扩展名）")
    content: str = Field(min_length=1, description="文档正文（UTF-8 文本）")
    category: str = Field(default="", max_length=64, description="分类，可空")
    visibility: str = Field(default="public", description="可见性：public / private")
    owner: str | None = Field(
        default=None, max_length=64,
        description="文档归属用户；由 Java 层从登录态注入，直接调试时可手动传",
    )


@router.post("/documents", summary="上传文档并入库")
def upload_document(req: KbUploadRequest):
    """切片、向量化并写入 Milvus。同一 doc_id（文件名主干）重复上传为幂等覆盖。"""
    try:
        data = kb_service.ingest(
            file_name=req.fileName,
            content=req.content,
            owner=req.owner or "default",
            category=req.category or "",
            visibility=req.visibility or "public",
        )
    except KbDuplicateError as e:
        # 去重门拦截：治理生效的正常结果，返回结构化信息供前端区分提示
        logger.info("知识库入库被去重门拦截 | file=%s layer=%s", req.fileName, e.layer)
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR), message=str(e),
                           data={"duplicate": True, "layer": e.layer, **e.detail})
    except KbError as e:
        # 参数/内容问题：明确告知用户哪里不对，不吞成 500
        logger.warning("知识库入库被拒 | file=%s reason=%s", req.fileName, e)
        return fail(StatusCode.VALIDATE_ERROR, str(e))
    except GlmApiError as e:
        logger.error("知识库入库 embedding 失败 | file=%s err=%s", req.fileName, e)
        return fail(StatusCode.AGENT_CALL_ERROR, f"向量化服务暂不可用：{e}")
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库入库异常 | file=%s", req.fileName)
        return ApiResponse(
            code=int(StatusCode.OPERATION_FAILED),
            message=f"入库失败：{type(e).__name__}: {e}",
            data=None,
        )
    return ok(data)


def _kb_fail(e: KbError) -> ApiResponse:
    """KbError → 业务响应。

    区分"对象不存在"（3002）与"参数/权限/冲突问题"（3001）：前端对前者
    一般刷新列表即可，对后者需要用户改操作，提示口径不同。

    确认门（G2）额外带回结构化信息：前端需要知道"该复述的文档名是什么"，
    否则只能盲点确认。
    """
    if isinstance(e, KbConfirmRequired):
        return ApiResponse(code=int(StatusCode.VALIDATE_ERROR), message=str(e),
                           data={"requireConfirm": True, "gate": e.gate, **e.detail})
    msg = str(e)
    if "不存在" in msg or "没有该文档" in msg:
        return fail(StatusCode.DATA_UNEXISTS, msg)
    return fail(StatusCode.VALIDATE_ERROR, msg)


def _unavailable(action: str, e: Exception) -> ApiResponse:
    logger.exception("%s异常", action)
    return ApiResponse(
        code=int(StatusCode.AGENT_UNAVAILABLE),
        message=f"知识库暂不可用：{type(e).__name__}",
        data=None,
    )


@router.get("/documents", summary="文档列表（分页）")
def list_documents(
    owner: str | None = Query(default=None, max_length=64, description="当前用户"),
    scope: str = Query(default="mine", description="mine=仅自己和公开；all=全部（管理员）"),
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE,
                           alias="pageSize", description=PAGE_SIZE_DESC),
):
    """按 doc_id 聚合的文档列表（分页）。scope=all 供管理员查看全库。"""
    try:
        return ok(kb_service.list_documents(
            owner=owner, scope=scope, page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        return _unavailable("文档列表查询", e)


@router.delete("/documents/{doc_id}", summary="移入回收站（软删除）")
def delete_document(
    doc_id: str = Path(min_length=1, max_length=120),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可删任意文档"),
):
    """把文档移入回收站：切片从向量库移除（立即不可检索），备份保留可恢复。"""
    try:
        return ok(kb_service.delete_document(doc_id, owner=owner, is_admin=is_admin))
    except KbError as e:
        return _kb_fail(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("文档删除失败 | doc_id=%s", doc_id)
        return ApiResponse(
            code=int(StatusCode.OPERATION_FAILED),
            message=f"删除失败：{type(e).__name__}",
            data=None,
        )


@router.get("/recycle", summary="回收站列表（分页）")
def list_recycled(
    owner: str | None = Query(default=None, max_length=64, description="当前用户"),
    scope: str = Query(default="mine", description="mine=自己删除的；all=全部（管理员）"),
    page: int = Query(default=1, ge=1, description=PAGE_DESC),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE,
                           alias="pageSize", description=PAGE_SIZE_DESC),
):
    """已移入回收站的文档列表，含删除时间与删除人。"""
    try:
        return ok(kb_service.list_recycled(
            owner=owner, scope=scope, page=page, page_size=page_size))
    except Exception as e:  # noqa: BLE001
        return _unavailable("回收站列表查询", e)


@router.post("/recycle/{doc_id}/restore", summary="从回收站恢复")
def restore_document(
    doc_id: str = Path(min_length=1, max_length=120),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可恢复任意文档"),
    confirm: bool = Query(default=False, description="L3 确认门：相似度过高时带 true 重发"),
):
    """按原切片回灌向量（备份带 dense 时**零 embedding**，离线可恢复）。

    恢复前过 L3 语义查重确认门：若该文档与在库语料余弦相似度 ≥ 阈值，
    返回 `3001 + data.requireConfirm=true` + 相似文档列表（**此时并未恢复**），
    用户确认后带 `confirm=true` 重发。

    确认门为什么不能用 2000 返回：本次操作没有发生，2000 会让调用方（含前端与验收脚本）
    直接判成"恢复成功"——实测脚本就被这个假成功骗过，还连带误判了后续断言。
    用 3001（参数校验失败，缺 confirm 参数）语义准确，且与彻底删的确认门同形。
    关键约束是 **data 必须能穿过 Java 透传层**：Java 全局异常处理器只保留 message，
    故 Java 侧为确认门单开了一条携带 detail 的异常通道（ConfirmRequiredException）。
    """
    try:
        return ok(kb_service.restore_document(
            doc_id, owner=owner, is_admin=is_admin, confirm=confirm))
    except KbError as e:
        return _kb_fail(e)
    except GlmApiError as e:
        return fail(StatusCode.AGENT_CALL_ERROR, f"向量化服务暂不可用，请稍后重试：{e}")
    except Exception as e:  # noqa: BLE001
        logger.exception("文档恢复失败 | doc_id=%s", doc_id)
        return ApiResponse(
            code=int(StatusCode.OPERATION_FAILED),
            message=f"恢复失败：{type(e).__name__}",
            data=None,
        )


@router.delete("/recycle/{doc_id}", summary="彻底删除（不可恢复）")
def purge_document(
    doc_id: str = Path(min_length=1, max_length=120),
    owner: str | None = Query(default=None, max_length=64),
    is_admin: bool = Query(default=False, description="管理员可彻底删除任意文档"),
    confirm: bool = Query(default=False, description="二次确认：必须为 true"),
    confirm_doc_name: str | None = Query(
        default=None, max_length=255, alias="confirmDocName",
        description="二次确认：复述文档名（文档标题 / docId / 文件名主干三者之一）"),
):
    """三清（向量 + 备份 + 源文件）后清除登记，审计永久保留。

    双确认：**只允许管理员**，且必须 `confirm=true` 并且 `confirmDocName` 与文档名一致；
    未满足时返回 3001 + `data.requireConfirm=true`（含 `expectedDocName`），不执行任何删除。
    """
    try:
        return ok(kb_service.purge_document(
            doc_id, owner=owner, is_admin=is_admin,
            confirm=confirm, confirm_doc_name=confirm_doc_name))
    except KbError as e:
        return _kb_fail(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("文档彻底删除失败 | doc_id=%s", doc_id)
        return ApiResponse(
            code=int(StatusCode.OPERATION_FAILED),
            message=f"彻底删除失败：{type(e).__name__}",
            data=None,
        )


@router.get("/stats", summary="知识库统计")
def kb_stats(
    owner: str | None = Query(default=None, max_length=64),
    scope: str = Query(default="mine", description="mine / all"),
):
    """文档数、切片数、分类与可见性分布，供管理界面概览卡片使用。"""
    try:
        return ok(kb_service.stats(owner=owner, scope=scope))
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库统计失败")
        return ApiResponse(
            code=int(StatusCode.AGENT_UNAVAILABLE),
            message=f"知识库暂不可用：{type(e).__name__}",
            data=None,
        )

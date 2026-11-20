"""知识库管理服务：运行期的单文档增量维护（上传 / 列表 / 删除 / 统计）。

与 scripts/build_kb.py 的分工
------------------------------
build_kb.py : 离线全量重建（drop collection → 重灌），用于首次建库与换切片方案对照
KbService   : 运行期增量维护，供管理界面调用
              上传 → chunker 切片 → GLM embedding-3 → Milvus 增量写入
              删除 → **移入回收站**（备份到本地 + 从 Milvus 移除切片）

设计要点
--------
1. **幂等覆盖**：同一 doc_id 重新上传时先删旧切片再写新的。否则一篇文档会留下
   新旧两版切片同时参与检索，答案可能引到已废弃的内容。
2. **owner / visibility 与检索侧同源**：复用 retriever 的鉴权表达式
   `visibility == "public" || owner == "<user>"`，避免出现"列表看不见但检索命中"
   或"能搜到但删不掉"这类不一致。
3. **源文件落盘** `data/kb/raw/uploads/`：便于下载原文与二次重建；
   该目录已在 .gitignore 中（用户上传内容不入版本库）。
4. **写入前字段字节体检**：Milvus 对 VARCHAR 超限只回一句 "serialize proto"，
   不指出是哪个字段，必须在客户端按 UTF-8 字节逐字段核对（沿用 build_kb.py 做法）。
5. 当前支持 `.md / .markdown / .txt`（chunker 面向 markdown 结构设计）。
   其它格式在此显式拒绝，不做静默降级。
6. **删除 = 移入回收站（软删除）**：切片从 Milvus 真实移除（保证不可被检索，
   而不是靠过滤条件排除），同时把元数据与全文备份到 `data/kb/recycle/`，
   源文件保留。恢复时复用 ingest 重新入库。详见 docs/specs/知识库回收站与分页.md。
7. **语料治理（G1，见 docs/specs/语料治理规约.md）**：
   - 入库前过两层去重门——L1 整篇文件指纹、L2 切片指纹；L2 **在 embedding 之前**
     执行，重复切片直接复用已有切片的 dense 向量，省掉向量化调用与费用
   - 文档登记/状态流转/审计写入 SQLite 治理库（`app/governance/`），
     治理层任何异常都只告警（`[GOV-WARN]`）不阻断知识库主链路
8. **生命周期强化（G2）**：
   - 备份记录 v2 **随身带 dense 向量**（`app/services/kb_backup.py`），
     恢复/启用走"**按原切片回灌**"：直接写回向量，**零 embedding 调用、离线可用**；
     同时解决 G1 时期"恢复要重新切片、chunker 一改边界就漂移"的不保真问题
   - 停用（DISABLED）与软删（RECYCLED）都是"物理删向量 + 备份落盘"，差别只在备份目录
     与生命周期（停用无 TTL，回收站 30 天）——见 `kb_backup` 的目录约定
   - 恢复前过 **L3 语义查重确认门**（相似度 ≥ 阈值须 `confirm=true`）；
     彻底删除过 **双确认**（admin + `confirm=true` + 文档名复述一致）
9. **版本链（G3）**：
   - 同名上传不再"旧版直接蒸发"——覆盖前把旧版抓成**历史快照**入回收站
     （`role=history`，文件 `<doc_id>@v<n>.json`），版本号递增，可查可回滚
   - 回滚 = 把某个历史版本重新变成当前版本，**版本号升为历史最大值 +1**（不回退编号，
     否则审计与备份文件名会指向同一个号却内容不同）
   - 内容完全没变的重传仍被 **L1 拦截**（不产生"内容相同的 v2"，那种版本毫无意义）
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pymilvus import MilvusClient

from app.core.config import settings
from app.rag import glm
from app.rag.chunker import Chunk, ChunkConfig, chunk_document
from app.services.governance_service import GovAction, GovState, governance_service
from app.services.kb_backup import (
    ROLE_CURRENT,
    ROLE_HISTORY,
    STATE_DISABLED,
    STATE_RECYCLED,
    decode_dense,
    encode_dense,
    is_history,
    kb_backups,
    unlink_quietly,
)

logger = logging.getLogger(__name__)

from app.core.paths import DATA_DIR                 # noqa: E402  数据根目录（DATA_ROOT 可覆盖）

ROOT = Path(__file__).resolve().parents[2]          # agt_python/
# 上传源文件落盘目录：随数据根目录走（测试隔离见 app/core/paths.py）
UPLOAD_DIR = DATA_DIR / "kb/raw/uploads"
# 回收站 / 停用区的目录与读写统一由 app/services/kb_backup.py 负责（G2 抽出）
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_CONTENT_CHARS = 2_000_000                        # 单文件正文上限（约 2M 字）

DEFAULT_PAGE_SIZE = 20                               # 列表默认每页条数
MAX_PAGE_SIZE = 100                                  # 上限：防止构造超大 pageSize 拖垮服务

INSERT_BATCH = 50                                    # Milvus 单次插入条数
EMBED_BATCH = 32                                     # 单批 embedding 条数（过大易 429）

# Milvus VARCHAR 字段字节上限（与 scripts/build_kb.py 的 schema 一致）
FIELD_LIMITS: dict[str, int] = {
    "chunk_id": 512, "doc_id": 512, "doc_title": 2048, "heading": 1024,
    "owner": 256, "visibility": 32, "category": 256, "source": 512,
    "source_type": 64, "doc_version": 32, "doc_updated": 32, "lang": 16,
    "text": 16384,
}

# 列表/统计只需元数据，不拉 text / dense（避免把大对象搬进内存）
META_FIELDS = [
    "doc_id", "doc_title", "category", "owner", "visibility",
    "source", "source_type", "doc_version", "doc_updated", "chunk_index", "char_len",
]

# 备份取回字段（G2）：比 META_FIELDS 多出**回灌所需的全部字段**——
# text（含标题前缀与 overlap 的原样文本）+ dense（8KB/片）+ 引用定位三件套。
# 为什么不能只存 body 再重新拼前缀：那等于又跑了一遍切片逻辑，chunker 一改就不保真。
BACKUP_FIELDS = [
    "doc_id", "doc_title", "heading", "chunk_index", "total_chunks",
    "char_start", "char_end", "char_len", "text", "dense",
    "owner", "visibility", "category", "source", "source_type",
    "doc_version", "doc_updated", "lang",
]

# L3 语义查重最多取样的切片数：每片一次 ANN 检索，一篇 200 片的文档没必要全查
L3_MAX_QUERIES = 32

# Milvus 单次 query 返回上限（服务端硬限制 16384）
QUERY_LIMIT = 16384


class KbError(RuntimeError):
    """知识库操作的可预期失败（参数/状态问题），由 API 层转成业务错误码。"""


class KbDuplicateError(KbError):
    """去重门拦截（L1 文件指纹 / L2 切片指纹命中）。

    单独成类是为了让上层能区分"操作失败"与"内容已存在"——
    后者其实是治理生效的正常结果，前端提示口径完全不同。
    额外携带结构化信息（命中哪一层、重复了多少切片、省下多少 embedding），
    让接口能返回"为什么被拦 + 本次节省了多少向量化调用"。
    """

    def __init__(self, message: str, *, layer: str = "", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.layer = layer
        self.detail: dict[str, Any] = detail or {}


class KbConfirmRequired(KbError):
    """确认门拦截（G2）：操作本身合法，但需要二次确认才能执行，**本次并未执行**。

    为什么不直接返回一个不带信息的通用错误：确认门要告诉前端
    **确认什么**（相似文档是哪几篇 / 该复述的文档名是什么），否则用户只能盲点"确定"。
    `detail` 里的 `requireConfirm=true` 是前端的判断依据，`gate` 区分两类门。

    返回形态统一为 **3001 + data.requireConfirm**（由 `_kb_fail` 组装）：
    - 用 3001 而非 2000：本次操作**没有发生**，用成功码会让粗心前端直接提示"操作成功"
      （G1 验收脚本就被这个假成功的 2000 骗过一次）
    - data 必须能穿过 Java 透传层：Java 的全局异常处理器只保留 message，
      故 Java 侧为确认门单开了一条携带 detail 的异常通道（见 ConfirmRequiredException）
    """

    def __init__(self, message: str, *, gate: str = "", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.gate = gate
        self.detail: dict[str, Any] = detail or {}


def truncate_utf8(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断（Milvus VARCHAR max_length 单位是字节，中文占 3 字节）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def sanitize_name(name: str) -> str:
    """文件名 → 安全标识。

    既防路径穿越（`../`），也防特殊字符破坏 Milvus 过滤表达式的引号，
    因此只保留中英文、数字、下划线、点、连字符。
    """
    stem = Path(name).name                      # 去掉任何目录部分
    stem = Path(stem).stem                      # 去掉扩展名
    cleaned = _SAFE_NAME_RE.sub("_", stem).strip("._")
    return cleaned[:120] or f"doc_{int(time.time())}"


def decode_text(data: bytes) -> str:
    """解码上传内容：优先 UTF-8，失败再试 GBK（Windows 记事本存量文档常见）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise KbError("文件编码无法识别，请另存为 UTF-8 编码后重试")


def normalize_newlines(text: str) -> str:
    """换行归一：CRLF / 单独 CR → LF。

    为什么必须做（2026-09-18 回填时实测发现）：切片预算是**按字符数**算的，
    `\\r` 会白占预算（每行 +1 字），于是同一份文档在 Windows（CRLF）与
    Linux（LF）下会切出**不同的切片数量与边界**——历史语料用 LF 入库（21 片），
    磁盘上被存成 CRLF 后重切变成 24 片（逐片文本漂移）。

    指纹本身不受影响（规范化会去掉所有空白），但**切片序号会错位**，
    进而让"按片复用向量""按原片回灌"全部失效。入库前统一为 LF，
    保证任何来源的同一份内容切法一致。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_page(page: Any, page_size: Any) -> tuple[int, int]:
    """规范化分页参数：页码从 1 起，每页条数收敛到 [1, MAX_PAGE_SIZE]。

    非法输入不报错而是回落默认值——分页是展示层参数，用户手改 URL 传了脏值
    不应该让整个列表 500。
    """
    try:
        p = max(1, int(page or 1))
    except (TypeError, ValueError):
        p = 1
    try:
        ps = int(page_size or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        ps = DEFAULT_PAGE_SIZE
    return p, min(max(ps, 1), MAX_PAGE_SIZE)


def attach_recycle_ttl(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给回收站条目补 TTL 剩余期（规约 §6.3 要求前端展示倒计时）。

    口径与 TTL 清理任务对齐：都按 `gov_ttl_days`（默认 30 天）从**软删时间**起算。
    软删时间取备份记录的 `deletedAt`——它与账本 `docs.recycled_at` 是软删那一刻同时
    写下的两份；若将来出现不一致（例如手工改库），**以账本为准**（TTL 任务只认账本）。

    已超期条目返回 `ttlRemainingDays = 0` 且 `expired = true`，不返回负数：此刻它还
    留在回收站，说明 TTL 任务尚未跑到或上一轮失败，前端据此显示「已超期」比显示
    "-3 天"更准确。
    """
    ttl_days = max(1, int(settings.gov_ttl_days))
    now = datetime.now()
    for it in items:
        it["ttlDays"] = ttl_days
        raw = str(it.get("deletedAt") or "").strip()
        try:
            deleted = datetime.fromisoformat(raw)
        except ValueError:
            it["expiresAt"] = None
            it["ttlRemainingDays"] = None
            it["expired"] = False
            continue
        if deleted.tzinfo is not None:
            deleted = deleted.replace(tzinfo=None)
        expire = deleted + timedelta(days=ttl_days)
        remain_sec = (expire - now).total_seconds()
        it["expiresAt"] = expire.strftime("%Y-%m-%d %H:%M:%S")
        it["ttlRemainingDays"] = math.ceil(remain_sec / 86400) if remain_sec > 0 else 0
        it["expired"] = remain_sec <= 0
    return items


class KbService:
    """知识库管理服务。线程安全（懒加载 Milvus 连接 + 写锁）。"""

    def __init__(self) -> None:
        self._client: MilvusClient | None = None
        self._conn_lock = threading.Lock()
        # 写操作串行化：Milvus 客户端本身线程安全，但"先删后插"的幂等覆盖
        # 若并发执行会互相踩，用进程内锁保证同一时刻只有一个写事务
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------ 基础设施

    def _get_client(self) -> MilvusClient:
        with self._conn_lock:
            if self._client is None:
                self._client = MilvusClient(uri=settings.milvus_uri)
                logger.info("知识库已连接 Milvus | uri=%s collection=%s",
                            settings.milvus_uri, settings.milvus_collection)
            return self._client

    def _chunk_config(self) -> ChunkConfig:
        """复用全局实测标定的切片参数，避免与管理界面出现两套口径。"""
        return ChunkConfig(
            max_chars=settings.rag_chunk_size,
            overlap=settings.rag_chunk_overlap,
            min_chunk=settings.rag_chunk_min,
        )

    @staticmethod
    def _scope_expr(owner: str | None, scope: str) -> str:
        """可见范围过滤表达式，与 retriever._auth_expr 保持同一语义。

        scope=mine : 仅 public + 自己拥有（普通用户视角）
        scope=all  : 全部（管理员视角）
        """
        if scope == "all":
            return "id >= 0"
        if owner:
            safe = owner.replace('"', "")
            return f'(visibility == "public" || owner == "{safe}")'
        return 'visibility == "public"'

    # ------------------------------------------------------------ 上传入库

    def ingest(
        self,
        *,
        file_name: str,
        content: str,
        owner: str,
        category: str = "",
        visibility: str = "public",
        allow_full_duplicate: bool = False,
        bump_version: bool = True,
        version_no: int | None = None,
    ) -> dict[str, Any]:
        """单文档入库：去重门 → 切片 → embedding → Milvus（同 doc_id 幂等覆盖）。

        治理（G1）三步：
        1. **L1 去重门**：整篇正文指纹与已有文档相同 → 直接拦截（不覆盖、不调 embedding）
        2. **L2 去重门**：切片指纹查重，**在 embedding 之前**；重复切片复用已有向量
        3. 入库成功后登记治理账本 + 审计 UPLOAD

        版本链（G3）：同名覆盖前先把旧版留成**历史快照**（回收站 `role=history`），
        新版本号 = 旧版本号 + 1；内容没变的重传被 L1 拦住、不产生无意义的版本。

        `allow_full_duplicate`：**仅回收站恢复路径传 True**。恢复不是"新增语料"——
        它的内容必然曾经在库中（RECYCLED 文档当初就是过门入库的），无法用它绕过对重门；
        而若恢复时整篇内容已被别的文档覆盖（例如用户先上传了该文档的增强版），
        仍按 L2 拦截会让用户陷入"删了就再也拿不回来"的死角。此时放行并复用已有向量，
        恢复代价从"重新 embedding"降为毫秒级，重复情况由审计与 G4 巡检工单暴露。

        `bump_version` / `version_no`：**仅供 `_write_back` 回落路径**显式指定。
        那次 ingest 不是"用户上传新内容"，而是"把某个已知版本的内容写回去"，
        必须沿用记录里的版本号——否则恢复一次就凭空涨一个版本，版本链直接失真。
        """
        suffix = Path(file_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise KbError(
                f"暂不支持 {suffix or '该'} 格式，请上传 "
                f"{'/'.join(sorted(SUPPORTED_SUFFIXES))} 文件"
            )

        content = normalize_newlines((content or "").strip())
        if not content:
            raise KbError("文件内容为空")
        if len(content) > MAX_CONTENT_CHARS:
            raise KbError(f"文件过大（{len(content)} 字），上限 {MAX_CONTENT_CHARS} 字")

        visibility = visibility if visibility in ("public", "private") else "public"
        doc_id = sanitize_name(file_name)

        # ---- L1 去重门：整篇内容已存在则拦截 ----
        # 放在切片之前：拦截时零成本（不切片、不 embedding、不动向量库）
        file_hash = governance_service.file_fingerprint(content)
        l1_hit = governance_service.check_file_duplicate(file_hash, doc_id=doc_id)
        if l1_hit:
            # 命中的可能是**自己**（同名同内容重传）。这不是"与别的语料重复"，
            # 而是"内容没变、不构成新版本"——两者给用户的下一步动作完全不同，
            # 文案必须分开，否则用户会去删一个根本不冲突的文档（G3 明确）
            same_doc = str(l1_hit.get("doc_id") or "") == doc_id
            cur_ver = int(l1_hit.get("version_no") or 1)
            governance_service.audit(
                GovAction.DEDUP_INTERCEPT, doc_id=doc_id, doc_name=file_name,
                detail={
                    "layer": "L1",
                    "fileHash": file_hash,
                    "matchedDocId": l1_hit.get("doc_id"),
                    "matchedState": l1_hit.get("state"),
                    "sameDoc": same_doc,
                    "currentVersion": cur_ver,
                },
                operator=owner,
            )
            raise KbDuplicateError(
                (f"内容与当前版本（v{cur_ver}）完全相同，未产生新版本。"
                 f"如需强制覆盖请在源文件中修改内容后重传。")
                if same_doc else
                (f"该文档与已有语料「{l1_hit.get('doc_id')}」内容完全相同"
                 f"（L1 文件指纹命中），未重复入库。"
                 f"如需替换请先删除原文档，或修改内容后重传。"),
                layer="L1",
                detail={
                    "matchedDocId": l1_hit.get("doc_id"),
                    "matchedState": l1_hit.get("state"),
                    "fileHash": file_hash,
                    "sameDoc": same_doc,
                    "currentVersion": cur_ver,
                    "embeddingSkipped": 0,      # 连切片都没做
                },
            )

        t0 = time.time()
        cfg = self._chunk_config()
        defaults = {
            "owner": owner or "default",
            "visibility": visibility,
            "source": f"uploads/{doc_id}{suffix}",
            "source_type": "upload",
            "updated": datetime.now().strftime("%Y-%m-%d"),
            "lang": "zh",
        }
        chunks: list[Chunk] = chunk_document(
            content,
            stem=doc_id,
            default_category=category or "",
            defaults=defaults,
            cfg=cfg,
        )
        if not chunks:
            raise KbError("切片结果为空：正文过短或缺少有效段落（建议不少于 100 字）")

        # ---- L2 去重门：切片指纹（embedding 之前）----
        # 指纹对象是 `c.body`（去标题前缀与 overlap 的正文），保证"内容相同即同指纹"，
        # 换文件名/换标题不影响命中。
        chunk_rows = [
            (c.chunk_index, governance_service.chunk_fingerprint(c.body or c.text), c.char_len)
            for c in chunks
        ]
        dup = governance_service.find_duplicate_chunks(chunk_rows, doc_id=doc_id)
        if dup and len(dup) == len(chunks) and not allow_full_duplicate:
            matched = sorted({v[1] for v in dup.values()})
            governance_service.audit(
                GovAction.DEDUP_INTERCEPT, doc_id=doc_id, doc_name=chunks[0].doc_title,
                detail={
                    "layer": "L2",
                    "chunks": len(chunks),
                    "uniqueHashes": len({h for _, h, _ in chunk_rows}),
                    "matchedDocIds": matched[:10],
                },
                operator=owner,
            )
            raise KbDuplicateError(
                f"该文档全部 {len(chunks)} 个切片与已有语料重复（L2 切片指纹命中），"
                f"未重复入库。命中文档：{'、'.join(matched[:3])}",
                layer="L2",
                detail={
                    "chunks": len(chunks),
                    "matchedDocIds": matched[:10],
                    # L2 门在 embedding 之前，命中即意味着本次一次向量化调用都没发
                    "embeddingSkipped": len(chunks),
                },
            )
        if dup and len(dup) == len(chunks):
            # 恢复路径：整篇已在库中。放行并复用（下方复用逻辑会全部命中），但必须留痕
            logger.warning("[GOV-WARN] 整篇内容已存在于其它文档，按恢复语义放行并复用向量 "
                           "| doc=%s chunks=%d matched=%s",
                           doc_id, len(chunks), sorted({v[1] for v in dup.values()})[:5])

        client = self._get_client()
        collection = settings.milvus_collection

        # ---- 向量化：重复切片复用已有向量，其余走 embedding ----
        vectors: dict[int, list[float]] = {}
        reused = 0
        if dup and settings.gov_reuse_vector:
            for idx, (_h, _src_doc, _src_idx, src_chunk_id) in dup.items():
                vec = self._fetch_dense(client, collection, src_chunk_id)
                if vec:
                    vectors[idx] = vec
                    reused += 1
                else:
                    # 源切片向量已不在集合中（对账标记缺失/被并发删除）：
                    # 回落 embedding 保证内容完整，不为了省一次调用丢掉切片
                    logger.info("[GOV] 复用向量未命中，回落 embedding | src=%s", src_chunk_id)

        pending = [c for c in chunks if c.chunk_index not in vectors]
        for i in range(0, len(pending), EMBED_BATCH):
            batch = pending[i : i + EMBED_BATCH]
            got = glm.embed_texts([c.text for c in batch])
            for c, v in zip(batch, got):
                vectors[c.chunk_index] = v

        if len(vectors) != len(chunks):
            raise KbError(f"向量化条数不匹配：切片 {len(chunks)} / 向量 {len(vectors)}")

        rows = [c.to_milvus_row(vectors[c.chunk_index], truncate_utf8) for c in chunks]
        self._check_limits(rows)

        # ---- 版本链（G3）：覆盖前把旧版留成历史快照 ----
        # 刻意放在"向量化完成、还没动向量库"的位置：早于此（embedding 之前）做，
        # 一旦 embedding 失败就会留下"有 v1 快照但当前还是 v1"的空转版本；
        # 晚于此则旧切片已被删，快照无从取得。
        prev = governance_service.get_doc(doc_id) or {}
        snapshot = self._snapshot_before_replace(doc_id, operator=owner or "default")

        # 先翻转"期望向量存在"（对账基准），再动向量——顺序不能颠倒：
        # 若账本里这条 doc_id 还停在 RECYCLED/DISABLED（期望=无），而向量已经写入，
        # 恰好跑一轮对账就会把刚入库的切片当残留删掉。详见
        # governance_service.mark_vector_expectation 的说明。
        governance_service.mark_vector_expectation(doc_id, True)

        with self._write_lock:
            # 幂等覆盖：先清除同 doc_id 的旧切片
            removed = self._delete_by_doc_id(client, collection, doc_id)
            for i in range(0, len(rows), INSERT_BATCH):
                client.insert(collection, rows[i : i + INSERT_BATCH])
            client.flush(collection)

        # 源文件落盘（失败不影响入库结果，仅记录）
        self._save_source(doc_id, suffix, content)

        # ---- 治理登记：必须在 Milvus 写入成功之后（账本要反映事实）----
        # 版本号只有"真的产生新内容"才递增：显式指定（回落路径）优先，
        # 其次按 bump_version 决定——首次入库为 v1，同名覆盖为旧版本 +1
        gov_doc = governance_service.register_upload(
            doc_id=doc_id,
            doc_name=chunks[0].doc_title,
            file_hash=file_hash,
            owner=owner or "default",
            category=chunks[0].category,
            visibility=visibility,
            chunk_rows=chunk_rows,
            chunk_count=len(chunks),
            char_count=sum(c.char_len for c in chunks),
            replaced_chunks=removed,
            duplicate_chunks=len(dup),
            reused_vectors=reused,
            version_no=version_no,
            bump_version=bump_version and version_no is None and bool(prev),
            replaced_version=int(prev.get("version_no") or 0) if prev else None,
            operator=owner,
        )

        elapsed = round(time.time() - t0, 1)
        logger.info("知识库入库完成 | doc_id=%s chunks=%d replaced=%d dup=%d reused=%d "
                    "owner=%s %.1fs",
                    doc_id, len(chunks), removed, len(dup), reused, owner, elapsed)
        return {
            "docId": doc_id,
            "docTitle": chunks[0].doc_title,
            "category": chunks[0].category,
            "visibility": visibility,
            "owner": owner,
            "chunkCount": len(chunks),
            "replacedChunks": removed,
            "charCount": sum(c.char_len for c in chunks),
            "elapsedSec": elapsed,
            # ---- 治理信息（G1）：前端可据此提示"本次节省了多少向量化调用" ----
            "fileHash": file_hash,
            "versionNo": int(gov_doc.get("version_no") or 1),
            "duplicateChunks": len(dup),
            "reusedVectors": reused,
            # ---- 版本链信息（G3）：本次覆盖是否留下可回滚的旧版快照 ----
            "replacedVersion": int(prev.get("version_no") or 0) if prev else None,
            "versionSnapshot": snapshot,
        }

    def _snapshot_before_replace(self, doc_id: str, *, operator: str) -> dict[str, Any]:
        """同名覆盖前，把"即将被顶替的旧版"留成历史快照（G3）。

        两条来源都要处理，缺一就会丢版本：
        1. **回收站/停用区里已有的当前版本备份**——文档曾被软删或停用（向量不在库，
           快照只能取自那份备份文件）。把它**降级**为历史快照；
           否则新文档入库后，回收站里会留一个"名字与在库文档相同、恢复必然冲突"的僵尸条目
        2. **向量库里的旧切片**——文档当前在库（ACTIVE/DISABLED），直接抓一份带向量的快照

        与"删除前必须备份"的关键区别：那里的备份是**恢复的唯一数据来源**，写不成必须中止；
        这里的快照只是**历史留档**，失败后果是"少一个可回滚的版本"，
        因此**只告警不阻断**——用户要更新文档，不该因为归档失败而更新不了。
        但状态如实回传（`snapshot=false`）+ 日志告警，不制造"以为能回滚"的假象。
        """
        if not (settings.gov_version_chain_enabled and settings.gov_version_snapshot_enabled):
            return {"snapshot": False, "reason": "version-chain-disabled"}

        info: dict[str, Any] = {"snapshot": False, "archived": [], "versionNo": None,
                                "chunks": 0}
        for st in (STATE_RECYCLED, STATE_DISABLED):
            try:
                moved = kb_backups.archive_current(doc_id, st)
            except Exception as e:  # noqa: BLE001
                logger.warning("[GOV-WARN] 归档旧备份失败 | doc=%s state=%s err=%s",
                               doc_id, st, e)
                continue
            if moved:
                info["archived"].append(str(moved))
                logger.info("[GOV] 旧版备份已降级为历史快照 | doc=%s file=%s",
                            doc_id, moved.name)

        try:
            old_rows = self._fetch_rows(doc_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[GOV-WARN] 旧版切片读取失败，本次覆盖无快照 | doc=%s err=%s",
                           doc_id, e)
            old_rows = []

        if old_rows:
            gov_doc = governance_service.get_doc(doc_id) or {}
            ver = int(gov_doc.get("version_no") or 1)
            suffix = Path(str(old_rows[0].get("source") or "")).suffix or ".md"
            record = self._build_backup_record(
                doc_id, old_rows, state=STATE_RECYCLED, operator=operator, is_admin=False,
                file_hash=gov_doc.get("file_hash"), version_no=ver, suffix=suffix,
                role=ROLE_HISTORY,
            )
            try:
                path = self._write_backup(record)
            except KbError as e:
                logger.warning("[GOV-WARN] 旧版快照写入失败，本次覆盖后无历史版本可回滚 "
                               "| doc=%s ver=%s err=%s", doc_id, ver, e)
                info["error"] = str(e)
            else:
                info.update({"snapshot": True, "versionNo": ver,
                             "chunks": len(old_rows), "path": str(path)})

        if info["archived"]:
            info["snapshot"] = True
        if info["snapshot"]:
            # 修剪：只保留版本号最大的 N 份历史快照（防"每天覆盖一次"无限堆积）
            try:
                info["pruned"] = kb_backups.prune_versions(doc_id, settings.gov_version_keep)
            except Exception as e:  # noqa: BLE001
                logger.warning("[GOV-WARN] 版本快照修剪失败 | doc=%s err=%s", doc_id, e)
        return info

    def _check_limits(self, rows: list[dict]) -> None:
        """写入前逐字段核对字节上限。

        Milvus 超限时报 "serialize proto" 之类的底层错误，不告诉你哪个字段超了，
        等到那时排查成本很高——必须在客户端提前拦住（与 build_kb.py 同一策略）。
        """
        for field, limit in FIELD_LIMITS.items():
            if field not in rows[0]:
                continue
            mx = max(len(str(r[field]).encode("utf-8")) for r in rows)
            if mx > limit:
                raise KbError(f"字段 {field} 超出上限：{mx} > {limit} 字节")

        dims = {len(r["dense"]) for r in rows}
        if len(dims) != 1:
            raise KbError(f"向量维度不一致：{dims}")

    @staticmethod
    def _save_source(doc_id: str, suffix: str, content: str) -> None:
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (UPLOAD_DIR / f"{doc_id}{suffix}").write_text(content, encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            logger.warning("源文件落盘失败（不影响入库）| doc_id=%s err=%s", doc_id, e)

    @staticmethod
    def _delete_by_doc_id(client: MilvusClient, collection: str, doc_id: str) -> int:
        """删除某文档的全部切片，返回删除前该文档的切片数。"""
        safe = doc_id.replace('"', "")
        expr = f'doc_id == "{safe}"'
        existing = client.query(
            collection_name=collection, filter=expr,
            output_fields=["count(*)"],
        )
        n = int(existing[0].get("count(*)", 0)) if existing else 0
        if n:
            client.delete(collection_name=collection, filter=expr)
        return n

    @staticmethod
    def _fetch_dense(
        client: MilvusClient, collection: str, chunk_id: str,
    ) -> list[float] | None:
        """按 chunk_id 取回切片的 dense 向量（L2 去重复用，省一次 embedding）。

        实测可行（Milvus 2.6.24 + pymilvus 3.0.1：向量字段可被 query 返回）。
        取不到就返回 None，由调用方回落 embedding——**不为了省调用而丢切片**。
        """
        safe = str(chunk_id).replace('"', "")
        try:
            rows = client.query(
                collection_name=collection, filter=f'chunk_id == "{safe}"',
                output_fields=["dense"], limit=1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[GOV-WARN] 复用向量查询失败，回落 embedding | chunk=%s err=%s",
                           chunk_id, e)
            return None
        if not rows:
            return None
        vec = rows[0].get("dense")
        return list(vec) if isinstance(vec, (list, tuple)) else None

    # ------------------------------------------------------------ 共享访问器
    # 对账任务（reconcile_service）等治理组件复用同一连接与同一套删除语义，
    # 避免各模块自己 new 一个 MilvusClient（多连接各自持缓存，flush 时序也难对齐）

    def milvus(self) -> MilvusClient:
        """共享 Milvus 连接。"""
        return self._get_client()

    @property
    def collection(self) -> str:
        return settings.milvus_collection

    def query_count(self, doc_id: str) -> int:
        """某文档在向量库中的切片数（对账与冲突校验用）。"""
        return self._count_by_doc_id(self._get_client(), self.collection, doc_id)

    def delete_vectors(self, doc_id: str) -> int:
        """物理删除某文档的全部切片并 flush，返回删除条数（对账任务用）。"""
        client = self._get_client()
        with self._write_lock:
            removed = self._delete_by_doc_id(client, self.collection, doc_id)
            if removed:
                client.flush(self.collection)
        return removed

    # ------------------------------------------------------------ 备份与回灌（G2）

    def _fetch_rows(self, doc_id: str) -> list[dict[str, Any]]:
        """取回某文档的全部切片（含 text 与 dense），按 chunk_index 排序。

        dense 会一并取回：备份必须能独立重建向量，否则"恢复"又要回去调 embedding。
        """
        client = self._get_client()
        safe = doc_id.replace('"', "")
        rows = client.query(
            collection_name=self.collection, filter=f'doc_id == "{safe}"',
            output_fields=BACKUP_FIELDS, limit=QUERY_LIMIT,
        )
        rows = [r for r in rows if r.get("doc_id")]
        rows.sort(key=lambda r: int(r.get("chunk_index") or 0))
        return rows

    def _build_backup_record(
        self,
        doc_id: str,
        rows: list[dict[str, Any]],
        *,
        state: str,
        operator: str,
        is_admin: bool,
        file_hash: str | None = None,
        version_no: int | None = None,
        suffix: str = "",
        role: str = ROLE_CURRENT,
    ) -> dict[str, Any]:
        """把 Milvus 切片行 + 治理账本信息合成备份记录（v2）。

        `role`（G3）：`current` = 该文档"当前在编的那一版"，写 `<doc_id>.json`，
        是恢复/启用的唯一数据来源；`history` = 同名覆盖时被顶替的旧版，
        写 `<doc_id>@v<n>.json`，只供版本链查看与回滚。
        """
        first = rows[0]
        chunks: list[dict[str, Any]] = []
        for i, r in enumerate(rows):
            text = str(r.get("text") or "")
            chunk: dict[str, Any] = {
                "chunkIndex": int(r.get("chunk_index") if r.get("chunk_index") is not None else i),
                "heading": r.get("heading") or "",
                "charStart": int(r.get("char_start") or 0),
                "charEnd": int(r.get("char_end") or 0),
                "charLen": int(r.get("char_len") or len(text)),
                "text": text,
            }
            if settings.gov_backup_include_dense:
                encoded = encode_dense(r.get("dense"))
                if encoded:
                    chunk["dense"] = encoded
            chunks.append(chunk)

        return {
            "schema": 2,
            "state": state,
            "role": role,
            "docId": doc_id,
            "docTitle": first.get("doc_title") or doc_id,
            "category": first.get("category") or "",
            "owner": first.get("owner") or "",
            "visibility": first.get("visibility") or "public",
            "source": first.get("source") or "",
            "sourceType": first.get("source_type") or "",
            "docUpdated": first.get("doc_updated") or "",
            "docVersion": first.get("doc_version") or "",
            "lang": first.get("lang") or "zh",
            # 治理账本里的指纹与版本：恢复时校验内容是否已存在、版本链（G3）使用
            "fileHash": file_hash,
            "versionNo": int(version_no or 1),
            "chunkCount": len(rows),
            "charCount": sum(int(r.get("char_len") or 0) for r in rows),
            "fileName": f"{doc_id}{suffix}" if suffix else (first.get("source") or doc_id),
            "deletedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "deletedBy": operator or "unknown",
            "deletedByAdmin": bool(is_admin),
            "chunks": chunks,
        }

    def _write_backup(self, record: dict[str, Any]) -> Path:
        """落盘备份。失败必须抛错——不允许在没有备份的情况下删除向量。"""
        try:
            return kb_backups.write(record)
        except OSError as e:
            logger.error("备份写入失败 | doc_id=%s state=%s err=%s",
                         record.get("docId"), record.get("state"), e)
            raise KbError(f"备份写入失败，已中止操作：{e}") from e

    def _rows_from_backup(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """备份 → Milvus 插入行（**回灌**：不切片、不 embedding）。

        整篇**全有或全无**：任一切片缺向量就返回空列表，由调用方回落"重新切片入库"。
        理由：半篇回灌会产出一份"看似恢复、实际与删除前不一致"的语料，
        比老老实实重跑一遍更危险（而且很难被发现）。
        """
        doc_id = str(record.get("docId") or "")
        chunks = kb_backups.chunks_of(record)
        if not chunks or not kb_backups.has_vectors(record):
            return []

        rows: list[dict[str, Any]] = []
        for i, c in enumerate(chunks):
            text = str(c.get("text") or "")
            vec = decode_dense(c.get("dense"), dim=settings.embedding_dim)
            if not text or vec is None:
                if not text:
                    continue
                logger.warning("[GOV-WARN] 备份切片缺向量，回落重新切片 | doc=%s idx=%s",
                                   doc_id, c.get("chunkIndex"))
                return []
            idx = c.get("chunkIndex")
            rows.append({
                "chunk_id": truncate_utf8(f"{doc_id}#{idx if idx is not None else i}", 400),
                "doc_id": truncate_utf8(doc_id, 400),
                "doc_title": truncate_utf8(str(record.get("docTitle") or doc_id), 1900),
                "heading": truncate_utf8(str(c.get("heading") or ""), 900),
                "chunk_index": int(idx if idx is not None else i),
                "total_chunks": len(chunks),
                "char_start": int(c.get("charStart") or 0),
                "char_end": int(c.get("charEnd") or 0),
                "owner": truncate_utf8(str(record.get("owner") or ""), 200),
                "visibility": truncate_utf8(str(record.get("visibility") or "public"), 20),
                "category": truncate_utf8(str(record.get("category") or ""), 200),
                "source": truncate_utf8(str(record.get("source") or ""), 400),
                "source_type": truncate_utf8(str(record.get("sourceType") or ""), 60),
                "doc_version": truncate_utf8(str(record.get("docVersion") or ""), 30),
                "doc_updated": truncate_utf8(str(record.get("docUpdated") or ""), 30),
                "lang": truncate_utf8(str(record.get("lang") or "zh"), 10),
                "char_len": int(c.get("charLen") or len(text)),
                "text": truncate_utf8(text, 16000),
                "dense": vec,
            })
        return rows

    def _write_back(self, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """把备份放回向量库。返回 `(结果摘要, 模式)`，模式 = writeback | ingest。

        优先**回灌**（毫秒级、零 embedding、与删除前逐字节一致）；
        备份来自 G1（无向量）或向量解码失败时回落 `ingest`（重新切片 + 重新 embedding）。
        """
        doc_id = str(record.get("docId") or "")
        client = self._get_client()
        collection = self.collection

        rows = self._rows_from_backup(record)
        if rows:
            self._check_limits(rows)
            # 先翻转期望再写向量（对账基准先行，防"对账把刚写回的向量当残留清掉"）
            governance_service.mark_vector_expectation(doc_id, True)
            with self._write_lock:
                # 防御性清理：正常流程此刻不该有同 doc_id 的向量，
                # 但对账任务修复前的残留会走到这里，顺手清掉避免新旧混存
                self._delete_by_doc_id(client, collection, doc_id)
                for i in range(0, len(rows), INSERT_BATCH):
                    client.insert(collection, rows[i : i + INSERT_BATCH])
                client.flush(collection)
            return {
                "docId": doc_id,
                "docTitle": record.get("docTitle") or doc_id,
                "category": record.get("category") or "",
                "visibility": record.get("visibility") or "public",
                "owner": record.get("owner") or "default",
                "chunkCount": len(rows),
                "charCount": sum(int(r.get("char_len") or 0) for r in rows),
                "embeddingCalls": 0,
                "restoreMode": "writeback",
            }, "writeback"

        # ---- 回落：备份无向量（G1 记录）→ 重新切片 + embedding ----
        content = self._restore_content(record)
        suffix = Path(str(record.get("fileName") or "")).suffix or ".md"
        logger.warning("[GOV-WARN] 备份无可用向量，回落重新切片入库 | doc=%s chunks=%s",
                       doc_id, record.get("chunkCount"))
        result = self.ingest(
            file_name=f"{doc_id}{suffix}",          # 保证恢复后 doc_id 不变
            content=content,
            owner=str(record.get("owner") or "default"),
            category=str(record.get("category") or ""),
            visibility=str(record.get("visibility") or "public"),
            # 恢复语义：内容必然曾经过门入库，不按"新增语料"拦截（详见 ingest 文档）
            allow_full_duplicate=True,
            # 版本链（G3）：回落路径**不是**"上传了新内容"，必须沿用记录里的版本号——
            # 否则恢复一次就凭空涨一个版本，版本链与备份文件名会整体错位
            version_no=int(record.get("versionNo") or 1),
            bump_version=False,
        )
        chunks = int(result.get("chunkCount") or 0)
        reused = int(result.get("reusedVectors") or 0)
        result["embeddingCalls"] = max(chunks - reused, 0)
        result["restoreMode"] = "ingest"
        return result, "ingest"

    @staticmethod
    def _require_backup_owner(record: dict[str, Any], owner: str | None,
                              is_admin: bool) -> None:
        """备份操作的权限校验：非管理员只能处理自己删/停的文档。"""
        actor = record.get("deletedBy") or record.get("owner") or ""
        if not is_admin and owner and actor and actor != owner:
            raise KbError("只能操作自己处理的文档")

    @staticmethod
    def _confirm_name_matches(given: str | None, record: dict[str, Any]) -> bool:
        """二次确认的文档名复述校验。

        接受文档标题 / doc_id / 文件名主干三者之一（严格相等，去首尾空白）——
        界面上显示哪个就允许用户复述哪个，但必须是**确切的**那一个，
        不能是"随便点一下确认"。
        """
        if not given:
            return False
        target = given.strip()
        candidates = {
            str(record.get("docTitle") or "").strip(),
            str(record.get("docId") or "").strip(),
            Path(str(record.get("fileName") or "")).stem.strip(),
        }
        return target in {c for c in candidates if c}

    def _l3_similarity(self, record: dict[str, Any]) -> dict[str, Any]:
        """L3 语义查重：备份文档是否与**在库的其它文档**高度相似（余弦 ≥ 阈值）。

        实现要点（这是 G2 比参考实现更省的地方）：
        - 查询向量直接取自**备份里的 dense**——恢复不需要重新 embedding 一次就能查重；
          备份无向量（G1 老记录）才临时 embedding 一次（恢复是低频操作，可接受）
        - 一次 `search` 传多个查询向量（Milvus 支持批量），避免按片 N 次往返
        - 只关心"有没有别的文档像它"：按 doc_id 归并取最大值，同一个文档命中多片也只算一条

        返回 `{status: ok|similar|unavailable, maxSimilarity, threshold, hits, sampled}`
        """
        threshold = float(settings.gov_l3_threshold)
        base = {"threshold": threshold, "maxSimilarity": 0.0, "hits": [], "sampled": 0}

        chunks = kb_backups.chunks_of(record)
        vecs: list[list[float]] = []
        for c in chunks:
            v = decode_dense(c.get("dense"), dim=settings.embedding_dim)
            if v:
                vecs.append(v)
        if not vecs:
            text = kb_backups.plain_text(record)[:2000]
            if not text:
                return {**base, "status": "unavailable", "reason": "备份缺正文与向量，无法查重"}
            try:
                vecs = glm.embed_texts([text])
                base["sampled"] = -1        # 负数标记"查重本身用了一次 embedding"
            except Exception as e:  # noqa: BLE001  （含 GlmApiError：余额/限流）
                logger.warning("[GOV-WARN] L3 查重无法向量化，按放行处理 | doc=%s err=%s",
                               record.get("docId"), e)
                return {**base, "status": "unavailable", "reason": str(e)[:200]}

        # 均匀取样，最多 L3_MAX_QUERIES 片
        step = max(1, len(vecs) // L3_MAX_QUERIES)
        sampled = vecs[::step][:L3_MAX_QUERIES]
        base["sampled"] = len(sampled) if base["sampled"] >= 0 else base["sampled"]

        doc_id = str(record.get("docId") or "").replace('"', "")
        try:
            results = self._get_client().search(
                collection_name=self.collection,
                data=sampled,
                filter=f'doc_id != "{doc_id}"',
                limit=5,
                output_fields=["doc_id", "doc_title", "chunk_index"],
                search_params={"metric_type": "COSINE", "params": {}},
                anns_field="dense",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[GOV-WARN] L3 查重检索失败，按放行处理 | doc=%s err=%s",
                           record.get("docId"), e)
            return {**base, "status": "unavailable", "reason": str(e)[:200]}

        best: dict[str, dict[str, Any]] = {}
        for hits in results or []:
            for h in hits:
                score = float(h.get("distance") or 0.0)
                ent = h.get("entity") or {}
                other = str(ent.get("doc_id") or "")
                if not other:
                    continue
                cur = best.get(other)
                if cur is None or score > cur["score"]:
                    best[other] = {
                        "docId": other,
                        "docTitle": ent.get("doc_title") or other,
                        "chunkIndex": ent.get("chunk_index"),
                        "score": round(score, 4),
                    }

        hits_sorted = sorted(best.values(), key=lambda x: -x["score"])
        similar = [h for h in hits_sorted if h["score"] >= threshold]
        return {
            **base,
            "status": "similar" if similar else "ok",
            "maxSimilarity": hits_sorted[0]["score"] if hits_sorted else 0.0,
            "hits": similar[:5] or hits_sorted[:3],
        }

    # ------------------------------------------------------------ L3 巡检（G4）

    def l3_scan_active(
        self,
        *,
        doc_ids: list[str],
        threshold: float,
        samples: int = 3,
    ) -> dict[str, dict[str, Any]]:
        """L3 巡检：在一批**正在库里的**文档之间找近重复对（余弦 ≥ 阈值）。

        与 `_l3_similarity` 的区别是数据来源：后者面向"回收站备份记录"（向量从备份里取，
        用于恢复确认门）；本方法面向**在库文档**，向量直接从 Milvus 取——巡检要查的是
        "库里已经同时躺着两份很像的语料"，那时没有备份记录可用。

        返回 `{docId: {maxSimilarity, matchedDocId, matchedDocTitle}}`，只含**超阈值**的；
        同一文档命中多片只记最高分；**自命中（同 doc 的其它切片）一律跳过**。

        实现要点：
        - 一次 `search` 传全部候选文档的采样向量，靠返回顺序与入参一一对应，
          省掉"每篇一次往返"（79 篇 → 1 次而不是 79 次）
        - `limit=10` 而不是 5：自命中与同文档内部重复会占掉前排席位，
          limit 太小会让真正"像别的文档"的命中被挤出去（实测取 5 时漏检）
        - 检索失败**向上抛**，不返回空 dict：巡检失败绝不等于"没有重复"，
          上层要把它记成"本轮巡检失败"，否则一次 Milvus 抖动就会让治理静默失效
        """
        if not doc_ids:
            return {}
        client = self._get_client()
        quoted = ", ".join(f'"{str(d).replace(chr(34), "")}"' for d in doc_ids)
        rows = client.query(
            collection_name=self.collection,
            filter=f"doc_id in [{quoted}]",
            output_fields=["doc_id", "doc_title", "chunk_index", "dense"],
            limit=QUERY_LIMIT,
        )

        need = max(1, int(samples))
        vectors: dict[str, list[list[float]]] = {}
        titles: dict[str, str] = {}
        for r in rows or []:
            did = str(r.get("doc_id") or "")
            dense = r.get("dense")
            if not did or not dense:
                continue
            titles.setdefault(did, str(r.get("doc_title") or did))
            bucket = vectors.setdefault(did, [])
            if len(bucket) < need:
                bucket.append(list(dense))

        data: list[list[float]] = []
        src: list[str] = []
        for did, vecs in vectors.items():
            for v in vecs:
                data.append(v)
                src.append(did)
        if not data:
            return {}

        results = client.search(
            collection_name=self.collection,
            data=data,
            limit=10,
            output_fields=["doc_id", "doc_title"],
            search_params={"metric_type": "COSINE", "params": {}},
            anns_field="dense",
        )

        found: dict[str, dict[str, Any]] = {}
        for i, hits in enumerate(results or []):
            if i >= len(src):
                break
            me = src[i]
            for h in hits or []:
                score = float(h.get("distance") or 0.0)
                entity = h.get("entity") or {}
                other = str(entity.get("doc_id") or "")
                if not other or other == me or score < float(threshold):
                    continue
                cur = found.get(me)
                if cur is None or score > cur["maxSimilarity"]:
                    best = {
                        "maxSimilarity": round(score, 4),
                        "matchedDocId": other,
                        "matchedDocTitle": str(entity.get("doc_title") or titles.get(other) or other),
                    }
                    found[me] = best
        return found

    # ------------------------------------------------------------ 查询

    def _aggregate_documents(
        self, owner: str | None, scope: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """拉取元数据并按 doc_id 聚合，返回（全量文档列表, 是否被查询上限截断）。

        Milvus 不支持 GROUP BY，这里拉元数据后在进程内聚合。
        只取 11 个小字段（不含 text/dense），当前规模（万级切片）开销可接受。
        """
        client = self._get_client()
        collection = settings.milvus_collection
        expr = self._scope_expr(owner, scope)

        rows = client.query(
            collection_name=collection, filter=expr,
            output_fields=META_FIELDS, limit=QUERY_LIMIT,
        )

        docs: dict[str, dict[str, Any]] = {}
        for r in rows:
            did = r.get("doc_id") or ""
            item = docs.get(did)
            if item is None:
                item = {
                    "docId": did,
                    "docTitle": r.get("doc_title") or did,
                    "category": r.get("category") or "",
                    "owner": r.get("owner") or "",
                    "visibility": r.get("visibility") or "public",
                    "source": r.get("source") or "",
                    "sourceType": r.get("source_type") or "",
                    "docUpdated": r.get("doc_updated") or "",
                    "chunkCount": 0,
                    "charCount": 0,
                }
                docs[did] = item
            item["chunkCount"] += 1
            item["charCount"] += int(r.get("char_len") or 0)

        return sorted(docs.values(), key=lambda d: d["docId"]), len(rows) >= QUERY_LIMIT

    def list_documents(
        self,
        *,
        owner: str | None = None,
        scope: str = "mine",
        page: Any = 1,
        page_size: Any = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """文档列表（分页）。

        分页发生在**聚合之后**：先聚出全量文档再切页。当前规模下聚合耗时 < 1s，
        可接受；切片数涨到十万级时需改用 Milvus query_iterator 或独立元数据表。
        """
        items, truncated = self._aggregate_documents(owner, scope)
        total = len(items)
        p, ps = normalize_page(page, page_size)
        total_pages = max(1, (total + ps - 1) // ps)
        start = (p - 1) * ps
        return {
            "total": total,
            "page": p,
            "pageSize": ps,
            "totalPages": total_pages,
            "items": items[start : start + ps],
            "truncated": truncated,
        }

    def stats(self, *, owner: str | None = None, scope: str = "mine") -> dict[str, Any]:
        """知识库统计：文档数 / 切片数 / 分类分布 / 可见性分布。

        注意按**全量**文档统计，不能复用分页后的 items——否则统计卡片会随翻页变化。
        """
        items, _ = self._aggregate_documents(owner, scope)

        by_category: dict[str, int] = {}
        by_visibility: dict[str, int] = {}
        for d in items:
            cat = d["category"] or "未分类"
            by_category[cat] = by_category.get(cat, 0) + 1
            by_visibility[d["visibility"]] = by_visibility.get(d["visibility"], 0) + 1

        return {
            "docCount": len(items),
            "chunkCount": sum(d["chunkCount"] for d in items),
            "charCount": sum(d["charCount"] for d in items),
            "publicDocCount": by_visibility.get("public", 0),
            "privateDocCount": by_visibility.get("private", 0),
            "categoryCount": len([c for c in by_category if c != "未分类"]),
            "byCategory": [{"category": k, "count": v}
                           for k, v in sorted(by_category.items(), key=lambda x: -x[1])],
            "collection": settings.milvus_collection,
        }

    # ------------------------------------------------------------ 生命周期（G1 软删 / G2 停用·启用·双确认）

    def delete_document(
        self, doc_id: str, *, owner: str | None = None, is_admin: bool = False,
    ) -> dict[str, Any]:
        """移入回收站（软删除）。

        切片从 Milvus **真实移除**，不采用"打标记 + 检索时过滤排除"的假删除：
        后者只要有一处过滤条件漏改，被删文档就会继续被智能体检索到，
        且这种泄漏不会报错、极难发现。备份落本地后即可随时恢复。

        源文件**保留**——它是未经切片的完整原文；
        只有彻底删除时才清理。

        G2 升级：备份记录 v2**随身带 dense 向量**（`chunks[]`），
        使恢复走"按原切片回灌"（零 embedding、与删除前逐字节一致）。
        """
        rows = self._fetch_rows(doc_id)
        if not rows:
            raise KbError(f"文档不存在：{doc_id}")

        doc_owner = rows[0].get("owner") or ""
        if not is_admin and owner and doc_owner != owner:
            raise KbError("只能删除自己上传的文档")

        suffix = Path(str(rows[0].get("source") or "")).suffix or ".md"
        gov_doc = governance_service.get_doc(doc_id) or {}
        record = self._build_backup_record(
            doc_id, rows, state=STATE_RECYCLED,
            operator=owner or "unknown", is_admin=is_admin,
            file_hash=gov_doc.get("file_hash"), version_no=gov_doc.get("version_no"),
            suffix=suffix,
        )

        # 先落备份、再删向量：中途失败最坏是多一条可清理的回收站记录，
        # 绝不会出现"向量已删、备份没写成"的静默数据丢失。
        self._write_backup(record)

        # 先落"删除意图"（期望=无），再删向量：对账任务的判据先于事实变化，
        # 万一删除只做了一半，下一轮对账会接着把它清干净（spec §5 铁律 1）
        governance_service.mark_vector_expectation(doc_id, False)

        with self._write_lock:
            removed = self._delete_by_doc_id(self._get_client(), self.collection, doc_id)
            self._get_client().flush(self.collection)

        # ---- 治理登记：向量确已移除后才置 RECYCLED（vector_present=0）----
        governance_service.mark_state(
            doc_id, GovState.RECYCLED,
            action=GovAction.SOFT_DELETE,
            vector_present=False,
            doc_name=record["docTitle"],
            detail={
                "removedChunks": removed,
                "isAdmin": bool(is_admin),
                "versionNo": record["versionNo"],
                "backupVectors": kb_backups.has_vectors(record),
            },
            operator=owner or record["deletedBy"],
        )

        logger.info("文档已移入回收站 | doc_id=%s chunks=%d by=%s admin=%s",
                    doc_id, removed, owner, is_admin)
        return {
            "docId": doc_id,
            "docTitle": record["docTitle"],
            "removedChunks": removed,
            "recycled": True,
        }

    def disable_document(
        self, doc_id: str, *, owner: str | None = None, is_admin: bool = False,
    ) -> dict[str, Any]:
        """停用文档（DISABLED）：物理删向量 + 备份落 `data/kb/disabled/`。

        与软删的差别只有两处：备份目录（无 TTL）与状态语义（"下架"而非"删除"）——
        检索侧的表现完全一样：**内容不在集合里，就不可能被召回**。

        停用后随时可 `enable` 回灌，不需要重新 embedding。
        """
        if not settings.gov_disable_enabled:
            raise KbError("停用功能未开启（GOV_DISABLE_ENABLED=false）")

        rows = self._fetch_rows(doc_id)
        if not rows:
            raise KbError(f"文档不存在：{doc_id}")

        doc_owner = rows[0].get("owner") or ""
        if not is_admin and owner and doc_owner != owner:
            raise KbError("只能停用自己上传的文档")

        gov_doc = governance_service.get_doc(doc_id) or {}
        if str(gov_doc.get("state") or "") == GovState.DISABLED:
            raise KbError("文档已处于停用状态")

        suffix = Path(str(rows[0].get("source") or "")).suffix or ".md"
        record = self._build_backup_record(
            doc_id, rows, state=STATE_DISABLED,
            operator=owner or "unknown", is_admin=is_admin,
            file_hash=gov_doc.get("file_hash"), version_no=gov_doc.get("version_no"),
            suffix=suffix,
        )
        self._write_backup(record)

        governance_service.mark_vector_expectation(doc_id, False)
        with self._write_lock:
            removed = self._delete_by_doc_id(self._get_client(), self.collection, doc_id)
            self._get_client().flush(self.collection)

        governance_service.mark_state(
            doc_id, GovState.DISABLED,
            action=GovAction.DISABLE,
            vector_present=False,
            doc_name=record["docTitle"],
            detail={"removedChunks": removed, "isAdmin": bool(is_admin),
                    "backupVectors": kb_backups.has_vectors(record)},
            operator=owner or record["deletedBy"],
        )

        logger.info("文档已停用 | doc_id=%s chunks=%d by=%s admin=%s",
                    doc_id, removed, owner, is_admin)
        return {
            "docId": doc_id,
            "docTitle": record["docTitle"],
            "removedChunks": removed,
            "disabled": True,
        }

    def enable_document(
        self, doc_id: str, *, owner: str | None = None, is_admin: bool = False,
    ) -> dict[str, Any]:
        """启用被停用的文档：从 `data/kb/disabled/` 备份**回灌**向量（零 embedding）。"""
        if not settings.gov_disable_enabled:
            raise KbError("停用功能未开启（GOV_DISABLE_ENABLED=false）")

        record = kb_backups.read(doc_id, STATE_DISABLED)
        if record is None:
            state = (governance_service.get_doc(doc_id) or {}).get("state")
            hint = "（该文档当前状态：%s）" % state if state else ""
            raise KbError(f"没有该文档的停用备份，无法启用：{doc_id}{hint}")
        self._require_backup_owner(record, owner, is_admin)

        if self.query_count(doc_id) > 0:
            raise KbError("已存在同名的在库文档，启用会与其冲突——请先处理现有文档后重试")

        result, mode = self._write_back(record)
        kb_backups.remove(doc_id, STATE_DISABLED)

        governance_service.mark_state(
            doc_id, GovState.ACTIVE,
            action=GovAction.ENABLE,
            vector_present=True,
            doc_name=record.get("docTitle") or doc_id,
            detail={"mode": mode, "chunks": result.get("chunkCount"),
                    "embeddingCalls": result.get("embeddingCalls"),
                    "isAdmin": bool(is_admin)},
            operator=owner or record.get("deletedBy"),
        )

        logger.info("文档已启用 | doc_id=%s mode=%s chunks=%s embedding=%s by=%s",
                    doc_id, mode, result.get("chunkCount"),
                    result.get("embeddingCalls"), owner)
        return {**result, "enabled": True}

    # ---- 备份读写（委托 kb_backups，保留同名薄封装以兼容既有调用点）----

    @staticmethod
    def _recycle_path(doc_id: str) -> Path:
        return kb_backups.path_of(doc_id, STATE_RECYCLED)

    def _write_recycle(self, record: dict[str, Any]) -> None:
        self._write_backup({**record, "state": STATE_RECYCLED})

    def _read_recycle(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return kb_backups.read(doc_id, STATE_RECYCLED)
        except ValueError as e:
            raise KbError(str(e)) from e

    @staticmethod
    def _remove_recycle(doc_id: str) -> None:
        kb_backups.remove(doc_id, STATE_RECYCLED)

    @staticmethod
    def _require_recycle_owner(
        record: dict[str, Any], owner: str | None, is_admin: bool,
    ) -> None:
        """回收站操作的权限校验：非管理员只能处理自己删除的文档。"""
        KbService._require_backup_owner(record, owner, is_admin)

    # ---- 回收站能力 ----

    def list_recycled(
        self,
        *,
        owner: str | None = None,
        scope: str = "mine",
        page: Any = 1,
        page_size: Any = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """回收站列表（分页）。

        回收站是"已删除内容"的索引，只对登录用户开放（在 Java 层收口）。
        scope=all 为管理员视角，可看到全部人的删除记录。

        列表**不回传重字段**（`chunks[]` 含全文与 2048 维向量，一篇几百 KB）——
        需要全文时走单文档恢复路径，不在列表里搬大数据。
        """
        records = kb_backups.list_records(STATE_RECYCLED)

        if scope != "all":
            records = [
                r for r in records
                if not owner or (r.get("deletedBy") or r.get("owner")) == owner
            ]

        records.sort(key=lambda r: str(r.get("deletedAt") or ""), reverse=True)

        items = [kb_backups.strip_heavy(r) for r in records]
        # 补 TTL 剩余期：前端回收站列表据此显示「剩余 N 天」（规约 §6.3）。
        # 停用区（list_disabled）刻意不加——停用状态没有 TTL，不参与自动清理。
        attach_recycle_ttl(items)

        total = len(items)
        p, ps = normalize_page(page, page_size)
        total_pages = max(1, (total + ps - 1) // ps)
        start = (p - 1) * ps
        return {
            "total": total,
            "page": p,
            "pageSize": ps,
            "totalPages": total_pages,
            "items": items[start : start + ps],
        }

    def restore_document(
        self, doc_id: str, *, owner: str | None = None, is_admin: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """从回收站恢复：L3 确认门 → **按原切片回灌**（备份带向量时零 embedding）。

        恢复语义的两处关键设计：
        1. **L3 确认门（G2）**：恢复会把一份语料放回检索池。若它已与在库文档
           高度相似（余弦 ≥ `gov_l3_threshold`），等于凭空多出一份近重复语料，
           必须先由人确认（`confirm=true`）。L3 服务不可用时**放行但留痕**
           （`[GOV-WARN]` + 审计 detail 记 `l3Status=unavailable`），
           否则向量服务一抖动就变成"谁也恢复不了"。
        2. **回灌而非重切**：备份里的 `chunks[]` 含原始 `text` 与 `dense`，
           直接写回即可得到与删除前**逐字节一致**的切片（引用定位也不漂移）；
           G1 老备份（无向量）才回落"重新切片 + embedding"。

        返回 `requireConfirm=True` 时**并未执行恢复**（前端据此弹确认框后带 confirm 重发）。
        该分支以 `3001 + data.requireConfirm` 抛出（KbConfirmRequired），与彻底删的确认门
        完全同形——两个门的返回形态若不一致，前端就得写两套判断。
        """
        record = self._read_recycle(doc_id)
        if record is None:
            raise KbError(f"回收站中没有该文档：{doc_id}")
        self._require_recycle_owner(record, owner, is_admin)

        if self.query_count(doc_id) > 0:
            # 不静默覆盖：恢复是把内容放回去，覆盖会连带丢掉用户新上传的版本
            raise KbError("已存在同名文档，恢复会覆盖它——请先处理现有文档（改名或删除）后重试")

        # ---- 确认门①：L3 语义查重 ----
        l3: dict[str, Any] = {"status": "skipped", "threshold": settings.gov_l3_threshold}
        if settings.gov_restore_l3_gate and not confirm:
            l3 = self._l3_similarity(record)
            if l3.get("status") == "similar":
                detail = {
                    "status": "similar",
                    "maxSimilarity": l3.get("maxSimilarity"),
                    "threshold": l3.get("threshold"),
                    "hits": l3.get("hits"),
                    "isAdmin": bool(is_admin),
                    "restored": False,
                }
                governance_service.audit(
                    GovAction.RESTORE, doc_id=doc_id,
                    doc_name=record.get("docTitle") or doc_id,
                    detail={"gate": "L3", **detail}, result="BLOCKED",
                    operator=owner or record.get("deletedBy"),
                )
                # 统一抛 KbConfirmRequired（不要 return 一个 2000 的 dict）：
                # 恢复本次**没有发生**，用成功码会让调用方误判（实测踩过），
                # 且确认门的返回形态必须与 purge 一致——前端只认一套
                raise KbConfirmRequired(
                    f"该文档与在库语料语义高度相似（最高 {l3.get('maxSimilarity')}，"
                    f"阈值 {l3.get('threshold')}），恢复会多出一份近重复语料；"
                    f"确认恢复请带 confirm=true 重发",
                    gate="L3", detail=detail,
                )

        result, mode = self._write_back(record)

        # 只有放回成功才移除备份：失败时保留，用户可直接重试
        self._remove_recycle(doc_id)

        governance_service.mark_state(
            doc_id, GovState.ACTIVE,
            action=GovAction.RESTORE,
            vector_present=True,
            doc_name=record.get("docTitle") or doc_id,
            detail={"mode": mode,
                    "chunks": result.get("chunkCount"),
                    "embeddingCalls": result.get("embeddingCalls"),
                    "isAdmin": bool(is_admin),
                    "versionNo": record.get("versionNo") or 1,
                    "l3Status": l3.get("status"),
                    "l3MaxSimilarity": l3.get("maxSimilarity"),
                    "confirmed": bool(confirm)},
            operator=owner or record.get("deletedBy"),
        )

        logger.info("文档已从回收站恢复 | doc_id=%s mode=%s chunks=%s embedding=%s by=%s",
                    doc_id, mode, result.get("chunkCount"),
                    result.get("embeddingCalls"), owner)
        return {**result, "restored": True, "confirmed": bool(confirm),
                "l3Status": l3.get("status"), "maxSimilarity": l3.get("maxSimilarity")}

    @staticmethod
    def _restore_content(record: dict[str, Any]) -> str:
        """恢复正文来源：优先落盘源文件（完整原文），缺失时用备份切片拼接。

        仅在"回灌不可用"（备份无向量）时才会被调用。
        """
        doc_id = str(record.get("docId") or "")
        for p in UPLOAD_DIR.glob(f"{doc_id}.*"):
            try:
                return p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("源文件读取失败，改用备份内容 | doc_id=%s err=%s", doc_id, e)
                break

        joined = kb_backups.plain_text(record)
        if not joined:
            raise KbError("回收站记录缺少可恢复内容（源文件与备份均为空）")
        return joined

    def purge_document(
        self, doc_id: str, *, owner: str | None = None, is_admin: bool = False,
        confirm: bool = False, confirm_doc_name: str | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """彻底删除（三清 + 双确认），不可恢复。

        铁律 2（spec §5）：**只允许管理员** + **必须显式二次确认**
        （`confirm=true` 且 `confirmDocName` 与文档名一致）。两件事都拦在这里，
        而不是只拦在 Java 层——Python 侧的直调路径（本地调试/脚本）同样不能绕过。

        三清：
        1. **Milvus 向量**——修 spec §5 隐患 2：软删时若 `_delete_by_doc_id` 部分失败，
           残留切片会因失去名称关联而**永久泄漏**（备份被删后再无线索指向它），
           因此彻底删前做一次防御性清理并记录条数
        2. **备份文件**——回收站与停用区两个目录都清（停用文档也可被管理员彻底删除）
        3. **落盘源文件** `data/kb/raw/uploads/<doc_id>.*`

        最后清除 SQLite 登记行与切片指纹，**审计永久保留**（PURGE 是唯一的留痕）。
        """
        found = kb_backups.locate(doc_id)
        if found is None:
            raise KbError(f"回收站中没有该文档：{doc_id}")
        origin, record = found
        self._require_backup_owner(record, owner, is_admin)

        actor = operator or owner or record.get("deletedBy") or "unknown"
        doc_name = str(record.get("docTitle") or doc_id)

        # ---- 铁律：彻底删除只允许管理员 ----
        if settings.gov_purge_require_admin and not is_admin:
            raise KbError("彻底删除仅管理员可操作")

        # ---- 确认门②：confirm=true + 文档名复述一致 ----
        if settings.gov_purge_require_confirm and not (
            confirm and self._confirm_name_matches(confirm_doc_name, record)
        ):
            governance_service.audit(
                GovAction.PURGE, doc_id=doc_id, doc_name=doc_name,
                detail={"gate": "confirm", "confirmed": bool(confirm),
                        "providedName": confirm_doc_name, "expectedName": doc_name,
                        "origin": origin},
                result="BLOCKED", operator=actor,
            )
            raise KbConfirmRequired(
                f"彻底删除不可恢复，需要二次确认：请带 confirm=true 与 "
                f"confirmDocName「{doc_name}」重发",
                gate="confirm",
                detail={"requireConfirm": True, "expectedDocName": doc_name,
                        "docId": doc_id, "origin": origin},
            )

        # 三清与登记清除统一走 _purge_core：TTL 自动清理复用同一执行体，
        # 否则"删了一半"的顺序语义会在两处实现里漂移（见该方法的说明）
        return self._purge_core(
            doc_id, record=record, origin=origin, actor=actor,
            action=GovAction.PURGE, is_admin=is_admin,
            extra_detail={"confirmDocName": confirm_doc_name, "gate": "confirm"},
        )

    def _purge_core(
        self,
        doc_id: str,
        *,
        record: dict[str, Any] | None,
        origin: str,
        actor: str,
        action: str,
        is_admin: bool,
        extra_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """三清执行体——**彻底删除的唯一实现**（人工 purge 与 TTL 自动清理共用）。

        为什么必须抽成一个方法，而不是让 TTL 再写一遍：三清的**顺序本身就是安全语义**
        （先清向量并核验 → 核验不过整体中止并保留备份/账本 → 再删备份与源文件 → 最后清账本），
        复制一份迟早会漂移；而"删了一半"的后果是不可逆的泄漏（spec §5 隐患 2）。
        两个调用方的差异只有三处，全部用参数表达：

        - `action`：审计动作（`PURGE` 人工 / `TTL_PURGE` 系统超期），凭这一列就能区分
          "人删的"与"过期自动删的"——事后追责的第一个问题就是这两者
        - `actor`：操作人（人工是登录用户；系统固定 `system`）
        - `extra_detail`：TTL 额外记超期时间等上下文

        **本方法不做任何权限判定**（管理员门、确认门、归属校验都在调用方），
        因此它只能被内部调用，绝不可直接暴露成 HTTP 接口。
        """
        doc_name = str((record or {}).get("docTitle") or doc_id)

        # ---- 三清①：向量（含残留）----
        # 顺序刻意如此：先清向量并**核验**，核验不过就整体中止，
        # 保留备份与账本行让对账继续清——反过来（先删备份/账本）会让残留向量
        # 失去一切账面依据，永久泄漏且再也清不掉（spec §5 隐患 2）
        client = self._get_client()
        with self._write_lock:
            residue = self._delete_by_doc_id(client, self.collection, doc_id)
            if residue:
                client.flush(self.collection)
                logger.warning("[GOV-WARN] 彻底删除时发现向量残留并已清理 | doc_id=%s 残留切片=%d",
                               doc_id, residue)

        leftover = self._await_vectors_absent(doc_id)
        if leftover > 0:
            governance_service.audit(
                action, doc_id=doc_id, doc_name=doc_name,
                detail={"origin": origin, "vectorResidueRemoved": residue,
                        "leftover": leftover, "aborted": True},
                result="FAILED", operator=actor,
            )
            logger.error("[GOV-WARN] 向量未清干净，已中止彻底删除（保留备份与登记待重试）| "
                         "doc_id=%s leftover=%d", doc_id, leftover)
            raise KbError(
                f"向量库中仍有 {leftover} 个切片未清除，已中止彻底删除并保留备份与登记；"
                f"请稍后重试（对账任务也会继续清理）"
            )

        # ---- 三清②③：备份文件 + 落盘源文件 ----
        backups_removed = kb_backups.remove(doc_id)           # 回收站 + 停用区
        src_before = self._source_paths(doc_id)
        source_removed = self._remove_source(doc_id)

        # 三清必须**核验**并如实上报：向量与账本已经没了、备份却还在磁盘上，
        # 是最不该静默的情况——用户以为内容已不可恢复地销毁，实际全文与向量还在。
        # （环境可能拒绝删除：文件被占用、或单次调用的删除配额用尽）
        warnings: list[str] = []
        stuck_backups = kb_backups.remaining_paths(doc_id)
        if stuck_backups:
            warnings.append(
                f"有 {len(stuck_backups)} 个备份文件未能删除，内容仍留在磁盘上"
                f"（向量与登记已清除，需人工清理）：{stuck_backups}")
        stuck_src = [str(p) for p in src_before if p.exists()]
        if stuck_src:
            warnings.append(f"源文件未能删除，仍留在磁盘上：{stuck_src}")
        for w in warnings:
            logger.error("[GOV-WARN] 彻底删除未清干净 | doc_id=%s %s", doc_id, w)

        governance_service.unregister(
            doc_id,
            action=action,
            doc_name=doc_name,
            detail={"origin": origin,
                    "vectorResidueRemoved": residue,
                    "backupsRemoved": len(backups_removed),
                    "sourceRemoved": source_removed,
                    "charCount": (record or {}).get("charCount"),
                    "isAdmin": bool(is_admin),
                    **(extra_detail or {}),
                    **({"warnings": warnings} if warnings else {})},
            operator=actor,
        )

        logger.info("文档已彻底删除（三清）| doc_id=%s action=%s 残留=%d 备份=%d 源文件=%s by=%s admin=%s",
                    doc_id, action, residue, len(backups_removed), source_removed, actor, is_admin)
        return {
            "docId": doc_id,
            "docTitle": doc_name,
            "origin": origin,
            "action": action,
            "vectorResidueRemoved": residue,
            "backupsRemoved": len(backups_removed),
            "sourceRemoved": source_removed,
            "purged": True,
            # 非空即表示"没删干净"：调用方（前端/运维）必须能看到，别当成彻底销毁
            "warnings": warnings,
        }

    def purge_expired(self, doc_id: str, *, operator: str = "system") -> dict[str, Any]:
        """TTL 到期彻底删除（系统动作：**无管理员门、无确认门**）。

        与 `purge_document` 的差别只有前置校验：TTL 是既定策略在到期时自动执行，
        再要一次人工确认等于让 TTL 永远不生效。但它多了两道**前提检查**：

        1. **状态复检（TOCTOU）**：调用方的候选列表是几秒前算出来的，这中间用户可能
           刚点了"恢复"。删除不可逆，动手前必须再确认"当前仍是 RECYCLED"——
           对账任务踩过同类坑（判定后状态翻转 → 误删刚恢复的内容）
        2. **备份缺失不中止**：备份被人为删过时，账本与源文件仍要清，但要在
           `warnings` 里如实写明"未找到备份记录"——"清干净了"与"本来就没有"
           是两件事，调用方要能分辨

        本方法不做归属校验、不受 `gov_purge_require_admin` 约束——它是系统策略的执行末端，
        只应由 TTL 任务调用；对外暴露的人工入口是 `purge_document`。
        """
        doc = governance_service.get_doc(doc_id) or {}
        state = str(doc.get("state") or "")
        if state != GovState.RECYCLED:
            raise KbError(
                f"TTL 只清理回收站（RECYCLED）文档，当前状态为 {state or '不存在'}：{doc_id}"
            )

        found = kb_backups.locate(doc_id)
        origin, record = found if found else ("none", None)
        missing_backup = record is None
        result = self._purge_core(
            doc_id, record=record, origin=origin, actor=operator,
            action=GovAction.TTL_PURGE, is_admin=True,
            extra_detail={"source": "TTL", "ttlDays": int(settings.gov_ttl_days),
                          "recycledAt": doc.get("recycled_at")},
        )
        if missing_backup:
            result["warnings"] = [
                *result.get("warnings", []),
                "未找到备份记录（可能已被人工清理），本次仅清除账本与源文件",
            ]
        return result

    # ---- 版本链（G3）----

    @staticmethod
    def _version_item(record: dict[str, Any], *, is_current: bool,
                      live: bool = False) -> dict[str, Any]:
        """把一份备份（或账本行）整理成版本链条目（列表视图，不含 chunks）。

        字段名**两种写法都要读**：备份记录是驼峰（`versionNo`/`docTitle`），
        治理账本行是下划线（`version_no`/`doc_name`）。本方法两类输入都吃
        （`list_versions` 既传备份也传账本行），少读一种就会出现
        "当前版本在版本链里显示成 v1"这类静默错位——实测踩到过：
        `maxVersion` 因此算成历史最大值，前端的"可回滚版本"列表跟着错。
        """
        return {
            "versionNo": int(record.get("versionNo") or record.get("version_no") or 1),
            "role": str(record.get("role") or ROLE_CURRENT),
            "state": record.get("state"),
            "isCurrent": bool(is_current),
            "live": bool(live),          # true = 正躺在向量库里（可直接被检索到）
            "docTitle": (record.get("docTitle") or record.get("docName")
                         or record.get("doc_name") or ""),
            "fileName": record.get("fileName") or "",
            "category": record.get("category") or "",
            "owner": record.get("owner") or "",
            "chunkCount": int(record.get("chunkCount") or record.get("chunk_count") or 0),
            "charCount": int(record.get("charCount") or record.get("char_count") or 0),
            "fileHash": record.get("fileHash") or record.get("file_hash"),
            "snapshotAt": (record.get("deletedAt") or record.get("archivedAt")
                           or record.get("updated_at") or ""),
            "snapshotBy": record.get("deletedBy") or "",
            # 可回滚性：有向量 → 零 embedding 回灌；只有正文 → 回落重新切片；
            # 两者都没有（理论上不会出现）才不可回滚
            "hasVectors": kb_backups.has_vectors(record) if "chunks" in record
                          or "chunkTexts" in record else False,
        }

    def list_versions(self, doc_id: str) -> dict[str, Any]:
        """版本链：当前版本 + 历史版本快照（按版本号降序）。

        数据来源有三处，必须合并去重才不会漏版本：
        1. **账本 + 向量库**——文档在库时的"当前版本"（它没有备份文件）
        2. **回收站 / 停用区的当前版本备份**——文档不在库时，那一版就是当前版本
        3. **历史快照**（`role=history`）——同名覆盖时自动留档的旧版本

        同一个 versionNo 只会出现一条：current 优先于 history
        （正常不会撞号，撞了说明有过异常中断，此时以"当前版本"的语义为准）。
        """
        if not settings.gov_version_chain_enabled:
            raise KbError("版本链未开启（GOV_VERSION_CHAIN_ENABLED=false）")

        gov_doc = governance_service.get_doc(doc_id) or {}
        snapshots = kb_backups.list_versions(doc_id)
        cur_ver = int(gov_doc.get("version_no") or 0)

        by_ver: dict[int, dict[str, Any]] = {}
        for rec in snapshots:
            ver = int(rec.get("versionNo") or 1)
            item = self._version_item(rec, is_current=False)
            exist = by_ver.get(ver)
            if exist is None or (item["role"] == ROLE_CURRENT
                                 and exist["role"] != ROLE_CURRENT):
                by_ver[ver] = item

        state = str(gov_doc.get("state") or "")
        if state == GovState.ACTIVE:
            by_ver[cur_ver] = self._version_item(
                {**gov_doc, "docTitle": gov_doc.get("doc_name")},
                is_current=True, live=True)
        elif state in (GovState.RECYCLED, GovState.DISABLED) and cur_ver not in by_ver:
            # 账本有行但备份缺失（手工清理过磁盘）：仍列出当前版本，标注不可回滚
            by_ver[cur_ver] = self._version_item(
                {**gov_doc, "docTitle": gov_doc.get("doc_name")},
                is_current=True, live=False)

        items = sorted(by_ver.values(), key=lambda x: int(x["versionNo"]), reverse=True)
        max_ver = max([int(i["versionNo"]) for i in items] or [0])
        title = (gov_doc.get("doc_name") or next(
            (i["docTitle"] for i in items if i["docTitle"]), doc_id))
        return {
            "docId": doc_id,
            "docTitle": title,
            "state": state or None,
            "currentVersion": cur_ver or None,
            "maxVersion": max_ver,
            "total": len(items),
            "items": items,
        }

    def rollback_document(
        self, doc_id: str, *, target_version: int | None = None,
        owner: str | None = None, is_admin: bool = False,
        confirm: bool = False, confirm_doc_name: str | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """回滚到某个历史版本（G3）：把该版本的内容重新变成**当前版本**。

        四步，顺序都有理由：
        1. **当前版本先入历史快照**（`role=history`）——回滚是"换内容"，
           换掉的那份必须留档，否则用户回滚错了就再也回不去（这与"删除前先备份"同源）
        2. **回灌目标版本**（备份带向量时零 embedding，与删除前逐字节一致）
        3. **版本号升为历史最大值 + 1**，**不是**沿用目标版本号——
           若沿用，回收站里会同时存在"v1 的快照"和"当前版本 v1"，两个不同内容的
           东西共用一个号；备份文件名也会互相覆盖。版本号是**单调递增的世代号**，
           不是"内容标识"（内容相同与否由 fileHash 表达）
        4. 修剪超限的历史快照

        权限：仅管理员（`gov_rollback_require_admin`）。回滚会把**在库的可检索内容**
        整体换掉——智能体正在使用的语料被悄悄退回旧版，影响面是全体使用者，
        这是治理动作而非"管理自己的文档"（文档所有者更新内容走的是上传路径）。

        目标版本缺省 = 版本号最大的历史快照（即"退回上一版"）。
        """
        if not settings.gov_version_chain_enabled:
            raise KbError("版本链未开启（GOV_VERSION_CHAIN_ENABLED=false）")
        if settings.gov_rollback_require_admin and not is_admin:
            raise KbError("版本回滚仅管理员可操作")

        gov_doc = governance_service.get_doc(doc_id)
        if not gov_doc:
            raise KbError(f"文档不存在：{doc_id}")
        state = str(gov_doc.get("state") or "")
        if state == GovState.RECYCLED:
            raise KbError("文档在回收站中，请先用「恢复」把它放回库再回滚版本")
        if state == GovState.DISABLED:
            raise KbError("文档处于停用状态，请先「启用」再回滚版本")
        if not is_admin and owner and (gov_doc.get("owner") or "") != owner:
            raise KbError("只能回滚自己上传的文档")

        hist = [r for r in kb_backups.list_versions(doc_id) if is_history(r)]
        if not hist:
            raise KbError(f"不存在历史版本快照，无法回滚：{doc_id}")
        if target_version is None:
            target = hist[0]                       # list_versions 已按版本号降序
        else:
            target = next((r for r in hist
                           if int(r.get("versionNo") or 1) == int(target_version)), None)
            if target is None:
                have = "、".join(f"v{int(r.get('versionNo') or 1)}" for r in hist)
                raise KbError(f"不存在 v{target_version} 的历史快照（现有：{have}）")

        cur_ver = int(gov_doc.get("version_no") or 1)
        src_ver = int(target.get("versionNo") or 1)
        doc_name = str(gov_doc.get("doc_name") or doc_id)
        actor = operator or owner or "unknown"

        # ---- 确认门：confirm + 文档名复述（与彻底删除同形，前端只认一套）----
        if settings.gov_rollback_require_confirm and not (
            confirm and self._confirm_name_matches(
                confirm_doc_name,
                {"docId": doc_id, "docTitle": doc_name,
                 "fileName": target.get("fileName") or ""})
        ):
            detail = {"expectedDocName": doc_name, "docId": doc_id,
                      "fromVersion": cur_ver, "sourceVersion": src_ver,
                      "isAdmin": bool(is_admin), "rolledBack": False}
            governance_service.audit(
                GovAction.ROLLBACK, doc_id=doc_id, doc_name=doc_name,
                version_no=cur_ver, detail={"gate": "confirm", **detail},
                result="BLOCKED", operator=actor,
            )
            raise KbConfirmRequired(
                f"回滚会用历史版本 v{src_ver} 的内容替换当前版本 v{cur_ver}"
                f"（当前版本会自动留档为历史快照）。"
                f"确认请带 confirm=true 与 confirmDocName「{doc_name}」重发",
                gate="rollback", detail=detail,
            )

        # ---- 1. 当前版本入历史快照（换掉的那份必须留档）----
        snap_ok = True
        try:
            cur_rows = self._fetch_rows(doc_id)
            if cur_rows:
                suffix = Path(str(cur_rows[0].get("source") or "")).suffix or ".md"
                self._write_backup(self._build_backup_record(
                    doc_id, cur_rows, state=STATE_RECYCLED, operator=actor,
                    is_admin=bool(is_admin), file_hash=gov_doc.get("file_hash"),
                    version_no=cur_ver, suffix=suffix, role=ROLE_HISTORY,
                ))
        except Exception as e:  # noqa: BLE001
            # 与"上传路径的快照失败"同一取舍：留档失败不阻断回滚，但必须如实回报，
            # 否则用户以为"随时能切回去"，实际已经切不回去了
            snap_ok = False
            logger.warning("[GOV-WARN] 回滚前当前版本留档失败（回滚后无法切回本版）| "
                           "doc=%s ver=%s err=%s", doc_id, cur_ver, e)

        # ---- 2. 回灌目标版本 ----
        result, mode = self._write_back(target)

        # ---- 3. 版本号升为历史最大值 + 1（世代号单调递增，绝不回退）----
        versions = [int(r.get("versionNo") or 1) for r in kb_backups.list_versions(doc_id)]
        new_ver = max([cur_ver, src_ver, *versions]) + 1
        governance_service.mark_state(
            doc_id, GovState.ACTIVE, vector_present=True, doc_name=doc_name)
        governance_service.set_version(
            doc_id, new_ver,
            doc_name=doc_name, operator=actor,
            detail={"fromVersion": cur_ver, "sourceVersion": src_ver,
                    "mode": mode, "chunks": result.get("chunkCount"),
                    "embeddingCalls": result.get("embeddingCalls"),
                    "currentArchived": snap_ok, "isAdmin": bool(is_admin),
                    "confirmed": bool(confirm)},
        )

        # ---- 4. 修剪超限快照 ----
        try:
            kb_backups.prune_versions(doc_id, settings.gov_version_keep)
        except Exception as e:  # noqa: BLE001
            logger.warning("[GOV-WARN] 版本快照修剪失败 | doc=%s err=%s", doc_id, e)

        logger.info("文档已回滚 | doc_id=%s v%s → 内容取自 v%s，新版本号 v%s mode=%s chunks=%s "
                    "embedding=%s by=%s", doc_id, cur_ver, src_ver, new_ver, mode,
                    result.get("chunkCount"), result.get("embeddingCalls"), actor)
        return {**result, "rolledBack": True, "fromVersion": cur_ver,
                "sourceVersion": src_ver, "versionNo": new_ver,
                "versionSnapshot": snap_ok, "confirmed": bool(confirm)}

    # ---- 停用区 ----

    def list_disabled(self, *, page: Any = 1, page_size: Any = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """停用区列表（分页）：停用不是删除，单独一个视图，不进回收站。"""
        records = kb_backups.list_records(STATE_DISABLED)
        records.sort(key=lambda r: str(r.get("deletedAt") or ""), reverse=True)
        items = [kb_backups.strip_heavy(r) for r in records]
        total = len(items)
        p, ps = normalize_page(page, page_size)
        total_pages = max(1, (total + ps - 1) // ps)
        start = (p - 1) * ps
        return {
            "total": total,
            "page": p,
            "pageSize": ps,
            "totalPages": total_pages,
            "items": items[start : start + ps],
        }

    @staticmethod
    def _count_by_doc_id(client: MilvusClient, collection: str, doc_id: str) -> int:
        safe = doc_id.replace('"', "")
        rows = client.query(
            collection_name=collection, filter=f'doc_id == "{safe}"',
            output_fields=["count(*)"],
        )
        return int(rows[0].get("count(*)", 0)) if rows else 0

    def _await_vectors_absent(self, doc_id: str, attempts: int = 3,
                              interval: float = 0.6) -> int:
        """确认某文档在向量库里确无切片（删除后的**后置条件**核验）。

        删除 + flush 之后计数接口可能有短暂延迟，直接断言"已清干净"会误判；
        这里重试几次，仍 >0 才认定为真残留（此时调用方应中止后续不可逆动作）。
        """
        left = 0
        for i in range(max(1, attempts)):
            left = self.query_count(doc_id)
            if left == 0:
                return 0
            if i < attempts - 1:
                time.sleep(interval)
        return left

    @staticmethod
    def _source_paths(doc_id: str) -> list[Path]:
        """该文档落盘的源文件（清理前先取快照，清理后据此核验"是不是真删掉了"）。"""
        if not UPLOAD_DIR.exists():
            return []
        return list(UPLOAD_DIR.glob(f"{doc_id}.*"))

    @staticmethod
    def _remove_source(doc_id: str) -> bool:
        """删除落盘源文件，返回是否**确认已删除**。

        与备份清理同一口径：以后置条件（文件是否真的不在了）判定，
        不信任 `unlink` 的返回值/异常——有些环境的安全删除钩子会
        "删掉了但抛异常"，也有"没抛异常但文件仍被占用"；
        批量删除配额用尽时钩子还会抛 **SystemExit**（不是 OSError），
        统一交给 `unlink_quietly` 按"删没删掉"处理，别让异常穿成 500。
        """
        found = KbService._source_paths(doc_id)
        if not found:
            return False
        left = [p for p in found if not unlink_quietly(p)]
        return len(left) < len(found)


# 模块级单例：与 retriever 同样由 FastAPI 多请求共享连接
kb_service = KbService()

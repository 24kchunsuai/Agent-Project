"""知识库备份存储（回收站 / 停用区）——G2 生命周期强化的基础设施。

为什么单独成模块
----------------
G1 时期备份只是回收站的一个 JSON 落盘动作，写在 `kb_service` 里够用。G2 之后它承担三件事：
1. **恢复/启用的唯一数据来源**：备份必须能重建出**与删除前逐字节一致**的切片行
   （含 dense 向量），否则"回灌"不成立，只能回去重新 embedding
2. **两个生命周期状态共用**：RECYCLED（回收站，有 30 天 TTL）与 DISABLED（停用，无 TTL）
3. **可演进**：G3 版本链要按版本存快照（`recycle/<doc_id>.json` 单文件会互相覆盖）

目录约定（**刻意分开**）
------------------------
| 目录 | 状态 | 有无 TTL | 语义 |
|---|---|---|---|
| `data/kb/recycle/<doc_id>.json`  | RECYCLED | 有（30 天，G4） | 用户视角的"垃圾桶" |
| `data/kb/disabled/<doc_id>.json` | DISABLED | **无** | 运维视角的"下架"，随时可启用 |

分开的原因：TTL 清理按"回收站里的东西"扫，若把停用文档混在同一目录，
**一次 TTL 清理就会把停用文档的唯一一份向量备份删掉**（不可恢复）。
两个目录 = 两条独立的生命周期，互不干扰。

文件名与"角色"（G3 版本链）
----------------------------
| 角色 | 文件名 | 含义 | 可做什么 |
|---|---|---|---|
| `current` | `<doc_id>.json` | 该文档**当前在编**的那一版快照（软删/停用产生） | 恢复 / 启用 |
| `history` | `<doc_id>@v<n>.json` | **历史版本**快照（同名覆盖时旧版自动入回收站） | 回滚 / 版本链查看 |

为什么用 `@` 而不是 `.v2`：`safe_doc_id` 允许 `.` 与 `-`，若写成 `<doc_id>.v2.json`，
则文档 `a` 的清理会 glob 到文档 `a.v2` 的主备份（**跨文档误删**）。
`@` 不在 safe 字符集内，故 `<doc_id>@v*.json` 永不误匹配别的 doc_id——
用一个不可能出现的分隔符换取"清理时零校验成本"。

两种角色在列表里的处理：`list_records` 默认**只回 current**——
回收站/停用区是"用户能恢复的东西"，历史版本混进去会出现同名多条且恢复必然冲突。

备份记录 v2（schema=2）
----------------------
```json
{
  "schema": 2,
  "state": "RECYCLED" | "DISABLED",
  "docId": "...", "docTitle": "...", "category": "...", "owner": "...",
  "visibility": "...", "source": "...", "sourceType": "...",
  "docUpdated": "...", "docVersion": "...", "lang": "zh",
  "fileHash": "...", "versionNo": 3,
  "chunkCount": 21, "charCount": 8123, "fileName": "xx.md",
  "deletedAt": "...", "deletedBy": "...", "deletedByAdmin": false,
  "chunks": [
    {"chunkIndex": 0, "heading": "...", "charStart": 0, "charEnd": 388,
     "charLen": 420, "text": "<含标题前缀与 overlap 的原始切片文本>",
     "dense": "<float32 小端字节的 base64>"}
  ]
}
```

三个设计取舍
------------
1. **存 `text` 而不是只存 `body`**：回灌要写出与删除前**完全一致**的行，`text` 是
   Milvus 里的真实取值（含标题前缀与 overlap）。从 body 重新拼前缀等于又跑了一遍
   切片逻辑，chunker 一改就不保真——那正是 G1 时期"恢复不保真"的根因。
2. **dense 用 float32 + base64**：JSON 里直接写 `[0.0123, -0.0456, ...]`（2048 个浮点）
   体积膨胀到 ~25KB 且经过 float64 往返；base64 后固定 10.9KB 且**逐位无损**
   （`array('f')` 与 Milvus 存储的 float32 同一精度）。
3. **sparse 不存**：BM25 Function 会按 `text` 自动重建（G1 实测已验证）。

旧版兼容：G1 写的记录没有 `schema` 字段、用 `chunkTexts[]`、无向量——
`chunks_of()` / `texts_of()` 会把它转成统一视图，调用方据此**回落重新切片**入库。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

from app.core.paths import DATA_DIR                 # noqa: E402  数据根目录（DATA_ROOT 可覆盖）

ROOT = Path(__file__).resolve().parents[2]          # agt_python/
# 备份目录随数据根目录走：脚本设了 DATA_ROOT 即自动隔离（见 app/core/paths.py）
RECYCLE_DIR = DATA_DIR / "kb/recycle"
DISABLED_DIR = DATA_DIR / "kb/disabled"

SCHEMA_V2 = 2

STATE_RECYCLED = "RECYCLED"
STATE_DISABLED = "DISABLED"

# 备份角色（G3）：current = 当前在编版本（可恢复/启用）；history = 历史版本快照（可回滚）
ROLE_CURRENT = "current"
ROLE_HISTORY = "history"

# 历史快照的版本分隔符：**不在 safe_doc_id 的字符集内**，故 glob 永不误匹配（见模块 docstring）
VERSION_SEP = "@v"

# 重字段：列表接口不回传（全文 + 2048 维向量，一篇几百 KB）
HEAVY_FIELDS = ("chunks", "chunkTexts")

# 与 kb_service.sanitize_name 同一口径（防路径穿越；doc_id 入库时已 sanitize，
# 此处是对"外部传入的 doc_id"（如恢复/彻底删除参数）的兜底防线）
_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def _now() -> str:
    """备份记录里的时间戳口径（与治理库 now_iso 一致：秒级 ISO，便于跨表比对）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 本机安全删除钩子（sitecustomize 注入）**不止抛 OSError**：
#   - `SAFE_DELETE_FAIL_CLOSED`：系统回收站不可用 → 抛 OSError（可能文件其实已删）
#   - `SAFE_DELETE_BULK_GUARD`：单次工具调用的删除配额用尽 → 抛 **SystemExit(1)**
# SystemExit 是 BaseException，`except OSError` 兜不住 —— 它会一路穿到 FastAPI，
# 变成 **500 空响应**（实测踩到：G2 的 `enable` 因此崩在"响应不是 JSON"）。
# 更糟的是它发生在"清备份"这种**已删了向量、正准备收尾**的位置，
# 会把一个正常操作变成半途而废的 500。
UNLINK_ERRORS = (OSError, SystemExit)


def unlink_quietly(path: Path) -> bool:
    """尽力删除文件，返回**后置条件判定**的结果：文件是否真的已不在磁盘上。

    判定口径刻意不看"调用有没有抛异常"：
    - 钩子可能在**删除前**拒绝（批量配额用尽）→ 文件还在 → 返回 False（如实上报"没删掉"）
    - 钩子可能在**删除后**才报错（文件已被移入系统回收站）→ 文件不在 → 返回 True
    - 也可能"没抛异常但文件还在"（句柄占用）→ 返回 False

    以异常为准会让"三清"报告与实际完全相反，而这恰恰是运维用来判断
    "到底清没清"的数字。真伪只能问文件系统本身。
    """
    try:
        path.unlink()
    except UNLINK_ERRORS as e:
        logger.warning("删除调用报错（继续按后置条件判定）| path=%s err=%s", path, e)
    if path.exists():
        logger.error("文件仍在磁盘上，删除未生效 | path=%s", path)
        return False
    return True


def safe_doc_id(name: str) -> str:
    """doc_id → 可安全用作文件名的标识（只保留中英文、数字、下划线、点、连字符）。"""
    cleaned = _SAFE_NAME_RE.sub("_", Path(str(name)).name).strip("._")
    return cleaned[:120] or "unnamed"


def is_history(record: dict[str, Any]) -> bool:
    """该备份是否为**历史版本快照**（G3）。G2 及更早的记录没有 role 字段 → 视为当前版本。"""
    return str(record.get("role") or ROLE_CURRENT).lower() == ROLE_HISTORY


def encode_dense(vec: Iterable[float] | None) -> str | None:
    """dense 向量 → base64（float32 小端字节）。空向量返回 None。"""
    if vec is None:
        return None
    arr = array("f", [float(x) for x in vec])
    if not arr:
        return None
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_dense(encoded: str | None, dim: int | None = None) -> list[float] | None:
    """base64 → dense 向量。解码失败返回 None（调用方回落重新 embedding）。

    `dim` 是防御性校验：备份文件被手工改坏时，宁可放弃复用也不要写错维度的向量
    （Milvus 只在插入时报一句难懂的底层错误）。
    """
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
        arr = array("f")
        arr.frombytes(raw)
    except (ValueError, TypeError) as e:  # noqa: PERF203
        logger.warning("[GOV-WARN] 备份向量解码失败 | err=%s", e)
        return None
    if dim is not None and len(arr) != dim:
        logger.warning("[GOV-WARN] 备份向量维度不符 | got=%d want=%d", len(arr), dim)
        return None
    return list(arr)


class KbBackupStore:
    """备份读写（回收站 + 停用区）。无内部状态，线程安全由调用方保证。"""

    def __init__(self, recycle_dir: Path | str = RECYCLE_DIR,
                 disabled_dir: Path | str = DISABLED_DIR) -> None:
        self.recycle_dir = Path(recycle_dir)
        self.disabled_dir = Path(disabled_dir)

    # ------------------------------------------------------------ 路径

    def dir_of(self, state: str) -> Path:
        return self.disabled_dir if state == STATE_DISABLED else self.recycle_dir

    def path_of(self, doc_id: str, state: str, version_no: int | None = None) -> Path:
        """备份文件路径。`version_no=None` → 当前版本（`<doc_id>.json`）；
        给出版本号 → 历史快照（`<doc_id>@v<n>.json`）。"""
        base = safe_doc_id(doc_id)
        name = (f"{base}.json" if version_no is None
                else f"{base}{VERSION_SEP}{int(version_no)}.json")
        return self.dir_of(state) / name

    def path_of_record(self, record: dict[str, Any]) -> Path:
        """按记录自身的 role/state/versionNo 定位它该在的文件（写路径的唯一来源）。"""
        doc_id = str(record.get("docId") or "")
        state = str(record.get("state") or STATE_RECYCLED)
        ver = int(record.get("versionNo") or 1) if is_history(record) else None
        return self.path_of(doc_id, state, ver)

    def _snapshot_paths(self, doc_id: str, state: str) -> list[Path]:
        """某文档在某状态下的**全部**备份文件（当前版本 + 历史快照）。

        历史快照用 `@v` 分隔符定位，与其它 doc_id 不可能冲突，因此**无需读文件内容**校验——
        清理路径（彻底删除）因此不必把整个回收站的 JSON 读一遍。
        """
        base = safe_doc_id(doc_id)
        base_dir = self.dir_of(state)
        out: list[Path] = []
        current = base_dir / f"{base}.json"
        if current.exists():
            out.append(current)
        if base_dir.exists():
            out.extend(sorted(base_dir.glob(f"{base}{VERSION_SEP}*.json")))
        return out

    # ------------------------------------------------------------ 写

    def write(self, record: dict[str, Any]) -> Path:
        """写备份。失败**必须抛错**——不允许在没有备份的情况下删除向量。"""
        path = self.path_of_record(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：进程被杀也不会留下半个 JSON（恢复时读到会直接报废）
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def remove(self, doc_id: str, state: str | None = None) -> list[str]:
        """删除备份文件，返回**确认已不存在**的路径列表。state=None 时两个目录都清。

        包含**该文档的全部历史版本快照**——彻底删除必须连版本链一起清干净，
        否则回收站里会留下一堆"主人已不存在"的快照（既无入口可回滚，又白占磁盘）。

        判定口径是**后置条件**（文件事后是否真的不存在），不是"调用有没有抛异常"。
        钩子既可能抛 OSError，也可能在批量删除配额用尽时抛 SystemExit——两者都
        必须按"没删掉"处理并如实上报，绝不能让异常穿透成 500 空响应
        （详见 `unlink_quietly` 的说明）。
        """
        states = [state] if state else [STATE_RECYCLED, STATE_DISABLED]
        removed: list[str] = []
        stuck: list[str] = []
        for st in states:
            for p in self._snapshot_paths(doc_id, st):
                (removed if unlink_quietly(p) else stuck).append(str(p))
        if stuck:
            logger.error("备份文件未被清理（调用方应据此如实上报）| paths=%s", stuck)
        return removed

    def remaining_paths(self, doc_id: str) -> list[str]:
        """该文档**仍然留在磁盘上**的备份文件（清理后的核验用，正常应为空）。

        单独开这个查询，是为了让"彻底删除"能如实上报"没删干净"：
        如果清理被环境拒绝（磁盘占用 / 删除配额），向量和账本已经没了、
        但备份里还完整躺着全文与向量——**这是最不该静默的情况**
        （用户以为内容已不可恢复地销毁，实际还在磁盘上）。
        """
        out: list[str] = []
        for st in (STATE_RECYCLED, STATE_DISABLED):
            out.extend(str(p) for p in self._snapshot_paths(doc_id, st))
        return out

    def archive_current(self, doc_id: str, state: str = STATE_RECYCLED) -> Path | None:
        """把"当前版本"备份**降级**为历史快照（G3）。

        触发场景：文档被软删（RECYCLED，`<doc_id>.json` 在回收站），此后用户又上传了
        同名新文档——旧版就成了"历史版本"，不能再占着"当前版本"的位子（否则回收站里
        会出现一个"名字与在库文档相同、恢复必然冲突"的僵尸条目）。

        同名版本已有快照时**保留既有的那份**（它内容相同，且可能已被版本链引用），
        只把当前版本文件删掉，保证同一 versionNo 只有一份。
        """
        src = self.path_of(doc_id, state)
        if not src.exists():
            return None
        try:
            rec = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[GOV-WARN] 当前版本备份不可读，跳过归档 | path=%s err=%s", src, e)
            return None
        rec.setdefault("docId", safe_doc_id(doc_id))
        rec.setdefault("state", state)
        rec["role"] = ROLE_HISTORY
        rec["archivedAt"] = _now()
        dst = self.path_of(doc_id, state, int(rec.get("versionNo") or 1))
        if dst.exists():
            logger.info("[GOV] 历史快照已存在，直接清掉当前版本文件 | doc=%s ver=%s",
                        doc_id, rec.get("versionNo"))
            rec = None
        else:
            self.write(rec)
        if not unlink_quietly(src):
            logger.error("归档后当前版本文件仍在磁盘上 | path=%s", src)
            return None
        return dst

    # ------------------------------------------------------------ 读

    def locate(self, doc_id: str) -> tuple[str, dict[str, Any]] | None:
        """按 doc_id 查**当前版本**备份，返回 `(state, record)`。

        只看 current：恢复 / 启用的语义是"把这篇文档放回去"，
        历史的中间版本由 `list_versions` + `rollback` 走另一条路径。
        回收站优先（先删的先进回收站）。
        """
        for state in (STATE_RECYCLED, STATE_DISABLED):
            rec = self.read(doc_id, state)
            if rec is not None:
                return state, rec
        return None

    def read(self, doc_id: str, state: str) -> dict[str, Any] | None:
        """读当前版本备份（历史快照请用 `read_version`）。"""
        return self._read_path(self.path_of(doc_id, state), state)

    def read_version(self, doc_id: str, state: str, version_no: int) -> dict[str, Any] | None:
        """读指定版本的历史快照。"""
        return self._read_path(self.path_of(doc_id, state, version_no), state)

    def _read_path(self, path: Path, state: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("备份记录损坏 | path=%s err=%s", path, e)
            raise ValueError(f"备份记录已损坏，无法处理：{path.name}") from e
        record.setdefault("state", state)
        return record

    def list_records(self, state: str, *, include_history: bool = False
                     ) -> list[dict[str, Any]]:
        """列出某个状态的备份（跳过损坏文件，单条坏不应让列表打不开）。

        `include_history=False`（默认）只回**当前版本**：回收站与停用区列表是给用户
        恢复用的，历史快照混进去会出现同名多条，且用户点"恢复"必然撞上"已存在同名文档"。
        """
        base = self.dir_of(state)
        out: list[dict[str, Any]] = []
        if not base.exists():
            return out
        for path in sorted(base.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("备份记录损坏，已跳过 | file=%s err=%s", path.name, e)
                continue
            rec.setdefault("state", state)
            if not include_history and is_history(rec):
                continue
            out.append(rec)
        return out

    def list_versions(self, doc_id: str) -> list[dict[str, Any]]:
        """某文档的**全部**备份（当前版本 + 历史快照），按 versionNo 降序。

        版本链视图的数据来源。回收站与停用区都扫——同一文档在不同时刻可能
        分别被软删和停用过，两份快照都是它的历史。
        """
        out: list[dict[str, Any]] = []
        base = safe_doc_id(doc_id)
        for state in (STATE_RECYCLED, STATE_DISABLED):
            for path in self._snapshot_paths(doc_id, state):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("版本快照损坏，已跳过 | file=%s err=%s", path.name, e)
                    continue
                rec.setdefault("state", state)
                if safe_doc_id(str(rec.get("docId") or "")) != base:
                    continue        # 兜底：文件名同前缀但不是同一文档
                rec.setdefault("role", ROLE_CURRENT)
                out.append(rec)
        out.sort(key=lambda r: int(r.get("versionNo") or 1), reverse=True)
        return out

    def prune_versions(self, doc_id: str, keep: int) -> list[str]:
        """只保留版本号最大的 `keep` 份**历史快照**，返回被清理的路径列表。

        为什么必须有上限：版本快照是"每次同名覆盖都存一份"，一篇每天更新的文档
        一年就是 365 份 × 每份几百 KB。没有上限的版本链会把回收站目录撑爆，
        还把 G4 的 TTL 扫描拖慢。清理只动 history，**绝不碰 current**——
        当前版本的备份是恢复/启用的唯一数据来源。
        """
        if keep is None or int(keep) <= 0:
            return []
        snaps = [r for r in self.list_versions(doc_id) if is_history(r)]
        if len(snaps) <= int(keep):
            return []
        removed: list[str] = []
        for rec in snaps[int(keep):]:
            state = str(rec.get("state") or STATE_RECYCLED)
            path = self.path_of(doc_id, state, int(rec.get("versionNo") or 1))
            if unlink_quietly(path):
                removed.append(str(path))
        if removed:
            logger.info("[GOV] 已修剪超限历史快照 | doc=%s keep=%s removed=%d",
                        doc_id, keep, len(removed))
        return removed

    # ------------------------------------------------------------ 视图

    @staticmethod
    def strip_heavy(record: dict[str, Any]) -> dict[str, Any]:
        """去掉重字段（全文 + 向量），供列表接口使用。"""
        return {k: v for k, v in record.items() if k not in HEAVY_FIELDS}

    @staticmethod
    def chunks_of(record: dict[str, Any]) -> list[dict[str, Any]]:
        """统一切片视图（v2 的 chunks[]；v1 的 chunkTexts[] 转成只有 text 的形状）。"""
        chunks = record.get("chunks")
        if isinstance(chunks, list) and chunks:
            return chunks
        legacy = record.get("chunkTexts")
        if isinstance(legacy, list):
            return [{"chunkIndex": i, "text": str(t)} for i, t in enumerate(legacy)]
        return []

    @classmethod
    def has_vectors(cls, record: dict[str, Any]) -> bool:
        """备份里是否带可用向量（决定恢复走"回灌"还是"回落重新切片"）。"""
        return any(c.get("dense") for c in cls.chunks_of(record))

    @classmethod
    def plain_text(cls, record: dict[str, Any]) -> str:
        """备份里的全文（最粗的兜底恢复源：切片文本按序拼接）。"""
        texts = [str(c.get("text") or "") for c in cls.chunks_of(record)]
        return "\n\n".join(t for t in texts if t).strip()


# 模块级单例：路径固定，无状态
kb_backups = KbBackupStore()

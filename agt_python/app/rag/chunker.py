"""文档切片器：把知识库语料切成带完整元数据的 chunk。

设计决策（均有实测依据，详见 docs/切片方案与元数据设计.md）
----------------------------------------------------------
1. **结构优先**：先按 markdown 标题分段，贴合文档语义结构
2. **无标题降级**：整篇没有 `#` 标题时按空行切段，
   否则整篇会被当成一个 giant section 被硬切成定长片
3. **段落聚合 + 双向合并**：短段优先向前合并；前一片已无容量时改为向后合并。
   （对比 AgentCraft 的 `_merge_small_chunks`：它只做向后吸收，且**无上界检查**，
   连续短段会被累加成一个远超 chunk_size 的巨块）
4. **中文递归下探**：段落 → 行 → 句 → 逗号 → 空格，逐级降低切分粒度
5. ⭐ **预算制**：`max_chars` 是**最终片长的硬上限**。
   切分预算 = max_chars − 标题前缀 − overlap − 换行符。
   早期实现只约束正文，前缀与 overlap 额外叠加，实测配置 600 却产出 835 字的片
   （30.9% 超限），"chunk 大小"这个参数名不副实。
6. **overlap 拼接**：把前一片尾部拼到当前片开头，避免答案被切在边界。
   （对比 AgentCraft：`chunk_overlap` 是**死配置**，`split_text()` 里根本没实现，
   `fallback_splitter` 定义了却从未被调用）
7. **元数据三件套**：向量 + 原文 + 完整元数据，元数据分三类用途——
   鉴权过滤（owner/visibility）、引用追踪（doc_id/heading/chunk_index/char_offset）、
   时效与合规（version/updated/source/source_type）
8. **短段并入前片而非丢弃**：低于 `min_section` 的段落（超长段收尾、表格脚注等）
   并入前一片（允许超预算约 min_section 字符 ≈ 10%），无前片可并时才丢弃——
   尾部几十字也是语义，且引用区间 char_end 顺延不缺尾（2026-09-20 改，原为直接丢弃）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 常量

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 中文优先的分隔符优先级
SEPARATORS: list[str] = ["\n\n", "\n", "。", "！", "？", "；", "，", " "]

# 第三方语料默认可见性
DEFAULT_OWNER = "default"
DEFAULT_VISIBILITY = "public"


@dataclass(frozen=True)
class ChunkConfig:
    """切片参数。默认值来自实测标定（scripts/compare_chunking.py）。"""

    max_chars: int = 400          # 最终片长硬上限
    overlap: int = 80             # 相邻片重叠字符数
    min_chunk: int = 100          # 低于此长度的段落尝试合并
    min_section: int = 40         # 低于此长度的段落并入前一片（无前片可并时才丢弃）
    prefix_ratio_cap: float = 0.3  # 标题前缀最长占 max_chars 的比例


@dataclass
class Chunk:
    """一个切片，携带向量化所需的原文与全部元数据。"""

    text: str

    # 切片正文（**不含**标题前缀与 overlap）——语料指纹（L2 去重）的比对对象。
    # 为什么不用 `text`：text 里带了 `【文档标题】「小节名」` 前缀与上一片的尾部 overlap，
    # 换文件名/换标题就会变，导致"内容一样但指纹不同"，去重门形同虚设。
    body: str = ""

    # ---- 引用追踪 ----
    chunk_id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    heading: str = ""
    chunk_index: int = 0
    total_chunks: int = 0
    char_start: int = 0
    char_end: int = 0

    # ---- 鉴权过滤 ----
    owner: str = DEFAULT_OWNER
    visibility: str = DEFAULT_VISIBILITY

    # ---- 分类与时效 ----
    category: str = ""
    source: str = ""
    source_type: str = ""
    doc_version: str = ""
    doc_updated: str = ""
    lang: str = "zh"

    # ---- 派生 ----
    @property
    def char_len(self) -> int:
        return len(self.text)

    def to_milvus_row(self, vector: list[float], truncate) -> dict:
        """
        转成 Milvus 插入行。

        `truncate(s, max_bytes)` 由调用方注入，用于按 UTF-8 字节截断
        （Milvus VARCHAR max_length 单位是字节，中文占 3 字节）。
        """
        return {
            "chunk_id": truncate(self.chunk_id, 400),
            "doc_id": truncate(self.doc_id, 400),
            "doc_title": truncate(self.doc_title, 1900),
            "heading": truncate(self.heading, 900),
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "owner": truncate(self.owner, 200),
            "visibility": truncate(self.visibility, 20),
            "category": truncate(self.category, 200),
            "source": truncate(self.source, 400),
            "source_type": truncate(self.source_type, 60),
            "doc_version": truncate(self.doc_version, 30),
            "doc_updated": truncate(self.doc_updated, 30),
            "lang": truncate(self.lang, 10),
            "char_len": self.char_len,
            "text": truncate(self.text, 16000),
            "dense": vector,
        }


# ---------------------------------------------------------------- 工具函数

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆分 YAML frontmatter（简化解析，只支持 `key: value` 单行）。"""
    if not text.lstrip().startswith("---"):
        return {}, text
    parts = text.lstrip().split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, parts[2].lstrip("\n")


def split_sections(body: str) -> list[tuple[str, str, int]]:
    """
    按 markdown 标题切成 (标题, 内容, 在 body 中的起始偏移)。

    偏移用于引用追踪：前端可据此定位到原文位置。
    """
    out: list[tuple[str, str, int]] = []
    heading = ""
    buf: list[str] = []
    buf_start = 0
    pos = 0

    for line in body.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        m = HEADING_RE.match(raw)
        if m:
            if buf:
                out.append((heading, "".join(buf), buf_start))
                buf = []
            heading = m.group(2).strip()
            buf_start = pos + len(line)
        else:
            if not buf:
                buf_start = pos
            buf.append(line)
        pos += len(line)

    if buf:
        out.append((heading, "".join(buf), buf_start))
    return out


def recursive_split(text: str, max_chars: int, seps: list[str]) -> list[str]:
    """递归字符切分：按分隔符优先级逐级下探，保证每片 <= max_chars。"""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    if not seps:
        # 分隔符用尽仍超长（如无标点的长串），退化为硬切
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]

    sep, rest = seps[0], seps[1:]
    pieces = text.split(sep)
    out: list[str] = []
    buf = ""
    for p in pieces:
        candidate = f"{buf}{sep}{p}" if buf else p
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            if len(p) > max_chars:
                out.extend(recursive_split(p, max_chars, rest))
                buf = ""
            else:
                buf = p
    if buf:
        out.append(buf)
    return [c for c in out if c.strip()]


def _merge(prev_head: str, prev_text: str, head: str, text: str) -> tuple[str, str]:
    """合并两个段落，标题拼接（相同则去重）。"""
    if prev_head == head:
        merged = prev_head
    elif not prev_head:
        merged = head
    elif not head:
        merged = prev_head
    else:
        merged = f"{prev_head} · {head}"
    return merged, f"{prev_text}\n\n{text}"


def merge_short_sections(
    sections: list[tuple[str, str, int]], min_chunk: int, budget: int
) -> list[tuple[str, str, int]]:
    """
    合并过短段落，抑制碎片化（双向 + 上界检查）。

    为什么需要：技术文档标题极密，`## 一、概述` 下面常常只有一两句话。
    若每段独立成片，会产生大量 50~150 字的碎片——信息量不足、向量语义模糊、
    无谓放大 chunk 总数。

    两轮处理：
      1. 向前合并（容量允许时）
      2. 仍偏短的，与后一段合并（前片已满时的补救路径）
    """
    # 第一轮：向前合并
    tmp: list[tuple[str, str, int]] = []
    for heading, content, offset in sections:
        if tmp and len(content) < min_chunk:
            ph, pc, po = tmp[-1]
            if len(pc) + len(content) + 2 <= budget:
                mh, mc = _merge(ph, pc, heading, content)
                tmp[-1] = (mh, mc, po)
                continue
        tmp.append((heading, content, offset))

    # 第二轮：向后合并
    out: list[tuple[str, str, int]] = []
    i = 0
    while i < len(tmp):
        heading, content, offset = tmp[i]
        if len(content) < min_chunk and i + 1 < len(tmp):
            nh, nc, no = tmp[i + 1]
            if len(content) + len(nc) + 2 <= budget:
                mh, mc = _merge(heading, content, nh, nc)
                out.append((mh, mc, offset))
                i += 2
                continue
        out.append((heading, content, offset))
        i += 1
    return out


# ---------------------------------------------------------------- 主入口

def chunk_document(
    raw: str,
    *,
    stem: str,
    default_category: str = "",
    defaults: dict | None = None,
    cfg: ChunkConfig | None = None,
) -> list[Chunk]:
    """
    把一篇文档切成 chunks。

    参数
    ----
    raw               : 文档全文（含 frontmatter）
    stem              : 文件名主干，作为 doc_id / title 的兜底
    default_category  : frontmatter 无 category 时的兜底（通常是目录名）
    defaults          : 兜底元数据（owner / source_type / visibility 等）
    cfg               : 切片参数
    """
    cfg = cfg or ChunkConfig()
    defaults = defaults or {}

    meta, body = parse_frontmatter(raw)
    title = meta.get("title") or stem
    doc_id = meta.get("doc_id") or stem
    category = meta.get("category") or default_category

    sections = split_sections(body)

    # 完全没有 markdown 标题的文档：退化为按空行切段。
    # 否则整篇会被当成单个 giant section 直接硬切——这就是"固定大小切"的成因。
    if len(sections) == 1 and not sections[0][0]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", sections[0][1]) if p.strip()]
        sections = [("", p, sections[0][2]) for p in paras]

    # 合并上界预留 overlap 与前缀空间
    merge_budget = max(cfg.max_chars - cfg.overlap - 2, 100)
    sections = merge_short_sections(sections, cfg.min_chunk, merge_budget)

    chunks: list[Chunk] = []
    for heading, content, offset in sections:
        if len(content) < cfg.min_section:
            # ⭐ 尾片并入前片：以前"直接丢弃"，会白丢超长段/标题段的收尾几十字
            #（典型如表格脚注、"详见附件"等收束句）。并入前片最多超预算 min_section
            # 字符（默认 40，约 10%），对 embedding 长度无实质影响；引用区间
            # char_end 顺延到并入段的末尾，保证前端定位不缺尾。
            # 首片前的孤儿短段（无前片可并）仍丢弃——它们没有承载语义的宿主。
            tiny = content.strip()
            if chunks and tiny:
                last = chunks[-1]
                last.text = f"{last.text}\n{tiny}"
                last.body = f"{last.body}\n{tiny}"
                last.char_end = offset + len(tiny)
            continue

        prefix = f"【{title}】" + (f"「{heading}」" if heading else "")
        cap = int(cfg.max_chars * cfg.prefix_ratio_cap)
        if len(prefix) > cap:
            prefix = prefix[:cap]

        # 预算制：最终片长 = 前缀 + 正文 + overlap 段 + 换行 <= max_chars
        budget = max(cfg.max_chars - len(prefix) - cfg.overlap - 2, 80)

        cursor = offset
        for piece in recursive_split(content, budget, SEPARATORS):
            text = f"{prefix}\n{piece}".strip()
            chunks.append(
                Chunk(
                    text=text,
                    body=piece,
                    doc_id=doc_id,
                    doc_title=title,
                    heading=heading,
                    char_start=cursor,
                    char_end=cursor + len(piece),
                    owner=meta.get("owner") or defaults.get("owner", DEFAULT_OWNER),
                    visibility=meta.get("visibility")
                    or defaults.get("visibility", DEFAULT_VISIBILITY),
                    category=category,
                    source=meta.get("source") or defaults.get("source", ""),
                    source_type=meta.get("source_type")
                    or defaults.get("source_type", ""),
                    doc_version=meta.get("version") or defaults.get("version", ""),
                    doc_updated=meta.get("updated") or defaults.get("updated", ""),
                    lang=meta.get("lang") or defaults.get("lang", "zh"),
                )
            )
            cursor += len(piece)

    # 补 chunk_id / total_chunks，并加 overlap
    total = len(chunks)
    for i, c in enumerate(chunks):
        c.chunk_index = i
        c.total_chunks = total
        c.chunk_id = f"{c.doc_id}#{i}"
        if i > 0:
            prev = chunks[i - 1].text
            if len(prev) > cfg.overlap:
                c.text = prev[-cfg.overlap :] + "\n" + c.text

    return chunks


def iter_corpus(
    dirs: list[tuple[Path, dict]],
    cfg: ChunkConfig | None = None,
) -> tuple[list[Chunk], list[dict]]:
    """
    遍历语料目录并切片。

    参数
    ----
    dirs : [(目录, 默认元数据)]，默认元数据如
           {"owner": "public", "source_type": "third_party_github"}
    返回 : (chunks, 文档统计)
    """
    cfg = cfg or ChunkConfig()
    all_chunks: list[Chunk] = []
    stats: list[dict] = []

    for base, defaults in dirs:
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            raw = md.read_text(encoding="utf-8", errors="ignore")
            cs = chunk_document(
                raw,
                stem=md.stem,
                default_category=md.parent.name,
                defaults=defaults,
                cfg=cfg,
            )
            all_chunks.extend(cs)
            stats.append(
                {
                    "path": str(md),
                    "doc_id": cs[0].doc_id if cs else md.stem,
                    "category": cs[0].category if cs else md.parent.name,
                    "chunks": len(cs),
                    "chars": len(raw),
                    "origin": defaults.get("_origin", ""),
                }
            )
    return all_chunks, stats

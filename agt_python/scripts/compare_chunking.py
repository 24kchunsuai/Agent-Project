# -*- coding: utf-8 -*-
"""
切片方案对比：验证 chunk 大小对「检索质量」与「上下文预算」的影响。

背景
----
上一轮 verify_scale.py 得出"切片不宜缩短（截断到 300/120 字后期望文档
排名 #1 → #3）"的结论，但那个实验方法有缺陷：

    它用的是 **truncate 截断** —— 把 600 字的完整语义块砍掉一半，
    信息不完整，rerank 自然打低分。

而真实要做的是 **语义完整的小切片**（完整段落/完整句子聚合）。
两者不可比。本脚本用「语义完整的切片」重做对比。

对比方案
--------
  A  600/80/200  标题感知 + 中文递归 + 向前合并（当前实现，基准）
  B  500/50/100  AgentCraft 参数（CHUNK_SIZE/OVERLAP/MIN_CHUNK_SIZE）
  C  400/80/100  用户建议：语义段落 + 400 上限 + 双向上界合并
  D  300/60/100  激进短切片，探下界
  E  400/80/100  同 C 参数但保持"向前合并"，用于隔离合并策略的影响

统计维度
--------
  离线（零成本）：
    - chunk 总数、长度分布（min/中位/均值/p90/max）
    - 碎片率（<100 字 / <150 字 片数占比）
    - 上下文预算：取 top5 时正文总字数 → 估算 token 区间
  在线（需 API，--online）：
    - top1 / top5 / top10 命中率（expected_doc_ids 中的文档是否出现在前 N）
    - top1 rerank 分数、top5 最低分
    - 检索耗时

运行
----
  # 仅离线形态对比（秒级，零成本）
  D:/install/python/anaconda/envs/python311/python.exe scripts/compare_chunking.py

  # 含在线检索对比（建库 + 跑评估集，数分钟、消耗 embedding 额度）
  D:/install/python/anaconda/envs/python311/python.exe scripts/compare_chunking.py --online
  D:/install/python/anaconda/envs/python311/python.exe scripts/compare_chunking.py --online --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

try:
    from pymilvus import (
        AnnSearchRequest,
        DataType,
        Function,
        FunctionType,
        MilvusClient,
        RRFRanker,
    )
except ImportError:
    print("[FATAL] 未安装 pymilvus，请执行：pip install 'pymilvus>=2.6.0'")
    sys.exit(1)

# ---------------------------------------------------------------- 路径与配置

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "data/kb"
MILVUS_URI = "http://localhost:19530"
COLLECTION_PREFIX = "kb_cmp"

CORPUS_DIRS = [
    (KB / "raw", "own"),
    (KB / "external/uav-stack", "ext"),
]

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 中文优先的分隔符优先级：段 → 行 → 句 → 逗号 → 空格
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " "]

# 中文 token 估算：DeepSeek/GLM 官方口径约 1 token ≈ 1.5~1.8 汉字。
# 为保守起见给区间：乐观 1.5 字/token，保守 1.0 字/token。
CHARS_PER_TOKEN_OPT = 1.5
CHARS_PER_TOKEN_PES = 1.0


@dataclass
class Scheme:
    """一个切片方案。"""

    key: str
    label: str
    max_chars: int
    overlap: int
    min_chunk: int
    merge: str = "backward"   # backward=向前合并(现状) / both=双向且带上界


SCHEMES: list[Scheme] = [
    Scheme("A", "600/80/200 向前合并（当前基准）", 600, 80, 200, "backward"),
    Scheme("B", "500/50/100 双向合并（AgentCraft 参数）", 500, 50, 100, "both"),
    Scheme("C", "400/80/100 双向合并（用户建议）", 400, 80, 100, "both"),
    Scheme("D", "300/60/100 双向合并（激进短片）", 300, 60, 100, "both"),
    Scheme("E", "400/80/100 向前合并（隔离合并策略）", 400, 80, 100, "backward"),
]


def load_env() -> dict:
    cfg: dict = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


ENV = load_env()
GLM_BASE = ENV.get("EMBEDDING_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
GLM_KEY = ENV.get("EMBEDDING_API_KEY", "")
EMBED_MODEL = ENV.get("EMBEDDING_MODEL", "embedding-3")
RERANK_MODEL = ENV.get("RERANK_MODEL", "rerank-pro")
EMBED_BATCH = 32


# ---------------------------------------------------------------- 切片器（参数化）

def truncate_utf8(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断（Milvus VARCHAR max_length 单位是字节）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.lstrip().startswith("---"):
        return {}, text
    parts = text.lstrip().split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, parts[2].lstrip("\n")


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 markdown 标题切成 (标题, 内容) 段落。"""
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if buf:
                content = "\n".join(buf).strip()
                if content:
                    sections.append((heading, content))
                buf = []
            heading = m.group(2).strip()
        else:
            buf.append(line)
    if buf:
        content = "\n".join(buf).strip()
        if content:
            sections.append((heading, content))
    return sections


def recursive_split(text: str, max_chars: int, seps: list[str]) -> list[str]:
    """递归字符切分：按分隔符优先级逐级下探，保证每片不超过 max_chars。"""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    if not seps:
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


def merge_sections_backward(
    sections: list[tuple[str, str]], min_chunk: int, max_chars: int
) -> list[tuple[str, str]]:
    """
    现状策略：短段并入**前**一段（仅当合并后不超上限）。

    缺陷：若前一段已接近上限，短段无处可去，只能保留为碎片。
    """
    out: list[tuple[str, str]] = []
    for heading, content in sections:
        if out and len(content) < min_chunk:
            phead, pcontent = out[-1]
            if len(pcontent) + len(content) + 2 <= max_chars:
                merged_head = phead if phead == heading else (
                    f"{phead} · {heading}" if heading and phead else (phead or heading)
                )
                out[-1] = (merged_head, f"{pcontent}\n\n{content}")
                continue
        out.append((heading, content))
    return out


def merge_sections_both(
    sections: list[tuple[str, str]], min_chunk: int, max_chars: int
) -> list[tuple[str, str]]:
    """
    改进策略：**双向合并 + 上界检查**。

    1. 先向前合并（与前一片同标题优先）
    2. 前一片无容量时，改为把短段**暂存**，与后一片合并

    相比 AgentCraft 的 `_merge_small_chunks`：它只做向后吸收且**无上界检查**，
    连续短段会被累加成一个远超 chunk_size 的巨块。这里加了容量上限。
    """
    # 第一轮：向前合并
    tmp: list[tuple[str, str]] = []
    for heading, content in sections:
        if tmp and len(content) < min_chunk:
            phead, pcontent = tmp[-1]
            if len(pcontent) + len(content) + 2 <= max_chars:
                merged_head = phead if phead == heading else (
                    f"{phead} · {heading}" if heading and phead else (phead or heading)
                )
                tmp[-1] = (merged_head, f"{pcontent}\n\n{content}")
                continue
        tmp.append((heading, content))

    # 第二轮：仍偏短的，尝试并入后一片（合并后不超上限）
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tmp):
        heading, content = tmp[i]
        if len(content) < min_chunk and i + 1 < len(tmp):
            nhead, ncontent = tmp[i + 1]
            if len(content) + len(ncontent) + 2 <= max_chars:
                merged_head = heading if heading == nhead else (
                    f"{heading} · {nhead}" if heading and nhead else (heading or nhead)
                )
                out.append((merged_head, f"{content}\n\n{ncontent}"))
                i += 2
                continue
        out.append((heading, content))
        i += 1
    return out


def chunk_document(doc: dict, sc: Scheme) -> list[dict]:
    """
    把一篇文档按方案 sc 切成 chunks。

    ⚠️ 关键修正：**预算制切分**
    之前的实现里 `max_chars` 只约束"正文"，标题前缀（`【文档标题】「章节标题」`）
    与 overlap 是**额外叠加**的，导致实际片长最多超出 39%（配置 600 → 实测 835，
    30.9% 的切片超限）。这会让"chunk 大小"这个参数名不副实。

    现在改为：正文预算 = max_chars − 前缀 − overlap − 换行符，
    保证最终片长（前缀 + 正文 + overlap）**不超过 max_chars**。
    """
    meta, body = parse_frontmatter(doc["raw"])
    title = meta.get("title") or doc["stem"]
    doc_id = meta.get("doc_id") or doc["stem"]
    category = meta.get("category") or doc["category"]

    sections = split_sections(body)

    # 完全没有 markdown 标题的文档：退化为按空行切段，
    # 否则整篇会当成一个 giant section 被硬切（这就是"固定大小切"的成因）
    if len(sections) == 1 and not sections[0][0]:
        paras = [p.strip() for p in re.split(r"\n\s*\n", sections[0][1]) if p.strip()]
        sections = [("", p) for p in paras]

    merger = merge_sections_both if sc.merge == "both" else merge_sections_backward
    # 合并上界预留 overlap 与前缀空间
    merge_budget = max(sc.max_chars - sc.overlap - 2, 100)
    sections = merger(sections, sc.min_chunk, merge_budget)

    chunks: list[dict] = []
    for heading, content in sections:
        if len(content) < 40:            # 信息量过低，丢弃
            continue

        prefix = f"【{title}】" + (f"「{heading}」" if heading else "")
        # 前缀过长时截断，保证正文还有足够预算
        cap = int(sc.max_chars * 0.3)
        if len(prefix) > cap:
            prefix = prefix[:cap]

        # 预算：最终片长 = 前缀 + 正文 + overlap段 + 换行 <= max_chars
        budget = max(sc.max_chars - len(prefix) - sc.overlap - 2, 80)

        for piece in recursive_split(content, budget, SEPARATORS):
            chunks.append(
                {
                    "doc_id": doc_id,
                    "title": title,
                    "category": category,
                    "heading": heading,
                    "text": f"{prefix}\n{piece}".strip(),
                }
            )

    # 重叠：把前一片的尾部拼到当前片开头
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]["text"]
        if len(prev) > sc.overlap:
            chunks[i]["text"] = prev[-sc.overlap :] + "\n" + chunks[i]["text"]

    return chunks


def load_docs() -> list[dict]:
    docs: list[dict] = []
    for base, origin in CORPUS_DIRS:
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            docs.append(
                {
                    "stem": md.stem,
                    "raw": md.read_text(encoding="utf-8", errors="ignore"),
                    "category": md.parent.name,
                    "origin": origin,
                }
            )
    return docs


def build_chunks(docs: list[dict], sc: Scheme) -> list[dict]:
    chunks: list[dict] = []
    for doc in docs:
        cs = chunk_document(doc, sc)
        for j, c in enumerate(cs):
            c["chunk_id"] = f"{c['doc_id']}#{j}"
            c["origin"] = doc["origin"]
        chunks.extend(cs)
    return chunks


# ---------------------------------------------------------------- 离线统计

def offline_stats(sc: Scheme, chunks: list[dict]) -> dict:
    lens = [len(c["text"]) for c in chunks]
    if not lens:
        return {}
    top5_chars = sum(sorted(lens, reverse=True)[:5])   # 上界参考
    avg5_chars = 5 * statistics.mean(lens)             # 期望参考
    return {
        "key": sc.key,
        "label": sc.label,
        "n": len(chunks),
        "min": min(lens),
        "p50": statistics.median(lens),
        "mean": statistics.mean(lens),
        "p90": sorted(lens)[int(len(lens) * 0.9)],
        "max": max(lens),
        "frac_lt100": sum(1 for x in lens if x < 100) / len(lens),
        "frac_lt150": sum(1 for x in lens if x < 150) / len(lens),
        "frac_gt600": sum(1 for x in lens if x > 600) / len(lens),
        "exp_top5_tok": avg5_chars / CHARS_PER_TOKEN_OPT,
        "pes_top5_tok": avg5_chars / CHARS_PER_TOKEN_PES,
    }


def print_offline(rows: list[dict]) -> None:
    print("\n" + "=" * 108)
    print("  离线形态对比")
    print("=" * 108)
    hdr = (
        f"{'方案':<40}{'片数':>6}{'最小':>6}{'中位':>6}{'均值':>6}"
        f"{'p90':>6}{'最大':>6}{'<100':>8}{'<150':>8}{'>600':>8}"
    )
    print(hdr)
    print("-" * 108)
    for r in rows:
        print(
            f"{r['label']:<40}{r['n']:>6}{r['min']:>6}{r['p50']:>6.0f}{r['mean']:>6.0f}"
            f"{r['p90']:>6}{r['max']:>6}{r['frac_lt100']:>7.1%}{r['frac_lt150']:>8.1%}"
            f"{r['frac_gt600']:>7.1%}"
        )
    print("-" * 108)
    print("\n  上下文预算（取 top5 切片时的正文 token 估算，含标题前缀与 overlap）：")
    print(f"  {'方案':<40}{'期望总字数':>12}{'乐观token':>12}{'保守token':>12}")
    print("-" * 108)
    for r in rows:
        print(
            f"  {r['label']:<40}{r['exp_top5_tok'] * CHARS_PER_TOKEN_OPT:>12.0f}"
            f"{r['exp_top5_tok']:>12.0f}{r['pes_top5_tok']:>12.0f}"
        )


# ---------------------------------------------------------------- 在线检索

def embed_batch(texts: list[str], *, sleep_s: float = 0.6, max_retry: int = 6) -> list[list[float]]:
    """
    批量调用 GLM embedding-3。

    限流保护（实测踩坑）：
      智谱对 embedding 有速率限制，连续密集请求会返回 **429 Too Many Requests**。
      本函数做了三层防护：
        1. 每批之间固定 sleep（默认 0.6s）
        2. 命中 429 时**指数退避**（2/4/8/16/32/64s），而非立即重试
        3. 其他错误退化为逐条重试
    """
    out: list[list[float]] = []
    with httpx.Client(timeout=120) as c:
        for bi in range(0, len(texts), EMBED_BATCH):
            batch = texts[bi : bi + EMBED_BATCH]
            for attempt in range(max_retry):
                try:
                    r = c.post(
                        f"{GLM_BASE}/embeddings",
                        headers={"Authorization": f"Bearer {GLM_KEY}"},
                        json={"model": EMBED_MODEL, "input": batch},
                    )
                    if r.status_code == 429:
                        wait = 2 ** (attempt + 1)
                        print(f"      [429] 限流，退避 {wait}s 后重试 ...", flush=True)
                        time.sleep(wait)
                        continue
                    r.raise_for_status()
                    data = sorted(r.json()["data"], key=lambda d: d["index"])
                    out.extend(d["embedding"] for d in data)
                    break
                except httpx.HTTPStatusError as e:
                    if attempt == max_retry - 1:
                        raise RuntimeError(f"embedding 失败: {e}") from e
                    time.sleep(2 ** attempt)
                except Exception as e:  # noqa: BLE001
                    if attempt == max_retry - 1:
                        raise RuntimeError(f"embedding 失败: {e}") from e
                    time.sleep(2)
            time.sleep(sleep_s)
    return out


def glm_rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": documents,
        "top_n": top_n,
        "return_documents": False,
    }
    body = None
    with httpx.Client(timeout=90) as c:
        for attempt in range(5):
            r = c.post(
                f"{GLM_BASE}/rerank",
                headers={"Authorization": f"Bearer {GLM_KEY}"},
                json=payload,
            )
            if r.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"      [429] rerank 限流，退避 {wait}s ...", flush=True)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            break
        else:
            raise RuntimeError("rerank 重试耗尽")
    time.sleep(0.3)
    assert body is not None

    # rerank     → {"index", "relevance_score"}
    # rerank-pro → {"document", "relevance_score"}  （不回 index）
    out: list[tuple[int, float]] = []
    for it in body["results"]:
        if "index" in it:
            idx = int(it["index"])
        else:
            try:
                idx = documents.index(it["document"])
            except ValueError:
                continue
        out.append((idx, float(it["relevance_score"])))
    return out


def build_collection(client: MilvusClient, coll: str, chunks: list[dict]) -> None:
    if client.has_collection(coll):
        client.drop_collection(coll)

    probe = embed_batch(["维度探测"])
    dim = len(probe[0])

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=512)
    schema.add_field("title", DataType.VARCHAR, max_length=2048)
    schema.add_field("category", DataType.VARCHAR, max_length=256)
    schema.add_field("origin", DataType.VARCHAR, max_length=16)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=16384,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},
    )
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(
        Function(
            name="text_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse"],
        )
    )

    idx = client.prepare_index_params()
    idx.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    idx.add_index("dense", index_type="AUTOINDEX", metric_type="COSINE")
    client.create_collection(coll, schema=schema, index_params=idx)

    vectors = embed_batch([c["text"] for c in chunks])
    rows = [
        {
            "chunk_id": truncate_utf8(c["chunk_id"], 400),
            "doc_id": truncate_utf8(c["doc_id"], 400),
            "title": truncate_utf8(c["title"], 1900),
            "category": truncate_utf8(c["category"], 200),
            "origin": c["origin"],
            "text": truncate_utf8(c["text"], 16000),
            "dense": v,
        }
        for c, v in zip(chunks, vectors)
    ]
    for i in range(0, len(rows), 50):
        client.insert(coll, rows[i : i + 50])
    client.flush(coll)


def online_eval(
    client: MilvusClient,
    coll: str,
    rag_qs: list[dict],
    topk: int,
    qvecs: dict[str, list[float]] | None = None,
) -> dict:
    hit1 = hit5 = hit10 = 0
    n_eval = 0
    t1_scores: list[float] = []
    elapsed: list[float] = []
    dup_top5: list[int] = []       # top5 中来自同一 doc 的最大片数（候选堆积度）
    ctx_chars: list[int] = []      # top5 正文总字数（实测上下文开销）

    for q in rag_qs:
        expect = set(q.get("expected_doc_ids") or [])
        if not expect:
            continue                      # 无标注答案的（超纲题）不计命中率
        n_eval += 1

        t0 = time.time()
        # 查询向量优先复用预计算结果（40 条查询 × 5 方案 = 200 次请求，
        # 复用后可降到 2 次批量请求，既省额度也避免触发 429 限流）
        qv = qvecs[q["question"]] if qvecs else embed_batch([q["question"]])
        reqs = [
            AnnSearchRequest(
                data=[q["question"]], anns_field="sparse",
                param={"metric_type": "BM25"}, limit=topk,
            ),
            AnnSearchRequest(
                data=qv, anns_field="dense",
                param={"metric_type": "COSINE"}, limit=topk,
            ),
        ]
        hits = client.hybrid_search(
            collection_name=coll,
            reqs=reqs,
            ranker=RRFRanker(),
            limit=topk,
            output_fields=["chunk_id", "doc_id", "text"],
        )
        cand = [h["entity"] for h in (hits[0] if hits else [])]
        if not cand:
            elapsed.append(time.time() - t0)
            continue

        ranked = glm_rerank(q["question"], [c["text"] for c in cand], top_n=len(cand))
        scored = [
            (cand[i]["doc_id"], cand[i]["text"], s)
            for i, s in ranked
            if i < len(cand)
        ]
        elapsed.append(time.time() - t0)

        if scored:
            t1_scores.append(scored[0][2])
            top5 = scored[:5]
            ctx_chars.append(sum(len(t) for _, t, _ in top5))
            from collections import Counter as _C

            dup_top5.append(max(_C(d for d, _, _ in top5).values()))

        for rank, (d, _, _) in enumerate(scored, 1):
            if d in expect:
                if rank <= 1:
                    hit1 += 1
                if rank <= 5:
                    hit5 += 1
                if rank <= 10:
                    hit10 += 1
                break

    return {
        "n_eval": n_eval,
        "top1": hit1 / n_eval if n_eval else 0,
        "top5": hit5 / n_eval if n_eval else 0,
        "top10": hit10 / n_eval if n_eval else 0,
        "mean_top1_score": statistics.mean(t1_scores) if t1_scores else 0,
        "mean_latency": statistics.mean(elapsed) if elapsed else 0,
        "dup_top5": statistics.mean(dup_top5) if dup_top5 else 0,
        "ctx_top5_chars": statistics.mean(ctx_chars) if ctx_chars else 0,
    }


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="跑在线检索对比（建库+评估）")
    ap.add_argument("--limit", type=int, default=0, help="评估集前 N 条")
    ap.add_argument("--topk", type=int, default=20, help="召回候选数")
    ap.add_argument("--schemes", default="", help="只跑指定方案，如 A,C")
    ap.add_argument("--keep", action="store_true", help="评估后保留集合（默认删除以省内存）")
    args = ap.parse_args()

    schemes = SCHEMES
    if args.schemes:
        want = {s.strip().upper() for s in args.schemes.split(",")}
        schemes = [s for s in SCHEMES if s.key in want]

    print(f"\n加载语料 ...")
    t0 = time.time()
    docs = load_docs()
    print(f"  {len(docs)} 篇文档，耗时 {time.time() - t0:.1f}s")

    # ------------------------------------------------ 离线
    print("\n切片中 ...")
    all_chunks: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for sc in schemes:
        t0 = time.time()
        cs = build_chunks(docs, sc)
        all_chunks[sc.key] = cs
        st = offline_stats(sc, cs)
        rows.append(st)
        print(f"  [{sc.key}] {len(cs):>5} chunks  ({time.time() - t0:.1f}s)")

    print_offline(rows)

    if not args.online:
        print("\n（以上为离线对比；加 --online 可跑真实检索质量对比）\n")
        return 0

    # ------------------------------------------------ 在线
    if not GLM_KEY:
        print("[FATAL] .env 缺少 EMBEDDING_API_KEY")
        return 1

    qpath = KB / "eval/questions.jsonl"
    questions = [
        json.loads(l) for l in qpath.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    rag_qs = [q for q in questions if str(q.get("expected_route", "")).startswith("rag")]
    if args.limit:
        rag_qs = rag_qs[: args.limit]
    print(f"\n评估集：{len(rag_qs)} 条 RAG 类问题（有标注答案的计入命中率）")

    client = MilvusClient(uri=MILVUS_URI)
    print(f"Milvus {client.get_server_version()}")

    # 预计算全部查询向量（一次批量），供所有方案复用，避免重复请求触发限流
    uniq_q = list({q["question"] for q in rag_qs})
    print(f"预计算 {len(uniq_q)} 条查询向量 ...")
    qvec_list = embed_batch(uniq_q)
    qvecs = dict(zip(uniq_q, qvec_list))
    print("  完成")

    online_rows: list[dict] = []
    for sc in schemes:
        coll = f"{COLLECTION_PREFIX}_{sc.key.lower()}"
        print(f"\n[{sc.key}] 建库 {coll} ...")
        t0 = time.time()
        build_collection(client, coll, all_chunks[sc.key])
        print(f"  建库完成 {time.time() - t0:.1f}s")
        print(f"[{sc.key}] 评估中 ...")
        res = online_eval(client, coll, rag_qs, args.topk, qvecs)
        res["key"] = sc.key
        res["label"] = sc.label
        res["n_chunks"] = len(all_chunks[sc.key])
        online_rows.append(res)

    print("\n" + "=" * 100)
    print("  在线检索质量对比")
    print("=" * 100)
    print(f"  {'方案':<40}{'片数':>7}{'top1':>8}{'top5':>8}{'top10':>8}{'top1分':>9}{'耗时s':>8}")
    print("-" * 100)
    for r in online_rows:
        print(
            f"  {r['label']:<40}{r['n_chunks']:>7}{r['top1']:>7.1%}{r['top5']:>8.1%}"
            f"{r['top10']:>8.1%}{r['mean_top1_score']:>9.4f}{r['mean_latency']:>8.1f}"
        )
    print("-" * 100)
    print()

    out = ROOT.parent / "deploy/milvus/chunking-compare.json"
    out.write_text(
        json.dumps({"offline": rows, "online": online_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已存：{out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

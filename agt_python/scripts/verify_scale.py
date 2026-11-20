# -*- coding: utf-8 -*-
"""
真实规模检索验证：扩语料后，reranker 的分数是否仍然饱和？

背景
----
上一轮实验（scripts/verify_rerank.py）在 **10 条候选**的池子上发现：
GLM rerank 给完全无关的文档（北京地铁时刻表、红烧肉做法）也打 0.98~0.999 分，
分数全挤在小数点后第 4 位，导致任何绝对阈值/相对阈值筛选全部失效。

但那个实验有个明显缺陷：**候选池太小且几乎无领域外干扰**。
本脚本在 **扩语料后的真实规模**（76 篇文档 → 数百 chunk）上重做实验，
回答三个问题：

  Q1  扩语料后，rerank 分数是否拉开了？（分数跨度 / 标准差）
  Q2  正确答案所在 chunk 能否稳定排进 top-N？
  Q3  用评估集标注的 hit_level 分组，high 组与 none 组的分数是否可分？
      （若可分 → 阈值策略可用；若完全重叠 → 阈值不可用，只能靠排名）

流程
----
  加载语料(76篇) → 标题感知+中文递归切片 → GLM embedding-3
  → Milvus 建库(dense + BM25) → 混合检索 top20 → GLM rerank
  → 分数分布分析

运行
----
  D:/install/python/anaconda/envs/python311/python.exe scripts/verify_scale.py
  D:/install/python/anaconda/envs/python311/python.exe scripts/verify_scale.py --limit 20   # 只跑前 20 问
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
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

ROOT = Path(__file__).resolve().parent.parent          # agt_python/
KB = ROOT / "data/kb"
MILVUS_URI = "http://localhost:19530"
COLLECTION = "kb_scale"          # 真实规模验证集合（验证用，正式库另建）

# rerank 模型：务必用 `rerank-pro`。
# 基础版 `rerank` 区分度极差——实测给完全无关的文档（红烧肉、地铁时刻表）
# 打 0.86~0.95 分，导致分数全挤在 0.99999~1.0，任何阈值都失效。
# `rerank-pro` 对无关文档给 3e-06，分数分布拉开到 0.001~0.999，阈值可用。
RERANK_MODEL = ENV.get("RERANK_MODEL", "rerank-pro") if "ENV" in dir() else "rerank-pro"

CHUNK_SIZE = 600                 # 每片目标字符数
CHUNK_OVERLAP = 80               # 相邻片重叠字符数
MIN_CHUNK = 200                  # 低于此长度的段落尝试向前合并，抑制碎片化
EMBED_BATCH = 32                 # embedding 单批条数

# 语料来源：自有语料 + 第三方扩展语料
CORPUS_DIRS = [
    (KB / "raw", "own"),
    (KB / "external/uav-stack", "ext"),
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


# ---------------------------------------------------------------- 切片器

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 中文优先的分隔符优先级：段 → 行 → 句 → 逗号 → 空格
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " "]


def truncate_utf8(s: str, max_bytes: int) -> str:
    """
    按 UTF-8 **字节数**截断字符串。

    坑：Milvus 的 VARCHAR max_length 单位是字节而非字符。
    一个中文字符占 3 字节，若按字符数设置长度，中文内容会轻易超限，
    表现为插入时报 `Failed to serialize proto`（错误信息完全不提长度）。
    """
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆分 frontmatter 与正文。"""
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
    """按 markdown 标题切成 (标题路径, 内容) 段落。"""
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


def merge_small_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """
    把过短的段落向前合并，抑制切片碎片化。

    为什么需要：第三方知识库（尤其技术文档）标题极密，「## 一、概述」下面
    常常只有一两句话。若每段独立成片，会产生大量 50~150 字的碎片：
      - 碎片信息量不足，向量语义模糊，反而拉低召回
      - 无谓放大 chunk 总数，检索候选池被噪音填满

    合并规则：段落内容 < MIN_CHUNK 且与前一片合并后不超过 CHUNK_SIZE 时合并。
    """
    out: list[tuple[str, str]] = []
    for heading, content in sections:
        if out and len(content) < MIN_CHUNK:
            phead, pcontent = out[-1]
            if len(pcontent) + len(content) + 2 <= CHUNK_SIZE:
                merged_head = phead if phead == heading else (
                    f"{phead} · {heading}" if heading and phead else (phead or heading)
                )
                out[-1] = (merged_head, f"{pcontent}\n\n{content}")
                continue
        out.append((heading, content))
    return out


def chunk_document(doc: dict) -> list[dict]:
    """
    把一篇文档切成 chunks。

    设计要点：
      1. **标题感知**：先按 markdown 标题切段，天然贴合文档结构
      2. **碎片合并**：过短段落向前合并（见 merge_small_sections）
      3. **中文递归**：超长段落按「段→行→句→逗号」优先级逐级下探
      4. **标题前缀注入**：每片正文前拼上「文档标题 · 章节标题」，
         这是低成本高收益的做法——让切片自带上下文，显著提升召回
      5. **重叠**：相邻片保留 overlap 字符，避免答案被切断在边界
    """
    meta, body = parse_frontmatter(doc["raw"])
    title = meta.get("title") or doc["stem"]
    doc_id = meta.get("doc_id") or doc["stem"]
    category = meta.get("category") or doc["category"]

    sections = merge_small_sections(split_sections(body))

    chunks: list[dict] = []
    for heading, content in sections:
        # 跳过信息量过低的碎片
        if len(content) < 40:
            continue

        for piece in recursive_split(content, CHUNK_SIZE, SEPARATORS):
            prefix = f"【{title}】" + (f"「{heading}」" if heading else "")
            chunks.append(
                {
                    "doc_id": doc_id,
                    "title": title,
                    "category": category,
                    "heading": heading,
                    "text": f"{prefix}\n{piece}".strip(),
                }
            )

    # 加重叠
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]["text"]
        if len(prev) > CHUNK_OVERLAP:
            chunks[i]["text"] = prev[-CHUNK_OVERLAP:] + "\n" + chunks[i]["text"]

    return chunks


def load_corpus() -> tuple[list[dict], list[dict]]:
    """加载全部语料并切片，返回 (chunks, doc_stats)。"""
    chunks: list[dict] = []
    stats: list[dict] = []

    for base, origin in CORPUS_DIRS:
        if not base.exists():
            print(f"  [warn] 目录不存在，跳过: {base}")
            continue
        for md in sorted(base.rglob("*.md")):
            raw = md.read_text(encoding="utf-8", errors="ignore")
            doc = {
                "stem": md.stem,
                "raw": raw,
                "category": md.parent.name,
                "origin": origin,
            }
            cs = chunk_document(doc)
            # 给 chunk 编唯一 id
            for j, c in enumerate(cs):
                c["chunk_id"] = f"{c['doc_id']}#{j}"
                c["origin"] = origin
            chunks.extend(cs)
            stats.append(
                {
                    "path": str(md.relative_to(KB)),
                    "origin": origin,
                    "doc_id": cs[0]["doc_id"] if cs else md.stem,
                    "category": cs[0]["category"] if cs else md.parent.name,
                    "chunks": len(cs),
                    "chars": len(raw),
                }
            )
    return chunks, stats


# ---------------------------------------------------------------- API

def embed_batch(texts: list[str]) -> list[list[float]]:
    """批量调用 GLM embedding-3（失败自动降级为逐条重试）。"""
    out: list[list[float]] = []
    with httpx.Client(timeout=120) as c:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            try:
                r = c.post(
                    f"{GLM_BASE}/embeddings",
                    headers={"Authorization": f"Bearer {GLM_KEY}"},
                    json={"model": EMBED_MODEL, "input": batch},
                )
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
            except Exception as e:  # noqa: BLE001
                print(f"    [warn] 批量嵌入失败({type(e).__name__})，降级逐条重试")
                for t in batch:
                    r = c.post(
                        f"{GLM_BASE}/embeddings",
                        headers={"Authorization": f"Bearer {GLM_KEY}"},
                        json={"model": EMBED_MODEL, "input": [t]},
                    )
                    r.raise_for_status()
                    out.append(r.json()["data"][0]["embedding"])
    return out


def glm_rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    """调用 GLM rerank，返回 [(原始下标, 分数)] 降序。"""
    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": documents,
        "top_n": top_n,
        "return_documents": False,
    }
    with httpx.Client(timeout=90) as c:
        r = c.post(
            f"{GLM_BASE}/rerank",
            headers={"Authorization": f"Bearer {GLM_KEY}"},
            json=payload,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()

    # 两个模型的返回结构不同（实测）：
    #   rerank     → {"index": N, "relevance_score": s}
    #   rerank-pro → {"document": "<原文>", "relevance_score": s}   ← 不回 index
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


def hr(t: str):
    print("\n" + "=" * 76)
    print(f"  {t}")
    print("=" * 76)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑评估集前 N 条")
    ap.add_argument("--topk", type=int, default=20, help="召回候选数")
    ap.add_argument("--no-rebuild", action="store_true", help="复用已有集合，跳过建库")
    args = ap.parse_args()

    if not GLM_KEY:
        print("[FATAL] .env 缺少 EMBEDDING_API_KEY")
        return 1

    # ------------------------------------------------ 1. 加载 + 切片
    hr("第 1 步：加载语料并切片")
    t0 = time.time()
    chunks, doc_stats = load_corpus()
    own = [d for d in doc_stats if d["origin"] == "own"]
    ext = [d for d in doc_stats if d["origin"] == "ext"]
    print(f"  自有语料 : {len(own):>3} 篇 → {sum(d['chunks'] for d in own):>4} chunks")
    print(f"  扩展语料 : {len(ext):>3} 篇 → {sum(d['chunks'] for d in ext):>4} chunks")
    print(f"  ─────────────────────────────────────")
    print(f"  合计     : {len(doc_stats):>3} 篇 → {len(chunks):>4} chunks")
    if chunks:
        lens = [len(c["text"]) for c in chunks]
        print(
            f"  片长统计 : 平均 {statistics.mean(lens):.0f} 字 / "
            f"中位 {statistics.median(lens):.0f} / 最长 {max(lens)} / 最短 {min(lens)}"
        )
    print(f"  切片耗时 : {time.time() - t0:.1f}s")

    # ------------------------------------------------ 2. 连接 Milvus
    hr("第 2 步：连接 Milvus 并建库")
    client = MilvusClient(uri=MILVUS_URI)
    print(f"  服务版本 : {client.get_server_version()}")

    need_build = not args.no_rebuild or not client.has_collection(COLLECTION)

    if need_build:
        if client.has_collection(COLLECTION):
            client.drop_collection(COLLECTION)
            print(f"  已清理旧集合 {COLLECTION}")

        # 先拿一次维度
        probe = embed_batch(["维度探测"])
        dim = len(probe[0])
        print(f"  向量维度 : {dim}")

        schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=128)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=128)
        schema.add_field("title", DataType.VARCHAR, max_length=256)
        schema.add_field("category", DataType.VARCHAR, max_length=64)
        schema.add_field("origin", DataType.VARCHAR, max_length=16)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=8192,
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
        client.create_collection(COLLECTION, schema=schema, index_params=idx)
        print(f"  集合已创建（中文分析器 + BM25 稀疏向量）")

        # 分批嵌入 + 写入
        t0 = time.time()
        print(f"  正在向量化 {len(chunks)} 个 chunk ...")
        vectors = embed_batch([c["text"] for c in chunks])
        print(f"  向量化完成，耗时 {time.time() - t0:.1f}s")

        rows = [
            {
                "chunk_id": truncate_utf8(c["chunk_id"], 480),
                "doc_id": truncate_utf8(c["doc_id"], 480),
                "title": truncate_utf8(c["title"], 2000),
                "category": truncate_utf8(c["category"], 240),
                "origin": c["origin"],
                "text": truncate_utf8(c["text"], 8000),
                "dense": v,
            }
            for c, v in zip(chunks, vectors)
        ]

        # 体检：确认没有字段超限（Milvus 超限报错信息只有
        # "Failed to serialize proto"，完全不提是哪个字段、超了多少）
        print("  字段字节长度体检（max / 上限）：")
        limits = {
            "chunk_id": 512, "doc_id": 512, "title": 2048,
            "category": 256, "origin": 16, "text": 16384,
        }
        for k, lim in limits.items():
            mx = max(len(str(r[k]).encode("utf-8")) for r in rows)
            mark = "  ⚠️ 超限!" if mx > lim else ""
            print(f"    {k:<10} {mx:>6} / {lim}{mark}")
        dims = {len(r["dense"]) for r in rows}
        print(f"    dense 维度集合 {dims}（应为单元素集合）")

        t0 = time.time()
        for i in range(0, len(rows), 50):
            client.insert(COLLECTION, rows[i : i + 50])
        client.flush(COLLECTION)
        print(f"  写入 {len(rows)} 条，耗时 {time.time() - t0:.1f}s")
    else:
        print(f"  复用已有集合 {COLLECTION}")

    total = client.query(COLLECTION, filter="id >= 0", output_fields=["count(*)"])
    print(f"  集合条数 : {total[0].get('count(*)', '?')}")

    # ------------------------------------------------ 3. 加载评估集
    hr("第 3 步：加载评估集")
    qpath = KB / "eval/questions.jsonl"
    questions = [json.loads(l) for l in qpath.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        questions = questions[: args.limit]
    print(f"  评估问题 : {len(questions)} 条（仅取 expected_route=rag 的做检索验证）")

    rag_qs = [q for q in questions if q.get("expected_route", "").startswith("rag")]
    print(f"  其中 RAG 类: {len(rag_qs)} 条")

    # ------------------------------------------------ 4. 检索 + 重排
    hr(f"第 4 步：混合检索 top{args.topk} → GLM rerank → 分数分布")
    print("  说明：# = rerank 后的排名；✅ = 命中标注的 expected_doc_ids\n")

    results: list[dict] = []
    for qi, q in enumerate(rag_qs, 1):
        query = q["question"]
        expect_ids = set(q.get("expected_doc_ids") or [])
        level = q.get("expected_hit_level", "?")

        # 混合检索
        qv = embed_batch([query])
        reqs = [
            AnnSearchRequest(
                data=[query], anns_field="sparse", param={"metric_type": "BM25"}, limit=args.topk
            ),
            AnnSearchRequest(
                data=qv, anns_field="dense", param={"metric_type": "COSINE"}, limit=args.topk
            ),
        ]
        hits = client.hybrid_search(
            collection_name=COLLECTION,
            reqs=reqs,
            ranker=RRFRanker(),
            limit=args.topk,
            output_fields=["chunk_id", "doc_id", "title", "category", "origin", "text"],
        )
        cand = [h["entity"] for h in (hits[0] if hits else [])]
        if not cand:
            print(f"  [{qi:>2}] {query[:40]}  → 无召回")
            continue

        # 重排
        try:
            ranked = glm_rerank(query, [c["text"] for c in cand], top_n=len(cand))
        except Exception as e:  # noqa: BLE001
            print(f"  [{qi:>2}] {query[:40]}  → rerank 失败 {type(e).__name__}")
            continue

        scores = [s for _, s in ranked]
        top_doc = cand[ranked[0][0]]["doc_id"]

        # 正确答案的最高排名
        hit_rank = None
        for rank, (idx, _) in enumerate(ranked, 1):
            if cand[idx]["doc_id"] in expect_ids:
                hit_rank = rank
                break

        results.append(
            {
                "qid": q["qid"],
                "question": query,
                "level": level,
                "scores": scores,
                "top1_doc": top_doc,
                "hit_rank": hit_rank,
                "expect_hit": bool(expect_ids),
                "hit_level": q.get("expected_hit_level"),
            }
        )

        # 打印
        spread = scores[0] - scores[-1]
        rank_txt = f"#{hit_rank}" if hit_rank else "未进候选"
        flag = "✅" if hit_rank and hit_rank <= 4 else ("⚠️" if hit_rank else "❌")
        print(f"  [{qi:>2}] {q['qid']}  {query[:34]:<34} level={level:<7} "
              f"top1={scores[0]:.6f} 跨度={spread:.6f} 期望答案={rank_txt} {flag}")
        print(f"        top5: " + " ".join(f"{s:.6f}" for s in scores[:5])
              + f"   |  末3: " + " ".join(f"{s:.6f}" for s in scores[-3:]))

    # ------------------------------------------------ 5. 汇总分析
    hr("第 5 步：分数分布汇总分析")

    if results:
        all_scores = [s for r in results for s in r["scores"]]
        print(f"\n  ── 全体候选分数（{len(all_scores)} 个）──")
        print(f"    范围   : {min(all_scores):.6f} ~ {max(all_scores):.6f}")
        print(f"    中位数 : {statistics.median(all_scores):.6f}")
        print(f"    均值   : {statistics.mean(all_scores):.6f}")
        print(f"    标准差 : {statistics.pstdev(all_scores):.6f}")

        # 关键：按标注的 hit_level 分组看分数
        print(f"\n  ── 按评估集标注的 expected_hit_level 分组（看各组能否用分数区分）──")
        by_level: dict[str, list[float]] = {}
        for r in results:
            by_level.setdefault(r["level"], []).append(r["scores"][0])
        for lv in sorted(by_level):
            vals = by_level[lv]
            print(f"    {lv:<10} top1 分数: "
                  f"n={len(vals):<3} min={min(vals):.6f} med={statistics.median(vals):.6f} max={max(vals):.6f}")

        # 跨度统计
        spreads = [r["scores"][0] - r["scores"][-1] for r in results]
        print(f"\n  ── 每个查询的 top1−top20 跨度 ──")
        print(f"    平均跨度 : {statistics.mean(spreads):.6f}")
        print(f"    最大跨度 : {max(spreads):.6f}")
        print(f"    最小跨度 : {min(spreads):.6f}")

        # 命中率
        print(f"\n  ── 期望答案的 top-N 命中率 ──")
        for n in (1, 3, 5, 8, 20):
            ok = sum(1 for r in results if r["hit_rank"] and r["hit_rank"] <= n)
            print(f"    top{n:<3}: {ok:>3}/{len(results)}  ({ok / len(results):.0%})")

        # 输出完整分数表供进一步分析
        out = ROOT.parent / "deploy/milvus/verify-scale-result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "corpus": {
                        "docs": len(doc_stats),
                        "chunks": len(chunks),
                        "own_docs": len(own),
                        "ext_docs": len(ext),
                    },
                    "params": {"topk": args.topk, "chunk_size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP},
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  完整结果已写入 → {out.relative_to(ROOT.parent)}")

    hr("验证完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

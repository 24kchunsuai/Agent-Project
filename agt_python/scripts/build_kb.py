# -*- coding: utf-8 -*-
"""
正式知识库建库：kb_drone（M6 落地脚本）。

与验证脚本（verify_scale / compare_chunking）的区别
------------------------------------------------
1. 切片统一走 `app/rag/chunker.py`（正式实现，预算制 400/80/100），
   不再复制脚本内的临时切片器
2. Schema 带 **19 字段完整元数据**（鉴权过滤 / 引用追踪 / 时效合规三类）
3. 标量字段建 **INVERTED 倒排索引**（ Milvus 2.6 中 VARCHAR 标量过滤
   无索引会全量遍历；owner/visibility 鉴权过滤是每次查询的热路径）
4. 建库后立刻跑评估集基线，作为后续迭代的对照基准

流程
----
  语料(raw + external) → chunker 400/80/100 → GLM embedding-3
  → Milvus kb_drone(dense COSINE + BM25 中文分析器 + 标量倒排)
  → 混合检索 top20 → rerank-pro → 命中率基线

运行
----
  D:/install/python/anaconda/envs/python311/python.exe scripts/build_kb.py
  D:/install/python/anaconda/envs/python311/python.exe scripts/build_kb.py --no-eval
  D:/install/python/anaconda/envs/python311/python.exe scripts/build_kb.py --eval-limit 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent          # agt_python/
sys.path.insert(0, str(ROOT))

from app.rag.chunker import ChunkConfig, iter_corpus  # noqa: E402


def truncate_utf8(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断（Milvus VARCHAR max_length 单位是字节，中文占 3 字节）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")

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

# ---------------------------------------------------------------- 配置

KB = ROOT / "data/kb"
MILVUS_URI = "http://localhost:19530"
COLLECTION = "kb_drone"           # 正式库
EMBED_BATCH = 32
INSERT_BATCH = 50

# 语料目录 → 兜底元数据（frontmatter 缺省时使用）
CORPUS_DIRS = [
    (KB / "raw", {
        "owner": "default",
        "visibility": "public",
        "source_type": "own_doc",
        "lang": "zh",
    }),
    (KB / "external/uav-stack", {
        "owner": "public",
        "visibility": "public",
        "source_type": "third_party_github",
        "lang": "zh",
    }),
]

# 标量字段倒排索引：鉴权过滤(owner/visibility)与常用过滤是查询热路径
INVERTED_FIELDS = ["doc_id", "owner", "visibility", "category", "source_type"]


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


# ---------------------------------------------------------------- GLM API

def embed_batch(texts: list[str], *, sleep_s: float = 0.6, max_retry: int = 6) -> list[list[float]]:
    """批量 embedding，429 指数退避（复用 compare_chunking 验证过的实现）。"""
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
                        print(f"      [429] 限流，退避 {wait}s ...", flush=True)
                        time.sleep(wait)
                        continue
                    # 余额不足等业务错误直接暴露，不静默重试
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
    """rerank-pro 重排，返回 [(原始下标, 分数)] 降序。"""
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

    # rerank-pro → {"document": "<原文>", "relevance_score": s}（不回 index，靠文本反查）
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


# ---------------------------------------------------------------- 建库

def build_collection(client: MilvusClient, dim: int) -> None:
    """建 kb_drone：19 字段 + BM25 稀疏 + 标量倒排。"""
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
        print(f"  已清理旧集合 {COLLECTION}")

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)

    schema.add_field("id", DataType.INT64, is_primary=True)
    # ---- 引用追踪 ----
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_title", DataType.VARCHAR, max_length=2048)
    schema.add_field("heading", DataType.VARCHAR, max_length=1024)
    schema.add_field("chunk_index", DataType.INT32)
    schema.add_field("total_chunks", DataType.INT32)
    schema.add_field("char_start", DataType.INT32)
    schema.add_field("char_end", DataType.INT32)
    # ---- 鉴权过滤 ----
    schema.add_field("owner", DataType.VARCHAR, max_length=256)
    schema.add_field("visibility", DataType.VARCHAR, max_length=32)
    # ---- 分类与时效 ----
    schema.add_field("category", DataType.VARCHAR, max_length=256)
    schema.add_field("source", DataType.VARCHAR, max_length=512)
    schema.add_field("source_type", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_version", DataType.VARCHAR, max_length=32)
    schema.add_field("doc_updated", DataType.VARCHAR, max_length=32)
    schema.add_field("lang", DataType.VARCHAR, max_length=16)
    schema.add_field("char_len", DataType.INT32)
    # ---- 正文与向量 ----
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=16384,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},   # 服务端 jieba 分词，BM25 用
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
    idx.add_index("dense", index_type="AUTOINDEX", metric_type="COSINE")
    idx.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    for f in INVERTED_FIELDS:
        idx.add_index(f, index_type="INVERTED")
    client.create_collection(COLLECTION, schema=schema, index_params=idx)
    print(f"  集合已创建：19 字段 + dense COSINE + BM25(chinese) + 倒排{INVERTED_FIELDS}")


def insert_chunks(client: MilvusClient, chunks, vectors) -> None:
    rows = [c.to_milvus_row(v, truncate_utf8) for c, v in zip(chunks, vectors)]

    # 入库前体检：VARCHAR 按字节核对上限（Milvus 超限只报 serialize proto，
    # 不说哪个字段超了，必须在客户端提前拦住）
    limits = {
        "chunk_id": 512, "doc_id": 512, "doc_title": 2048, "heading": 1024,
        "owner": 256, "visibility": 32, "category": 256, "source": 512,
        "source_type": 64, "doc_version": 32, "doc_updated": 32, "lang": 16,
        "text": 16384,
    }
    bad = []
    for k, lim in limits.items():
        mx = max(len(str(r[k]).encode("utf-8")) for r in rows)
        if mx > lim:
            bad.append(f"{k} {mx}>{lim}")
        print(f"    {k:<12} max_bytes={mx:>6} / {lim}")
    if bad:
        raise RuntimeError("字段超限: " + "; ".join(bad))
    dims = {len(r["dense"]) for r in rows}
    if len(dims) != 1:
        raise RuntimeError(f"向量维度不一致: {dims}")

    for i in range(0, len(rows), INSERT_BATCH):
        client.insert(COLLECTION, rows[i : i + INSERT_BATCH])
    client.flush(COLLECTION)


# ---------------------------------------------------------------- 评估基线

def run_eval(client: MilvusClient, limit: int = 0) -> dict:
    """跑 RAG 评估集，产出正式库基线指标。"""
    qpath = KB / "eval/questions.jsonl"
    questions = [
        json.loads(l) for l in qpath.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    rag_qs = [q for q in questions if str(q.get("expected_route", "")).startswith("rag")]
    if limit:
        rag_qs = rag_qs[:limit]

    print(f"  评估问题: {len(rag_qs)} 条（RAG 类）")
    uniq_q = list({q["question"] for q in rag_qs})
    print(f"  预计算 {len(uniq_q)} 条查询向量 ...")
    qvec_list = embed_batch(uniq_q)
    qvecs = dict(zip(uniq_q, qvec_list))

    hit1 = hit3 = hit5 = hit8 = 0
    n_eval = 0
    t1_scores: list[float] = []
    ctx_chars: list[int] = []
    details: list[dict] = []

    for q in rag_qs:
        expect = set(q.get("expected_doc_ids") or [])
        query = q["question"]
        reqs = [
            AnnSearchRequest(
                data=[query], anns_field="sparse",
                param={"metric_type": "BM25"}, limit=20,
            ),
            AnnSearchRequest(
                # 注意必须是「向量的列表」而非扁平 list：
                # 传扁平 list 时 pymilvus 把每个 float 当一条向量，报
                # "object of type 'float' has no len()"
                data=[qvecs[query]], anns_field="dense",
                param={"metric_type": "COSINE"}, limit=20,
            ),
        ]
        hits = client.hybrid_search(
            collection_name=COLLECTION,
            reqs=reqs,
            ranker=RRFRanker(),
            limit=20,
            output_fields=["chunk_id", "doc_id", "text"],
        )
        cand = [h["entity"] for h in (hits[0] if hits else [])]
        if not cand:
            continue
        n_eval += 1

        if not expect:
            continue                      # 无标注的超纲题只占跑批，不计命中率

        try:
            ranked = glm_rerank(query, [c["text"] for c in cand], top_n=len(cand))
        except Exception as e:  # noqa: BLE001
            print(f"    [{q['qid']}] rerank 失败: {type(e).__name__}")
            continue

        scored = [
            (cand[i]["doc_id"], cand[i]["text"], s)
            for i, s in ranked
            if i < len(cand)
        ]
        hit_rank = None
        for rank, (d, _, _) in enumerate(scored, 1):
            if d in expect:
                hit_rank = rank
                break

        t1_scores.append(scored[0][2])
        top5 = scored[:5]
        ctx_chars.append(sum(len(t) for _, t, _ in top5))
        if hit_rank and hit_rank <= 1:
            hit1 += 1
        if hit_rank and hit_rank <= 3:
            hit3 += 1
        if hit_rank and hit_rank <= 5:
            hit5 += 1
        if hit_rank and hit_rank <= 8:
            hit8 += 1

        flag = "✅" if hit_rank and hit_rank <= 4 else ("⚠️" if hit_rank else "❌")
        print(f"    [{q['qid']}] {query[:32]:<32} level={q.get('expected_hit_level','?'):<7}"
              f" top1={scored[0][2]:.4f} 期望答案=#{hit_rank} {flag}")
        details.append({
            "qid": q["qid"],
            "question": query,
            "hit_level": q.get("expected_hit_level"),
            "hit_rank": hit_rank,
            "top1_score": scored[0][2],
        })

    n_hit = sum(1 for d in details if d["hit_rank"])
    n_label = sum(1 for d in details if d.get("hit_level") is not None) or len(details)
    # 命中率分母只算有标注答案的题（超纲题无正确答案，计入会稀释指标）
    return {
        "n_eval": n_eval,
        "n_labeled": n_label,
        "n_hit_any": n_hit,
        "top1": hit1 / n_label,
        "top3": hit3 / n_label,
        "top5": hit5 / n_label,
        "top8": hit8 / n_label,
        "mean_top1_score": statistics.mean(t1_scores) if t1_scores else 0,
        "mean_ctx_top5_chars": statistics.mean(ctx_chars) if ctx_chars else 0,
        "details": details,
    }


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-eval", action="store_true", help="只建库，不跑评估")
    ap.add_argument("--eval-only", action="store_true", help="复用已有集合，只跑评估")
    ap.add_argument("--eval-limit", type=int, default=0, help="评估集前 N 条")
    args = ap.parse_args()

    if not GLM_KEY:
        print("[FATAL] .env 缺少 EMBEDDING_API_KEY")
        return 1

    client = MilvusClient(uri=MILVUS_URI)

    if args.eval_only:
        print("\n=== 只跑评估（复用已有 kb_drone）===")
        res = run_eval(client, args.eval_limit)
        out = ROOT.parent / "deploy/milvus/kb-drone-baseline.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"collection": COLLECTION, "baseline": res},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  基线已存: {out}")
        return 0

    # ---- 1. 切片（正式 chunker，400/80/100）----
    print("\n=== 第 1 步：切片（app/rag/chunker.py，400/80/100 预算制）===")
    cfg = ChunkConfig()                   # 默认即 400/80/100
    t0 = time.time()
    chunks, doc_stats = iter_corpus(CORPUS_DIRS, cfg)
    own = [d for d in doc_stats if "own" in d["path"] or "raw" in d["path"]]
    ext = [d for d in doc_stats if "external" in d["path"]]
    print(f"  自有语料 : {len(own):>3} 篇")
    print(f"  扩展语料 : {len(ext):>3} 篇")
    print(f"  合计     : {len(doc_stats):>3} 篇 → {len(chunks):>4} chunks（{time.time()-t0:.1f}s）")

    lens = [c.char_len for c in chunks]
    print(f"  片长: min={min(lens)} 中位={statistics.median(lens):.0f} "
          f"均值={statistics.mean(lens):.0f} p90={sorted(lens)[int(len(lens)*0.9)]} max={max(lens)}")
    over = [l for l in lens if l > cfg.max_chars]
    print(f"  超上限(>{cfg.max_chars}字)的片: {len(over)} 个"
          + (f" ← 应为 0（预算制硬上限）⚠️" if over else " ✅"))
    frag = sum(1 for l in lens if l < cfg.min_chunk) / len(lens)
    print(f"  碎片率(<{cfg.min_chunk}字): {frag:.1%}")

    # ---- 2. 向量化 ----
    print("\n=== 第 2 步：GLM embedding-3 向量化 ===")
    probe = embed_batch(["维度探测"], sleep_s=0)
    dim = len(probe[0])
    print(f"  维度: {dim}，共 {len(chunks)} 片，约 {sum(lens)//1000}K 字")
    t0 = time.time()
    vectors = embed_batch([c.text for c in chunks])
    print(f"  完成，耗时 {time.time()-t0:.1f}s")

    # ---- 3. 建库写入 ----
    print("\n=== 第 3 步：Milvus 建库 kb_drone ===")
    print(f"  服务版本: {client.get_server_version()}")
    build_collection(client, dim)

    print("  字段字节体检:")
    t0 = time.time()
    insert_chunks(client, chunks, vectors)
    total = client.query(COLLECTION, filter="id >= 0", output_fields=["count(*)"])
    print(f"  写入 {total[0].get('count(*)')} 条，耗时 {time.time()-t0:.1f}s")

    # ---- 4. 评估基线 ----
    if not args.no_eval:
        print("\n=== 第 4 步：评估基线（混合检索 top20 → rerank-pro）===")
        res = run_eval(client, args.eval_limit)
        print(f"\n  ── kb_drone 基线 ──")
        print(f"  top1={res['top1']:.1%}  top3={res['top3']:.1%}  "
              f"top5={res['top5']:.1%}  top8={res['top8']:.1%}")
        print(f"  平均 top1 分数: {res['mean_top1_score']:.4f}")
        print(f"  top5 上下文均值: {res['mean_ctx_top5_chars']:.0f} 字")

        out = ROOT.parent / "deploy/milvus/kb-drone-baseline.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "collection": COLLECTION,
                    "chunk_config": {"max_chars": cfg.max_chars, "overlap": cfg.overlap,
                                     "min_chunk": cfg.min_chunk},
                    "corpus": {"docs": len(doc_stats), "chunks": len(chunks)},
                    "baseline": res,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  基线已存: {out}")

    print("\n=== 完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

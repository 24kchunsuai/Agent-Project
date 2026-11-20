# -*- coding: utf-8 -*-
"""
rerank 分数饱和归因诊断

现象
----
真实规模验证（scripts/verify_scale.py）中发现：
  · 20 条真实候选 → rerank 分数挤在 0.999993~1.000000（跨度 0.000007）
  · 3 条短文档对照 → 1.0 / 0.945 / 0.935（跨度 0.065）

同样是 GLM rerank，跨度差了 4 个数量级。本脚本用受控变量法定位原因，
依次排除三个可疑因素：

  实验 1  文档长度   —— 原样(≈700字) / 300字 / 120字
  实验 2  候选规模   —— top20 / top10 / top5
  实验 3  标题前缀   —— 带【标题】前缀 / 去掉前缀
  实验 4  语料同质性 —— 混入跨领域文档后分数是否拉开

每个实验都输出：分数范围 / 跨度 / 标准差 / 期望片的排名。

运行：
  D:/install/python/anaconda/envs/python311/python.exe scripts/diagnose_rerank.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import httpx

try:
    from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker
except ImportError:
    print("[FATAL] 未安装 pymilvus")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
MILVUS_URI = "http://localhost:19530"
COLLECTION = "kb_scale"

# rerank 模型：基础版 `rerank` 区分度极差（无关文档也能拿 0.86），
# `rerank-pro` 才是可用模型（无关文档 3e-06）。可用命令行参数覆盖。
RERANK_MODEL = sys.argv[1] if len(sys.argv) > 1 else "rerank-pro"

# 诊断用固定查询（答案明确落在 reg-classification-001）
QUERY = "微型无人机的重量上限是多少？"
EXPECT_DOC = "reg-classification-001"


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


def embed(texts: list[str]) -> list[list[float]]:
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{GLM_BASE}/embeddings",
            headers={"Authorization": f"Bearer {GLM_KEY}"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda x: x["index"])]


def rerank(query: str, documents: list[str], model: str = RERANK_MODEL) -> list[tuple[int, float]]:
    with httpx.Client(timeout=90) as c:
        r = c.post(
            f"{GLM_BASE}/rerank",
            headers={"Authorization": f"Bearer {GLM_KEY}"},
            json={
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
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


def stats(scores: list[float]) -> str:
    if len(scores) < 2:
        return f"n={len(scores)}"
    return (
        f"范围 {min(scores):.6f}~{max(scores):.6f}  "
        f"跨度 {max(scores) - min(scores):.6f}  "
        f"标准差 {statistics.pstdev(scores):.6f}"
    )


def rank_of(ranked: list[tuple[int, float]], doc_ids: list[str], target: str) -> str:
    for rank, (idx, _) in enumerate(ranked, 1):
        if doc_ids[idx] == target:
            return f"#{rank}"
    return "未命中"


def hr(t: str):
    print("\n" + "=" * 78)
    print(f"  {t}")
    print("=" * 78)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    client = MilvusClient(uri=MILVUS_URI)
    print(f"Milvus {client.get_server_version()} / 集合 {COLLECTION}")

    # 统一取一次 top20 候选
    qv = embed([QUERY])
    reqs = [
        AnnSearchRequest(data=[QUERY], anns_field="sparse",
                         param={"metric_type": "BM25"}, limit=20),
        AnnSearchRequest(data=qv, anns_field="dense",
                         param={"metric_type": "COSINE"}, limit=20),
    ]
    hits = client.hybrid_search(
        collection_name=COLLECTION, reqs=reqs, ranker=RRFRanker(),
        limit=20, output_fields=["doc_id", "title", "text"],
    )
    cand = [h["entity"] for h in hits[0]]
    texts = [c["text"] for c in cand]
    doc_ids = [c["doc_id"] for c in cand]

    print(f"查询: {QUERY}")
    print(f"期望文档: {EXPECT_DOC}")
    print(f"候选数: {len(cand)}")
    print(f"候选中期望文档的片数: {sum(1 for d in doc_ids if d == EXPECT_DOC)}")

    # 候选的原始长度分布
    lens = [len(t) for t in texts]
    print(f"候选文本长度: 平均 {statistics.mean(lens):.0f} / "
          f"中位 {statistics.median(lens):.0f} / "
          f"范围 {min(lens)}~{max(lens)}")

    # ============================================== 实验 1：文档长度
    hr("实验 1｜文档长度对分数区分度的影响")
    print(f"{'变体':<14}{'跨度':<18}{'范围':<30}{'期望片排名'}")
    print("-" * 78)

    variants = [
        ("原样(≈700字)", texts),
        ("截断 300 字", [t[:300] for t in texts]),
        ("截断 120 字", [t[:120] for t in texts]),
    ]
    for name, docs in variants:
        ranked = rerank(QUERY, docs)
        scores = [s for _, s in ranked]
        rng = f"{min(scores):.4f}~{max(scores):.4f}"
        print(f"{name:<14}{max(scores) - min(scores):<18.6f}{rng:<30}{rank_of(ranked, doc_ids, EXPECT_DOC)}")

    # ============================================== 实验 2：候选规模
    hr("实验 2｜候选规模对分数区分度的影响")
    print(f"{'候选数':<14}{'跨度':<18}{'范围':<30}{'期望片排名'}")
    print("-" * 78)

    for k in (20, 10, 5, 3):
        docs = texts[:k]
        ids = doc_ids[:k]
        ranked = rerank(QUERY, docs)
        scores = [s for _, s in ranked]
        rng = f"{min(scores):.4f}~{max(scores):.4f}"
        print(f"top{k:<11}{max(scores) - min(scores):<18.6f}{rng:<30}{rank_of(ranked, ids, EXPECT_DOC)}")

    # ============================================== 实验 3：标题前缀
    hr("实验 3｜标题前缀（【标题】「章节」）的影响")
    import re

    stripped = [re.sub(r"^【[^】]*】「?[^」\n]*」?\n?", "", t) for t in texts]
    print(f"{'变体':<14}{'跨度':<18}{'范围':<30}{'期望片排名'}")
    print("-" * 78)
    for name, docs in (("带前缀(原样)", texts), ("去掉前缀", stripped)):
        ranked = rerank(QUERY, docs)
        scores = [s for _, s in ranked]
        rng = f"{min(scores):.4f}~{max(scores):.4f}"
        print(f"{name:<14}{max(scores) - min(scores):<18.6f}{rng:<30}{rank_of(ranked, doc_ids, EXPECT_DOC)}")

    # ============================================== 实验 4：语料同质性
    hr("实验 4｜混入跨领域文档后，分数是否拉开？")
    noise = [
        "红烧肉的做法：五花肉切块，冷水下锅焯水去腥，加入冰糖小火炒出糖色，再放生抽老抽慢炖四十分钟。",
        "北京地铁1号线首末班车时间：苹果园站首班车5:10，末班车22:55；四惠东站首班车5:05。",
        "Python 列表推导式语法：[x * 2 for x in range(10) if x % 2 == 0]，等价于 for 循环加 append。",
        "咖啡豆烘焙程度分为浅烘、中烘、深烘，浅烘酸度明亮，深烘苦味突出。",
    ]
    for name, docs in (
        ("纯真实候选(20)", texts),
        ("真实20+噪音4", texts + noise),
        ("仅噪音4条", noise),
    ):
        ranked = rerank(QUERY, docs)
        scores = [s for _, s in ranked]
        first = docs[ranked[0][0]][:26]
        print(f"\n  【{name}】跨度 {max(scores) - min(scores):.6f}  "
              f"范围 {min(scores):.4f}~{max(scores):.4f}")
        print(f"    第1名: {first}…")

    # ============================================== 结论材料：top5 预览
    hr("附｜原样候选的 rerank top5 明细（人工判断相关性）")
    ranked = rerank(QUERY, texts)
    for rank, (idx, score) in enumerate(ranked[:5], 1):
        mark = "  ← 标注的期望文档" if doc_ids[idx] == EXPECT_DOC else ""
        print(f"\n  #{rank}  {score:.6f}  [{doc_ids[idx]}]{mark}")
        print(f"      {texts[idx][:110].replace(chr(10), ' ')}…")

    return 0


if __name__ == "__main__":
    sys.exit(main())

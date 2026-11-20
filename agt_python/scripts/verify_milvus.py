# -*- coding: utf-8 -*-
"""
Milvus v2.6 能力验证脚本（drone-agent001 RAG 前置验证）

验证四件事：
  1. GLM embedding-3 真实维度与连通性
  2. Milvus 中文分词 + BM25 全文检索是否可用（换 Milvus 的核心动机）
  3. 稠密向量检索
  4. RRF 混合检索（BM25 + 稠密）

同时用「黑飞」这类口语术语做一次负向验证，观察纯 BM25 在
「术语与原文零字面重合」时的表现 —— 这直接决定 Query 改写环节是否必需。

运行：
  D:/install/python/anaconda/envs/python311/python.exe scripts/verify_milvus.py
"""
import os
import sys
import time
from pathlib import Path

import httpx

try:
    from pymilvus import (
        MilvusClient, DataType, Function, FunctionType,
        AnnSearchRequest, RRFRanker, WeightedRanker,
    )
except ImportError:
    print("[FATAL] 未安装 pymilvus，请先执行：")
    print('  pip install "pymilvus>=2.6.0"')
    sys.exit(1)

# ---------------------------------------------------------------- 配置读取
def load_env() -> dict:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    cfg: dict = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


ENV = load_env()
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
COLLECTION = "kb_verify"

# ---------------------------------------------------------------- 测试语料
# 取自项目真实知识库切片，非编造
DOCS = [
    ("reg-001", "无人驾驶航空器按照重量分为微型、轻型、小型、中型、大型五类。"
                "微型无人驾驶航空器是指空机重量小于0.25千克，最大飞行真高不超过50米，"
                "最大平飞速度不超过40千米每小时的无人驾驶航空器。"),
    ("reg-002", "民用无人驾驶航空器所有者应当依法进行实名登记。未经实名登记的，"
                "由公安机关责令改正，可以处200元以下的罚款；情节严重的，"
                "处2000元以上2万元以下的罚款。"),
    ("reg-003", "在管制空域内飞行未经批准的，由公安机关责令停止飞行，可以处500元以下的罚款；"
                "情节严重的，没收实施违规飞行的无人驾驶航空器，"
                "并处1000元以上1万元以下的罚款。"),
    ("man-001", "起飞前检查清单：一、检查螺旋桨是否安装牢固、有无裂纹；"
                "二、检查电池电量是否充足并正确安装；三、校准指南针与IMU；"
                "四、确认GPS卫星数量不少于12颗；五、确认返航点已刷新。"),
    ("man-002", "电池保养：锂电池应存放在阴凉干燥处，环境温度以20至25摄氏度为宜。"
                "长期不使用时，应将电量保持在50%至60%之间，"
                "每隔三个月进行一次充放电维护。"),
    ("man-003", "返航高度设置：建议将返航高度设置为飞行区域内最高障碍物高度再增加30至50米，"
                "避免自动返航过程中撞上建筑物或高压线。"),
]

QUERIES = [
    ("起飞前要检查什么", "应命中 man-001"),
    ("实名登记怎么弄", "应命中 reg-002"),
    ("微型无人机重量标准", "应命中 reg-001"),
    ("电池怎么保养", "应命中 man-002"),
    ("黑飞会有什么后果", "⚠️ 口语词，原文零字面重合，观察 BM25 是否失效"),
]

# ---------------------------------------------------------------- 向量化
def embed(texts):
    """调用 GLM embedding-3"""
    base = ENV.get("EMBEDDING_BASE_URL", "").rstrip("/")
    key = ENV.get("EMBEDDING_API_KEY", "")
    model = ENV.get("EMBEDDING_MODEL", "embedding-3")
    if not base or not key:
        raise RuntimeError("缺少 EMBEDDING_BASE_URL / EMBEDDING_API_KEY")

    with httpx.Client(timeout=90) as c:
        r = c.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "input": texts},
        )
        r.raise_for_status()
        body = r.json()
    return [d["embedding"] for d in body["data"]]


def hr(title: str):
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


# ---------------------------------------------------------------- 主流程
def main():
    hr("第 1 步：连接 Milvus")
    client = MilvusClient(uri=MILVUS_URI)
    print(f"  连接地址 : {MILVUS_URI}")
    print(f"  服务版本 : {client.get_server_version()}")
    print(f"  已有集合 : {client.list_collections()}")

    hr("第 2 步：验证 GLM embedding-3")
    t0 = time.time()
    probe = embed(["测试"])
    dim = len(probe[0])
    print(f"  模型     : {ENV.get('EMBEDDING_MODEL')}")
    print(f"  向量维度 : {dim}")
    print(f"  首值样本 : {probe[0][:4]}")
    print(f"  耗时     : {time.time() - t0:.2f}s")

    hr("第 3 步：创建集合（中文 BM25 + 稠密向量）")
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
        print(f"  已清理旧集合 {COLLECTION}")

    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
    schema.add_field(
        field_name="doc_id", datatype=DataType.VARCHAR, max_length=64,
    )
    # 中文分词：优先 chinese(jieba)，失败则回退 standard
    analyzer_used = None
    for cand in ({"type": "chinese"}, {"tokenizer": "jieba"}, {"type": "standard"}):
        try:
            schema.add_field(
                field_name="text", datatype=DataType.VARCHAR, max_length=4096,
                enable_analyzer=True, analyzer_params=cand,
            )
            analyzer_used = cand
            break
        except Exception as e:  # noqa: BLE001
            print(f"  analyzer {cand} 不可用：{type(e).__name__}")
    print(f"  中文分析器: {analyzer_used}")

    schema.add_field(field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field(field_name="sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)

    # BM25 函数：由 Milvus 服务端自动把 text 转成稀疏向量
    schema.add_function(Function(
        name="text_bm25",
        function_type=FunctionType.BM25,
        input_field_names=["text"],
        output_field_names=["sparse"],
    ))
    print("  BM25 函数: text -> sparse （服务端自动生成）")

    idx = client.prepare_index_params()
    idx.add_index(
        field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    idx.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")

    t0 = time.time()
    client.create_collection(COLLECTION, schema=schema, index_params=idx)
    print(f"  集合创建完成，耗时 {time.time() - t0:.2f}s")

    hr("第 4 步：写入语料（同时生成稠密向量）")
    t0 = time.time()
    vectors = embed([d[1] for d in DOCS])
    rows = [
        {"doc_id": did, "text": txt, "dense": vec}
        for (did, txt), vec in zip(DOCS, vectors)
    ]
    client.insert(COLLECTION, rows)
    client.flush(COLLECTION)
    print(f"  写入 {len(rows)} 条，embedding+插入耗时 {time.time() - t0:.2f}s")

    # -------------------------------------------------- 检索对比
    hr("第 5 步：三种检索方式对比")
    for q, expect in QUERIES:
        print(f"\n  ┌─ 查询：{q}")
        print(f"  │  {expect}")

        # --- A. 纯 BM25 全文检索 ---
        print("  │")
        print("  │  [A] BM25 全文检索")
        try:
            r = client.search(
                collection_name=COLLECTION, data=[q], anns_field="sparse",
                limit=3, output_fields=["doc_id"],
            )
            hits = r[0] if r else []
            if not hits:
                print("  │      → 无命中")
            for h in hits:
                txt = next(d[1] for d in DOCS if d[0] == h["entity"]["doc_id"])
                print(f"  │      {h['distance']:>7.3f}  {h['entity']['doc_id']}  {txt[:34]}…")
        except Exception as e:  # noqa: BLE001
            print(f"  │      → 报错 {type(e).__name__}: {e}")

        # --- B. 纯稠密向量检索 ---
        print("  │")
        print("  │  [B] 稠密向量检索（COSINE）")
        try:
            qv = embed([q])
            r = client.search(
                collection_name=COLLECTION, data=qv, anns_field="dense",
                limit=3, output_fields=["doc_id"],
            )
            for h in (r[0] if r else []):
                txt = next(d[1] for d in DOCS if d[0] == h["entity"]["doc_id"])
                print(f"  │      {h['distance']:>7.3f}  {h['entity']['doc_id']}  {txt[:34]}…")
        except Exception as e:  # noqa: BLE001
            print(f"  │      → 报错 {type(e).__name__}: {e}")

        # --- C. RRF 混合检索 ---
        print("  │")
        print("  │  [C] RRF 混合检索（BM25 + 稠密）")
        try:
            reqs = [
                AnnSearchRequest(data=[q], anns_field="sparse",
                                 param={"metric_type": "BM25"}, limit=5),
                AnnSearchRequest(data=embed([q]), anns_field="dense",
                                 param={"metric_type": "COSINE"}, limit=5),
            ]
            r = client.hybrid_search(
                collection_name=COLLECTION, reqs=reqs, ranker=RRFRanker(),
                limit=3, output_fields=["doc_id"],
            )
            for h in (r[0] if r else []):
                txt = next(d[1] for d in DOCS if d[0] == h["entity"]["doc_id"])
                print(f"  │      {h['distance']:>7.4f}  {h['entity']['doc_id']}  {txt[:34]}…")
        except Exception as e:  # noqa: BLE001
            print(f"  │      → 报错 {type(e).__name__}: {e}")

        # --- D. 加权混合检索（BM25 权重 0.3 / 稠密权重 0.7）---
        # RRF 只看排名不看分数，会把某一路的"高置信错误"放大；
        # 加权融合保留分数信息，可压低 BM25 在口语查询上的噪音。
        print("  │")
        print("  │  [D] 加权混合检索（BM25 0.3 + 稠密 0.7）")
        try:
            reqs_d = [
                AnnSearchRequest(data=[q], anns_field="sparse",
                                 param={"metric_type": "BM25"}, limit=5),
                AnnSearchRequest(data=embed([q]), anns_field="dense",
                                 param={"metric_type": "COSINE"}, limit=5),
            ]
            r = client.hybrid_search(
                collection_name=COLLECTION, reqs=reqs_d,
                ranker=WeightedRanker(0.3, 0.7),
                limit=3, output_fields=["doc_id"],
            )
            for h in (r[0] if r else []):
                txt = next(d[1] for d in DOCS if d[0] == h["entity"]["doc_id"])
                print(f"  │      {h['distance']:>7.4f}  {h['entity']['doc_id']}  {txt[:34]}…")
        except Exception as e:  # noqa: BLE001
            print(f"  │      → 报错 {type(e).__name__}: {e}")
        print("  └" + "─" * 60)

    hr("验证完成")
    print(f"  集合 {COLLECTION} 已保留，可在 WebUI 查看：")
    print("  http://localhost:9091/webui/")


if __name__ == "__main__":
    main()

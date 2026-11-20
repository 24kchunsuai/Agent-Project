"""Milvus 集合的单点定义与幂等创建。

为什么有这个模块
----------------
集合 schema（19 字段）与索引参数（dense COSINE / BM25 chinese / 标量倒排）
原先散在 4 个脚本里各复制一份：``build_kb.py``、``compare_chunking.py``、
``verify_milvus.py``、``verify_scale.py``。任何字段调整都要改 4 处，漏一处就出现
"同一份代码在两个集合上跑出不同结果"。

本模块把它收敛成单点，目前服务于一个具体需求：
**让验收脚本能跑在隔离集合上**（``scripts/serve_isolated.py`` 起隔离服务时
自动补建 ``kb_drone_test_*``）。没有它，"隔离"只能隔离账本与文件，
测试语料的向量仍会灌进产品集合——正是 2026-09-18 那批孤儿向量的成因。

余下 4 个脚本里的重复定义**待收敛到本模块**（已记入 DEV_ISSUES §6.38），
本轮不动它们以缩小改动面。

字段与索引的取值口径以 ``docs/切片方案与元数据设计.md`` 为准。
"""

from __future__ import annotations

import logging

from pymilvus import DataType, Function, FunctionType, MilvusClient

logger = logging.getLogger(__name__)

#: 需要建标量倒排索引的字段（等值过滤用）
INVERTED_FIELDS = ["doc_id", "owner", "visibility", "category", "source_type"]

#: 正文最大长度（VARCHAR，按字符计；入库前还要按字节体检，见 build_kb.py）
TEXT_MAX_LENGTH = 16384


def build_schema(client: MilvusClient, dim: int):
    """构造 kb_drone 的 schema：19 字段 + BM25 稀疏向量函数。"""
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
        max_length=TEXT_MAX_LENGTH,
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
    return schema


def build_index_params(client: MilvusClient):
    """构造索引参数：dense AUTOINDEX(COSINE) + sparse BM25 + 标量倒排。"""
    idx = client.prepare_index_params()
    idx.add_index("dense", index_type="AUTOINDEX", metric_type="COSINE")
    idx.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    for f in INVERTED_FIELDS:
        idx.add_index(f, index_type="INVERTED")
    return idx


def ensure_collection(
    client: MilvusClient, collection: str, dim: int, *, recreate: bool = False,
) -> bool:
    """集合不存在则创建（幂等）。

    :param recreate: True 时先删再建（**危险**，仅供隔离测试集合使用）
    :returns: True = 本次真的创建了；False = 已存在、未改动
    """
    if client.has_collection(collection):
        if not recreate:
            logger.info("集合已存在，跳过创建 | collection=%s", collection)
            return False
        client.drop_collection(collection)
        logger.warning("集合已删除待重建 | collection=%s", collection)

    client.create_collection(
        collection, schema=build_schema(client, dim), index_params=build_index_params(client),
    )
    logger.info("集合已创建 | collection=%s dim=%s 字段=19 倒排=%s",
                collection, dim, INVERTED_FIELDS)
    return True

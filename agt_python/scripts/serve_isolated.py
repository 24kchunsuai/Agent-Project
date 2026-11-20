# -*- coding: utf-8 -*-
"""起一个"数据目录 + Milvus 集合都隔离"的服务实例，专供验收/探测脚本使用。

为什么需要它
------------
`app/core/test_guard.isolate()` 只能隔离**脚本进程自己**的数据目录。
验收脚本是 HTTP 客户端，它上传的语料由**目标服务**写盘——服务跑在产品目录上，
语料就落在产品库里。所以"彻底隔离"必须让服务实例本身也是隔离的：

    进程侧 isolate()  +  服务侧 DATA_ROOT 隔离  +  Milvus 集合隔离
    （脚本自己）          （本脚本设置）            （本脚本设置）

三者缺一：前两者管住账本/备份/源文件，第三者管住向量。少任何一个都会留下残留
（2026-09-18 那批孤儿向量就是只做了前两者）。

用法
----
    # 终端 A：起隔离服务（数据目录自动建临时目录，退出即删）
    python scripts/serve_isolated.py --port 8001

    # 终端 B：用隔离服务跑验收
    python scripts/gov_g4_e2e.py --base http://127.0.0.1:8001

可选参数：
    --data-dir <path>  指定隔离数据目录（默认自动建临时目录）
    --collection <名>  Milvus 集合名（默认 kb_drone_test，与产品 kb_drone 分开）
    --reuse-collection 复用既有测试集合（**默认是删掉重建**，见下）
    --keep             退出时不删临时数据目录（排查用）

为什么默认重建集合
------------------
"数据目录隔离"只隔离账本/备份/源文件；**向量住在 Milvus 里，不随数据目录走**。
只跟随数据目录的话，上一轮验收的向量会留在测试集合里，下一轮的
「对账报告无未知文档（账本覆盖 100%）」就会把它们报成未知文档——
2026-09-18 实测：``kb_drone_test`` 里留着上一轮 G4 的 7 个 ``g4e2e-153843-*``
文档（44 条向量），把 G2 打成 103/104 的假失败。

所以验收用的隔离服务**必须从零点起**：默认删除并重建集合，起点干净。
确实需要保留上一轮数据（例如专门验对账清理残留）时，加 ``--reuse-collection``。

退出请用 Ctrl+C（或发 SIGINT）
-----------------------------
临时数据目录的删除写在 ``finally`` 里。``Stop-Process -Force`` / ``taskkill /F``
这类**强杀**会跳过它，``%TEMP%`` 下就会留下 ``agt-isolated-*`` 目录
（2026-09-18 实测留下 4 个，共约 7.8MB）。这些目录里是测试账本与备份，不含产品数据，
但仍属残留——收尾时用 Ctrl+C。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRODUCT_DATA_DIR = ROOT / "data"
DEFAULT_COLLECTION = "kb_drone_test"


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _report_start_state(client, collection: str) -> None:
    """回显集合起点条数——"起点是否从零开始"要一眼可见。

    上一轮的向量残留会以"未知文档"的形式污染对账断言（见本文件 docstring 里的
    g4e2e-153843-* 事故），这里主动报出来，省得去怀疑对账逻辑。
    """
    try:
        rows = client.query(collection_name=collection, filter='doc_id != ""',
                            output_fields=["doc_id"], limit=10000)
    except Exception as e:                                    # noqa: BLE001
        # 空集合可能报"not loaded"之类，属正常
        print(f"  起点向量   : 0（查询返回 {type(e).__name__}，空集合的正常表现）")
        return
    ids = sorted({str(r["doc_id"]) for r in rows})
    if not ids:
        print("  起点向量   : 0 条（干净）")
        return
    print(f"  起点向量   : {len(rows)} 条 / {len(ids)} 个文档 ⚠️ 非空起点")
    for d in ids[:8]:
        print(f"               - {d}")
    if len(ids) > 8:
        print(f"               … 另有 {len(ids) - 8} 个")
    print("               ⚠️  非空起点会让「对账/无未知文档」类断言误报为功能失败")


def main() -> int:
    ap = argparse.ArgumentParser(description="起隔离数据目录 + 隔离 Milvus 集合的服务实例")
    ap.add_argument("--port", type=int, default=8001, help="监听端口（默认 8001，避开产品 8000）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default="", help="隔离数据目录（默认自动建临时目录）")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION, help="Milvus 集合名")
    ap.add_argument("--reuse-collection", action="store_true",
                    help="复用既有测试集合（默认**删除重建**：验收必须从零点开始，"
                         "上一轮残留的向量会让「无未知文档」这类断言假失败）")
    ap.add_argument("--recreate", action="store_true", help=argparse.SUPPRESS)  # 兼容旧命令
    ap.add_argument("--keep", action="store_true", help="退出时保留临时数据目录")
    ap.add_argument("--with-scheduler", action="store_true",
                    help="挂载对账/巡检/TTL 的**定时循环**（默认不挂载：验收要确定性结果，"
                         "后台任务会在断言窗口里自己开单；手动 API 不受影响）")
    args = ap.parse_args()

    # ---- 数据目录：必须在 import app.* 之前定下来（路径在 import 时求值）----
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
        if _under(data_dir, PRODUCT_DATA_DIR):
            print(f"✗ 拒绝：--data-dir 指向产品数据目录或其子树（{data_dir}）")
            return 2
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="agt-isolated-"))
    os.environ["DATA_ROOT"] = str(data_dir)

    # ---- Milvus 集合：与产品 kb_drone 物理分开 ----
    os.environ["MILVUS_COLLECTION"] = args.collection
    if args.collection == "kb_drone":
        print("✗ 拒绝：--collection 不能等于产品集合名 kb_drone")
        return 2

    # 常驻任务默认只关"定时循环"（gov_scheduler_enabled），**不动功能开关**：
    # 关功能开关（gov_inspection_enabled 等）会把手动 API 一起停用，验收必挂
    # （G4 实测：第 1 段"巡检开关默认开启"直接失败）。用途是让后台任务不在断言
    # 窗口里自己开单——隔离服务启动 120s 后巡检首次执行，曾把
    # "dryRun 前后 openCount 不变"打成假失败。
    if not args.with_scheduler:
        os.environ["GOV_SCHEDULER_ENABLED"] = "false"

    from app.core.config import settings
    from app.rag.vector_store import ensure_collection
    from pymilvus import MilvusClient

    recreate = not args.reuse_collection

    print("=" * 78)
    print("隔离服务启动中")
    print(f"  数据目录   : {data_dir}   （隔离；退出{'保留' if args.keep else '即删'}）")
    print(f"  治理账本   : {data_dir / 'governance/governance.db'}")
    print(f"  Milvus     : {settings.milvus_uri} / 集合 {args.collection}")
    print(f"  集合处理   : {'删除重建（起点干净）' if recreate else '复用既有（可能带上一轮残留）⚠️'}")
    print(f"  常驻定时   : {'挂载（对账/巡检/TTL 自动跑）' if args.with_scheduler else '不挂载（默认；手动 API 照常可用）'}")
    print(f"  监听       : http://{args.host}:{args.port}")
    print(f"  验收脚本   : python scripts/gov_g4_e2e.py --base http://{args.host}:{args.port}")
    print("=" * 78)

    try:
        client = MilvusClient(uri=settings.milvus_uri)
        created = ensure_collection(
            client, args.collection, settings.embedding_dim, recreate=recreate,
        )
        print(f"  {'✓ 集合已创建' if created else '· 集合已存在（复用）'}：{args.collection}")
        _report_start_state(client, args.collection)
    except Exception as e:                                    # noqa: BLE001
        print(f"✗ 连接 Milvus 或建集合失败：{type(e).__name__}: {e}")
        print("  Milvus 没起？隔离服务必须能连上向量库才能跑验收。")
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
        return 3

    try:
        import uvicorn
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False,
                    log_level="info")
    except KeyboardInterrupt:
        print("\n收到中断，正在收尾…")
    finally:
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
            print(f"✓ 临时数据目录已删除：{data_dir}")
        else:
            print(f"· 数据目录保留在：{data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

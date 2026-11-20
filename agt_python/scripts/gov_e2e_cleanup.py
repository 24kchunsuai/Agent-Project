"""清理历次 G2 E2E 留下的测试语料（一次性工具箱脚本）。

E2E 用 g2e2e- 前缀命名测试文档；中途失败的轮次会在向量库/账本/备份里留下残骸，
导致下一轮"同名内容被 L1 拦截"或对账报"缺失"。本脚本把它们清干净。

定位：**产品库运维工具**——它刻意操作产品数据目录（不做隔离守卫，
否则它就没法清理产品库了），但会把"要对哪个目录动手"打印在最前面，
避免"以为在清测试库、其实在清产品库"。
（2026-09-18 起验收脚本已用 `scripts/serve_isolated.py` 隔离运行，
本脚本只剩"清理历史遗留"一个用途。）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.paths import describe as describe_data_dir         # noqa: E402
from app.services.governance_service import governance_service   # noqa: E402
from app.services.kb_backup import STATE_DISABLED, STATE_RECYCLED, kb_backups, unlink_quietly  # noqa: E402
from app.services.kb_service import kb_service                   # noqa: E402

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "g2e2e-"

print(f"[cleanup] 数据目录 : {describe_data_dir()}")
print(f"[cleanup] 治理账本 : {governance_service.db_path}")
print(f"[cleanup] 清理前缀 : {PREFIX}")


def remove_sources(doc_id: str) -> int:
    """删落盘源文件，返回"确实已删"的数量。

    统一走 `unlink_quietly`：判定用后置条件（文件是否真的不在了），不看异常——
    本机安全删除钩子会抛 `OSError`（SHFileOperationW 失败 0x2，文件其实已进系统回收站），
    批量删除配额用尽时还会抛 **SystemExit**（不是 OSError，普通 try/except 兜不住，
    会让整个清理循环中断——实测踩过）。见 DEV_ISSUES 6.32 坑 1 与 6.33。
    """
    return sum(1 for p in (ROOT / "data/kb/raw/uploads").glob(f"{doc_id}.*")
               if unlink_quietly(p))


def main() -> int:
    store = governance_service.store
    briefs = store.list_doc_briefs()
    targets = [b["doc_id"] for b in briefs if str(b["doc_id"]).startswith(PREFIX)]
    # 向量库里可能有"账本无登记"的残骸，也一并搜出来
    client = kb_service.milvus()
    rows = client.query(collection_name=kb_service.collection, filter="id >= 0",
                        output_fields=["doc_id"], limit=16384)
    targets += [r["doc_id"] for r in rows if str(r.get("doc_id", "")).startswith(PREFIX)]
    targets = sorted(set(targets))

    print(f"待清理 {len(targets)} 个：{targets}")
    failed: list[str] = []
    for doc_id in targets:
        # 单个文档清理失败不中断整体：残骸本身就是从失败轮次来的，
        # 这里再中断就彻底清不掉了
        try:
            removed = kb_service.delete_vectors(doc_id)
            files = kb_backups.remove(doc_id)
            src_n = remove_sources(doc_id)
            store.delete_doc(doc_id)
            print(f"  {doc_id}: 向量={removed} 备份={len(files)} 源文件={src_n}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{doc_id}:{type(e).__name__}:{e}")
            print(f"  [FAIL] {doc_id}: {type(e).__name__}: {e}")

    left = [b["doc_id"] for b in store.list_doc_briefs()
            if str(b["doc_id"]).startswith(PREFIX)]
    print("账本残留：", left)
    if failed:
        print("清理失败：", failed)
    return 1 if (left or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())

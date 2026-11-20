"""语料治理层（Corpus Governance）。

模块划分（见 docs/specs/语料治理规约.md §3 / §13.5）：

- `hashing.py`  语料指纹：L1 文件哈希 / L2 切片哈希（规范化后 SHA-256）
- `store.py`    存储适配层：治理元数据的读写全部走仓储方法，**业务代码不拼 SQL**，
                将来治理域迁 MySQL 只需替换本模块实现

治理数据（docs / chunks / audit_log / tickets）落在 SQLite
`data/governance/governance.db`——它是"某个程序的私有状态"，只有 agt_python 读写；
Agent 全链路可观测数据属"中台资产"，另走 MySQL（spec §13.2）。
"""

from app.governance.hashing import chunk_fingerprint, normalize_for_hash, sha256_bytes, sha256_text
from app.governance.store import GovernanceRepository, SqliteGovernanceStore, get_store, now_iso

__all__ = [
    "GovernanceRepository",
    "SqliteGovernanceStore",
    "chunk_fingerprint",
    "get_store",
    "now_iso",
    "normalize_for_hash",
    "sha256_bytes",
    "sha256_text",
]

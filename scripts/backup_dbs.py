#!/usr/bin/env python
"""手动跑一轮数据备份 / 查看备份清单（企业级落地规约 E-7，判据 C4）。

为什么还要一个脚本（服务里已经有定时任务和 API）
------------------------------------------------
"坏了要能自己修"的前提是**服务挂了也能备份**。定时任务跑在 8000 进程里，
进程起不来时它一并消失；而这个脚本不依赖服务在跑，只要磁盘上的库还在，
就能在"上线前备份""迁移前备份""服务起不来时抢救"这三种最要紧的场景里用。

它与服务端**共用同一个 `app.services.backup_service`**——不存在"脚本一套、
服务一套"的第二实现（那样两边迟早不一致，而备份不一致的代价是恢复时才发现）。

用法
----
    # 备份到产品数据目录下的 data/backups/<今天>/
    python scripts/backup_dbs.py

    # 只看看现在有哪些备份（不写盘）
    python scripts/backup_dbs.py --list

    # 备份别的数据目录（换机器/演练用），--json 便于脚本消费
    python scripts/backup_dbs.py --data-root D:/other/data --json

退出码：0 = 备份成功（至少一个库校验通过）；1 = 失败或没有可备份的库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 本脚本在仓库根的 scripts/ 下（规约 §4 E-7 指定的路径），
# 而 `app` 包在 agt_python/ 下——**要加的是 agt_python/，不是仓库根**。
REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "agt_python"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))


def main() -> int:
    ap = argparse.ArgumentParser(description="数据备份（E-7）")
    ap.add_argument("--list", action="store_true", help="只列出已有备份，不做备份")
    ap.add_argument("--data-root", default="", help="覆盖 DATA_ROOT（默认 agt_python/data）")
    ap.add_argument("--force", action="store_true",
                    help="强制重备当天（默认每日幂等：当天已有好备份就复用，不覆盖演练报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本消费）")
    args = ap.parse_args()

    # ⚠️ 必须在 import app.* 之前设：数据目录在模块 import 时就求值（见 app/core/paths.py）
    if args.data_root:
        import os
        os.environ["DATA_ROOT"] = args.data_root

    from app.core.paths import describe
    from app.services.backup_service import list_backups, run_backup, target_databases

    if args.list:
        info = list_backups()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        print(f"备份根目录：{info['root']}（保留 {info['keep']} 份，共 {info['count']} 份）")
        if not info["backups"]:
            print("  还没有任何备份。跑一次：python scripts/backup_dbs.py")
            return 0
        for b in info["backups"]:
            flag = "✓" if b.get("verified") else "!"
            print(f"  {flag} {b['date']}  {b.get('generatedAt') or '(无清单)'}  "
                  f"库 {b.get('databases', '?')} 个 / 校验通过 {b.get('verified', '?')} / "
                  f"{round(int(b.get('bytes') or 0) / 1024)} KiB / {b.get('rowCount', '?')} 行")
        return 0

    print(f"数据目录：{describe()}")
    print("待备份：")
    for db in target_databases():
        exists = "存在" if db.path.exists() else "不存在（将跳过）"
        print(f"  - {db.name:<16} {db.label:<10} {exists}  {db.path}")

    report = run_backup(operator="cli", force=args.force)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1

    print()
    if report.get("reusedExisting"):
        print(f"结果：复用今日已有备份（{report.get('generatedAt')}）—— {report.get('message')}")
        print(f"落盘：{report.get('dir')}")
        return 0
    print(f"结果：{'成功' if report.get('ok') else '失败'} | {report.get('message')}")
    print(f"落盘：{report.get('dir')}")
    print(f"耗时：{report.get('elapsedMs')} ms")
    for d in report.get("databases", []):
        if d.get("integrity") == "ok":
            print(f"  ✓ {d['name']:<16} {int(d['bytes']) / 1024:8.1f} KiB  "
                  f"{d['rowCount']:>6} 行  sha256={d['sha256'][:12]}…  "
                  f"journal={d.get('journalMode')}")
        else:
            print(f"  ✗ {d['name']:<16} {d.get('skipped') or d.get('integrity')}")
    if report.get("removed"):
        print(f"滚动清理：删除过期备份 {', '.join(report['removed'])}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

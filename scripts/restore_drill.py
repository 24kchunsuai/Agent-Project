#!/usr/bin/env python
"""恢复演练：证明"这份备份真的能恢复"（企业级落地规约 E-7，判据 C4）。

为什么必须有它
--------------
C4 的原话是：**"删掉一份数据，他能从备份恢复；且这份备份是演练过的，
不是'有脚本'"**。`backup_dbs.py` 只能证明"备份产生了"；
"这份备份能不能恢复"是另一件事，必须**真做一遍**才有答案。
没演练过的备份最典型的失效方式：文件在、`integrity_check` 也 ok，
但恢复出来的库少了一天的数据（因为备份时刻取的是不一致的快照）。

本脚本做的四件事（每一步都是可验证的断言，不是"跑完就报成功"）
--------------------------------------------------------------
1. **恢复**：把最新一份备份的快照按原目录结构放回一个**临时数据目录**；
2. **完整性**：每个库 `PRAGMA integrity_check`，且恢复出的文件 sha256 与
   备份清单里记的一致（证明"文件没在复制中坏掉"）；
3. **逐表行数比对**：恢复后的每张表行数 vs 备份清单里记的行数，
   **逐表相等**才算过（这一条才是"数据没丢"的真正证据）；
4. **应用读得出**：把 `DATA_ROOT` 指向恢复目录，用**应用自己的 store**
   （治理 / 埋点 / 会话记忆 / 用户画像）各做一次真实业务读取——
   证明"不只是 sqlite 能打开，而是当前版本的应用能正常打开它"。

顺序有讲究：**行数比对必须在应用读取之前做**。应用打开库时会跑
`CREATE TABLE IF NOT EXISTS` 与幂等迁移，先读会被这些动作掩盖掉真实差异。

安全护栏
--------
- 恢复目标必须在产品数据目录**之外**，且脚本**只写临时目录**
  （产品数据目录一个字节都不动；备份目录只追加一份演练报告）。
- 演练结束默认删除临时目录（`--keep` 保留，供排查）。

用法
----
    python scripts/restore_drill.py                  # 演练最新一份备份
    python scripts/restore_drill.py --date 2026-09-24
    python scripts/restore_drill.py --backup-root D:/usb/backups --keep --json
    python scripts/restore_drill.py --serve --port 8001   # 额外起隔离服务冒烟（需 Milvus）

退出码：0 = 演练全部通过（"这份备份可以恢复"成立）；1 = 有断言失败；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 仓库根（本脚本在 scripts/ 下）
PY_DIR = ROOT / "agt_python"                        # `app` 包所在目录
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

PRODUCT_DATA_DIR = PY_DIR / "data"                  # 产品数据目录（DATA_ROOT 默认值）


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        return {n: int(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names}
    finally:
        conn.close()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _pick_backup(root: Path, date: str) -> Path | None:
    if not root.is_dir():
        return None
    if date:
        d = root / date
        return d if d.is_dir() else None
    cands = sorted((d for d in root.iterdir()
                    if d.is_dir() and (d / "manifest.json").is_file()),
                   key=lambda d: d.name, reverse=True)
    return cands[0] if cands else None


def _app_reads(restored_names: set[str], restore_dir: Path) -> list[dict]:
    """用**应用自己的 store** 在恢复目录上做真实读取。

    这些 store 都是模块级单例，路径在 import 时按 `DATA_ROOT` 求值——
    而 `DATA_ROOT` 已在本脚本 import app.* **之前**指向恢复目录，
    所以这里读到的就是恢复出来的那份数据。

    没被恢复的库（备份时本就不存在）不做探针：**不许顺手把它建出来**，
    否则"库不存在"这件事就被探针自己掩盖了（与 E-3 探针零副作用同一条规矩）。

    ⚠️ 三处 API 是 **property 不是方法**（`db_path` / `available`），
    写成 `db_path()` 会得到 `TypeError: 'WindowsPath' object is not callable`——
    首次演练就踩到了，写在这里省得下次再踩。
    """
    from app.services.backup_service import target_databases

    rel_by_name = {d.name: d.rel for d in target_databases()}
    out: list[dict] = []

    def probe(name: str, fn) -> None:
        try:
            out.append({"name": name, "ok": True, "result": fn()})
        except Exception as e:                      # noqa: BLE001  演练要出报告，不中断
            out.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"})

    if "governance" in restored_names:
        def _gov():
            from app.governance.store import get_store
            store = get_store()
            return {"dbPath": str(store.db_path), "countsByState": store.count_docs_by_state()}
        probe("governance", _gov)

    if "observability" in restored_names:
        def _obs():
            from app.services.telemetry import telemetry
            o = telemetry.overview()
            return {"overviewKeys": len(o), "counters": telemetry.counters_snapshot()}
        probe("observability", _obs)

    if "session_memory" in restored_names:
        def _mem():
            from app.services.memory_sqlite import get_sqlite_store
            s = get_sqlite_store()
            ids = s.load_all_session_ids()
            return {"degraded": s.degraded, "sessionIds": len(ids), "stats": s.stats()}
        probe("session_memory", _mem)

    if "user_profile" in restored_names:
        def _prof():
            """画像库多做一步：挑一个真实 user_id 走 `get_all()` 读回记录。

            只调 `available` 太弱（"连接没断"不等于"数据读得出"）。
            产品库当前可能是 0 行，那就如实报 0——不为了好看去造数据。
            """
            from app.services.user_profile import profile_store
            path = restore_dir / rel_by_name["user_profile"]
            users: list[int] = []
            if path.is_file():
                with sqlite3.connect(str(path)) as c:
                    users = [int(r[0]) for r in c.execute(
                        "SELECT DISTINCT user_id FROM agent_memory_profile").fetchall()]
            rows = profile_store.get_all(users[0]) if users else []
            return {"available": profile_store.available, "users": len(users),
                    "sampleUserRows": len(rows)}
        probe("user_profile", _prof)

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="恢复演练（E-7）")
    ap.add_argument("--backup-root", default="", help="备份根目录（默认 <DATA_ROOT>/backups）")
    ap.add_argument("--date", default="", help="指定备份日期 YYYY-MM-DD（默认最新一份）")
    ap.add_argument("--keep", action="store_true", help="保留临时恢复目录（排查用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--no-report", action="store_true", help="不往备份目录写演练报告")
    ap.add_argument("--serve", action="store_true",
                    help="额外起一个隔离服务读恢复库并冒烟（需 Milvus 在线）")
    ap.add_argument("--port", type=int, default=8001, help="--serve 的端口（默认 8001）")
    args = ap.parse_args()

    t_all = time.time()

    # ---------- 1. 定位备份（此步只用标准库：此时还没到设 DATA_ROOT 的时候）----------
    backup_root = (Path(args.backup_root).expanduser().resolve() if args.backup_root
                   else PRODUCT_DATA_DIR / "backups")
    backup_dir = _pick_backup(backup_root, args.date.strip())
    if backup_dir is None:
        print(f"✗ 找不到备份：{backup_root}"
              + (f"（指定日期 {args.date}）" if args.date else "（目录为空或不存在）"))
        print("  先跑一次：python scripts/backup_dbs.py")
        return 2
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"✗ 备份缺清单文件：{manifest_path}（无法比对行数，拒绝演练）")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {e["name"]: e for e in manifest.get("databases", [])}

    # ---------- 2. 建恢复目录并**在 import app.* 之前**定下 DATA_ROOT ----------
    restore_dir = Path(tempfile.mkdtemp(prefix="agt-restore-drill-"))
    if _under(restore_dir, PRODUCT_DATA_DIR):                # 理论上不可能，防御性断言
        print(f"✗ 拒绝运行：恢复目录落在产品数据目录内（{restore_dir}）")
        return 2
    os.environ["DATA_ROOT"] = str(restore_dir)

    from app.services.backup_service import target_databases   # 现在才能 import

    print("=" * 78)
    print("恢复演练")
    print(f"  备份目录   : {backup_dir}")
    print(f"  备份生成于 : {manifest.get('generatedAt')}")
    print(f"  恢复目录   : {restore_dir}（临时；{'保留' if args.keep else '结束即删'}）")
    print("=" * 78)

    t0 = time.time()
    checks: list[dict] = []
    failures: list[str] = []
    restored: set[str] = set()
    notes: list[str] = []

    try:
        # ---------- 3. 恢复 + 校验（在应用读取之前完成）----------
        for db in target_databases():
            entry = entries.get(db.name) or {}
            snapshot_name = entry.get("snapshot")
            row = {"name": db.name, "label": db.label, "relPath": db.rel}

            if not snapshot_name:
                row["restored"] = False
                row["reason"] = entry.get("skipped") or "备份清单里没有这个库"
                notes.append(f"{db.name}: 备份时就不存在（{row['reason']}），本轮不恢复也不比对")
                checks.append(row)
                continue

            src = backup_dir / snapshot_name
            if not src.is_file():
                row["restored"] = False
                row["reason"] = f"清单引用的快照缺失：{src.name}"
                failures.append(f"{db.name}: 快照文件缺失（{src.name}）")
                checks.append(row)
                continue

            dst = restore_dir / db.rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

            integrity = _integrity(dst)
            actual = _table_counts(dst)
            expected = {k: int(v) for k, v in (entry.get("tables") or {}).items()}
            row.update({
                "restored": True,
                "bytes": dst.stat().st_size,
                "integrity": integrity,
                "sha256Match": _sha256(dst) == entry.get("sha256"),
                "tables": actual,
                "expectedTables": expected,
                "rowCountMatch": actual == expected,
                "expectedRowCount": entry.get("rowCount"),
                "actualRowCount": sum(v for v in actual.values() if v > 0),
            })
            restored.add(db.name)

            if integrity != "ok":
                failures.append(f"{db.name}: integrity_check = {integrity}")
            if not row["sha256Match"]:
                failures.append(f"{db.name}: 恢复后 sha256 与备份清单不一致（文件在复制中变了）")
            if not row["rowCountMatch"]:
                diff = {k: (expected.get(k, 0), actual.get(k, 0))
                        for k in set(expected) | set(actual)
                        if expected.get(k, 0) != actual.get(k, 0)}
                failures.append(f"{db.name}: 行数比对不一致 {diff}")
            checks.append(row)

        restore_ms = round((time.time() - t0) * 1000)

        # ---------- 4. 应用读得出 ----------
        app_reads = _app_reads(restored, restore_dir)
        for r in app_reads:
            if not r["ok"]:
                failures.append(f"应用读取失败 {r['name']}: {r['error']}")

        # ---------- 5. 可选：起隔离服务冒烟 ----------
        smoke: dict = {"ran": False}
        if args.serve:
            smoke = _serve_smoke(restore_dir, args.port)

        report = {
            "ok": not failures,
            "drilledAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backupDate": backup_dir.name,
            "backupDir": str(backup_dir),
            "backupGeneratedAt": manifest.get("generatedAt"),
            "restoreDir": str(restore_dir),
            "restoreMs": restore_ms,
            "totalMs": round((time.time() - t_all) * 1000),
            "checks": checks,
            "appReads": app_reads,
            "smoke": smoke,
            "failures": failures,
            "notes": notes,
        }

        # ---------- 6. 出报告 ----------
        if not args.no_report:
            (backup_dir / "drill-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_report(report)
        return 0 if report["ok"] else 1
    finally:
        if not args.keep:
            shutil.rmtree(restore_dir, ignore_errors=True)


def _serve_smoke(restore_dir: Path, port: int) -> dict:
    """起隔离服务读恢复库并打健康端点（证明"整条应用能在恢复数据上启动"）。

    需要 Milvus 在线——隔离服务启动时会连向量库；连不上就如实记 `skipped`，
    不让"Milvus 没起"变成"备份不能恢复"的假失败。
    """
    import subprocess
    import urllib.error
    import urllib.request

    env = dict(os.environ, DATA_ROOT=str(restore_dir), GOV_SCHEDULER_ENABLED="false")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(PY_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(40):                      # 最多等 20s
            if proc.poll() is not None:
                return {"ran": True, "ok": False,
                        "error": f"隔离服务启动即退出（退出码 {proc.returncode}），"
                                 f"常见原因：Milvus 未启动"}
            time.sleep(0.5)
            try:
                with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=2) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return {"ran": True, "ok": True, "health": body.get("data") or body}
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return {"ran": True, "ok": False, "error": "等待隔离服务就绪超时（20s）"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _print_report(r: dict) -> None:
    print()
    print(f"演练结果：{'✓ 通过（这份备份可以恢复）' if r['ok'] else '✗ 未通过'}")
    print(f"  恢复耗时   : {r['restoreMs']} ms")
    print(f"  备份时间   : {r['backupGeneratedAt']}")
    print()
    print(f"  {'库':<16} {'完整性':<8} {'sha256':<8} {'行数比对':<10} 行数")
    print(f"  {'-' * 16} {'-' * 8} {'-' * 8} {'-' * 10} {'-' * 8}")
    for c in r["checks"]:
        if not c.get("restored"):
            print(f"  {c['name']:<16} {'—':<8} {'—':<8} {'跳过':<10} {c.get('reason')}")
            continue
        print(f"  {c['name']:<16} {c['integrity']:<8} "
              f"{'一致' if c['sha256Match'] else '不一致':<8} "
              f"{'逐表一致' if c['rowCountMatch'] else '不一致':<10} {c['actualRowCount']}")
    print()
    print("  应用读取（用应用自己的 store 打开恢复库）：")
    for a in r["appReads"]:
        print(f"    {'✓' if a['ok'] else '✗'} {a['name']:<16} "
              f"{json.dumps(a.get('result') or a.get('error'), ensure_ascii=False)[:110]}")
    if r.get("smoke", {}).get("ran"):
        s = r["smoke"]
        print(f"  隔离服务冒烟: {'✓ 启动并响应 /v1/health' if s.get('ok') else '✗ ' + str(s.get('error'))}")
    for n in r.get("notes", []):
        print(f"  备注: {n}")
    if r["failures"]:
        print()
        print("  失败项：")
        for f in r["failures"]:
            print(f"    ✗ {f}")


if __name__ == "__main__":
    raise SystemExit(main())

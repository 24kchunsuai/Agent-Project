"""数据备份（企业级落地规约 §4 E-7，判据 C4「可恢复」）。

C4 的原话是：**"删掉一份数据，他能从备份恢复；且这份备份是演练过的，
不是'有脚本'"**。所以本模块只负责"产出可信的备份"，
"这份备份真的能恢复"由 `scripts/restore_drill.py` 负责证明——两者缺一不可。

## 为什么不用"裸拷 db + -wal + -shm"

规约 §4 E-7 原文写的是"连同 `-wal` / `-shm` 一起复制"。实现时改为
**SQLite 在线备份 API**（`sqlite3.Connection.backup`），理由：

1. **裸拷在"服务正在写"时会拷到半个事务**。WAL 模式下一次提交可能跨 `-wal` 的
   多个帧；文件系统复制不是原子的，读到的 `db` 与读到的 `wal` 可能来自不同时刻。
   这种备份**能通过 `integrity_check`（页结构完整）却丢数据**——最危险的失败模式：
   校验全绿灯，恢复时才发现少了一天的会话。
2. 在线备份走的是 SQLite 自己的页级读锁协议，产出**单文件、自洽**的快照，
   不需要携带 `-wal`/`-shm`，恢复时也少一类"忘了带 wal"的坑。
3. 源库以 `PRAGMA query_only=ON` 打开——**备份过程绝不改动产品库**（不 checkpoint、
   不修改、不建库）。

源库的 `-wal` / `-shm` 现状仍会记进 manifest（`sourceWalBytes` 等），
供排查"备份时刻源库处于什么状态"，只是不作为恢复的依赖。此偏离已记档。

## 铁律（沿用项目既有口径）

- **不建库**：某个库不存在就如实记 `present: false`，绝不顺手创建
  （与 E-3 健康探针同一条规矩：探针零副作用）。
- **每日幂等**：当天已有且校验通过的备份不再重写（`force=True` 才强制重备）。
  它保护的是"演练过的那份不被换掉"——服务重启会让定时任务的 `initial_delay`
  再走一遍，不幂等的话每次重启都把当天备份换新、`drill-report.json` 随之失效，
  "演练过的备份"就退回到"未演练"。
- **失败的备份不许覆盖成功的备份**：先写 `<root>/.staging`，全部校验通过后才
  原子换名到 `YYYY-MM-DD/`。某一步失败 → 当日已有备份原样保留。
- **滚动清理只删自家目录**：只删 `<root>` 下形如 `YYYY-MM-DD` **且内含
  `manifest.json`** 的目录；其余一律不碰（见 `_prune`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

#: 备份目录名的唯一合法形状。滚动清理**只**认它，其余目录一律不碰。
_BACKUP_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 暂存目录名（不以日期开头 → 天然落在滚动清理的识别范围之外）
_STAGING_NAME = ".staging"

#: 备份清单文件名（也是"这目录是我们建的"的凭证）
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class TargetDatabase:
    """一个待备份的库。逻辑名同时用作备份文件名与 manifest 的键。

    :param rel: 相对 `DATA_ROOT` 的落点（如 `governance/governance.db`）。
                备份目录是**扁平**的（`governance.db` 等），
                恢复时必须按 `rel` 放回原来的目录结构——恢复演练依赖它，
                所以它不能由 `path.relative_to(DATA_DIR)` 现算：
                库里若配了绝对路径（`gov_db_path`），现算会直接抛异常。
    """

    name: str
    label: str
    describe: str
    path: Path
    rel: str


# ---------------------------------------------------------------- 四库发现


def _resolve(configured: str, default_rel: str) -> Path:
    """按各库**自己**的规则解析路径（配置优先，空则用 DATA_DIR 下的默认值）。

    这里刻意复刻上游模块的规则而不是 import 它们的私有方法：
    四个库分属三个模块，它们的路径解析时机都在"实例化时"，而备份要能在
    **服务未启动/实例不存在**时也能算出路径（何况探针不该有副作用）。
    复刻的风险由单测兜住——`tests/test_backup.py` 会断言本函数的结果与
    四个真实 store 的路径**逐字相等**，所以规则漂移会让门禁变红，而不是静默备份错文件。
    """
    raw = (configured or "").strip()
    return Path(raw) if raw else DATA_DIR / default_rel


def target_databases() -> list[TargetDatabase]:
    """四个落盘库的清单（顺序即 manifest 与报告里的展示顺序）。

    库与归属模块的对应关系（改路径解析规则时四处一起改）：

    ===============  ==========================  =================================
    逻辑名            归属                        解析规则出处
    ===============  ==========================  =================================
    governance       app/governance/store.py     settings.gov_db_path
    observability    app/services/telemetry.py   settings.telemetry_db_path
    session_memory   app/services/memory_sqlite  DATA_DIR/memory/session_memory.db
    user_profile     app/services/user_profile   DATA_DIR/memory/user_profile.db
    ===============  ==========================  =================================
    """
    return [
        TargetDatabase("governance", "治理账本",
                       "文档治理元数据 / 版本链 / 巡检工单 / 治理审计流水",
                       _resolve(settings.gov_db_path, "governance/governance.db"),
                       "governance/governance.db"),
        TargetDatabase("observability", "数据中台埋点库",
                       "四类事件表 + DWS(metrics_daily) + 反馈闭环",
                       _resolve(settings.telemetry_db_path, "metrics/observability.db"),
                       "metrics/observability.db"),
        TargetDatabase("session_memory", "会话记忆库",
                       "会话消息持久化（M-1：重启不丢记忆）",
                       DATA_DIR / "memory" / "session_memory.db",
                       "memory/session_memory.db"),
        TargetDatabase("user_profile", "用户画像库",
                       "用户偏好 KV（M-5）",
                       DATA_DIR / "memory" / "user_profile.db",
                       "memory/user_profile.db"),
    ]


def backup_root() -> Path:
    """备份根目录（默认 `DATA_ROOT/backups`，可用 settings.backup_dir 外迁）。"""
    raw = (settings.backup_dir or "").strip()
    return Path(raw) if raw else DATA_DIR / "backups"


def keep_count() -> int:
    return max(1, int(settings.backup_keep_days))


# ---------------------------------------------------------------- 单库快照


def _file_state(path: Path) -> dict[str, Any]:
    """文件现状（不存在时不报错，如实返回 present=False）。"""
    if not path.exists():
        return {"present": False}
    st = path.stat()
    return {
        "present": True,
        "bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """全部业务表的行数（`sqlite_%` 内部表与 `_wal` 影子表不计）。

    行数是"恢复演练"的比对基准：备份时记一份，恢复后再数一遍，
    两边逐表相等才算"这条库真的被完整搬过来了"。
    """
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    out: dict[str, int] = {}
    for n in names:
        try:
            out[n] = int(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0])
        except sqlite3.Error:  # noqa: PERF203  坏表不能拖垮整条库的备份
            out[n] = -1
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(src: Path, dst: Path) -> dict[str, Any]:
    """把 `src` 一致地备份到 `dst`，并就地校验。返回该库的 manifest 条目。

    :raises sqlite3.Error: 源库打不开/备份失败/校验不通过——由调用方决定整体是否算失败
    """
    timeout = max(1.0, int(settings.backup_busy_timeout_ms) / 1000)
    dst.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(str(src), timeout=timeout)
    try:
        # 只读语义：备份是"读"，任何情况下都不许改到产品库
        src_conn.execute("PRAGMA query_only=ON")
        src_conn.execute(f"PRAGMA busy_timeout={int(settings.backup_busy_timeout_ms)}")
        # journal_mode 只是记录现状（用于排查），不改它
        journal_mode = str(src_conn.execute("PRAGMA journal_mode").fetchone()[0])

        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)      # ← 在线备份：页级读锁，跨进程安全
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # ---- 校验（对**备份副本**做，不碰源库）----
    ver_conn = sqlite3.connect(str(dst))
    try:
        integrity = str(ver_conn.execute("PRAGMA integrity_check").fetchone()[0])
        tables = _table_counts(ver_conn)
    finally:
        ver_conn.close()

    if integrity != "ok":
        raise sqlite3.DatabaseError(f"{src.name} 备份后 integrity_check 未通过：{integrity}")

    return {
        "name": "",                    # 由调用方填
        "snapshot": dst.name,
        "bytes": dst.stat().st_size,
        "sha256": _sha256(dst),
        "integrity": integrity,
        "journalMode": journal_mode,
        "tables": tables,
        "rowCount": sum(v for v in tables.values() if v > 0),
    }


def _backup_one(db: TargetDatabase, staging: Path) -> dict[str, Any]:
    """备份一个库；不存在则如实记为 skipped（**不建库**）。"""
    src_state = _file_state(db.path)
    entry: dict[str, Any] = {
        "name": db.name,
        "label": db.label,
        "describe": db.describe,
        "source": str(db.path),
        "present": bool(src_state.get("present")),
        "sourceBytes": src_state.get("bytes"),
        "sourceMtime": src_state.get("mtime"),
        # 源库 WAL 现状：只作排查信息（本方案不依赖携带 -wal/-shm）
        "sourceWalBytes": _file_state(Path(str(db.path) + "-wal")).get("bytes"),
        "sourceShmBytes": _file_state(Path(str(db.path) + "-shm")).get("bytes"),
    }
    if not src_state.get("present"):
        entry["skipped"] = "源库不存在（不创建）"
        return entry

    dst = staging / f"{db.name}.db"
    entry.update(_snapshot(db.path, dst))
    entry["name"] = db.name
    return entry


# ---------------------------------------------------------------- 滚动清理


def _prune(root: Path, keep: int) -> list[str]:
    """保留最近 `keep` 份，返回被清理的目录名。

    安全护栏（三条，缺一条都可能删错东西）：

    1. 只认**直接子目录**里名字形如 `YYYY-MM-DD` 的（`.staging`、日志、临时目录天然不匹配）；
    2. 目录内**必须有 `manifest.json`**——这目录是我们建的凭证，
       手工放进去的备份（没有清单）一律不动；
    3. `remove` 只对该目录自身调用一次，不做通配、不递归到 `root` 之外。
    """
    if not root.is_dir():
        return []
    candidates = sorted(
        (d for d in root.iterdir()
         if d.is_dir() and _BACKUP_DIR_RE.match(d.name) and (d / MANIFEST_NAME).is_file()),
        key=lambda d: d.name,
        reverse=True,
    )
    removed: list[str] = []
    for stale in candidates[keep:]:
        try:
            shutil.rmtree(stale)
            removed.append(stale.name)
            logger.info("[BACKUP] 滚动清理：删除过期备份 %s", stale.name)
        except OSError as e:  # noqa: PERF203  删不掉不该让整轮备份失败
            logger.warning("[BACKUP] 滚动清理失败（保留不删）| dir=%s err=%s", stale.name, e)
    return removed


# ---------------------------------------------------------------- 对外主入口


def _existing_ok_backup(final: Path) -> dict[str, Any] | None:
    """当日已有、且**校验全部通过**的那份备份清单（没有/不完整则 None）。

    用于"每日幂等"：定时任务重跑（服务重启后 `initial_delay` 会再走一遍）不该
    把当天那份已经**演练过**的备份换掉——换掉的代价是 `drill-report.json` 一起失效，
    于是"演练过的备份"又退回到"未演练"。
    """
    mf = final / MANIFEST_NAME
    if not mf.is_file():
        return None
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dbs = m.get("databases") or []
    if not dbs or any(d.get("integrity") != "ok" for d in dbs):
        return None                     # 半成品不算，照常重备
    return m


def _run_backup_impl(operator: str = "system", *, force: bool = False) -> dict[str, Any]:
    """跑一轮完整备份，返回报告（同时落 `<root>/<date>/manifest.json`）。

    :param force: `False`（默认）时**每日幂等**——当天已有且校验通过的备份就直接复用，
                  不重写、不动 `drill-report.json`；`True` 时无条件重备
                  （CLI `--force` / API `?force=true`）。定时任务永远用默认值。

    报告结构即 API / 前端 / 演练脚本共同消费的契约，键名不轻易改。
    """
    t0 = time.time()
    root = backup_root()
    date = datetime.now().strftime("%Y-%m-%d")
    staging = root / _STAGING_NAME
    report: dict[str, Any] = {
        "ok": False, "date": date, "dir": str(root / date), "operator": operator,
        "keep": keep_count(), "databases": [], "skipped": [], "removed": [],
        "reusedExisting": False, "message": "",
    }

    # ---- 每日幂等：当天已有好备份就不动它（保护演练报告）----
    if not force:
        prev = _existing_ok_backup(root / date)
        if prev is not None:
            report.update({
                "ok": True, "reusedExisting": True,
                "generatedAt": prev.get("generatedAt"),
                "databases": prev.get("databases", []),
                "message": f"今日已有备份（{prev.get('generatedAt')}），未重复备份；"
                           "如需强制重备：CLI 加 --force / API 加 ?force=true",
            })
            report["elapsedMs"] = round((time.time() - t0) * 1000)
            logger.info("[BACKUP] 跳过：今日已有备份 | dir=%s", report["dir"])
            return report

    # 暂存区：清掉上次失败留下的残留（`_STAGING_NAME` 不是日期目录，绝不在滚动清理范围内）
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    swapped = False          # 是否已完成"staging → 当日目录"的换名

    try:
        for db in target_databases():
            try:
                entry = _backup_one(db, staging)
            except (sqlite3.Error, OSError) as e:
                entry = {
                    "name": db.name, "label": db.label, "describe": db.describe,
                    "source": str(db.path), "present": True, "skipped": f"{type(e).__name__}: {e}",
                    "integrity": "error",
                }
                report["skipped"].append({"name": db.name, "reason": entry["skipped"]})
                logger.error("[BACKUP] 库备份失败 | db=%s err=%s", db.name, e)
            report["databases"].append(entry)

        backed_up = [d for d in report["databases"] if d.get("integrity") == "ok"]
        if not backed_up:
            report["message"] = "四个库均未能备份（可能都还不存在）；已保留原有备份不动"
            return report

        manifest = {
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generatedAtUtc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "dataDir": str(DATA_DIR),
            "keep": report["keep"],
            "databases": report["databases"],
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ---- 原子换名：先删同名旧目录（只可能是昨天/今天更早的一版），再改名 ----
        final = root / date
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)
        swapped = True

        report["ok"] = True
        report["removed"] = _prune(root, report["keep"])
        report["message"] = (
            f"{len(backed_up)}/{len(report['databases'])} 个库已备份并校验通过"
            + (f"；{len(report['skipped'])} 个跳过（{', '.join(s['name'] for s in report['skipped'])}）"
               if report["skipped"] else "")
        )
        return report
    finally:
        # 没换成名 = 这轮没成，暂存区里的残片不能留：它不是日期目录，
        # 滚动清理永远看不到它 → 每失败一次就永久多一堆半成品（2026-09-24 单测实测）。
        # 失败原因已在报告与日志里，不需要靠残片来诊断。
        if not swapped:
            shutil.rmtree(staging, ignore_errors=True)
        report["elapsedMs"] = round((time.time() - t0) * 1000)
        logger.info("[BACKUP] 备份结束 | ok=%s dir=%s elapsedMs=%s msg=%s",
                    report["ok"], report["dir"], report["elapsedMs"], report["message"])


def run_backup(operator: str = "system", *, force: bool = False) -> dict[str, Any]:
    """跑一轮备份并**留审计**（E-8）。

    审计写在这一层而不是 `_run_backup_impl` 里：后者有三个 return 分支
    （复用当日已有 / 四个库都没备成 / 正常完成），逐个加审计迟早会漏一个。
    包一层则"每调用一次恰好一条"，且时间戳与结果口径统一。

    审计失败绝不反过来打断备份——`governance_service.audit` 自身吞异常。
    """
    report = _run_backup_impl(operator, force=force)
    _audit_backup(report, operator)
    return report


def _audit_backup(report: dict[str, Any], operator: str) -> None:
    """把备份结果记进治理域审计表（target=backup）。"""
    try:
        # 延迟导入：backup_service 被 scheduler 与 ops 端点共同引用，
        # 模块级导入 governance_service 会把治理域拖进备份的启动路径
        from app.services.governance_service import (  # noqa: PLC0415
            GovAction, TARGET_BACKUP, governance_service,
        )
        ok_count = sum(1 for d in report.get("databases", [])
                       if d.get("integrity") == "ok")
        total = len(report.get("databases", []))
        governance_service.audit(
            GovAction.BACKUP_RUN,
            operator=operator or "system",
            target=TARGET_BACKUP,
            result="SUCCESS" if report.get("ok") else "FAILED",
            detail={
                "ok": bool(report.get("ok")),
                "reusedExisting": bool(report.get("reusedExisting")),
                "backedUp": f"{ok_count}/{total}",
                "dir": report.get("dir"),
                "elapsedMs": report.get("elapsedMs"),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[BACKUP] 审计写入失败（已忽略）| err=%s", e)


def list_backups() -> dict[str, Any]:
    """备份清单（新的在前）：供状态端点与运维查看。"""
    root = backup_root()
    items: list[dict[str, Any]] = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not (d.is_dir() and _BACKUP_DIR_RE.match(d.name)):
                continue
            mf = d / MANIFEST_NAME
            entry: dict[str, Any] = {"date": d.name, "dir": str(d), "hasManifest": mf.is_file()}
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    dbs = m.get("databases") or []
                    entry.update({
                        "generatedAt": m.get("generatedAt"),
                        "databases": len(dbs),
                        "verified": sum(1 for x in dbs if x.get("integrity") == "ok"),
                        "bytes": sum(int(x.get("bytes") or 0) for x in dbs),
                        "rowCount": sum(int(x.get("rowCount") or 0) for x in dbs),
                    })
                except (OSError, json.JSONDecodeError) as e:
                    entry["manifestError"] = f"{type(e).__name__}: {e}"
            items.append(entry)
    return {
        "root": str(root),
        "keep": keep_count(),
        "enabled": bool(settings.backup_enabled),
        "intervalSec": int(settings.backup_interval_sec),
        "count": len(items),
        "backups": items,
    }

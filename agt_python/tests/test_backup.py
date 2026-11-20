"""E-7 数据备份单测（企业级落地规约 §4 E-7 验收标准：单测覆盖复制/校验/滚动逻辑）。

覆盖：
1. **在线备份能取到只存在于 `-wal` 里的数据** ← 这条是本模块存在的理由，
   也是"为什么不用裸拷 db+wal+shm"的**可执行证据**（对照用例见 T2）；
2. 裸拷主库文件会**丢掉**尚未 checkpoint 的数据（反面证明，不是猜测）；
3. 快照做 `integrity_check` 且带 sha256；
4. 库不存在 → 如实跳过且**不创建**（探针零副作用，与 E-3 同一条规矩）；
5. `run_backup` 端到端：产出当日目录 + 清单 + 全部 integrity ok；
6. **本轮失败不许破坏上一份成功的备份**（staging + 原子换名）；
6b. **每日幂等**：同一天再跑（服务重启会触发）复用当日备份、**不动演练报告**；
6c. `force=True` 才是"重备今天"，演练报告随之失效（有意的）；
7. 滚动清理的三条护栏：只删日期目录、只删带清单的、多余的按最老先删；
8. **路径解析不漂移**：`target_databases()` 与四个真实 store 的路径逐字相等
   （备份到错的文件是最难发现的故障——它"成功"了，只是备了个别的东西）；
9. 端点鉴权：无 key → 4010（备份会写盘，不该匿名可触发）；`force` 参数生效。

数据目录由 tests/conftest.py 隔离到 tmp，本文件不碰产品数据目录。
"""

from __future__ import annotations

import json
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import backup_service as bs

HEADERS = {"X-Agent-Key": settings.agent_shared_key}
client = TestClient(app)


# --------------------------------------------------------------- 工具


def _make_wal_db(path: Path) -> sqlite3.Connection:
    """建一个 WAL 模式的库，并**保持连接打开**（否则关闭时会 checkpoint，数据就落到主库了）。

    返回的连接必须由调用方关闭；关闭之前，`-wal` 里才有"尚未落到主库"的数据。
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    assert Path(str(path) + "-wal").exists(), "前提不成立：WAL 文件没生成"
    return conn


def _table_names(path: Path) -> list[str]:
    with sqlite3.connect(str(path)) as c:
        return [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]


def _one_db(tmp_path: Path) -> bs.TargetDatabase:
    """造一个只含单库的备份目标（让每条用例只测一件事）。

    建表用 `IF NOT EXISTS`：同一个用例里可能被调用两轮（如 T6 先成功一次再失败一次），
    第二次不该因为"表已存在"而挂在准备阶段——那会把"备份被冲掉"的断言带偏成
    "测试自己写错了"。
    """
    src = tmp_path / "src" / "demo.db"
    src.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(src)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS k(v TEXT)")
        c.execute("INSERT INTO k VALUES ('a')")
        c.commit()
    return bs.TargetDatabase("demo", "演示库", "单测用", src, "demo/demo.db")


# --------------------------------------------------------------- 1~3 快照


def test_snapshot_reads_data_that_only_exists_in_wal(tmp_path):
    """T1：数据只在 `-wal` 里时，在线备份仍应拿到完整数据。

    这是本模块选用 SQLite 在线备份 API 的全部理由——裸拷做不到（见 T2）。
    """
    src = tmp_path / "wal.db"
    conn = _make_wal_db(src)
    try:
        dst = tmp_path / "snap.db"
        entry = bs._snapshot(src, dst)
    finally:
        conn.close()

    assert entry["integrity"] == "ok"
    assert entry["tables"]["t"] == 2        # ← 两条都在（它们此刻只存在于 -wal）
    assert entry["rowCount"] == 2
    assert entry["journalMode"].lower() == "wal"
    assert len(entry["sha256"]) == 64
    assert entry["bytes"] == dst.stat().st_size


def test_naive_copy_would_lose_wal_data(tmp_path):
    """T2：反面证明——只拷主库文件，表都读不到。

    这条用例存在的意义是**把设计决定变成可执行证据**：
    如果哪天有人把 `_snapshot` 改回 `shutil.copy2`，T1 会红，而 T2 说明为什么红。
    """
    src = tmp_path / "wal.db"
    conn = _make_wal_db(src)
    try:
        naive = tmp_path / "naive.db"
        shutil.copy2(src, naive)            # 裸拷：不管 -wal / -shm
        assert "t" not in _table_names(naive)
    finally:
        conn.close()


def test_snapshot_does_not_create_missing_source(tmp_path):
    """T3：源库不存在 → 不建库、不报错，如实记 present=False（探针零副作用）。"""
    db = bs.TargetDatabase("ghost", "不存在的库", "", tmp_path / "ghost" / "x.db", "ghost/x.db")
    entry = bs._backup_one(db, tmp_path / "staging")
    assert entry["present"] is False
    assert "不存在" in entry["skipped"]
    assert not db.path.exists()


# --------------------------------------------------------------- 5~6 端到端


def test_run_backup_end_to_end(tmp_path, monkeypatch):
    """T5：一轮完整备份 → 当日目录、清单、每个库 integrity ok。"""
    monkeypatch.setattr(bs, "backup_root", lambda: tmp_path / "backups")
    monkeypatch.setattr(bs, "target_databases", lambda: [_one_db(tmp_path)])

    report = bs.run_backup(operator="pytest")
    assert report["ok"] is True
    assert report["date"] == datetime.now().strftime("%Y-%m-%d")

    day = tmp_path / "backups" / report["date"]
    manifest = json.loads((day / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["databases"][0]["name"] == "demo"
    assert manifest["databases"][0]["integrity"] == "ok"
    assert manifest["databases"][0]["tables"]["k"] == 1
    assert (day / "demo.db").is_file()
    assert "1/1 个库已备份并校验通过" in report["message"]


def test_failed_run_keeps_previous_backup(tmp_path, monkeypatch):
    """T6：本轮失败 → 上一份成功的备份原样保留（先写 staging，校验通过才换名）。"""
    monkeypatch.setattr(bs, "backup_root", lambda: tmp_path / "backups")
    monkeypatch.setattr(bs, "target_databases", lambda: [_one_db(tmp_path)])

    first = bs.run_backup(operator="pytest")
    day = tmp_path / "backups" / first["date"]
    before = (day / "manifest.json").read_text(encoding="utf-8")

    # 让快照在"写盘之后、换名之前"失败。**必须 force=True**：
    # 默认的每日幂等会直接复用当天那份好备份，根本不会走到失败路径。
    def boom(src, dst):
        raise sqlite3.DatabaseError("模拟：备份中途失败")

    monkeypatch.setattr(bs, "_snapshot", boom)
    second = bs.run_backup(operator="pytest", force=True)

    assert second["ok"] is False
    assert second["databases"][0]["skipped"].startswith("DatabaseError")
    assert day.is_dir(), "失败的备份把上一份好的冲掉了"
    assert (day / "manifest.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / "backups" / bs._STAGING_NAME).exists(), "staging 应被清理"


def test_second_run_same_day_reuses_and_keeps_drill_report(tmp_path, monkeypatch):
    """T6b：同一轮里再跑一次（服务重启会触发）→ 复用当日备份，**不动演练报告**。

    这条守的是"演练过的备份"这个概念本身：服务重启会让定时任务的 `initial_delay`
    再走一遍，若每次都重备，`drill-report.json` 就每次失效，
    C4 要求的"这份备份是演练过的"永远成立不了。
    """
    monkeypatch.setattr(bs, "backup_root", lambda: tmp_path / "backups")
    monkeypatch.setattr(bs, "target_databases", lambda: [_one_db(tmp_path)])

    first = bs.run_backup(operator="pytest")
    day = tmp_path / "backups" / first["date"]
    sentinel = day / "drill-report.json"          # 假装跑过演练
    sentinel.write_text('{"ok": true}', encoding="utf-8")
    manifest_before = (day / "manifest.json").read_text(encoding="utf-8")

    second = bs.run_backup(operator="system")

    assert second["ok"] is True
    assert second["reusedExisting"] is True
    assert "已有备份" in second["message"]
    assert sentinel.is_file(), "每日幂等失效：把当天已演练的备份换掉了"
    assert sentinel.read_text(encoding="utf-8") == '{"ok": true}'
    assert (day / "manifest.json").read_text(encoding="utf-8") == manifest_before


def test_force_rewrites_today(tmp_path, monkeypatch):
    """T6c：`force=True` 才是"重备今天"——演练报告随之失效（有意的）。"""
    monkeypatch.setattr(bs, "backup_root", lambda: tmp_path / "backups")
    monkeypatch.setattr(bs, "target_databases", lambda: [_one_db(tmp_path)])

    first = bs.run_backup(operator="pytest")
    day = tmp_path / "backups" / first["date"]
    sentinel = day / "drill-report.json"
    sentinel.write_text('{"ok": true}', encoding="utf-8")

    forced = bs.run_backup(operator="cli", force=True)

    assert forced["ok"] is True
    assert forced["reusedExisting"] is False
    assert not sentinel.exists(), "force 重备后旧演练报告本应失效（对应旧快照）"
    assert (day / "manifest.json").is_file()


# --------------------------------------------------------------- 7 滚动清理


def _fake_backup_dir(root: Path, name: str, *, with_manifest: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if with_manifest:
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    return d


def test_prune_keeps_recent_and_only_own_dirs(tmp_path):
    """T7：保留最近 N 份；非日期目录、无清单目录、散落文件一律不碰。"""
    root = tmp_path / "backups"
    for i in range(1, 17):                       # 16 份日期目录
        _fake_backup_dir(root, f"2026-01-{i:02d}")
    _fake_backup_dir(root, "handmade-backup")     # 非日期名
    _fake_backup_dir(root, "2026-01-17", with_manifest=False)   # 日期名但没清单（不是我们建的）
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / bs._STAGING_NAME).mkdir()             # 暂存目录

    removed = bs._prune(root, keep=14)

    assert removed == ["2026-01-02", "2026-01-01"]      # 只删最老的 2 份
    assert len(removed) == 2
    assert (root / "2026-01-03").is_dir()
    assert (root / "2026-01-17").is_dir(), "无清单的目录不是我们建的，不能删"
    assert (root / "handmade-backup").is_dir()
    assert (root / "notes.txt").is_file()
    assert (root / bs._STAGING_NAME).is_dir()


def test_prune_disabled_when_keep_is_positive_but_few(tmp_path):
    """T7b：份数不足时一份都不删（避免误配 keep 导致清空）。"""
    root = tmp_path / "backups"
    for i in range(1, 4):
        _fake_backup_dir(root, f"2026-02-{i:02d}")
    assert bs._prune(root, keep=14) == []
    assert len(list(root.iterdir())) == 3


# --------------------------------------------------------------- 8 路径不漂移


def test_target_databases_match_real_stores():
    """T8：备份目标路径必须与四个 store 的真实路径逐字相等。

    这条是"防备份到别的东西上"的唯一机制：`target_databases()` 复刻了
    三个模块的路径解析规则（不能 import 它们的私有方法，因为备份要能在
    服务未启动时也算出路径）。规则一旦漂移，这里变红——
    否则症状是"备份一直成功，只是内容不对"，恢复时才发现。
    """
    from app.governance.store import get_store
    from app.services.memory_sqlite import get_sqlite_store
    from app.services.telemetry import telemetry
    from app.services.user_profile import profile_store

    by_name = {d.name: d for d in bs.target_databases()}
    assert set(by_name) == {"governance", "observability", "session_memory", "user_profile"}

    assert by_name["governance"].path == get_store().db_path       # 注意：属性，不是方法
    assert by_name["observability"].path == telemetry._db_path()   # noqa: SLF001 内部解析器即契约
    assert by_name["session_memory"].path == get_sqlite_store().db_path
    assert by_name["user_profile"].path == profile_store._path     # noqa: SLF001
    # rel 必须与真实落点一致，否则恢复演练会放错位置
    for db in bs.target_databases():
        assert str(db.path).replace("\\", "/").endswith(db.rel)


# --------------------------------------------------------------- 9 端点


def test_backup_run_endpoint_requires_key():
    """备份会写盘，必须受 S-1 保护（默认不放白名单）。"""
    resp = client.post("/v1/ops/backup/run")
    assert resp.status_code == 200
    assert resp.json()["code"] == 4010


def test_backup_run_endpoint_with_key():
    resp = client.post("/v1/ops/backup/run?operator=pytest", headers=HEADERS)
    body = resp.json()
    assert body["code"] == 2000
    data = body["data"]
    assert data["operator"] == "pytest"
    assert {"ok", "date", "dir", "databases", "keep", "reusedExisting"} <= set(data)


def test_backup_run_endpoint_accepts_force():
    """`force` 是"重备今天"的显式开关；默认幂等由 test_second_run_same_day 守着。"""
    resp = client.post("/v1/ops/backup/run?operator=pytest&force=true", headers=HEADERS)
    body = resp.json()
    assert body["code"] == 2000
    assert body["data"]["reusedExisting"] is False


def test_backup_status_endpoint_with_key():
    resp = client.get("/v1/ops/backup/status", headers=HEADERS)
    body = resp.json()
    assert body["code"] == 2000
    data = body["data"]
    assert data["keep"] >= 1
    assert {"root", "enabled", "count", "backups"} <= set(data)


def test_backup_task_registered_in_scheduler():
    """E-7 要求"复用治理任务表机制"，所以它必须出现在任务表里。"""
    from app.services.scheduler import default_tasks
    task = next((t for t in default_tasks() if t.name == "db_backup"), None)
    assert task is not None, "db_backup 未挂进 default_tasks()"
    assert task.interval_sec == int(settings.backup_interval_sec)
    assert task.enabled() is bool(settings.backup_enabled)


@pytest.mark.parametrize("attr", ["backup_enabled", "backup_keep_days", "backup_interval_sec"])
def test_settings_defaults_present(attr):
    assert hasattr(settings, attr)

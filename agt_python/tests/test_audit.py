"""E-8 操作审计留痕单测（企业级落地规约 E-8，判据 C5 可追溯）。

覆盖四层，每层钉住的是**设计决策**而不是实现细节：

1. **存储层**（`governance/store.py`）
   - 统一维度 `actor/action/target/result/ip/traceId/at` 的真实映射（老列名与新列的等价关系）
   - `target` 缺省由 `doc_id` 兜底，老调用点无需逐个改造
   - **升级路径**：已存在的 `audit_log` 表必须能被 `ALTER TABLE` 补列且**老数据不丢**
     （只加列不重建，见数据库规约 §2.6）
   - 前缀过滤：`target='tool:'` 能一次捞出所有工具类动作

2. **服务层**（`governance_service.audit`）
   - `traceId` 自动取请求上下文（调用方不必逐个传）
   - **审计失败绝不抛出**——审计不能反过来打断业务

3. **配置变更留痕**（`config_audit`）——本系统无运行时改配置入口，
   只能在启动时比对指纹；这里钉住"首次不写、未变不写、变了写清改了什么"，
   以及**密钥永不进审计表**（只留 12 位哈希）

4. **覆盖面**：备份触发、确认门放行/拒绝两条分支、Java 侧补录端点

全部走临时库（`tmp_path`）+ monkeypatch，**不碰产品数据目录**（项目铁律）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.response import StatusCode
from app.main import app

# 密钥由 tests/conftest.py 注入（早于任何 app.* 导入）
HEADERS = {"X-Agent-Key": settings.agent_shared_key}

client = TestClient(app)


# --------------------------------------------------------------- 夹具


@pytest.fixture()
def gov(tmp_path, monkeypatch):
    """把治理库单例换成独立临时库（不写 data/governance/governance.db）。"""
    from app.governance import store as store_mod

    store = store_mod.SqliteGovernanceStore(str(tmp_path / "gov.db"))
    store.ensure_schema()
    monkeypatch.setattr(store_mod, "_store", store, raising=False)
    monkeypatch.setattr(settings, "gov_enabled", True)
    return store


def _rows(store) -> list[dict]:
    """直接读表（绕过仓储方法，用于验证"真的写进去了哪些列"）。"""
    cur = store._connection().execute("SELECT * FROM audit_log ORDER BY id")
    return [dict(r) for r in cur.fetchall()]


# =============================================================== 1. 存储层


def test_target_derived_from_doc_id_for_legacy_callers(gov):
    """老调用点只传 doc_id → target 自动补成 `doc:<id>`，不必逐个改造。"""
    gov.log_audit(operator="admin", action="SOFT_DELETE", doc_id="kb_123",
                  doc_name="手册.md")

    row = _rows(gov)[0]
    assert row["target"] == "doc:kb_123"


def test_explicit_target_wins_over_doc_id(gov):
    """显式 target 优先（非文档类动作要能自定指向）。"""
    gov.log_audit(operator="admin", action="ROLLBACK", doc_id="kb_123",
                  target="doc:kb_123#v2")

    assert _rows(gov)[0]["target"] == "doc:kb_123#v2"


def test_uniform_dimensions_are_all_persisted(gov):
    """统一维度的七个语义位都必须真的落盘。"""
    gov.log_audit(operator="admin", action="LOGIN_OK", target="user:admin",
                  result="SUCCESS", ip="10.1.2.3", trace_id="abc123def456")

    row = _rows(gov)[0]
    assert row["operator"] == "admin"          # ≡ actor
    assert row["action"] == "LOGIN_OK"
    assert row["target"] == "user:admin"
    assert row["result"] == "SUCCESS"
    assert row["ip"] == "10.1.2.3"
    assert row["trace_id"] == "abc123def456"
    assert row["ts"]                            # ≡ at


def test_existing_table_is_upgraded_without_losing_rows(tmp_path, monkeypatch):
    """**升级路径**：已存在的旧表（无新列）经 ensure_schema 后补列且老数据不丢。

    这条是本组最关键的回归项：真实部署里 `audit_log` 是**已经有数据**的表，
    `CREATE TABLE IF NOT EXISTS` 对它完全不起作用（不会加列），
    必须靠显式 `ALTER TABLE ADD COLUMN`。写错了的表现是
    "本地新库全绿、现场老库一查就报 no such column"。
    """
    from app.governance.store import SCHEMA_STATEMENTS, SqliteGovernanceStore

    db = tmp_path / "legacy.db"
    # 造一个"E-8 之前"的库：只有老列
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
        "operator TEXT NOT NULL, action TEXT NOT NULL, doc_id TEXT, doc_name TEXT, "
        "version_no INTEGER, detail TEXT, result TEXT NOT NULL DEFAULT 'SUCCESS')"
    )
    conn.execute("INSERT INTO audit_log (ts, operator, action, result) "
                 "VALUES ('2026-01-01 00:00:00', 'old', 'UPLOAD', 'SUCCESS')")
    conn.commit()
    # 确认升级前确实没有新列（否则本用例就成了"永远绿"的假测试）
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert {"target", "ip", "trace_id"}.isdisjoint(cols_before)
    conn.close()

    store = SqliteGovernanceStore(str(db))
    store.ensure_schema()
    # 幂等：再跑一次不报错
    store.ensure_schema()

    rows = _rows(store)
    assert len(rows) == 1, "升级不得丢老数据"
    assert rows[0]["operator"] == "old"
    assert rows[0]["target"] is None, "老行新列应为 NULL，不是空串"

    # 新写入能带上新列
    store.log_audit(operator="admin", action="LOGIN_OK", target="user:admin")
    assert _rows(store)[1]["target"] == "user:admin"


def test_schema_statements_still_declare_audit_indexes():
    """表结构常量里仍声明审计索引（升级路径与全新库都要有索引）。"""
    from app.governance.store import SCHEMA_STATEMENTS

    joined = " ".join(SCHEMA_STATEMENTS)
    assert "idx_audit_ts" in joined
    assert "idx_audit_action" in joined


def test_query_audit_filters_by_target_prefix(gov):
    """前缀过滤让"按对象类型"筛选成立：`tool:` 一次捞全、`doc:` 一次捞全。"""
    gov.log_audit(operator="admin", action="UPLOAD", doc_id="kb_1")
    gov.log_audit(operator="admin", action="SOFT_DELETE", doc_id="kb_2")
    gov.log_audit(operator="admin", action="TOOL_CONFIRM_ALLOW", target="tool:dispatch")
    gov.log_audit(operator="admin", action="LOGIN_OK", target="user:admin")

    tools, tools_total = gov.query_audit(target="tool:")
    docs, docs_total = gov.query_audit(target="doc:")

    assert tools_total == 1 and tools[0]["action"] == "TOOL_CONFIRM_ALLOW"
    assert docs_total == 2 and {r["doc_id"] for r in docs} == {"kb_1", "kb_2"}


def test_query_audit_exact_target_and_combined_filters(gov):
    """精确到对象 + 与 action/operator 组合过滤。"""
    gov.log_audit(operator="alice", action="LOGIN_OK", target="user:alice")
    gov.log_audit(operator="alice", action="LOGIN_FAIL", target="user:alice")
    gov.log_audit(operator="bob", action="LOGIN_OK", target="user:bob")

    rows, total = gov.query_audit(target="user:alice", action="LOGIN_FAIL")

    assert total == 1
    assert rows[0]["operator"] == "alice"
    assert rows[0]["action"] == "LOGIN_FAIL"


def test_legacy_rows_derive_target_without_touching_data(gov):
    """E-8 之前的历史行（`target` 为 NULL）**读取时派生**成 `doc:<doc_id>`。

    为什么不回填：审计表"只增不改"是最硬的性质，`UPDATE` 回填的收益（少一个 COALESCE）
    远小于风险（历史证据被改动、且不可解释）。所以库里保持 NULL，读取时算——
    关键是**筛选与显示用同一个表达式**，否则会出现"筛出来了但对象列写着别的"。
    """
    # 模拟 E-8 之前写入的行：有 doc_id、无 target
    gov._connection().execute(
        "INSERT INTO audit_log (ts, operator, action, doc_id, doc_name, result) "
        "VALUES ('2026-01-01 00:00:00', 'admin', 'UPLOAD', 'kb_legacy', '老手册.md', 'SUCCESS')")
    gov._connection().commit()

    rows, total = gov.query_audit(target="doc:")

    assert total == 1
    assert rows[0]["target"] == "doc:kb_legacy", "派生值要能被显示出来"
    # 库里仍是 NULL：证明没有偷偷改历史数据
    raw = dict(gov._connection().execute(
        "SELECT target FROM audit_log WHERE doc_id = 'kb_legacy'").fetchone())
    assert raw["target"] is None


def test_query_audit_still_works_without_target_filter(gov):
    """不加 target 时行为与之前一致（老前端/老调用零感知）。"""
    gov.log_audit(operator="admin", action="UPLOAD", doc_id="kb_1")
    gov.log_audit(operator="admin", action="LOGIN_OK", target="user:admin")

    rows, total = gov.query_audit(operator="admin")

    assert total == 2
    assert len(rows) == 2


# =============================================================== 2. 服务层


def test_audit_picks_up_trace_id_from_request_context(gov):
    """`traceId` 自动取上下文——调用方不必逐个传，"一次 grep 串三端"才成立。"""
    from app.core import trace as trace_mod
    from app.services.governance_service import governance_service

    token = trace_mod._trace_id.set("trace-from-context")
    try:
        governance_service.audit("LOGIN_OK", operator="admin", target="user:admin")
    finally:
        trace_mod._trace_id.reset(token)

    assert _rows(gov)[0]["trace_id"] == "trace-from-context"


def test_explicit_trace_id_overrides_context(gov):
    """显式传入优先（补录历史事件时不该被当前请求的 id 覆盖）。"""
    from app.core import trace as trace_mod
    from app.services.governance_service import governance_service

    token = trace_mod._trace_id.set("context-id")
    try:
        governance_service.audit("LOGIN_OK", operator="admin",
                                 target="user:admin", trace_id="explicit-id")
    finally:
        trace_mod._trace_id.reset(token)

    assert _rows(gov)[0]["trace_id"] == "explicit-id"


def test_audit_never_raises_when_store_is_broken(gov, monkeypatch):
    """审计写失败只告警，**绝不抛出**——审计不能反过来打断业务。"""
    from app.services.governance_service import governance_service

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(type(gov), "log_audit", _boom, raising=True)
    # 不应抛异常
    governance_service.audit("SOFT_DELETE", operator="admin", doc_id="kb_1")


def test_audit_skipped_when_governance_disabled(gov, monkeypatch):
    """治理关掉时静默跳过（引擎开关语义，而不是报错）。"""
    from app.services.governance_service import governance_service

    monkeypatch.setattr(settings, "gov_enabled", False)
    governance_service.audit("SOFT_DELETE", operator="admin", doc_id="kb_1")

    assert _rows(gov) == []


def test_audit_detail_dict_is_json_encoded(gov):
    """detail 支持 dict（调用方不必自己 json.dumps）。"""
    from app.services.governance_service import governance_service

    governance_service.audit("CONFIG_CHANGE", target="config:env",
                             detail={"changed": {"gov_ttl_days": {"from": "30", "to": "90"}}})

    detail = json.loads(_rows(gov)[0]["detail"])
    assert detail["changed"]["gov_ttl_days"]["to"] == "90"


def test_service_query_audit_returns_new_columns(gov):
    """查询结果要带上新维度（前端审计页需要 target/ip/trace_id 列）。"""
    from app.services.governance_service import governance_service

    governance_service.audit("LOGIN_OK", operator="admin", target="user:admin",
                             ip="10.0.0.5", trace_id="tid-1")
    page = governance_service.query_audit(page=1, page_size=20)

    item = page["items"][0]
    assert item["target"] == "user:admin"
    assert item["ip"] == "10.0.0.5"
    assert item["trace_id"] == "tid-1"
    assert page["total"] == 1


# =============================================================== 3. 配置变更留痕


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """配置留痕的独立环境：快照目录指向 tmp，且保证治理库可用。"""
    from app.services import config_audit as ca

    monkeypatch.setattr(ca, "SNAPSHOT_DIR", tmp_path / "ops")
    return ca


class TestConfigAudit:
    def test_first_run_writes_snapshot_but_no_audit(self, cfg, gov):
        """首次（无上次快照）默认不写审计——"从无到有"信息量低还占一行。"""
        report = cfg.audit_config_change()

        assert report["firstRun"] is True
        assert report["audited"] is False
        assert _rows(gov) == []
        assert (cfg.SNAPSHOT_DIR / cfg.SNAPSHOT_NAME).exists()

    def test_unchanged_config_writes_nothing(self, cfg, gov):
        """配置没变 → 不写审计（重启是常态，不能每次重启都记一笔）。"""
        cfg.audit_config_change()
        report = cfg.audit_config_change()

        assert report["firstRun"] is False
        assert report["audited"] is False
        assert report["changed"] == 0
        assert _rows(gov) == []

    def test_changed_key_is_audited_with_before_and_after(self, cfg, gov, monkeypatch):
        """变了就要写清"改了什么、从什么变成什么"——这才是审计的价值。"""
        from app.core import config as config_mod

        cfg.audit_config_change()
        monkeypatch.setattr(config_mod.settings, "gov_ttl_days", 999)
        report = cfg.audit_config_change()

        assert report["audited"] is True
        assert report["changedKeys"] == ["gov_ttl_days"]
        row = _rows(gov)[0]
        assert row["action"] == "CONFIG_CHANGE"
        assert row["target"] == "config:env"
        assert row["operator"] == "system"
        detail = json.loads(row["detail"])
        assert detail["changed"]["gov_ttl_days"]["to"] == "999"

    def test_secret_value_never_appears_in_plaintext(self, cfg, gov, monkeypatch):
        """密钥类配置**只留哈希**：快照与审计 detail 里都不许出现原值。"""
        from app.core import config as config_mod

        secret = "sk-super-secret-value-12345"
        monkeypatch.setattr(config_mod.settings, "llm_api_key", secret)

        cfg.audit_config_change()
        snapshot_text = (cfg.SNAPSHOT_DIR / cfg.SNAPSHOT_NAME).read_text(encoding="utf-8")
        assert secret not in snapshot_text
        assert "sha256:" in snapshot_text

        # 再改一次 → 审计 detail 里也只能是哈希
        monkeypatch.setattr(config_mod.settings, "llm_api_key", secret + "-rotated")
        cfg.audit_config_change()
        detail_text = _rows(gov)[0]["detail"]
        assert secret not in detail_text
        assert json.loads(detail_text)["changed"]["llm_api_key"]["to"].startswith("sha256:")

    def test_is_secret_key_by_name(self, cfg):
        """敏感判定按**键名**（不看值，避免"值恰好长得像密钥"的误判）。"""
        assert cfg.is_secret_key("agent_shared_key")
        assert cfg.is_secret_key("llm_api_key")
        assert cfg.is_secret_key("rerank_api_key")
        assert not cfg.is_secret_key("gov_ttl_days")
        assert not cfg.is_secret_key("log_level")

    def test_build_marker_excludes_volatile_build_time(self, cfg):
        """指纹不含 buildTime：否则每次重新打包都被记成"改配置"→ 告警疲劳。"""
        marker = cfg._build_marker()
        assert "version" in marker and "gitCommit" in marker
        assert "buildTime" not in marker

    def test_diff_reports_added_and_removed_keys(self, cfg):
        """新增/删除配置项也要能报出来（不只是"值变了"）。"""
        prev = {"values": {"a": "1", "b": "2"}, "build": {"version": "1"}}
        cur = {"values": {"a": "1", "b": "3", "c": "9"}, "build": {"version": "1"}}

        diff = cfg.diff_snapshots(prev, cur)

        assert diff["changed"] == {"b": {"from": "2", "to": "3"}}
        assert diff["added"] == ["c"]
        assert diff["removed"] == []
        assert diff["count"] == 2

    def test_audit_failure_does_not_break_startup(self, cfg, monkeypatch):
        """留痕失败也要返回报告（启动路径不能因此抛异常）。"""
        monkeypatch.setattr(cfg, "build_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        report = cfg.audit_config_change()

        assert "error" in report
        assert report["audited"] is False


# =============================================================== 4. 覆盖面：备份


def test_run_backup_records_backup_run_audit(gov, monkeypatch):
    """备份触发留痕（target=backup）——定时任务与手动触发走同一处置点。"""
    from app.services import backup_service as bs
    from app.services.governance_service import governance_service

    fake = {"ok": True, "reusedExisting": False, "dir": "data/backups/2026-09-24",
            "databases": [{"name": "governance", "integrity": "ok"},
                          {"name": "metrics", "integrity": "ok"}],
            "skipped": [], "elapsedMs": 42, "message": "2/2 个库已备份并校验通过"}
    monkeypatch.setattr(bs, "_run_backup_impl", lambda operator, force=False: fake)

    report = bs.run_backup(operator="admin", force=True)

    assert report is fake
    row = _rows(gov)[0]
    assert row["action"] == "BACKUP_RUN"
    assert row["target"] == "backup"
    assert row["operator"] == "admin"
    assert row["result"] == "SUCCESS"
    detail = json.loads(row["detail"])
    assert detail["backedUp"] == "2/2"
    assert detail["reusedExisting"] is False


def test_backup_audit_marks_failed_result(gov, monkeypatch):
    """备份失败 → result=FAILED（审计页能一眼看出没备成）。"""
    from app.services import backup_service as bs

    fake = {"ok": False, "dir": "x", "databases": [], "skipped": [], "elapsedMs": 1,
            "message": "四个库均未能备份"}
    monkeypatch.setattr(bs, "_run_backup_impl", lambda operator, force=False: fake)

    bs.run_backup(operator="system")

    assert _rows(gov)[0]["result"] == "FAILED"


def test_backup_audit_failure_does_not_break_backup(gov, monkeypatch):
    """审计写失败不能把备份搞挂——备份是主体，审计只是"顺手记一笔"。"""
    from app.services import backup_service as bs
    from app.services import governance_service as gs_mod

    fake = {"ok": True, "dir": "x", "databases": [], "skipped": [], "elapsedMs": 1}
    monkeypatch.setattr(bs, "_run_backup_impl", lambda operator, force=False: fake)

    def _boom(*a, **k):
        raise RuntimeError("审计库挂了")

    monkeypatch.setattr(gs_mod.governance_service, "audit", _boom)

    # 备份照常返回，不抛
    assert bs.run_backup() is fake


# =============================================================== 5. 覆盖面：确认门


def _pending(tool: str = "dispatch_task"):
    from app.services.guard import PendingAction

    return PendingAction(session_id="s1", tool=tool, arguments={"a": 1},
                         description="给无人机 A 派发任务")


def test_confirm_allow_is_audited(gov):
    """确认门放行留痕：target=tool:<工具名>，能回答"这个工具被确认过几次"。"""
    from app.services.agent_service import _audit_confirm
    from app.services.governance_service import GovAction

    _audit_confirm(GovAction.TOOL_CONFIRM_ALLOW, _pending(), {"username": "pilot01"})

    row = _rows(gov)[0]
    assert row["action"] == "TOOL_CONFIRM_ALLOW"
    assert row["target"] == "tool:dispatch_task"
    assert row["operator"] == "pilot01"
    assert row["result"] == "SUCCESS"
    assert json.loads(row["detail"])["confirmed"] is True


def test_confirm_deny_is_audited_with_reason(gov):
    """拒绝也要留痕且带原因（"为什么危险操作没执行"是审计最该回答的问题之一）。"""
    from app.services.agent_service import _audit_confirm
    from app.services.governance_service import GovAction

    _audit_confirm(GovAction.TOOL_CONFIRM_DENY, _pending(), {"username": "pilot01"},
                   reason="role_denied", detail="当前角色无写权限")

    detail = json.loads(_rows(gov)[0]["detail"])
    assert detail["reason"] == "role_denied"
    assert detail["confirmed"] is False
    assert "无写权限" in detail["detail"]


def test_confirm_audit_hides_tool_arguments(gov):
    """**工具参数不进审计**（可能含业务数据）——只留工具名与原因。"""
    from app.services.agent_service import _audit_confirm
    from app.services.governance_service import GovAction
    from app.services.guard import PendingAction

    pending = PendingAction(session_id="s1", tool="create_task",
                            arguments={"secretBizKey": "SHOULD-NOT-APPEAR"},
                            description="创建任务")
    _audit_confirm(GovAction.TOOL_CONFIRM_ALLOW, pending, {"username": "admin"})

    assert "SHOULD-NOT-APPEAR" not in _rows(gov)[0]["detail"]


def test_confirm_audit_uses_anonymous_when_no_identity(gov):
    """登录失效场景也要留痕（否则"确认了但没执行"查不出原因）。"""
    from app.services.agent_service import _audit_confirm
    from app.services.governance_service import GovAction

    _audit_confirm(GovAction.TOOL_CONFIRM_DENY, _pending(), None, reason="session_expired")

    assert _rows(gov)[0]["operator"] == "anonymous"


# =============================================================== 6. 端点


def test_audit_append_endpoint_writes_row(gov):
    """Java 侧补录入口：登录/登出这类不经过 Python 的事件在这里落表。"""
    resp = client.post("/v1/kb/governance/audit", headers=HEADERS, json={
        "actor": "admin", "action": "LOGIN_OK", "target": "user:admin",
        "result": "SUCCESS", "ip": "10.1.2.3",
    })

    assert resp.status_code == 200
    assert resp.json()["code"] == 2000
    row = _rows(gov)[0]
    assert row["action"] == "LOGIN_OK"
    assert row["ip"] == "10.1.2.3"


def test_audit_append_endpoint_requires_agent_key():
    """补录是写操作，必须带服务间密钥（与其它非探针接口同口径）。"""
    resp = client.post("/v1/kb/governance/audit",
                       json={"actor": "admin", "action": "LOGIN_OK"})

    assert resp.json()["code"] == 4010


def test_audit_append_endpoint_rejects_overlong_action(gov):
    """字段长度受 Pydantic 约束（超长拒收，不让脏数据进表）。

    注：本服务把所有业务异常/校验失败都包装成 **HTTP 200 + 业务码**，
    所以断言看的是 `code` 而不是 status（这是既有约定，不是本用例的疏漏）。
    """
    resp = client.post("/v1/kb/governance/audit", headers=HEADERS,
                       json={"actor": "admin", "action": "X" * 64})

    assert resp.status_code == 200
    assert resp.json()["code"] == int(StatusCode.VALIDATE_ERROR)
    assert _rows(gov) == []


def test_audit_query_endpoint_filters_by_target(gov):
    """GET 侧新增的 target 过滤走通（前端"按对象筛选"依赖它）。"""
    from app.services.governance_service import governance_service

    governance_service.audit("UPLOAD", operator="admin", doc_id="kb_1")
    governance_service.audit("LOGIN_OK", operator="admin", target="user:admin")

    resp = client.get("/v1/kb/governance/audit?target=user:",
                      headers=HEADERS)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["action"] == "LOGIN_OK"


# =============================================================== 7. 审计导出（CSV）


def test_export_audit_csv_has_bom_and_fixed_header(gov):
    """导出必须带 BOM 且表头固定（顺序即前端/运维脚本依赖的接口）。

    缺 BOM 时 Excel 按 ANSI 解码，中文列名整片乱码——"导出的证据打不开"。
    """
    from app.services.governance_service import AUDIT_CSV_HEADERS, governance_service

    governance_service.audit("UPLOAD", operator="admin", doc_id="kb_1")

    resp = client.get("/v1/kb/governance/audit/export", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 2000
    data = body["data"]
    assert data["content"].startswith("\ufeff")
    header_line = data["content"].lstrip("\ufeff").splitlines()[0]
    assert header_line.split(",") == list(AUDIT_CSV_HEADERS)
    assert data["filename"].startswith("audit_") and data["filename"].endswith(".csv")
    assert data["rows"] == 1


def test_csv_cell_keeps_plain_numbers_but_neutralizes_formulas():
    """两个方向都要钉住，只顾一边都会出错。

    只顾"防注入" → 把负数 `-5` 写成 `'-5`，数字变文本（下游统计全废）。
    只顾"保留数字" → 放过 `-5+1` 这类以减号开头的公式。
    """
    from app.services.governance_service import _csv_cell

    assert _csv_cell(None) == ""              # 空值统一成空串，不是 "None"
    assert _csv_cell("-5") == "-5"            # 纯负数是数据
    assert _csv_cell("-5+1") == "'-5+1"       # 减号开头但是公式
    assert _csv_cell("=1+1") == "'=1+1"
    assert _csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_cell("文献A") == "文献A"       # 正常中文不受影响


def test_export_audit_csv_neutralizes_user_controlled_cells(gov):
    """用户可控的文档名不得原样落进证据文件（Excel 会当公式执行，可外联取数）。"""
    from app.services.governance_service import governance_service

    governance_service.audit("UPLOAD", operator="admin", doc_id="kb_1",
                             doc_name="=cmd|'/c calc'!A1")

    content = client.get("/v1/kb/governance/audit/export",
                         headers=HEADERS).json()["data"]["content"]

    assert "'=cmd|'/c calc'!A1" in content
    assert "\n=cmd" not in content and ",=cmd" not in content


def test_export_audit_csv_reports_truncation_honestly(gov):
    """到顶时必须如实标注 `truncated`。

    这是"导出放服务端"的**唯一理由**：前端循环翻页若中途网络失败，
    用户拿到的是一份**看不出残缺的残缺证据**。
    """
    from app.services.governance_service import governance_service

    for i in range(5):
        governance_service.audit("UPLOAD", operator="admin", doc_id=f"kb_{i}")

    data = governance_service.export_audit_csv(limit=2)

    assert data["rows"] == 2
    assert data["total"] == 5
    assert data["truncated"] is True


def test_export_audit_csv_applies_same_filters_as_paging(gov):
    """导出与列表共用同一套过滤（否则"看到的"和"导出的"不是同一批）。"""
    from app.services.governance_service import governance_service

    governance_service.audit("UPLOAD", operator="admin", doc_id="kb_1")
    governance_service.audit("LOGIN_OK", operator="admin", target="user:admin")

    data = client.get("/v1/kb/governance/audit/export?target=user:",
                      headers=HEADERS).json()["data"]

    assert data["rows"] == 1
    assert "LOGIN_OK" in data["content"]
    assert "UPLOAD" not in data["content"]


def test_export_audit_csv_requires_agent_key():
    """导出含操作人 / IP / traceId，属敏感数据 → 与其它接口同口径要密钥。"""
    assert client.get("/v1/kb/governance/audit/export").json()["code"] == 4010


def test_export_audit_csv_degrades_when_store_broken(gov, monkeypatch):
    """库不可用返回空内容 + 原因，而不是 500（与审计"永不抛出"同取向）。"""
    from app.services.governance_service import governance_service

    def boom(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gov, "query_audit", boom)

    data = governance_service.export_audit_csv()

    assert data["content"] == ""
    assert data["rows"] == 0
    assert data["truncated"] is False
    assert "OperationalError" in data["reason"]

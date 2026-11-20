"""v4 知识缺口分析单测（数据中台规约 §5「未命中问题分析」）。

覆盖三件容易回归的事，每件都对应一个设计决策：
1. **口径**：只有 no_hit / insufficient_evidence 算缺口——kept（答上了）与
   cross_domain（问的不属知识域）必须被排除，否则视图会被"今天天气"淹掉；
2. **聚类**：同主题不同问法必须合并（这里用的是实词切段 + 重叠系数，
   早期 bigram + Jaccard 实测漏聚约 25%，是本模块踩过的坑）；
3. **不伪造**：问题原文来自会话记忆（易失），捞不到时只出主题与计数，
   并用 questionAvailable/reason 如实标注，绝不编一句假问题。

埋点不落正文（规约 §10），所以缺口分析拿不到原句是**正常状态**而非异常。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """独立临时库的 TelemetryStore（不碰 data/metrics/observability.db）。"""
    from app.core import config as config_mod
    from app.services import telemetry as tl_mod

    db = tmp_path / "t.db"
    monkeypatch.setattr(config_mod.settings, "telemetry_db_path", str(db))
    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", True)
    monkeypatch.setattr(config_mod.settings, "kb_gap_min_occurrences", 3)
    monkeypatch.setattr(config_mod.settings, "kb_gap_max_questions", 200)
    monkeypatch.setattr(config_mod.settings, "kb_gap_hours_default", 24 * 7)
    s = tl_mod.TelemetryStore()
    s._enabled = True
    s._ensure_conn()
    return s


def _seed(s, rows):
    """rows: (trace_id, session_id, decision, docs, question)

    末位 "" 为 rag_rewrite（查询改写来源，v5 / DEV_ISSUES 6.62）；本组用例
    只关心知识缺口，改写来源不参与断言，恒填空串（等价于 none）。
    再往后的 (0, 0, 0) 为 v6 记忆三列（会话记忆规约 §6 M-4），缺口视图不关心；
    最后的 "" 为 v8 usage_source（E-13），同不关心。
    """
    for tid, sid, dec, docs, _q in rows:
        s._q_chat.append((tid, sid, 1, "admin", "rag", "rule", "REACT", "m", 1, 1, 5.0,
                          1, 0, 10, "", "2026-09-18T20:00:00", dec, docs, "", 0, 0, 0, ""))
    s.flush_now()


def _fake_memory(monkeypatch, rows):
    class FakeMem:
        _d = {sid: q for _t, sid, _dec, _docs, q in rows}

        def get(self, sid):
            return [{"role": "user", "content": self._d[sid]}] if sid in self._d else []

        def has(self, sid):
            return sid in self._d

    from app.services import memory as mem_mod
    monkeypatch.setattr(mem_mod, "session_memory", FakeMem(), raising=False)


ROWS = [
    ("t1", "s1", "insufficient_evidence", "d-a,d-b", "查询方法是什么"),
    ("t2", "s2", "insufficient_evidence", "d-a", "查询方法的步骤"),
    ("t3", "s3", "insufficient_evidence", "d-c", "查询方法有哪些参数"),
    ("t4", "s4", "insufficient_evidence", "d-a", "查询方法怎么用"),
    ("t5", "s5", "no_hit", "", "航拍电池续航"),
    ("t6", "s6", "no_hit", "", "航拍电池维修"),
    ("t7", "s7", "kept", "d-a,d-z", "什么是无人机"),
    ("t8", "s8", "cross_domain", "", "今天天气如何"),
]


def test_schema_v4_columns_and_version(store):
    """v4 迁移：两列加上、schema_version 记到 4、旧库可平滑加列。"""
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(agent_chat_log)").fetchall()}
    assert "rag_decision" in cols and "rag_docs" in cols
    vers = {r[0] for r in store._conn.execute("SELECT version FROM schema_version").fetchall()}
    assert {1, 2, 3, 4} <= vers


def test_gap_excludes_kept_and_cross_domain(store, monkeypatch):
    """口径：kept 与 cross_domain 不进缺口统计（会被分别列在 byDecision 里）。"""
    _seed(store, ROWS)
    _fake_memory(monkeypatch, ROWS)
    r = store.unanswered(hours=24 * 7)
    assert r["byDecision"] == {"kept": 1, "cross_domain": 1,
                               "insufficient_evidence": 4, "no_hit": 2}
    assert r["gapTotal"] == 6, "缺口只该有 4 条 insuff + 2 条 no_hit"
    assert "今天天气如何" not in [s for t in r["topics"] for s in t["samples"]], \
        "cross_domain（问的不属知识域）不该出现在缺口主题里"


def test_cluster_merges_same_topic_keeps_others_apart(store, monkeypatch):
    """聚类：4 条同主题（措辞各异）合成 1 簇且优先级「高」；异主题不误合。"""
    _seed(store, ROWS)
    _fake_memory(monkeypatch, ROWS)
    r = store.unanswered(hours=24 * 7)
    assert r["distinctTopics"] == 2, [t["keywords"] for t in r["topics"]]
    top = r["topics"][0]
    assert top["count"] == 4 and top["priority"] == "高", top
    assert "查询方法" in top["keywords"], top["keywords"]
    # 航拍电池只出现 2 次（< min_occurrences=3）→ 只能「观察」
    low = [t for t in r["topics"] if "航拍" in "".join(t["keywords"])]
    assert low and low[0]["priority"] == "低", low


def test_suggestion_priority_mapping(store, monkeypatch):
    """建议三档：证据不足→高（补一篇就能救）；纯无召回且够次数→中。"""
    assert store._suggestion({"insufficient_evidence": 5}, 5, 3)[1] == "高"
    assert store._suggestion({"no_hit": 5}, 5, 3)[1] == "中"
    assert store._suggestion({"no_hit": 2}, 2, 3)[1] == "低"
    assert store._suggestion({"no_hit": 3, "insufficient_evidence": 2}, 5, 3)[1] == "高"


def test_doc_usage_only_counts_kept(store, monkeypatch):
    """文档使用度只算 kept：拒答会话即使召回到文档也不算被使用。"""
    _seed(store, ROWS)
    u = store.doc_usage(hours=24 * 7)
    hits = {x["docId"]: x["hits"] for x in u["rows"]}
    assert hits == {"d-a": 1, "d-z": 1}, hits
    assert "d-b" not in hits and "d-c" not in hits, \
        "insufficient_evidence 会话的召回文档不该计入使用度（它们没答上）"


def test_no_question_source_is_reported_not_faked(store, monkeypatch):
    """原句不可得：只出主题与计数 + reason 标注，绝不伪造问题文本。"""
    _seed(store, ROWS)
    _fake_memory(monkeypatch, [])  # 模拟记忆已淘汰
    r = store.unanswered(hours=24 * 7)
    assert r["questionAvailable"] is False
    assert r["reason"] and "问题原文不可得" in r["reason"]
    assert all(not t["samples"] for t in r["topics"])
    assert all(t["count"] >= 1 for t in r["topics"])
    assert r["gapTotal"] == 6, "缺原句不影响计数口径"


def test_disabled_returns_enabled_flag(tmp_path, monkeypatch):
    """埋点关闭时安全降级：不抛异常、返回 enabled=false。"""
    from app.core import config as config_mod
    from app.services import telemetry as tl_mod
    monkeypatch.setattr(config_mod.settings, "telemetry_db_path", str(tmp_path / "x.db"))
    monkeypatch.setattr(config_mod.settings, "telemetry_enabled", False)
    s = tl_mod.TelemetryStore()
    r = s.unanswered(hours=24)
    assert r["enabled"] is False and r["reason"] == "telemetry disabled"
    assert s.doc_usage(hours=24)["enabled"] is False


def test_export_csv_new_views(store, monkeypatch):
    """两个新 CSV view：带 BOM、可落盘（补库清单能直接给知识库同学当工单）。"""
    _seed(store, ROWS)
    _fake_memory(monkeypatch, ROWS)
    for view, min_lines in (("unanswered", 2), ("doc_usage", 2)):
        c = store.export_csv(view=view, hours=24 * 7)
        assert c["content"].startswith("\ufeff")
        assert c["filename"].endswith(".csv")
        assert len(c["content"].strip().splitlines()) >= min_lines, c


def test_keywords_drops_cross_word_noise():
    """关键词切段：跨词边界的虚字噪声已剔除（这是聚类能合并的前提）。"""
    from app.services.telemetry import TelemetryStore as T
    kws = T._keywords("查询方法的步骤")
    assert "查询方法" in kws, kws
    assert not any("法怎" in k or "的步" in k for k in kws), kws
    assert T._similarity(T._keywords("查询方法是什么"), T._keywords("查询方法怎么用")) >= 0.99
    assert T._similarity(T._keywords("查询方法参数"), T._keywords("航拍电池续航")) == 0.0


def test_cluster_representative_does_not_grow(monkeypatch):
    """回归：簇代表集**不取并集**（取并集会让代表膨胀，第 3 条同主题被漏掉）。

    真实数据复现（2026-09-19 实测）：三条反重力问题，
    取并集时第 3 条与膨胀后的代表只有 0.45 < 0.5 → 被漏成独立簇。
    本用例把这三句固定下来，防止以后有人"顺手优化"成并集。
    """
    from app.services.telemetry import TelemetryStore as T
    qs = [
        "反重力推进器的维护周期是多久",
        "反重力推进器维护间隔多少天",
        "反重力推进器怎么保养",
    ]
    items = [{"traceId": f"t{i}", "question": q, "decision": "no_hit",
              "keywords": T._keywords(q)} for i, q in enumerate(qs)]
    clusters = T._cluster(items)
    assert len(clusters) == 1, [c["keywords"] for c in clusters]
    assert clusters[0]["count"] == 3


def test_cluster_keeps_different_topics_apart():
    """回归：不同主题不被误合并（共享"多久"这类问法词也不该合）。"""
    from app.services.telemetry import TelemetryStore as T
    qs = ["反重力推进器的维护周期是多久", "无人机电池续航一般多久",
          "量子纠缠通信模块的校准流程"]
    items = [{"traceId": f"t{i}", "question": q, "decision": "no_hit",
              "keywords": T._keywords(q)} for i, q in enumerate(qs)]
    assert len(T._cluster(items)) == 3


def test_samples_deduped_but_count_kept():
    """样例去重、计数不去重：用户复述同一句既是高频信号，也不必重复展示。"""
    from app.services.telemetry import TelemetryStore as T
    items = [{"traceId": f"t{i}", "question": "反重力推进器怎么保养",
              "decision": "no_hit", "keywords": T._keywords("反重力推进器怎么保养")}
             for i in range(3)]
    c = T._cluster(items)[0]
    assert c["count"] == 3

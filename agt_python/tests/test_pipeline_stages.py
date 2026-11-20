"""v5 链路阶段还原单测（数据中台规约 §6.1「执行流程」视图）。

覆盖四件容易回归的事，每件都对应一个设计决策：

1. **不画假节点**：RAG 链路真实不存在「query 改写」环节（代码全文检索
   rewrite/改写 只命中语料治理的版本号改写），业务链路也不存在独立的
   「槽位填充」阶段——阶段序列必须与代码控制流一致，不能照搬直觉链路。
2. **四色口径**：`rag_refused`（如实拒答）算 **gray 不算 error**。
   实测库里 78 条拒答远超真故障数，一律标红会让看板满屏红，把设计内行为
   误读成系统不可用。灰/红拆分的依据是处置动作不同：灰→补语料，红→查系统。
3. **idle 不参与渲染**：REACT 模式下的「任务规划」必须是 idle，
   不能标 ok（标 ok 会让"没发生的事"看起来发生了）。
4. **同阶段取最严重**：一个阶段挂多条痕迹时按 error > warn > gray > ok 取。
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
    s = tl_mod.TelemetryStore()
    s._enabled = True
    s._ensure_conn()
    return s


def _chat(s, tid, *, intent="rag", decision="", docs="", mode="REACT",
          ok=True, degraded=False, error="", rewrite="", memory=(0, 0, 0)):
    """写一条 chat_log（列顺序必须与 _CHAT_COLS 一致）。

    rewrite（DEV_ISSUES 6.62 v5）：查询改写来源，取值
    dict / dict_suffix / llm / llm+dict / none / failed；空串视同 none。
    memory（会话记忆规约 §6 M-4 v6）：(载入, 注入, 压缩) 三列，单位为消息条数。
    """
    total, used, trimmed = memory
    s._q_chat.append((tid, "s-" + tid, 1, "admin", intent, "llm", mode, "m",
                      1, 1, 100.0, 1 if ok else 0, 1 if degraded else 0, 10,
                      error, "2026-09-19T10:00:00", decision, docs, rewrite,
                      total, used, trimmed, "api"))
    s.flush_now()


def _fault(s, path, detail="", tid=""):
    """写一条 fault_event。flush 用队列保证 trace_id 关联正确。"""
    s._q_fault.append((path, "s-" + tid, detail, "2026-09-19T10:00:01", tid))
    s.flush_now()


def _tool(s, tid, step, tool, ok=True, blocked="", error=""):
    s._q_tool.append((tid, "s-" + tid, step, tool, 1 if ok else 0, blocked,
                      error, 5.0, "{}", "2026-09-19T10:00:02"))
    s.flush_now()


def _stage(pipeline, key):
    return next(x for x in pipeline["stages"] if x["key"] == key)


# ---------- ① 阶段序列与代码控制流一致 ----------


def test_rag_pipeline_has_query_rewrite_stage(store):
    """RAG 链路**有** query 改写阶段（DEV_ISSUES 6.62 落地）。

    真实序列：意图识别 → 查询改写 → 召回 → 重排 → 精选 → 评估 → 回答。

    历史提醒：本用例原名 `has_no_query_rewrite_stage`，断言「代码里不存在该环节，
    有人照着设计文档补上假节点会点开没内容」。查询改写实现后该断言已反转——
    现在**必须**有这个节点，否则弹窗与实际链路不一致（漏掉真实执行的一步）。
    这条注释留着，是为了说明"该不该画"的判据始终是**代码里有没有**，不是文档里有没有。
    """
    _chat(store, "a001", decision="kept", docs="d-a,d-b,d-c")
    p = store.trace_detail("a001")["pipeline"]
    names = [s["name"] for s in p["stages"]]
    assert names == ["意图识别", "会话记忆", "查询改写", "召回", "重排", "精选", "评估", "回答"]


def test_rewrite_stage_reflects_source(store):
    """改写段的文案随实际来源变化；未改写时如实说明「未做改写」而不是空白。"""
    cases = [
        ("a010", "dict", "口语术语"),
        ("a011", "dict_suffix", "口语术语"),
        ("a012", "llm", "指代"),
        ("a013", "llm+dict", "指代"),
        ("a014", "failed", "回退原文"),
        ("a015", "", "未做改写"),
    ]
    for tid, src, kw in cases:
        _chat(store, tid, decision="kept", docs="d-a", rewrite=src)
        p = store.trace_detail(tid)["pipeline"]
        st = _stage(p, "rewrite")
        assert kw in st["detail"], f"{src!r} 的改写段文案应为含 {kw!r}，实际 {st['detail']!r}"


def test_rewrite_failed_is_warn_not_error(store):
    """改写失败是降级不是故障：用户仍拿到答案，段状态取 warn。"""
    _chat(store, "a016", decision="kept", docs="d-a", rewrite="failed")
    p = store.trace_detail("a016")["pipeline"]
    assert _stage(p, "rewrite")["status"] == "warn"


def test_business_pipeline_has_no_slot_filling_stage(store):
    """回归：业务链路的「参数提取/槽位填充」不是独立阶段。

    模型在 tool_calls 里直接产出 JSON 参数，工具侧才校验——
    参数摘要挂在工具调用的每一步（args_digest），不单开节点。
    """
    _chat(store, "b001", intent="business", mode="REACT")
    p = store.trace_detail("b001")["pipeline"]
    names = [s["name"] for s in p["stages"]]
    assert names == ["意图识别", "会话记忆", "复杂度路由", "任务规划", "专家委派", "工具调用", "回答"]
    assert not any("槽位" in n or "参数提取" in n for n in names)


def test_chitchat_pipeline_is_three_stages(store):
    """闲聊链路三段：意图 → 会话记忆 → 回答（M-4 起闲聊也装配历史，M-1 验证已证实）。"""
    _chat(store, "c001", intent="chitchat")
    p = store.trace_detail("c001")["pipeline"]
    assert [s["name"] for s in p["stages"]] == ["意图识别", "会话记忆", "回答"]


# ---------- ② 四色口径 ----------


def test_kept_pipeline_all_ok(store):
    """正常答上：全程绿，worstStatus=ok（记忆段在 rag 链路是 idle，不算绿也不算异常）。"""
    _chat(store, "a002", decision="kept", docs="d-a,d-b")
    p = store.trace_detail("a002")["pipeline"]
    assert p["worstStatus"] == "ok"
    assert all(s["status"] in ("ok", "idle") for s in p["stages"])


def test_insufficient_evidence_is_gray_not_error(store):
    """**核心口径**：如实拒答标 gray，不标 error。

    78 条拒答若一律标红，看板会满屏红，把设计内行为误读成系统不可用。
    """
    _chat(store, "a003", decision="insufficient_evidence", docs="d-a")
    _fault(store, "rag_refused", "judge 判证据不足", tid="a003")
    p = store.trace_detail("a003")["pipeline"]
    assert p["worstStatus"] == "gray"
    assert _stage(p, "evaluate")["status"] == "gray"
    assert _stage(p, "answer")["status"] == "gray"
    assert _stage(p, "recall")["status"] == "ok"      # 检索本身跑通了


def test_cross_domain_recall_is_gray(store):
    """跨域：问题不在知识域，召回段即判灰（不是检索坏了）。"""
    _chat(store, "a004", decision="cross_domain")
    _fault(store, "rag_refused", "原因：cross_domain", tid="a004")
    p = store.trace_detail("a004")["pipeline"]
    assert p["worstStatus"] == "gray"
    assert _stage(p, "recall")["status"] == "gray"
    assert "跨域" in _stage(p, "recall")["detail"]


def test_rag_degraded_is_warn(store):
    """检索链路故障 → 召回段黄（降级但仍服务），不是红。"""
    _chat(store, "a005", decision="degraded")
    _fault(store, "rag_degraded", "Milvus 连不上", tid="a005")
    p = store.trace_detail("a005")["pipeline"]
    assert _stage(p, "recall")["status"] == "warn"
    assert p["worstStatus"] == "warn"


def test_llm_fallback_is_error(store):
    """LLM 兜底话术 = 没答上 → 回答段红（真故障）。"""
    _chat(store, "b002", intent="business", mode="REACT", ok=False, error="LLM 持续故障")
    _fault(store, "llm_fallback", "本应正常回答，实际转兜底话术", tid="b002")
    p = store.trace_detail("b002")["pipeline"]
    assert _stage(p, "answer")["status"] == "error"
    assert p["worstStatus"] == "error"


def test_fingerprint_break_is_error(store):
    """指纹熔断 = 真故障 → 工具段红。"""
    _chat(store, "b003", intent="business", mode="REACT")
    _fault(store, "loop_fingerprint_break", "重复调用熔断", tid="b003")
    p = store.trace_detail("b003")["pipeline"]
    assert _stage(p, "tools")["status"] == "error"
    assert p["worstStatus"] == "error"


def test_tool_permission_blocked_is_warn_not_error(store):
    """权限拦截：用户拿得到解释，系统没坏 → 黄不红。"""
    _chat(store, "b004", intent="business", mode="REACT")
    _tool(store, "b004", 1, "drone_control", ok=False, blocked="权限不足")
    p = store.trace_detail("b004")["pipeline"]
    assert _stage(p, "tools")["status"] == "warn"
    assert p["worstStatus"] == "warn"


def test_retry_marker_is_warn(store):
    """自愈重试：推了模型一次然后成功 → 黄（路径不理想但服务到了）。"""
    _chat(store, "b005", intent="business", mode="REACT")
    _tool(store, "b005", 1, "-", ok=False, error="refusal_retry")
    _tool(store, "b005", 2, "drone_query", ok=True)
    p = store.trace_detail("b005")["pipeline"]
    assert _stage(p, "tools")["status"] == "warn"
    assert "重试" in _stage(p, "tools")["detail"]


# ---------- ③ idle 不参与渲染 ----------


def test_react_mode_plan_stage_is_idle(store):
    """REACT 模式没有规划阶段 → 必须 idle（标 ok 会让没发生的事看起来发生了）。"""
    _chat(store, "b006", intent="business", mode="REACT")
    p = store.trace_detail("b006")["pipeline"]
    assert _stage(p, "plan")["status"] == "idle"
    assert _stage(p, "route")["status"] == "ok"


def test_plan_mode_plan_stage_is_ok(store):
    """PLAN 模式确实规划了 → ok。"""
    _chat(store, "b007", intent="business", mode="PLAN")
    p = store.trace_detail("b007")["pipeline"]
    assert _stage(p, "plan")["status"] == "ok"


def test_no_delegation_stage_is_idle(store):
    """没委派时委派段 idle，不能默认标 ok。"""
    _chat(store, "b008", intent="business", mode="REACT")
    _tool(store, "b008", 1, "drone_query", ok=True)
    p = store.trace_detail("b008")["pipeline"]
    assert _stage(p, "delegate")["status"] == "idle"


# ---------- ④ 同阶段多条痕迹取最严重 ----------


def test_same_stage_takes_worst_status(store):
    """同一阶段挂 warn + error 两条痕迹 → 取 error。"""
    _chat(store, "b009", intent="business", mode="REACT")
    _fault(store, "tool_timeout", "工具超时", tid="b009")
    _fault(store, "tool_fail_break", "连续失败熔断", tid="b009")
    p = store.trace_detail("b009")["pipeline"]
    assert _stage(p, "tools")["status"] == "error"


# ---------- ⑤ 边界与健壮性 ----------


def test_unknown_trace_returns_no_pipeline(store):
    """查不到的 trace：found=false，pipeline 保持 None，不报错。"""
    d = store.trace_detail("deadbeef")
    assert d["found"] is False
    assert d["pipeline"] is None


def test_invalid_trace_format_returns_not_found(store):
    """格式非法直接返回 found=false（不查库、不抛异常）。"""
    d = store.trace_detail("../../../etc/passwd")
    assert d["found"] is False


def test_trace_detail_reads_before_any_write(store):
    """回归：**刚起进程、还没写过埋点就来读 trace** 不能静默返回 found=false。

    _conn 是懒初始化，旧实现直接判 `self._conn is None: return data`，
    表现为"接口 200 但查不到任何轨迹"，极易误判成数据丢失。
    """
    from app.services import telemetry as tl_mod
    fresh = tl_mod.TelemetryStore()
    fresh._enabled = True
    fresh._conn = None              # 模拟进程刚起
    d = fresh.trace_detail("abcdef12")
    assert d["enabled"] is True
    assert d["found"] is False      # 无数据是正常的，但不能因连接未建而误判


def test_answer_stage_gets_closing_detail(store):
    """正常收尾时回答段要有说明（空白像个缺失项）。"""
    _chat(store, "b00a", intent="business", mode="REACT")
    _tool(store, "b00a", 1, "drone_query", ok=True)
    p = store.trace_detail("b00a")["pipeline"]
    assert "1 步工具结果" in _stage(p, "answer")["detail"]


# ---------- ⑥ 记忆段（M-4，会话记忆规约 §6 G14）----------
#
# 着色矩阵（验收项「未裁剪/已裁剪/已过滤/降级」的判定口径）：
#   未裁剪 / 已裁剪 / 已过滤 → ok：都是设计内行为（预算机制 + 精选机制），
#       靠 detail 文案区分；打黄会让长会话满屏黄，把正常机制误读成路径不理想。
#   降级（memory_sqlite_degraded / memory_filter_degraded / memory_summary_failed）→ warn。
# 数字口径：载入/注入/压缩三列单位为消息条数，过滤淘汰 = total - used - trimmed。


def test_memory_stage_full_numbers(store):
    """过滤 + 裁剪都发生：文案按「载入 → 过滤 → 压缩 → 注入」闭环展示，状态 ok。"""
    _chat(store, "a0d1", intent="chitchat", memory=(26, 8, 8))
    p = store.trace_detail("a0d1")["pipeline"]
    st = _stage(p, "memory")
    assert st["status"] == "ok"
    assert "载入 26 条" in st["detail"]
    assert "过滤 10 条" in st["detail"]     # 26 - 8 - 8
    assert "压缩 8 条" in st["detail"]
    assert "注入 8 条" in st["detail"]


def test_memory_stage_untouched_is_ok(store):
    """未裁剪未过滤：载入=注入，没有过滤/压缩字样。"""
    _chat(store, "a0d2", intent="chitchat", memory=(6, 6, 0))
    p = store.trace_detail("a0d2")["pipeline"]
    st = _stage(p, "memory")
    assert st["status"] == "ok"
    assert "载入 6 条" in st["detail"]
    assert "过滤" not in st["detail"] and "压缩" not in st["detail"]
    assert "注入 6 条" in st["detail"]


def test_memory_stage_filtered_only(store):
    """只过滤未裁剪（业务链路中等长度会话的常见形态）。"""
    _chat(store, "a0d3", intent="business", mode="REACT", memory=(10, 8, 0))
    p = store.trace_detail("a0d3")["pipeline"]
    st = _stage(p, "memory")
    assert st["status"] == "ok"
    assert "过滤 2 条" in st["detail"]
    assert "注入 8 条" in st["detail"]


def test_memory_stage_degraded_is_warn(store):
    """过滤降级 → 记忆段黄；数字文案保留（数字唯一来源是三列，事件只管状态）。"""
    _chat(store, "a0d4", intent="chitchat", memory=(26, 26, 0))
    _fault(store, "memory_filter_degraded", "embedding 失败，已回退全量历史", tid="a0d4")
    p = store.trace_detail("a0d4")["pipeline"]
    st = _stage(p, "memory")
    assert st["status"] == "warn"
    assert "载入 26 条" in st["detail"]


def test_memory_stage_summary_failed_is_warn(store):
    """摘要失败 = 被裁内容没有补偿 → 黄（记忆段唯一的 warn 级设计内事件）。"""
    _chat(store, "a0d5", intent="chitchat", memory=(26, 8, 8))
    _fault(store, "memory_summary_failed", "摘要生成失败", tid="a0d5")
    p = store.trace_detail("a0d5")["pipeline"]
    assert _stage(p, "memory")["status"] == "warn"


def test_rag_memory_stage_is_idle(store):
    """rag 直读全量历史（规约 §8 开放问题 4）：idle 不画假节点。"""
    _chat(store, "a0d6", decision="kept", docs="d-a")
    p = store.trace_detail("a0d6")["pipeline"]
    assert _stage(p, "memory")["status"] == "idle"


def test_memory_stage_zero_is_idle(store):
    """首轮无历史 / 未登录 / v6 前存量：idle，不冒充装配过。"""
    _chat(store, "a0d7", intent="chitchat", memory=(0, 0, 0))
    p = store.trace_detail("a0d7")["pipeline"]
    st = _stage(p, "memory")
    assert st["status"] == "idle"
    assert "无历史记忆" in st["detail"]


def test_memory_columns_migration_idempotent(store):
    """v6 迁移幂等：同一库重复建连/迁移，不报错、不重复加列。"""
    from app.services import telemetry as tl_mod

    cols_before = {r[1] for r in store._conn.execute(
        "PRAGMA table_info(agent_chat_log)").fetchall()}
    assert {"memory_turns_total", "memory_turns_used", "memory_trimmed"} <= cols_before
    # 同一路径再起两个实例（模拟重启后再次迁移）
    again = tl_mod.TelemetryStore()
    again._enabled = True
    again._ensure_conn()
    again._ensure_conn()
    cols_after = {r[1] for r in store._conn.execute(
        "PRAGMA table_info(agent_chat_log)").fetchall()}
    assert cols_after == cols_before


def test_record_chat_consumes_memory_stats(store):
    """note_memory_stats → record_chat → 落库 → trace 读取，全链路数字一致；消费即清。"""
    from app.services import telemetry as tl_mod

    tl_mod.set_trace_id("4cd70001")
    tl_mod.note_memory_stats(total=26, used=8, trimmed=8)
    store.record_chat(session_id="s-4cd70001", intent="chitchat", model="m")
    store.flush_now()
    chat = store.trace_detail("4cd70001")["chat"]
    assert chat["memoryTurnsTotal"] == 26
    assert chat["memoryTurnsUsed"] == 8
    assert chat["memoryTrimmed"] == 8

    # 消费即清：同一任务内第二次 record_chat 不得复用上一轮数字
    tl_mod.set_trace_id("4cd70002")
    store.record_chat(session_id="s-4cd70002", intent="chitchat", model="m")
    store.flush_now()
    chat2 = store.trace_detail("4cd70002")["chat"]
    assert (chat2["memoryTurnsTotal"], chat2["memoryTurnsUsed"],
            chat2["memoryTrimmed"]) == (0, 0, 0)

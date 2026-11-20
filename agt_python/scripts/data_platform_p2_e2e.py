"""数据中台 P2 离线验收（feedback 闭环 + 告警联动 + CSV 导出）。

设计：
- 全程临时目录建库（telemetry_db_path 指向 tempfile），绝不碰 data/metrics/ 产品库；
- 不起 uvicorn，直接进程内调 TelemetryStore 方法（接口层只是薄包装）；
- jsdstu 用 stub 替身（alarm_link 的延迟导入按模块属性查找，monkeypatch 生效）；
- 每个用例独立断言，一条失败不影响后续（final exit code 汇总）。

运行：python scripts/data_platform_p2_e2e.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import telemetry as telemetry_mod  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="p2_e2e_"))
settings.telemetry_db_path = str(TMP / "observability.db")
settings.feedback_capture_content = True

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def fresh_store() -> telemetry_mod.TelemetryStore:
    store = telemetry_mod.TelemetryStore()
    store._enabled = True
    store._ensure_conn()
    return store


def seed_chat(store, trace_id: str, session_id: str, question: str, answer: str) -> None:
    """造一条已落库的会话行 + 会话记忆最后一轮（模拟真实对话后的状态）。"""
    conn = store._ensure_conn()
    conn.execute(
        "INSERT INTO agent_chat_log(trace_id, session_id, user_id, permission, intent, intent_source, "
        "mode, model, prompt_tokens, completion_tokens, duration_ms, ok, degraded, content_chars, "
        "error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trace_id, session_id, 1, "admin", "business", "rule", "REACT", "test-model",
         100, 50, 1234.0, 1, 0, len(answer), "", "2026-09-18T12:00:00"))
    conn.commit()
    from app.services.memory import session_memory
    session_memory.record_turn(session_id, question, answer)


async def main() -> int:
    print("== 1. feedback 提交：合法 / 非法评分 / 未知 trace ==")
    store = fresh_store()
    seed_chat(store, "aabbccdd1122", "sess-ok", "现在有多少台无人机？", "共 9 台。")

    r = store.submit_feedback(trace_id="aabbccdd1122", rating="down", comment="数量不对")
    check("合法点踩 recorded", r.get("recorded") is True, str(r))
    r = store.submit_feedback(trace_id="aabbccdd1122", rating="up")
    check("同 trace 二次反馈=新事件", r.get("recorded") is True, str(r))
    r = store.submit_feedback(trace_id="aabbccdd1122", rating="5星")
    check("非法评分被拒", r.get("recorded") is False and "rating" in (r.get("reason") or ""), str(r))
    r = store.submit_feedback(trace_id="ffffffffffff", rating="up")
    check("未知 trace 被拒", r.get("recorded") is False and "trace" in (r.get("reason") or ""), str(r))
    r = store.submit_feedback(trace_id="..//evil", rating="up")
    check("非法 traceId 格式被拒", r.get("recorded") is False, str(r))

    n_content = store._conn.execute("SELECT COUNT(*) FROM feedback_content").fetchone()[0]
    check("内容表捕获 2 行（两次合法反馈）", n_content == 2, f"n={n_content}")
    q = store._conn.execute(
        "SELECT question, answer FROM feedback_content WHERE trace_id='aabbccdd1122' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    check("内容来自会话记忆最后一轮", q == ("现在有多少台无人机？", "共 9 台。"), str(q))

    print("== 2. feedback 列表：join 会话轨迹 + 统计 ==")
    lst = store.feedback_list(hours=24)
    check("列表 2 行", len(lst["rows"]) == 2, str(len(lst["rows"])))
    row = lst["rows"][0]
    check("行含会话摘要字段", row["intent"] == "business" and row["chatOk"] is True
          and row["durationMs"] == 1234.0, str(row))
    check("统计 up=1 down=1", lst["up"] == 1 and lst["down"] == 1, str(lst))
    lst_down = store.feedback_list(hours=24, rating="down")
    check("rating 过滤", len(lst_down["rows"]) == 1 and lst_down["rows"][0]["rating"] == "down")

    print("== 3. 金标集候选导出（JSONL）==")
    cand = store.eval_candidates()
    check("候选按 trace 去重=1 行", cand["count"] == 1, str(cand["count"]))
    obj = json.loads(cand["jsonl"].splitlines()[0])
    check("qid=fb-<traceId>", obj["qid"] == "fb-aabbccdd1122", obj["qid"])
    check("question 来自内容表", obj["question"] == "现在有多少台无人机？", obj["question"])
    check("reference_answer 在场", obj["reference_answer"] == "共 9 台。")
    check("rating 保留", obj["rating"] == "up")
    check("category=feedback_candidate", obj["category"] == "feedback_candidate")

    print("== 4. overview 反馈统计 ==")
    ov = store.overview()
    check("feedback24h up=1 down=1", ov.get("feedback24h", {}) == {"up": 1, "down": 1}, str(ov.get("feedback24h")))

    print("== 5. CSV 导出：五个视图 ==")
    for view, min_len in (("chats", 2), ("tools", 1), ("delegations", 1), ("faults", 1), ("feedback", 2)):
        exp = store.export_csv(view=view, hours=24)
        check(f"export {view} 有表头+内容", exp["content"].count("\n") >= min_len
              and exp["filename"].startswith(f"metrics_{view}_"), exp.get("reason", ""))
    bad = store.export_csv(view="nope")
    check("非法 view 被拒", "reason" in bad)

    print("== 6. 告警联动：stub jsdstu（精确故障关联 + ±30 分钟近似匹配）==")

    class StubClient:
        async def get(self, path, token=None, params=None):
            assert path == "/v1/alarm/list"
            # 记录**全部**调用：B1 之后 alarm_link 会拉两次（窗口一次 + 独立 pending 一次），
            # 只留最后一次（`self.seen_params = params`）会让"时间窗口参数下传"断言
            # 误取到 pending 那次调用而失败——属测试桩未跟进产品变更。
            self.calls.append(params or {})
            self.seen_params = params
            return {"list": [
                {"id": 1, "title": "电量低告警", "severity": "P1", "status": "pending",
                 "droneCode": "D001", "triggeredAt": "2026-09-18 12:20:00"},   # 距委派 20 分钟 → 匹配
                {"id": 2, "title": "围栏告警", "severity": "P0", "status": "processing",
                 "droneCode": "D002", "triggeredAt": "2026-09-18 13:30:00"},   # 距委派 90 分钟 → 不匹配
            ]}

    class _Stub(StubClient):
        def __init__(self):
            self.calls: list[dict] = []
            self.seen_params: dict = {}

    stub = _Stub()
    original = telemetry_mod.__dict__.get("jsdstu_client")
    import app.services.biz as biz_mod
    real_client = biz_mod.jsdstu_client
    biz_mod.jsdstu_client = stub
    try:
        seed_delegation = store._ensure_conn().execute(
            "INSERT INTO agent_delegation(trace_id, session_id, agent, status, duration_ms, created_at) "
            "VALUES('aabbccdd1122','sess-ok','alarm-analyst','DONE',5000.0,'2026-09-18T12:00:00')")
        store._conn.commit()
        link = await store.alarm_link(hours=24, token="fake-token", window_minutes=30)
        check("linked=True", link["linked"] is True, str(link.get("reason")))
        check("委派 1 条且带故障列表", len(link["delegations"]) == 1
              and isinstance(link["delegations"][0]["faults"], list))
        matched = {a["id"] for a in link["alarms"] if a["matchedTraceIds"]}
        check("近似匹配只中 12:20 那条", matched == {1}, str(matched))
        # 断言"窗口那次调用"确实下传了 startTime/endTime；
        # 并确认 pending 那次**不带**时间窗参数（两者语义必须分开，见数据中台规约 §6/§8）。
        win = [c for c in stub.calls if c.get("startTime")]
        pend = [c for c in stub.calls if c.get("status") == "pending"]
        check("时间窗口参数下传 jsdstu",
              bool(win) and win[0].get("startTime") == "2026-09-18 11:30:00"
              and win[0].get("endTime") == "2026-09-18 12:30:00", str(stub.calls))
        check("独立拉取 pending 告警（不带时间窗）",
              bool(pend) and not pend[0].get("startTime"), str(stub.calls))
        no_token = await store.alarm_link(hours=24, token=None)
        check("无 token 优雅降级", no_token["linked"] is False and "登录" in (no_token["reason"] or ""), str(no_token))
    finally:
        biz_mod.jsdstu_client = real_client
        del stub

    print("== 7. 反馈域 TTL 清理（内容表 30 天 / 事件表 90 天，统一入口 _cleanup_expired）==")
    # 7.1 内容表：改成 2026-05-01（远超 30 天）→ 应被清
    store._conn.execute(
        "UPDATE feedback_content SET created_at = '2026-05-01T00:00:00' WHERE trace_id='aabbccdd1122'")
    store._conn.commit()
    # 7.2 事件表造两行：一行超期(200 天前)、一行未超期(10 天前) → 只清超期那条
    store._conn.execute(
        "INSERT INTO agent_feedback(trace_id, session_id, user_id, rating, comment, created_at) "
        "VALUES('ttl-old','sess-ttl',1,'up','过期反馈','2026-02-01T00:00:00')")
    store._conn.execute(
        "INSERT INTO agent_feedback(trace_id, session_id, user_id, rating, comment, created_at) "
        "VALUES('ttl-new','sess-ttl',1,'down','新鲜反馈', datetime('now','localtime','-10 days'))")
    store._conn.commit()
    before_fb = store._conn.execute("SELECT COUNT(*) FROM agent_feedback").fetchone()[0]

    store._cleanup_expired()

    left_content = store._conn.execute("SELECT COUNT(*) FROM feedback_content").fetchone()[0]
    check("内容表过期行被清（30 天口径）", left_content == 0, f"left={left_content}")
    old_gone = store._conn.execute(
        "SELECT COUNT(*) FROM agent_feedback WHERE trace_id='ttl-old'").fetchone()[0]
    check("事件表超期行被清（90 天口径）", old_gone == 0, f"left={old_gone}")
    new_kept = store._conn.execute(
        "SELECT COUNT(*) FROM agent_feedback WHERE trace_id='ttl-new'").fetchone()[0]
    check("事件表未超期行保留（不许误删）", new_kept == 1, f"left={new_kept}")
    check("事件表删除量恰为 1 行", before_fb - store._conn.execute(
        "SELECT COUNT(*) FROM agent_feedback").fetchone()[0] == 1, f"before={before_fb}")

    print("== 8. telemetry 关闭时静默降级 ==")
    store._enabled = False
    r = store.submit_feedback(trace_id="aabbccdd1122", rating="up")
    check("关闭时 recorded=False", r.get("recorded") is False and r.get("reason") == "telemetry disabled")

    print(f"\n结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)

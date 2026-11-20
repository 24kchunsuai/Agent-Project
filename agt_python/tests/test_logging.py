"""E-6 结构化日志 + 链路上下文单测（企业级落地规约 §4 E-6，判据 C3）。

覆盖四组：

1. **字段契约**（`docs/日志规范.md` §2）：九字段恒在、顺序固定、时区带偏移、
   `event`/`durationMs` 的缺省语义 —— 契约一旦漂移，下游 `jq` 脚本就开始"时好时坏"；
2. **脱敏**（§7）：登记的密钥字面量、正则兜底、**已掩码形态不被二次破坏**；
3. **落盘与轮转**（§4/§5）：测试期不写仓库 `deploy/logs/`、轮转产生 `.1`、装配幂等；
4. **traceId 链路**（§6）：采纳入站 `X-Trace-Id`、**拒绝畸形头**（防日志伪造）、
   回写响应头、`http.request` 带耗时、日志与埋点取同一个 id。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import logging_setup as L
from app.core import trace as T
from app.core.config import settings
from app.main import app

AGENT_SHARED_KEY_TEST = os.environ["AGENT_SHARED_KEY"]
client = TestClient(app)
HEADERS = {"X-Agent-Key": AGENT_SHARED_KEY_TEST}

SPEC_FIELDS = ["ts", "level", "service", "traceId", "userId", "event", "durationMs", "msg", "err"]


@pytest.fixture
def logfile(tmp_path):
    """把结构化日志指到 tmp_path 并重新装配；退出时恢复默认目录。

    ⚠️ 退出时必须**恢复并重新 setup**：`reset_for_test()` 会摘掉所有 handler，
    不恢复的话后续用例的日志会被静默丢弃（表现为"某几个用例莫名查不到日志"）。
    """
    saved = (settings.log_dir, settings.log_max_bytes)
    settings.log_dir = str(tmp_path)
    settings.log_max_bytes = 200_000
    L.reset_for_test()
    L.setup_logging()
    try:
        yield L.log_file()
    finally:
        L.reset_for_test()
        settings.log_dir, settings.log_max_bytes = saved
        L.setup_logging()


def read_lines(path: Path) -> list[dict]:
    file = Path(path)
    if not file.exists():            # 一条都没写过时文件可能还没建
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# --------------------------------------------------------------- 1 字段契约


def test_json_line_has_all_spec_fields_in_order(logfile):
    logging.getLogger("app.demo").info("hello")
    rows = read_lines(logfile)
    assert rows, "没有写出任何结构化日志"
    assert list(rows[-1].keys()) == SPEC_FIELDS


def test_timestamp_has_millis_and_timezone_offset(logfile):
    logging.getLogger("app.demo").info("ts 检查")
    ts = read_lines(logfile)[-1]["ts"]
    # 规范 §2 硬规则 3：只写 "12:40:01" 会在跨机器对齐时错 8 小时
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", ts), ts


def test_event_defaults_to_logger_name(logfile):
    logging.getLogger("app.services.demo").info("无显式 event")
    assert read_lines(logfile)[-1]["event"] == "app.services.demo"


def test_explicit_event_and_duration_win(logfile):
    logging.getLogger("app.demo").info(
        "带 event", extra={"event": "chat.stream", "durationMs": 1234})
    row = read_lines(logfile)[-1]
    assert row["event"] == "chat.stream"
    assert row["durationMs"] == 1234


def test_duration_is_minus_one_when_not_measured(logfile):
    logging.getLogger("app.demo").info("没计时")
    # 规范 §2：不适用时为 -1（不用 null，避免消费者做两种空值判断）
    assert read_lines(logfile)[-1]["durationMs"] == -1


@pytest.mark.parametrize("bad", [None, "abc", object()])
def test_duration_non_numeric_falls_back_to_minus_one(logfile, bad):
    logging.getLogger("app.demo").info("坏值", extra={"durationMs": bad})
    assert read_lines(logfile)[-1]["durationMs"] == -1


def test_trace_and_user_id_come_from_context(logfile):
    tokens = T.bind("abc12345def67890", "42")
    try:
        logging.getLogger("app.demo").info("在请求里")
    finally:
        T.unbind(tokens)
    row = read_lines(logfile)[-1]
    assert row["traceId"] == "abc12345def67890"
    assert row["userId"] == "42"


def test_trace_and_user_are_empty_outside_request(logfile):
    logging.getLogger("app.demo").info("请求外")
    row = read_lines(logfile)[-1]
    assert row["traceId"] == ""
    assert row["userId"] == ""


def test_exception_stays_on_one_physical_line(logfile):
    """栈必须折叠进单行：多写一行就是多一条"日志"，JSON 解析也会崩。"""
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("app.demo").error("炸了", exc_info=True)
    text = Path(logfile).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    last = rows[-1]
    assert "ValueError" in last["err"]
    assert "\n" in last["err"]          # 换行被转义进 JSON 字符串里
    assert "Traceback" in last["err"]


def test_msg_newlines_do_not_break_json_lines(logfile):
    logging.getLogger("app.demo").info("第一行\n第二行\"带引号\"\\反斜杠")
    text = Path(logfile).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert "第一行" in rows[-1]["msg"]


def test_level_and_service_are_fixed(logfile):
    logging.getLogger("app.demo").warning("warn 级")
    row = read_lines(logfile)[-1]
    assert row["level"] == "WARNING"
    assert row["service"] == "agt_python"    # 规范 §2.1：两侧各一个固定值


# --------------------------------------------------------------- 2 脱敏


def test_registered_secret_is_masked(logfile):
    secret = "super-secret-token-abcdefghijklmnop"
    L.register_secret(secret)
    logging.getLogger("app.demo").info("密钥是 %s 请查", secret)
    row = read_lines(logfile)[-1]
    assert secret not in row["msg"]
    assert "***" in row["msg"]


def test_agent_key_header_value_is_masked(logfile):
    logging.getLogger("app.demo").info("X-Agent-Key: 7f3ac9d2e1b84f60")
    assert "7f3ac9d2e1b84f60" not in read_lines(logfile)[-1]["msg"]


def test_bearer_token_is_masked(logfile):
    logging.getLogger("app.demo").info("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9")
    msg = read_lines(logfile)[-1]["msg"]
    assert "eyJhbGciOiJIUzI1NiJ9" not in msg


def test_third_party_key_and_json_password_are_masked(logfile):
    logging.getLogger("app.demo").info('调用失败 body={"apiKey":"sk-abcdef0123456789"}')
    msg = read_lines(logfile)[-1]["msg"]
    assert "sk-abcdef0123456789" not in msg


def test_already_masked_key_is_not_double_masked(logfile):
    """`ab***yz(len=43)` 这类已掩码形态必须原样保留。

    如果这条挂了，说明脱敏正则把"掩码"也当成了密钥——于是日志里
    再也看不出密钥长度，排障时反而丢信息。
    """
    masked = "ab***yz(len=43)"
    logging.getLogger("app.demo").info("sharedKey=%s", masked)
    assert masked in read_lines(logfile)[-1]["msg"]


def test_qlen_only_records_length():
    # 规范 §7：完整用户输入不落日志，只记长度（内容是产品数据）
    assert L.qlen("无人机怎么起飞") == "len=7"
    assert L.qlen(None) == "len=0"


# --------------------------------------------------------------- 3 落盘与轮转


def test_log_dir_is_under_isolated_data_root():
    """测试/脚本期不得写仓库 deploy/logs（否则重演"测试污染产品目录"）。"""
    from app.core.paths import DATA_DIR

    resolved = str(L.log_dir())
    assert "deploy" not in resolved.replace("\\", "/").split("/")
    assert resolved.startswith(str(DATA_DIR))


def test_rotation_creates_backup_file(logfile):
    settings.log_max_bytes = 300          # 逼它立刻轮到第二份
    L.reset_for_test()
    L.setup_logging()
    logger = logging.getLogger("app.demo")
    for i in range(30):
        logger.info("填充第 %d 条，长度够长好触发轮转 %s", i, "x" * 40)
    backup = Path(str(L.log_file()) + ".1")
    assert backup.exists(), "超过 maxBytes 后应生成 .1（与 Java FixedWindow 命名一致）"
    assert L.log_file().exists()


def test_setup_logging_is_idempotent(logfile):
    """重复装配不得让一条日志写两遍（启动路径 + 测试路径都会调用它）。"""
    L.setup_logging()
    L.setup_logging()
    logging.getLogger("app.demo").info("只应出现一次的一次性标记")
    rows = read_lines(logfile)
    assert sum(1 for r in rows if r["msg"] == "只应出现一次的一次性标记") == 1


def test_third_party_debug_is_silenced(logfile):
    """第三方 DEBUG 必须被压掉。

    实测证据：一次对话请求命中 23 条日志，其中 14 条是 httpcore 的
    connect/TLS/send/receive 细节。它们也被同一个 formatter 染上 traceId，
    于是"看上去像业务日志"——业务行反被淹掉，这正是结构化日志最难用的一种坏法。
    """
    logging.getLogger("httpcore.connection").debug("connect_tcp.started host='api.deepseek.com'")
    assert not read_lines(logfile), "第三方 DEBUG 不该进结构化日志（应为 WARNING 级）"


def test_our_own_debug_still_recorded(logfile):
    """压的是第三方，不是我们自己：本项目的 DEBUG 必须留着（排障就靠它）。"""
    logging.getLogger("app.services.agent_service").debug("本轮装配细节")
    assert read_lines(logfile)[-1]["msg"] == "本轮装配细节"


# --------------------------------------------------------------- 4 traceId 链路


def test_incoming_trace_header_is_adopted(logfile):
    """E-6 的核心：Java 传来的 id 必须被采纳，而不是自造一个。"""
    resp = client.get("/v1/health", headers={**HEADERS, T.TRACE_HEADER: "deadbeef12345678"})
    assert resp.headers.get(T.TRACE_RESPONSE_HEADER) == "deadbeef12345678"


def test_malformed_trace_header_is_rejected(logfile):
    """畸形 id 不得进入日志（否则一个引号就能伪造出日志行）。"""
    evil = 'evil"id\ninjected'
    resp = client.get("/v1/health", headers={**HEADERS, T.TRACE_HEADER: evil})
    actual = resp.headers.get(T.TRACE_RESPONSE_HEADER)
    assert actual and actual != evil
    assert re.fullmatch(r"[0-9A-Za-z_-]{4,64}", actual), actual


def test_response_always_carries_trace_header(logfile):
    """连鉴权拒绝的响应也要带 id —— 那正是最需要 traceId 的场景。"""
    resp = client.get("/v1/kb/stats")           # 无 key → 4010
    assert resp.json()["code"] == 4010
    assert re.fullmatch(r"[0-9A-Za-z_-]{4,64}",
                        resp.headers.get(T.TRACE_RESPONSE_HEADER) or "")


def test_user_header_is_adopted_but_validated(logfile):
    ok = client.get("/v1/health", headers={**HEADERS, T.USER_HEADER: "7"})
    assert ok.status_code == 200
    bad = client.get("/v1/health", headers={**HEADERS, T.USER_HEADER: "7); drop"})
    assert bad.status_code == 200             # 非法值被忽略，不影响请求


def test_http_request_event_logged_with_duration(logfile):
    tid = "0f0f0f0f0f0f0f0f"
    client.get("/v1/kb/stats", headers={**HEADERS, T.TRACE_HEADER: tid})
    rows = [r for r in read_lines(logfile) if r["event"] == "http.request"]
    assert rows, "http.request 事件缺失：按 traceId 串链路就断了一端"
    row = rows[-1]
    assert row["traceId"] == tid
    assert row["durationMs"] >= 0             # 计过时就不再是 -1
    assert row["level"] == "INFO"


def test_quiet_path_success_is_not_logged(logfile):
    """探针成功不记（否则 30s 一条把结构化日志冲成噪音），失败才记。"""
    client.get("/v1/health", headers=HEADERS)
    rows = [r for r in read_lines(logfile) if r["event"] == "http.request"]
    assert not rows


def test_request_log_omits_query_string(logfile):
    """规范 §7：查询串里常有用户输入与 key —— 不记比"脱敏"更可靠。"""
    client.get("/v1/kb/stats?keyword=%E6%97%A0%E4%BA%BA%E6%9C%BA&key=abcdef",
               headers=HEADERS)
    rows = [r for r in read_lines(logfile) if r["event"] == "http.request"]
    if rows:                                   # /v1/kb/stats 非 quiet 路径，应当有
        assert "keyword" not in rows[-1]["msg"]
        assert "abcdef" not in rows[-1]["msg"]


def test_logging_and_telemetry_share_one_trace_id(logfile):
    """日志与埋点必须取同一个 id，否则"查日志"和"查埋点"会给出两套结果。"""
    from app.services.telemetry import current_trace_id, set_trace_id

    tokens = T.bind("5a5a5a5a5a5a5a5a", "")
    try:
        assert current_trace_id() == "5a5a5a5a5a5a5a5a"
        logging.getLogger("app.demo").info("埋点与日志同一 id")
        assert read_lines(logfile)[-1]["traceId"] == "5a5a5a5a5a5a5a5a"
    finally:
        # ⚠️ 必须清理 contextvar：泄漏后（如 test_pii 被排在本文件前、
        # 或有人单独指定文件顺序），后续文件的"无请求上下文"断言会假失败
        T.unbind(tokens)


def test_ensure_trace_id_reuses_existing(logfile):
    """`ensure_trace_id` 是业务入口用的那个：已有就复用，没有才新建。"""
    tokens = T.bind("cafebabe12345678", "")
    try:
        assert T.ensure_trace_id() == "cafebabe12345678"
    finally:
        T.unbind(tokens)
    try:
        first = T.ensure_trace_id()      # 无上下文 → 新建一个
        second = T.ensure_trace_id()
        assert first == second and len(first) == 16
    finally:
        # ⚠️ 新建的 id 已被 ensure_trace_id 绑进 contextvar，必须显式清空——
        # 泄漏会打爆其它文件里"无请求上下文 trace_id 应为空"的断言（乱序运行实测踩到）。
        # 不能用 bind("")+unbind()：reset 语义会把值还原成"泄漏的那个"。
        T.set_trace_id("")

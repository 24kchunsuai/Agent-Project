"""S-8 PII 脱敏单测（企业级落地规约 S-8，与 E-6 同源）。

覆盖三组：

1. **规则本身**（`app/core/pii.py::redact_pii`）：手机号 / 身份证 / 邮箱三类命中替换；
   **反例同样重要**——非 PII 文本一字不动（否则日志就没法读了）；
2. **接线**：`logging_setup.redact()` 出口（日志 + client_error 同源）、
   `telemetry._digest()`（埋点写入路径，**先脱敏再截断**）；
3. **开关**：`PII_REDACT_ENABLED=false` 时原样放行（受控调试逃生口）。

顺序契约（邮箱 → 身份证 → 手机号）由"手机号邮箱"用例钉死：
先吃数字会剩下 `***@139.com`，`*` 不在邮箱字符集里、邮箱规则永远救不回来。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core import logging_setup as L
from app.core.config import settings
from app.core.pii import redact_pii


@pytest.fixture
def pii_off():
    """临时关闭 S-8 开关（受控调试形态），退出时恢复。"""
    saved = settings.pii_redact_enabled
    settings.pii_redact_enabled = False
    try:
        yield
    finally:
        settings.pii_redact_enabled = saved


@pytest.fixture
def logfile(tmp_path):
    """把结构化日志指到 tmp_path（与 test_logging.py 的同名 fixture 同语义）；
    退出时恢复目录并重新装配，否则后续用例的日志会被静默丢弃。"""
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


# --------------------------------------------------------------- 1 规则本身


def test_phone_masked():
    assert redact_pii("联系我 13812345678 谢谢") == "联系我 *** 谢谢"


def test_id_card_masked_with_x_suffix():
    assert redact_pii("身份证 11010119900307851X 登记过了") == "身份证 *** 登记过了"


def test_id_card_masked_all_digits():
    assert redact_pii("id=110101199003078515") == "id=***"


def test_email_masked():
    assert redact_pii("发送到 zhang.san@example.com 即可") == "发送到 *** 即可"


def test_phone_style_email_is_fully_masked_not_residue():
    """顺序契约：13812345678@139.com 必须整体脱成 ***，不能剩 ***@139.com。"""
    assert redact_pii("邮箱 13812345678@139.com") == "邮箱 ***"


def test_mixed_pii_all_masked():
    text = "手机 13812345678，证件 110101199003078515，邮箱 a@b.com.cn"
    assert redact_pii(text) == "手机 ***，证件 ***，邮箱 ***"


def test_id_card_inside_long_digits_not_partial_eaten():
    """18 位长号整体替换，不能被手机号规则吃掉中段留下残渣。"""
    out = redact_pii("编号110101199003078515后缀")
    assert out == "编号***后缀"
    assert "199003" not in out and "078515" not in out


def test_trace_id_with_embedded_digits_not_corrupted():
    """hex 感知环视（真机存量扫描的实证）：16 位 hex traceId 内嵌 11 位十进制段
    `13378864346`，纯数字环视会被手机号规则误伤、把日志里的 traceId 打花。"""
    tid = "1db4f13378864346"
    assert redact_pii(f"trace {tid} failed") == f"trace {tid} failed"


# ---- 反例：非 PII 文本一字不动（过度脱敏会让日志失去可读性）----


@pytest.mark.parametrize("text", [
    "查询无人机 D007 的告警",                     # 纯中文
    "端口 8000，设备编号 D007",                   # 4 位数字
    "订单号 1234567890123456789012345",           # 25 位长数字：两侧环视挡住手机号/身份证
    "编号 12345678901",                           # 11 位但 1 后面是 2（非 1[3-9] 段）
    "今天天气不错，我们去试飞",                   # 常规中文句子
    "Bearer eyJhbGciOiJIUzI1NiJ9 已过期",         # token 交给 E-6 层管
    "",                                           # 空串
])
def test_non_pii_untouched(text):
    assert redact_pii(text) == text


def test_already_masked_text_not_double_damaged():
    assert redact_pii("key=*** email=***") == "key=*** email=***"


def test_masked_form_with_len_marker_survives():
    """E-6 的掩码形态 `ab***yz(len=43)` 不被 S-8 二次破坏。"""
    text = "ab***yz(len=43)"
    assert redact_pii(text) == text


# --------------------------------------------------------------- 2 接线


def test_redact_log_outgoing_masks_pii():
    """E-6 出口（redact）叠加 S-8：一条日志同时带密钥与 PII 时两类都被抹掉。"""
    text = "user 13812345678 with X-Agent-Key: abc123def456 done"
    out = L.redact(text)
    assert "13812345678" not in out
    assert "abc123def456" not in out


def test_jsonl_log_line_masks_pii(logfile):
    logging.getLogger("app.demo").info("用户留下手机 13912345678")
    rows = [json.loads(line) for line in
            logfile.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and "13912345678" not in rows[-1]["msg"]
    assert "***" in rows[-1]["msg"]


def test_telemetry_digest_masks_pii():
    """埋点写入路径（_digest）落库摘要无明文 PII。"""
    from app.services.telemetry import _digest
    digest = _digest("客户 13812345678 投诉")
    assert "13812345678" not in digest and "***" in digest


def test_telemetry_digest_masks_before_truncation():
    """先脱敏再截断：PII 恰跨截断边界时也不能漏出半截。

    反例（若实现成"先截断再脱敏"）：`xx13812345678yy` 截 5 字符得 `xx138`——
    5 位残片既脱不掉也不完整；先脱敏则得 `xx***yy` 再截，无残渣。
    """
    from app.services.telemetry import _digest
    digest = _digest("xx13812345678yy", limit=5)
    assert digest == "xx***"


def test_telemetry_digest_handles_non_string():
    from app.services.telemetry import _digest
    assert _digest({"phone": "13812345678"}) == '{"phone": "***"}'
    assert _digest(None) == ""


def test_client_error_report_masks_pii(monkeypatch):
    """client_error 的 message/stack 在服务层过同一个 redact()，PII 不得入库。

    直测服务层（与 test_client_error.py 同款姿势），不依赖真实端口与库。
    """
    from app.services import client_error as ce

    captured: dict = {}

    class _Store:
        def record_client_error(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ce, "telemetry", _Store())
    monkeypatch.setattr(ce.settings, "client_error_sample_rate", 1.0)
    monkeypatch.setattr(ce.settings, "client_error_enabled", True)

    out = ce.report(
        {"page": "/dashboard", "source": "vue",
         "message": "前端崩了，联系方式 13812345678",
         "stack": "TypeError at zhang@corp.com:1:1"},
        user_id=1,
    )
    assert out["accepted"], out
    assert "13812345678" not in captured["message"]
    assert "zhang@corp.com" not in captured["stack"]
    assert "***" in captured["message"] and "***" in captured["stack"]


# --------------------------------------------------------------- 3 开关


def test_switch_off_passes_through(pii_off):
    assert redact_pii("手机 13812345678") == "手机 13812345678"


def test_switch_on_by_default():
    assert settings.pii_redact_enabled is True

"""S-1 服务间信任边界单测（企业级落地规约 §S-1 验收标准）。

覆盖四件事：
1. 无 key / 错 key → **4010**，且落一条 `auth_key_rejected`（可查账）；
2. 对 key → 放行（走到路由层）；
3. `/v1/health` 与文档页**无 key 可达**（探针不能被鉴权拦死，否则监控全瞎）；
4. `agent_shared_key` 为空 → 拒绝启动。

为什么用 TestClient 而不是 httpx.ASGITransport + pytest-asyncio：项目其它测试
（tests/test_agent_api.py）就是 TestClient，且没有 pytest-asyncio 依赖；保持一致
可以少一个依赖、少一类"异步 fixture 写法"的坑。TestClient 不走 lifespan
（未用 `with`），因此不会拉起 scheduler / telemetry 后台任务——本测试只关心
入口鉴权，不需要那些。
"""

import os

from fastapi.testclient import TestClient

from app.core.auth import assert_shared_key_configured
from app.core.config import settings
from app.main import app
from app.services.telemetry import PATH_AUTH_KEY_REJECTED, telemetry

# 密钥由 tests/conftest.py 注入进程环境（早于任何 app.* 导入）。
# ⚠️ 刻意**不** `from tests.conftest import ...`：那会再执行一次 conftest 的模块级
# 代码（又一次 mktemp 与 DATA_ROOT 重置），留下一个不会被清理的临时目录。
AGENT_SHARED_KEY_TEST = os.environ["AGENT_SHARED_KEY"]

client = TestClient(app)

HEADERS = {"X-Agent-Key": AGENT_SHARED_KEY_TEST}


def test_settings_reads_shared_key_from_env():
    """配置确实从环境变量读进来了——否则中间件会拿空串比对，全部请求被拒。"""
    assert settings.agent_shared_key == AGENT_SHARED_KEY_TEST


def test_missing_key_is_rejected():
    resp = client.get("/v1/kb/stats")
    body = resp.json()
    assert body["code"] == 4010
    assert resp.status_code == 200  # 沿用项目约定：HTTP 恒 200，业务码在 body


def test_wrong_key_is_rejected():
    resp = client.get("/v1/kb/stats", headers={"X-Agent-Key": "not-the-key"})
    assert resp.json()["code"] == 4010


def test_empty_key_is_rejected():
    """传空头等价于没传：不能被当成"匹配空密钥"放行。"""
    resp = client.get("/v1/kb/stats", headers={"X-Agent-Key": ""})
    assert resp.json()["code"] == 4010


def test_correct_key_passes_through():
    """对 key 必须放行到路由层：这里用一个不存在的路径，
    期望拿到 3002（请求数据不存在）而不是 4010 —— 证明拦的是"鉴权"而非"路由"。
    """
    resp = client.get("/v1/definitely-not-a-route", headers=HEADERS)
    assert resp.json()["code"] == 3002


def test_health_is_exempt():
    """探针豁免：监控与 Java 健康检查都必须无 key 可达。"""
    resp = client.get("/v1/health")
    assert resp.json()["code"] == 2000


def test_root_and_openapi_are_exempt():
    """根路径与 OpenAPI 文档豁免（只含 app 名/版本与接口结构，无业务数据）。"""
    assert client.get("/").json()["app"] == settings.app_name
    assert "paths" in client.get("/openapi.json").json()


def test_rejections_are_counted():
    """规约要求：校验失败要落一条 auth_key_rejected（降级计数 + fault_event）。

    这里断言计数器；fault_event 明细走同一 record_fault 调用（进内存队列，
    由 telemetry 的落库循环批量写），队列内容的正确性由 telemetry 自身测试覆盖。
    """
    before = telemetry.counters_snapshot().get(PATH_AUTH_KEY_REJECTED, 0)
    client.get("/v1/kb/stats")
    client.get("/v1/kb/stats", headers={"X-Agent-Key": "bad"})
    after = telemetry.counters_snapshot().get(PATH_AUTH_KEY_REJECTED, 0)
    assert after - before == 2


def test_blank_key_refuses_to_start():
    """fail-closed：空密钥拒绝启动（"忘了配密钥就上线"必须在启动时炸掉）。"""
    for bad in ("", "   ", "\t"):
        try:
            assert_shared_key_configured(bad)
        except RuntimeError as exc:
            assert "AGENT_SHARED_KEY" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"空密钥 {bad!r} 竟然通过了启动校验")


def test_configured_key_passes_start_check():
    assert_shared_key_configured("any-non-empty") is None


# ================================================ E-10 密钥轮换：双密钥过渡窗口
#
# 为什么不用 TestClient 测这一段：`app.add_middleware(...)` 在 import 时就固化了
# 当时的密钥，改 settings 不会再影响已构造的中间件；要测多个密钥组合就得重建 app。
# 直接驱动 ASGI 中间件更准，也顺带能构造 TestClient 造不出来的**畸形请求头**。


def _call_middleware(mw, path: str, headers: list[tuple[bytes, bytes]]):
    """同步驱动一次纯 ASGI 中间件调用，返回 `(status, allowed)`。

    ⚠️ 返回值刻意**不是** `(status, body)`：本项目约定"HTTP 恒 200，业务码在 body"，
    中间件**拒绝时也返回 200**。所以只断言 status 根本区分不出放行与拒绝
    （这一点是被 mutation test 抓出来的——见 DEV_ISSUES §6.82）。
    这里直接把 body 翻译成"是否放行"，避免每个用例重复这段判断。
    """
    import asyncio
    import json

    sent: list[dict] = []
    scope = {"type": "http", "path": path, "headers": headers,
             "client": ("127.0.0.1", 12345), "method": "GET"}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    status = sent[0]["status"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        code = json.loads(body)["code"] if body else None
    except (ValueError, KeyError):  # pragma: no cover —— 防御式
        code = None
    return status, code == 2000


async def _downstream_ok(scope, receive, send):
    """下游桩：走到这里就说明鉴权放行了。"""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"code":2000}'})


def _mw(current: str, prev: str = ""):
    from app.core.auth import SharedKeyMiddleware

    return SharedKeyMiddleware(_downstream_ok, shared_key=current, prev_key=prev)


def test_accepted_keys_order_dedupe_and_blank():
    """密钥集合的构造规则：顺序即优先级、去重、空值不参与。

    先过滤空值再判重（而不是反过来）——否则"两把都是空串"会被去重成一把空密钥，
    看起来"配置了"，实际是"谁都能进"。
    """
    from app.core.auth import accepted_keys

    assert accepted_keys("NEW", "OLD") == ("NEW", "OLD")
    assert accepted_keys("NEW", "") == ("NEW",)
    assert accepted_keys("NEW", "NEW") == ("NEW",)      # 同值去重
    assert accepted_keys("", "") == ()
    assert accepted_keys("  NEW  ", "") == ("NEW",)     # 去空白


def test_rotation_window_accepts_both_keys():
    """轮换期：新密钥与上一把密钥**都能放行**——这是"不停服轮换"的全部依据。"""
    mw = _mw("NEW-KEY", "OLD-KEY")

    assert _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"NEW-KEY")])[1] is True
    assert _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"OLD-KEY")])[1] is True


def test_rotation_window_rejects_third_key():
    """轮换期≠放行期：只有那两把能进，随便一把别的照样 4010。

    这条防的是"过渡窗口被实现成一段时间内不校验"这类危险简化。
    """
    mw = _mw("NEW-KEY", "OLD-KEY")

    status, allowed = _call_middleware(
        mw, "/v1/kb/stats", [(b"x-agent-key", b"SOMEONE-ELSE")])

    assert status == 200  # 项目约定：HTTP 恒 200，业务码在 body
    assert allowed is False


def test_prev_cleared_means_old_key_dead_immediately():
    """清掉 PREV（= 不在轮换期）后，旧密钥**立刻**失效——不再有任何过渡窗口。

    这条是"轮换结束"的定义：停用旧密钥不需要额外动作，删配置 + 重启即可。
    """
    mw = _mw("NEW-KEY", "")

    assert _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"NEW-KEY")])[1] is True
    assert _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"OLD-KEY")])[1] is False


def test_legacy_use_is_counted_but_never_counted_as_fault():
    """旧密钥放行：**只计数、不落 fault_event**。

    为什么必须分开：`auth_key_rejected` 是"真被拒绝"，看板按它算故障；
    而"轮换期还有人拿旧钥匙"请求是**成功处理完的**，把它算进故障数就是让看板说谎。
    缺了计数又不行——它是"能不能把 PREV 清掉"的唯一机器判据。
    """
    from app.services.telemetry import (
        PATH_AUTH_KEY_LEGACY_USED, PATH_AUTH_KEY_REJECTED, telemetry,
    )

    mw = _mw("NEW-KEY", "OLD-KEY")
    legacy_before = telemetry.counters_snapshot().get(PATH_AUTH_KEY_LEGACY_USED, 0)
    reject_before = telemetry.counters_snapshot().get(PATH_AUTH_KEY_REJECTED, 0)

    _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"OLD-KEY")])

    after = telemetry.counters_snapshot()
    assert after.get(PATH_AUTH_KEY_LEGACY_USED, 0) - legacy_before == 1
    assert after.get(PATH_AUTH_KEY_REJECTED, 0) == reject_before


def test_current_key_hit_is_not_counted_as_legacy():
    """用当前密钥不该被记成"旧钥匙还在用"，否则这个指标永远不为 0、失去判据价值。"""
    from app.services.telemetry import PATH_AUTH_KEY_LEGACY_USED, telemetry

    mw = _mw("NEW-KEY", "OLD-KEY")
    before = telemetry.counters_snapshot().get(PATH_AUTH_KEY_LEGACY_USED, 0)

    _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"NEW-KEY")])

    assert telemetry.counters_snapshot().get(PATH_AUTH_KEY_LEGACY_USED, 0) == before


def test_legacy_warning_is_emitted_once_per_process(caplog):
    """旧密钥的 WARNING **每进程只喊一次**：轮换窗口可持续数天，逐条 WARN 会刷屏。

    长期趋势交给指标（计数器），日志只负责"让人第一次知道有这回事"。
    """
    import logging

    from app.core import auth

    # `_legacy_warned` 是进程级开关，其它用例可能已把它置 True —— 显式复位
    assert hasattr(auth, "_legacy_warned"), "缺少每进程一次的告警开关"
    monkey = auth._legacy_warned
    auth._legacy_warned = False
    try:
        mw = _mw("NEW-KEY", "OLD-KEY")
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"OLD-KEY")])
            _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", b"OLD-KEY")])
        hits = [r for r in caplog.records if "上一把" in r.getMessage()]
        assert len(hits) == 1
    finally:
        auth._legacy_warned = monkey


def test_non_ascii_key_header_is_rejected_not_500():
    """畸形请求头（非 ASCII 字节）必须走 4010，不能让比较逻辑抛异常变 500。

    这是 E-10 顺带修掉的**既有缺陷**：原实现把头部 decode 成 str 后交给
    `hmac.compare_digest(str, str)`，而它对非 ASCII 会抛 `TypeError`。
    请求头是攻击者可控的 —— 发一个含 0xFF 的头就能把"该拒绝"变成 500，
    既污染看板又给了廉价探测信号。改为比字节后对任意输入都安全。
    """
    mw = _mw("NEW-KEY", "OLD-KEY")

    for bad in (b"\xff\xfe\xfd", b"NEW-KEY\xff", "\u4e2d\u6587key".encode("utf-8")):
        status, allowed = _call_middleware(mw, "/v1/kb/stats", [(b"x-agent-key", bad)])
        assert status == 200, f"畸形头 {bad!r} 不该变成 HTTP {status}"
        assert allowed is False, f"畸形头 {bad!r} 应当被拒"


def test_prev_equal_to_current_refuses_to_start():
    """PREV 与当前密钥同值 → 拒绝启动。

    不是洁癖：两把一样意味着"以为在轮换其实没换"。操作者会在观察指标时
    看到 legacy 计数恒为 0（命中的永远是当前密钥），据此得出"可以停用旧密钥"，
    而真相是**旧密钥从未被停用**。静默的安全债远比启动失败危险。
    """
    try:
        assert_shared_key_configured("SAME-KEY", "SAME-KEY")
    except RuntimeError as exc:
        assert "AGENT_SHARED_KEY_PREV" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("PREV 与当前密钥同值时竟然通过了启动校验")


def test_distinct_keys_pass_start_check():
    """正常轮换配置必须通过（否则没人敢轮换）。"""
    assert assert_shared_key_configured("NEW-KEY", "OLD-KEY") is None
    assert assert_shared_key_configured("NEW-KEY") is None

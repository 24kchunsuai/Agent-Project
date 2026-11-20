"""前端运行时报错收集（企业级落地规约 E-12，判据 C3「可观测」）。

为什么需要它
------------
E-12 之前，"前端炸了"这件事系统里**没有任何记录**：用户看到的是一片空白或
一句"操作失败"，而后端日志干干净净——因为错误发生在浏览器里，从没到过服务端。
排障只能靠"用户描述 + 让他按 F12 截图"，这在工单里是最常见的死循环。
E-3 的顶栏状态灯也帮不上：它看的是服务端组件，前端自己崩了它依然是绿的。

三处收口（漏一处就有一整类错误收不到）
--------------------------------------
* `Vue.config.errorHandler` —— 组件渲染 / 生命周期 / 事件处理器内的异常（Vue 会吞掉它们，
  不配这个的话连控制台都可能只有一行 warning）；
* `window.onerror`        —— 组件之外的同步脚本错误（含第三方库、路由钩子）；
* `unhandledrejection`    —— 未 catch 的 Promise（忘了 await / 漏 catch 的 async 最常见）。

只挂其中一两个是常见错误：**Vue 的 errorHandler 收不到** `addEventListener` 里的异步
回调异常与未处理的 Promise，而那恰好是真实项目里最多的一类。三处缺一不可。

三条口径（与埋点铁律同源）
--------------------------
1. **上报失败绝不影响用户**：本模块与上报接口里的任何异常都在内部消化、返回成功。
   把"错误上报失败"变成用户可见的错误，等于用一个观测能力制造一个新的故障源。
2. **合并去重**：见 `_bucket`。前端一个渲染循环里的报错能在几秒内刷出上千条，
   逐条落库既撑爆表、也让看板退化成一屏滚动字幕。
3. **截断 + 脱敏**：`message` / `stack` 截断入库（长度在 config，可调）并过 `redact()`
   抹掉密钥类文本；`page` 丢掉查询串与 hash 片段——那里常有 id、token、用户输入。

身份从哪来（**结构性约束，不靠自觉**）
------------------------------------
上报体的模型里**没有 `userId` 字段**，身份只能由上游（Java）以 `user_id` 查询参数注入。
这样"前端传的 userId 一律不采信"就不是一条口头约定，而是**字段根本不存在**。
（与 S-1 同一条铁律：身份只由 Java 注入。）
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.logging_setup import redact
from app.services.telemetry import telemetry

logger = logging.getLogger(__name__)

#: 收口来源白名单：**不用自由文本**。这一列要给看板分组，脏值会让分组碎成一地
#: （"vue" / "Vue" / "vue3" / "vue-error" 各算一类，看板就废了）。
SOURCES = ("vue", "window", "promise")
SOURCE_UNKNOWN = "unknown"

#: page 的最大长度（与 config 里的 max_message 无关：路径本来就短，200 足够）
_PAGE_LIMIT = 200
#: 版本号 / UA 的截断长度。UA 完整值常有 150+ 字符，但看板只需要"哪个浏览器"这层信息
_UA_LIMIT = 200
_VERSION_LIMIT = 32
#: fingerprint 取 sha256 前 16 位十六进制：碰撞概率可忽略，而短到能直接显示
_FP_CHARS = 16
#: message 参与指纹计算时的截断长度。取前 200 字符：
#: 报错信息的前半段已足够区分错误类型，而把整段（可能带随机 id）算进指纹会让
#: **同一个错误每次算出不同的指纹 → 去重完全失效**。
_FP_MESSAGE_CHARS = 200


def _clamp_rate(value: Any) -> float:
    """采样率收敛到 [0, 1]。配错值（如 2 / -1）时**收敛而不是拒绝启动**：
    采样率是个可观测性旋钮，不该有能力让服务起不来。"""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, rate))


def _truncate(text: str, limit: int) -> str:
    """截断并显式标注被截断（`…`）——看的人要知道"后面还有内容没显示"，
    否则会把截断后的怪结尾当成真实报错文本。"""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _clean_page(raw: Any) -> str:
    """页面标识：只保留 **path**，丢掉 `?query` 与 `#hash`。

    为什么必须丢：SPA 的 hash 路由与查询串里常带 id、筛选条件，乃至第三方回跳时
    捎带的 token。这些进了库就是"用户数据落在可观测表里"，而本项目的规矩是
    观测表**只记结构与计数，不记内容**（数据中台规约 §10）。
    """
    text = str(raw or "").strip()
    for sep in ("?", "#"):
        idx = text.find(sep)
        if idx >= 0:
            text = text[:idx]
    return _truncate(text, _PAGE_LIMIT)


def _normalize_source(raw: Any) -> str:
    """来源归一到白名单；不认识的一律记 `unknown`（**保留而不是丢弃**：
    出现 unknown 本身就说明"我们的收口漏了某种场景"，这正是要看的信息）。"""
    text = str(raw or "").strip().lower()
    return text if text in SOURCES else SOURCE_UNKNOWN


def _fingerprint(page: str, source: str, message: str) -> str:
    """错误指纹：`page | source | message 前 200 字符` 的 sha256 前 16 位。

    这是"同一个错误"的**定义**（合并去重全靠它），所以口径必须写死在这里：
    换指纹口径 = 换了一套去重语义，历史数据与新数据的 count 不可比。
    """
    raw = f"{page}|{source}|{message[:_FP_MESSAGE_CHARS]}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:_FP_CHARS]


def _bucket(now: float, window_seconds: int) -> str:
    """时间桶：把"合并窗口"落到一个**确定性**的键上。

    用**固定桶**（`floor(now/window)*window`）而不是"距上次出现不足 N 秒就合并"的
    滑动窗口。滑动窗口要么在每次上报时读一次库（把上报路径变成一次查询，报错风暴时
    恰好是最不该加负载的时候），要么在内存里维护一张随时间漂移的表（重启即失，
    多 worker 下各持一份、合并结果还取决于请求落到哪个进程）。

    固定桶是无状态、可重启、可单测的。代价：**跨桶边界的两次相同错误会各占一行**
    （例如 10:00:59 与 10:01:01）。这个代价在"看频率趋势"这个用途上无所谓，
    而换来的是"同一分钟必然合并"这个可断言的确定性。

    ⚠️ 非法窗口（`<=0` / 缺失 / 非数字）一律收敛为 **1 秒**，不是 60 秒。
    1 秒是**最小的合并窗口 = 最保守的合并**（最不容易把两条无关错误并成一条）。
    这里的坑在于 `window_seconds or 60` 这种写法：`0 or 60` 得 60，而
    `-5 or 60` 得 -5 再被 `max(1, ...)` 抬成 1 —— **同样是"非法值"却走出两个不同窗口**。
    配错值时的行为必须唯一且可预测，所以这里显式收敛到 1。
    """
    try:
        window = int(window_seconds)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        window = 1
    start = int(now // window) * window
    return datetime.fromtimestamp(start).isoformat(timespec="seconds")


def report(payload: dict[str, Any], *, user_id: int = 0) -> dict[str, Any]:
    """处理一次前端上报：清洗 → 采样 → 指纹 → 入队。**永不抛异常**。

    `user_id` 由调用方（接口层）从**上游注入的参数**传入，绝不出自 `payload`。

    返回结构（前端不需要，但排障时看它一眼就能知道"为什么这条没进库"）：
    `{accepted, stored, reason, fingerprint?, bucket?}`
    """
    out: dict[str, Any] = {"accepted": False, "stored": False, "reason": ""}
    try:
        if not settings.client_error_enabled:
            # 关掉的开关就明确说关掉了，而不是静默丢弃——静默丢弃会让
            # "看板怎么一直是空的"变成一个要翻配置才能回答的问题
            out["reason"] = "client_error disabled"
            return out

        rate = _clamp_rate(settings.client_error_sample_rate)
        if rate <= 0:
            out["reason"] = "sampled out (rate=0)"
            return out
        if rate < 1 and random.random() >= rate:
            out["reason"] = "sampled out"
            return out

        page = _clean_page(payload.get("page"))
        source = _normalize_source(payload.get("source"))
        message = _truncate(
            redact(str(payload.get("message") or "").strip()),
            int(settings.client_error_max_message),
        ) or "(无消息)"
        stack = _truncate(
            redact(str(payload.get("stack") or "")),
            int(settings.client_error_max_stack),
        )
        app_version = _truncate(str(payload.get("appVersion") or "").strip(), _VERSION_LIMIT)
        user_agent = _truncate(str(payload.get("userAgent") or "").strip(), _UA_LIMIT)

        fingerprint = _fingerprint(page, source, message)
        bucket = _bucket(time.time(), settings.client_error_dedup_seconds)
        telemetry.record_client_error(
            fingerprint=fingerprint, bucket=bucket, page=page, source=source,
            message=message, stack=stack, app_version=app_version,
            user_agent=user_agent, user_id=int(user_id or 0),
        )
        out.update({
            "accepted": True,
            # 埋点总开关关闭时 record_client_error 是空操作 —— 这里如实说"收了但没存"，
            # 不谎报 stored=true（"接口说存了、库里没有"是最难查的一类不一致）
            "stored": bool(settings.telemetry_enabled),
            "fingerprint": fingerprint,
            "bucket": bucket,
        })
    except Exception as exc:  # noqa: BLE001 —— 上报链路上任何异常都必须在这里终止
        logger.warning("[WARN] 前端报错上报处理失败（已忽略）: %s: %s", type(exc).__name__, exc)
        out["reason"] = f"internal: {type(exc).__name__}"
    return out


def summary(*, hours: int = 24, limit: int = 20) -> dict[str, Any]:
    """看板数据源：委托给埋点库（SQL 与口径都在 `telemetry.client_error_summary`）。

    这里只做参数收敛，**不重复实现聚合口径**（铁律 7：判定逻辑只许有一份）。
    """
    return telemetry.client_error_summary(hours=hours, limit=limit)

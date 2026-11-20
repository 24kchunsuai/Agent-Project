"""结构化日志（企业级落地规约 **E-6**，判据 C3）——**字段契约的唯一实现处**。

契约与理由见 `docs/日志规范.md`；本模块只做三件事：

1. **格式**：把一条 `LogRecord` 渲染成**固定九字段**的 JSON 单行
   （`ts/level/service/traceId/userId/event/durationMs/msg/err`）；
2. **轮转**：`RotatingFileHandler` 写 `<log_dir>/8000-agt_python.jsonl`，50MB × 10 份；
3. **脱敏**：出口处统一过一遍 `redact()`（与 S-8 同源），**不靠"写日志时记得别写"**。

## 为什么"双 handler + 控制台保持人类可读"

排障有两种动作：**按 id 精确串链路**（要机器格式）与**肉眼扫一眼断在哪**（要人读格式）。
两者都留：`.jsonl` 是查询口径，控制台输出被计划任务重定向进 `.log`
（进程在 logging 初始化之前就死掉时，那是唯一线索）。见 `docs/日志规范.md` §4。

## 为什么脱敏要"字面量 + 正则"两层

正则只能挡住**已知写法**（`X-Agent-Key: xxx`），换个措辞就漏。所以启动时把
**真实密钥值本身**登记成字面量（`register_secret`），命中即替换——
"值出现"这件事与措辞无关。正则作为第二层，兜住还没配到值就发生的日志。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import traceback
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.paths import BASE_DIR, DATA_DIR, is_isolated
from app.core.pii import redact_pii
from app.core.trace import get_trace_id, get_user_id

#: 结构化字段顺序（与 `docs/日志规范.md` §2 逐字一致；改这里要同时改 Java 的 JsonLogLayout）
FIELDS = ("ts", "level", "service", "traceId", "userId", "event", "durationMs", "msg", "err")

#: 长度下限：短于它的"密钥"不当字面量替换，否则会把正常文本（如 "true"）打成 ***
_MIN_SECRET_LEN = 8

_secrets: set[str] = set()

#: 第二层：已知写法的正则。**顺序有意义**——`Bearer` 那条必须排在
#: `Authorization` 那条**前面**：否则 `Authorization: Bearer eyJ...` 会先被
#: authorization 规则把值 "Bearer" 替掉，真正的 token 反而留在日志里
#: （这是本模块首次跑单测就抓到的缺陷，见 test_bearer_token_is_masked）。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer <token>：值必须是"像 token 的东西"（6 位以上），避免把 "Bearer 已过期" 打花
    (re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-]{6,})"), "Bearer ***"),
    # X-Agent-Key: v / AGENT_SHARED_KEY=v / x-agent-key v
    # ⚠️ 值部分要求 6 位以上 `[A-Za-z0-9._-]`：这样 `ab***yz(len=43)` 这类
    # **已掩码**的形态不会被二次破坏（`*` 不在字符集里，凑不满 6 位）。
    (re.compile(r"(?i)(x-agent-key|agent[_-]?shared[_-]?key)\s*[:=]?\s*([A-Za-z0-9._\-]{6,})"),
     r"\1=***"),
    # Authorization: <value>（含 "Bearer" 已被上一条吃掉后的残形）
    (re.compile(r"(?i)(authorization|proxy-authorization)\s*[:=]\s*(?:Bearer\s+)?(\S+)"),
     r"\1 ***"),
    (re.compile(r"(?i)\b(x-token|agt[_-]?token|access[_-]?token|refresh[_-]?token|token)"
                r"\s*[:=]\s*([^\s,;\"'}]+)"), r"\1=***"),
    # JSON 形态：{"apiKey":"sk-xxx"} / {"password":"xxx"}
    (re.compile(r"(?i)\"(api[_-]?key|secret|password|passwd|pwd|shared[_-]?key)\"\s*:\s*\"[^\"]*\""),
     r'"\1":"***"'),
    # 常见第三方密钥形态
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"), "***"),
)


def register_secret(value: str | None) -> None:
    """登记一个**真实密钥值**：此后它出现在任何日志里都会被替换成 `***`。"""
    if value and len(value) >= _MIN_SECRET_LEN:
        _secrets.add(value)


def redact(text: str) -> str:
    """抹掉日志文本里的密钥/token 与 PII（S-8）。**只做减法，绝不抛异常**（日志出口不能失败）。"""
    if not text:
        return ""
    try:
        for secret in _secrets:
            if secret in text:
                text = text.replace(secret, "***")
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
    except Exception:  # noqa: BLE001 —— 脱敏失败宁可少脱，也不能让日志写不出去
        return text
    # S-8：密钥层之后叠加 PII 层（手机号/身份证/邮箱，规则单点在 app/core/pii.py）。
    # 放在 try 外是刻意的：pii 自己兜全部异常，且它管的是另一类规则，互不拖累。
    return redact_pii(text)


def qlen(text: str | None) -> str:
    """用户输入的**长度**描述（`len=37`）。

    日志规范 §7 规定"完整用户输入不得进日志，只记长度"——这个助手就是把
    这条约定变成"一行可调用的东西"，比在注释里叮嘱可靠。
    内容是产品数据：用同一个 `traceId` 去 `/v1/agent/metrics/traces/{traceId}` 查。
    """
    return f"len={len(text or '')}"


# ---------------------------------------------------------------- 格式化


def _ts(record: logging.LogRecord) -> str:
    """`record.created`（浮点秒）→ 带本地时区偏移的 ISO-8601。

    刻意用 `record.created` 而非 `datetime.now()`：日志被队列/延迟写出时，
    时间戳仍应是**事件发生的那一瞬**。
    """
    return datetime.fromtimestamp(record.created).astimezone().isoformat(
        timespec="milliseconds")


def _duration(value: object) -> int:
    """`durationMs` 归一化：非数字/缺失一律 `-1`（规范 §2：不用 null，避免两种空值判断）。"""
    try:
        return int(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


class JsonFormatter(logging.Formatter):
    """把一条记录渲染成 JSON 单行（`json.dumps` 会把换行转义，所以物理上恒为一行）。"""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = redact(record.getMessage())
        except Exception:  # noqa: BLE001 —— 格式化参数出错也得留下痕迹
            msg = "<unformattable>"
        err = ""
        if record.exc_info:
            err = redact("".join(traceback.format_exception(*record.exc_info)).strip())
        elif record.exc_text:
            err = redact(record.exc_text)
        payload = {
            "ts": _ts(record),
            "level": record.levelname,
            "service": self._service,
            "traceId": get_trace_id(),
            "userId": get_user_id(),
            "event": str(getattr(record, "event", "") or record.name),
            "durationMs": _duration(getattr(record, "durationMs", -1)),
            "msg": msg,
            "err": err,
        }
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """人读格式（保持改造前的观感，`.log` 里靠它肉眼扫）。

    两处刻意的设计：

    1. **带 `[traceId]` 前缀**（只有处于请求上下文里才带）。控制台输出会被计划任务
       重定向进 `.log`，带上 id 之后"按 traceId 一次 grep"连 `.log` 也一并命中，
       不用先猜"该去哪个文件找"。
    2. **同样过 `redact()`**。`.log` 也是落盘文件，脱敏不能只在 `.jsonl` 那条路上做——
       只在一边脱敏等于没脱。
    """

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id()
        head = (
            f"{self.formatTime(record, '%H:%M:%S')} | {record.levelname:<5} | "
            f"{record.name} | "
        )
        if trace_id:
            head += f"[{trace_id}] "
        try:
            text = head + redact(record.getMessage())
        except Exception:  # noqa: BLE001
            text = head + "<unformattable>"
        if record.exc_info:
            text += "\n" + redact(self.formatException(record.exc_info))
        elif record.exc_text:
            text += "\n" + redact(record.exc_text)
        return text


# ---------------------------------------------------------------- 装配


def log_dir() -> Path:
    """结构化日志目录。

    - 默认 `<仓库根>/deploy/logs`（与部署手册、`trim-logs.ps1` 同一个约定）；
    - `LOG_DIR` 可覆盖（容器化时挂卷会用到）；
    - **数据目录被隔离时（测试/脚本）自动改到隔离目录下** —— 否则跑一次单测
      就会往仓库 `deploy/logs/` 里写文件，重演"测试污染产品目录"的老问题
      （见 `DEV_ISSUES.md` §6.37）。
    """
    raw = (settings.log_dir or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (BASE_DIR / p)
    if is_isolated():
        return DATA_DIR / "logs"
    return BASE_DIR.parent / "deploy" / "logs"


def log_file() -> Path:
    """结构化日志文件：`<端口>-<服务>.jsonl`（与 `.log` 同名不同后缀，一眼能配对）。"""
    name = (settings.log_json_file or "").strip() or f"{settings.port}-{settings.log_service}.jsonl"
    return log_dir() / name


def _level() -> int:
    raw = (settings.log_level or "").strip().upper()
    if raw:
        return getattr(logging, raw, logging.INFO)
    return logging.DEBUG if settings.debug else logging.INFO


#: 第三方库的 DEBUG 会把结构化日志冲成噪音。
#: 实测证据：一次对话请求命中 23 条日志，其中 **14 条** 是 httpcore 的
#: connect/TLS/send/receive 细节。它们会被同一个 formatter 染上 traceId，
#: 于是"看上去像业务日志"，反而把真正的业务行淹掉。
#: 压到 WARNING：出错（如连不上）仍然看得见，正常时序细节不再记。
_NOISY_LOGGERS = (
    "httpcore",          # httpx 的底层连接库，一次 LLM 调用 12+ 行
    "httpx",
    "urllib3",
    "asyncio",
    "openai",
    "multipart",
    "watchfiles",
    "pymilvus",
)

#: 是否已装配（`setup_logging` 幂等的依据）
_configured = False


def setup_logging() -> logging.Logger:
    """装配根 logger（**幂等**：重复调用只保留一套 handler，避免日志写 N 遍）。"""
    global _configured

    register_secret(settings.agent_shared_key)
    # E-10：轮换期的**上一把**密钥同样必须脱敏。它在过渡窗口里仍是有效凭据——
    # 漏掉它会让「正在被淘汰的密钥」以明文出现在日志里，比不脱敏更糟：
    # 一把密钥被写进日志就等于已经泄露，而它此刻还能开门。
    register_secret(getattr(settings, "agent_shared_key_prev", ""))
    register_secret(getattr(settings, "llm_api_key", ""))
    register_secret(getattr(settings, "embedding_api_key", ""))
    register_secret(getattr(settings, "llm_backup_api_key", ""))

    root = logging.getLogger()
    if _configured:
        return root

    root.setLevel(_level())
    for handler in list(root.handlers):       # 清掉 basicConfig / uvicorn 留下的
        root.removeHandler(handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(path),
            maxBytes=int(settings.log_max_bytes),
            backupCount=int(settings.log_backup_count),
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter(settings.log_service))
        root.addHandler(file_handler)
    except OSError as exc:                     # 目录不可写不该让服务起不来
        root.warning("结构化日志文件不可写（降级为仅控制台）: %s cause=%s", path, exc)

    _configured = True
    return root


def reset_for_test() -> None:
    """单测用：允许重新装配（`setup_logging` 是幂等的，测试要换目录必须显式重置）。"""
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()                    # Windows 上不 close 会锁住文件删不掉
        except Exception:  # noqa: BLE001
            pass
    _secrets.clear()
    _configured = False

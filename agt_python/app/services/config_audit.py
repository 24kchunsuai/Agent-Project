"""配置变更留痕（E-8，判据 C5）。

为什么有这个东西
----------------
规约 E-8 要求审计覆盖面包含"配置变更"。但本系统**没有运行时改配置的入口**：
Python 侧配置来自 `.env`，Java 侧来自 `application.properties`，改动路径一律是
"改文件 + 重启"。于是系统能观测到配置变化的唯一时刻就是**启动时**。

做法：启动时对当前生效配置算指纹，与上次记住的快照比对，变了就写一条
`CONFIG_CHANGE` 审计（含**变了哪些键、从什么变成什么**）。这样审计页能回答
"这套系统的配置什么时候被动过、动了什么"，不用去翻 git 或问人。

安全铁律
--------
配置里有 `AGENT_SHARED_KEY` / `LLM_API_KEY` 这类密钥，**绝不能进审计表**。
敏感判定用**键名**（含 key / secret / password / token / credential 即视为敏感），
敏感项只记 `sha256:<12 位前缀>`——足够判断"换没换"，且不可反推原值。

为什么单独一个快照文件（`data/ops/config_snapshot.json`）
-----------------------------------------------------------
审计行只记"变了什么"；要算出"变了什么"，就必须有一份**上次的完整快照**做比对。
把 137 个键塞进审计行的 `detail` 会让表迅速膨胀，而快照是**运营状态**不是审计留痕，
放文件更合适（与 telemetry 的 `fault_counters.json` 同一思路）。

失败语义
--------
全程吞异常：配置留痕失败绝不能拦住服务启动（与埋点铁律一致——
"非阻塞、绝不抛异常"）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

WARN_PREFIX = "[CFG-AUDIT]"

#: 快照落盘位置（`data/ops/`，与 backups / governance / metrics 同级）
SNAPSHOT_DIR = DATA_DIR / "ops"
SNAPSHOT_NAME = "config_snapshot.json"

#: 敏感键名判定片段（小写子串匹配）——命中则只记哈希，不记值
SECRET_HINTS: tuple[str, ...] = (
    "key", "secret", "password", "passwd", "token", "credential",
)

#: 键名里含这些片段也按敏感处理（`base_url` 可能带 query 里的凭据）
_URL_HINTS: tuple[str, ...] = ("api_key", "apikey", "auth")

#: 不参与指纹的字段：每次构建都变，算进去会让"重新打包"看起来像"改了配置"，
#: 直接导致告警疲劳。版本与提交号要记（那是真正的可追溯信息）。
EXCLUDED_FIELDS: frozenset[str] = frozenset({"build_time", "buildTime"})

#: 审计 detail 里最多列几个变更键（防某次大改把 detail 撑爆）
MAX_LISTED_KEYS = 25

#: 值转字符串时的截断长度（长列表/长串只留头部，指纹仍按全文算）
VALUE_PREVIEW_CHARS = 120


def is_secret_key(name: str) -> bool:
    """键名是否敏感（只看名字，不看值——值本身不参与判定，避免误判）。"""
    low = name.lower()
    if any(h in low for h in _URL_HINTS):
        return True
    return any(h in low for h in SECRET_HINTS)


def _short_hash(text: str) -> str:
    """12 位十六进制前缀——够区分、不可反推。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _preview(value: Any) -> str:
    """把值规整成可读且可控长度的字符串（用于 detail 与指纹）。"""
    if isinstance(value, (list, tuple)):
        text = json.dumps(list(value), ensure_ascii=False)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return text if len(text) <= VALUE_PREVIEW_CHARS else text[:VALUE_PREVIEW_CHARS] + "…"


def _field_names() -> list[str]:
    """当前 Settings 的全部字段名（按名排序，保证指纹稳定）。"""
    fields = getattr(type(settings), "model_fields", None)
    if fields is None:                       # pydantic v1 兜底
        fields = getattr(settings, "__fields__", {})
    return sorted(fields.keys())


def build_snapshot() -> dict[str, Any]:
    """当前生效配置的快照：{generatedAt, version, fingerprint, values}。

    `values` 里**敏感项存哈希**、其余存可读值（截断到 `VALUE_PREVIEW_CHARS`）。
    """
    values: dict[str, str] = {}
    for name in _field_names():
        if name in EXCLUDED_FIELDS:
            continue
        raw = getattr(settings, name, None)
        text = _preview(raw)
        values[name] = f"sha256:{_short_hash(text)}" if is_secret_key(name) else text

    # 版本与提交号是"配置的一部分"——发布换版时审计里应当看得到
    build = _build_marker()

    canonical = json.dumps(
        {"values": values, "build": build}, ensure_ascii=False, sort_keys=True
    )
    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "build": build,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "values": values,
    }


def _build_marker() -> dict[str, str]:
    """从 build_info 取**稳定**的版本标识。

    刻意**不含 `buildTime`**：每次打包它都会变，算进指纹会让"重新构建"
    被记成"配置变更"，几次之后审计页就没人看了。
    """
    try:
        from app.core.build_info import get_build_info  # noqa: PLC0415

        info = get_build_info().as_dict()
        return {
            "version": str(info.get("version") or "unknown"),
            "gitCommit": str(info.get("gitCommit") or "unknown"),
        }
    except Exception:  # noqa: BLE001 —— 取不到版本不影响配置留痕
        return {"version": "unknown", "gitCommit": "unknown"}


def _snapshot_path() -> Path:
    return SNAPSHOT_DIR / SNAPSHOT_NAME


def _load_prev() -> dict[str, Any] | None:
    """读上次快照；不存在或损坏都返回 None（当作"首次留痕"）。"""
    path = _snapshot_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 上次快照读取失败（按首次处理）| err=%s", WARN_PREFIX, e)
        return None


def _save_snapshot(snap: dict[str, Any]) -> None:
    """落盘（失败只告警——留痕不可用不该拦住启动）。"""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        content = json.dumps(snap, ensure_ascii=False, indent=2)
        # 先写临时文件再换名：避免"读到半截 JSON"被当成损坏快照
        tmp = _snapshot_path().with_suffix(".json.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(_snapshot_path())
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 快照落盘失败（已忽略）| err=%s", WARN_PREFIX, e)


def diff_snapshots(prev: dict[str, Any] | None,
                   cur: dict[str, Any]) -> dict[str, Any]:
    """比对两份快照，返回 {changed, added, removed, buildChanged, count}。

    - `changed`：键在两边都有但值不同 → `{键: {from, to}}`
    - `added` / `removed`：只在一边出现的键（新增配置项 / 删掉的配置项）
    - 敏感键的 from/to 已是哈希，不会泄露原值
    """
    prev_values: dict[str, str] = dict((prev or {}).get("values") or {})
    cur_values: dict[str, str] = dict(cur.get("values") or {})

    changed: dict[str, dict[str, str]] = {}
    for name in sorted(set(prev_values) & set(cur_values)):
        if prev_values[name] != cur_values[name]:
            changed[name] = {"from": prev_values[name], "to": cur_values[name]}
    added = sorted(set(cur_values) - set(prev_values))
    removed = sorted(set(prev_values) - set(cur_values))

    build_changed = bool(prev) and (prev.get("build") != cur.get("build"))
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "buildChanged": build_changed,
        "count": len(changed) + len(added) + len(removed),
    }


def _trim_diff(diff: dict[str, Any]) -> dict[str, Any]:
    """把 diff 压到可安全写进审计 detail 的大小；超出的只报数量。"""
    changed = dict(list(diff["changed"].items())[:MAX_LISTED_KEYS])
    added = diff["added"][:MAX_LISTED_KEYS]
    removed = diff["removed"][:MAX_LISTED_KEYS]
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "buildChanged": diff["buildChanged"],
        "count": diff["count"],
        "truncated": diff["count"] > MAX_LISTED_KEYS,
    }


def audit_config_change(*, first_time_max: bool = False) -> dict[str, Any]:
    """启动时调用：比对配置指纹，有变化就写审计并刷新快照。

    :return: 报告（供日志与单测断言）：
             `{firstRun, changed, audited, fingerprint, ...}`

    首次运行（没有上次快照）：**默认不写审计**——一次"从无到有"的记录
    信息量低还占一行。需要留痕时显式传 `first_time_max=True`。
    """
    report: dict[str, Any] = {"firstRun": False, "audited": False, "changed": 0}
    try:
        cur = build_snapshot()
        report["fingerprint"] = cur["fingerprint"]
        prev = _load_prev()

        if prev is None:
            report["firstRun"] = True
            if first_time_max:
                _write_audit(cur, None)
                report["audited"] = True
            _save_snapshot(cur)
            return report

        if prev.get("fingerprint") == cur["fingerprint"]:
            # 指纹一致：配置没动，**不落盘**（省一次写；也能暴露"指纹算得不稳"）
            report["changed"] = 0
            return report

        diff = diff_snapshots(prev, cur)
        report.update({"changed": diff["count"], **{
            k: diff[k] for k in ("added", "removed", "buildChanged")
        }, "changedKeys": sorted(diff["changed"].keys()),
            "prevGeneratedAt": prev.get("generatedAt")})
        _write_audit(cur, _trim_diff(diff))
        report["audited"] = True
        _save_snapshot(cur)
        return report
    except Exception as e:  # noqa: BLE001 —— 留痕失败绝不影响启动
        logger.warning("%s 配置留痕异常（已忽略）| err=%s", WARN_PREFIX, e)
        report["error"] = f"{type(e).__name__}: {e}"
        return report


def _write_audit(cur: dict[str, Any], diff: dict[str, Any] | None) -> None:
    """把变更写进治理域审计表（复用同一套 `audit()`，不新建第二套）。"""
    from app.services.governance_service import (  # noqa: PLC0415
        GovAction, TARGET_CONFIG, governance_service,
    )

    if diff is None:
        detail: dict[str, Any] = {
            "event": "首次留痕（此前无快照）",
            "version": cur["build"]["version"],
            "gitCommit": cur["build"]["gitCommit"],
        }
    else:
        detail = {"event": "生效配置发生变化", **diff,
                  "version": cur["build"]["version"],
                  "gitCommit": cur["build"]["gitCommit"]}

    governance_service.audit(
        GovAction.CONFIG_CHANGE,
        operator="system",
        target=f"{TARGET_CONFIG}env",     # 配置来自 .env / 启动环境
        result="SUCCESS",
        detail=detail,
    )
    logger.info("%s 已留痕 | changed=%s firstRun=%s",
                WARN_PREFIX, (diff or {}).get("count", "-"), diff is None)

"""LLM 成本计量与配额（企业级落地规约 E-13，判据 C7「可计量」）。

为什么需要它
------------
C7 的判据是"问'这个月大模型花了多少钱、哪个部门用的'，答得出"。E-13 之前
`agent_chat_log` 里虽已有 `prompt_tokens / completion_tokens / model / user_id`
（计量原料在库），但没有人把它们折成钱、也没有任何用量上限——token 烧到多少
只有 LLM 服务商的账单知道，且**事后才知道**。

三条设计决策（都会在看板口径里复述，改一处必须同步另一处）
------------------------------------------------------------
1. **成本是"查询期折算"，不在 DWS 冻结**。价目只存一份（`LLM_PRICE_JSON`），
   成本 = 读取期的 token 数 × 当前单价。理由：供应商改价是常态，若把"价格 ×
   token"预聚合进 `metrics_daily`，一次改价就会让历史口径整体漂移且无法追溯；
   而 token 数是不变量。代价是成本查询要扫 ODS 明细——本系统是单机单租户
   （D2），量级可忽略。
2. **单价单位 = 元 / 百万 token（CNY）**。与 DeepSeek 等供应商定价页的口径一致，
   手工核算时不用做单位换算（判据要求"与手工核算一致"）。
3. **配额超限"降级为提示"而非拒绝**（规约原文）。直接拒绝会把误伤放大成事故
   （一个跑批任务把当日额度烧完，整个部门下午不能用）；提示把控制权留给用户，
   同时落 `fault_event` 让运营侧看得见。**每用户每日至多一条** fault（防刷，
   超限后每轮对话都落一条的话，fault 表会被同一件事刷满）。

部门维度从哪来（如实登记，不装成熟）
------------------------------------
全项目目前**没有组织架构/部门管理**（用户管理在无人机主控 8060，本系统拿不到）。
规约说"复用用户画像的部门维度"——画像（M-5）是 LLM 抽取的 KV，用户聊到
"我是XX部门的"会落一个 `department` 键。因此部门 = 画像里**声明过的**部门，
读取期派生、不回填；没声明过的归 `unknown`。它回答的是"哪些人在用"，不是
"哪个组织在用"——这个边界写进了看板与交付物清单。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 画像库里的部门键名（与 user_profile 模块共用同一 KV 空间）
DEPT_KEY = "department"
#: 没有部门信息时的归并值（看板分组用，脏值会让分组碎成一地）
DEPT_UNKNOWN = "unknown"


def parse_prices(raw: str | None) -> dict[str, dict[str, float]]:
    """解析 `LLM_PRICE_JSON` 配置 → `{model: {"prompt": x, "completion": y}}`。

    单位：**元 / 百万 token**。配错的键/值**收敛处理而非拒启**（与 E-12 采样率
    同一条哲学：计量配置坏了不该拦着系统跑）：
    - JSON 非法 / 顶层不是 dict → 空表 + warning（此后成本恒为 0，看板会标"未配价"）；
    - 模型行不是 dict 或缺 completion 键 → 该行丢弃；
    - 数值非法 / 负数 → 该值按 0 处理（免费比报错安全：负价会算出"倒贴钱"）。
    """
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("LLM_PRICE_JSON 不是合法 JSON，价目表按空处理: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("LLM_PRICE_JSON 顶层不是对象，价目表按空处理: %r", str(raw)[:80])
        return {}
    prices: dict[str, dict[str, float]] = {}
    for model, spec in data.items():
        if not isinstance(spec, dict) or "completion" not in spec:
            logger.warning("价目表模型行不合法（需含 completion 键），已丢弃: %s", model)
            continue
        entry: dict[str, float] = {}
        for side in ("prompt", "completion"):
            try:
                val = float(spec.get(side) or 0)
            except (TypeError, ValueError):
                val = 0.0
            entry[side] = max(0.0, val)  # 负价收敛为 0：计量层不许出现"倒贴钱"
        prices[str(model)] = entry
    return prices


def price_table() -> dict[str, dict[str, float]]:
    """当前生效的价目表（每次现读 config：测试与轮转期间改配置即刻生效）。"""
    return parse_prices(settings.llm_price_json)


def model_cost(model: str, prompt_tokens: int, completion_tokens: int,
               prices: dict[str, dict[str, float]] | None = None) -> float:
    """单次调用成本（元）。未知模型返回 0.0——**缺价 ≠ 免费照标**，看板会单列"未配价"提示。"""
    table = prices if prices is not None else price_table()
    spec = table.get(str(model or ""))
    if not spec:
        return 0.0
    return (int(prompt_tokens or 0) * spec["prompt"]
            + int(completion_tokens or 0) * spec["completion"]) / 1_000_000.0


def quota_cap() -> int:
    """单用户日 token 上限（0 = 不限）。非法负值收敛为 0。"""
    return max(0, int(settings.quota_user_daily_tokens or 0))


def quota_notice(used_tokens: int, cap_tokens: int) -> dict[str, Any]:
    """超限提示文案（用户可见，因此是给人读的中文，不是错误码）。

    口径：`used` 是**含本轮**的当日累计（调用方在记录本轮之后查询），所以这里
    只描述事实与影响，不吓唬人——本系统超限不拒绝服务（规约原文的降级要求）。
    """
    return {
        "exceeded": True,
        "usedTokens": int(used_tokens or 0),
        "capTokens": int(cap_tokens or 0),
        "message": (
            f"提示：您的账号今日大模型用量已达上限（{int(used_tokens or 0):,} / "
            f"{int(cap_tokens or 0):,} tokens）。功能仍可正常使用，系统已记录本次超限，"
            "如需调高额度请联系管理员。"
        ),
    }

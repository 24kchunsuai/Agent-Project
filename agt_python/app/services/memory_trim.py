"""会话历史裁剪：token 预算 + 双锚点（docs/specs/会话记忆规约.md §6 M-2）。

解决的问题（缺口 G11 / G12）：
- G11：原窗口按「条数」算（`window=10`），长回复与短回复等价占位，预算不可控；
- G12：原窗口是**纯尾部**——会话聊到第 20 轮时，开头的「我是 XX 公司巡检员、
  只负责 3 号机」已被挤出，模型给的是不针对该用户的通用答案。

裁剪策略（借鉴 open-agent-master `ContextTrimmer`，但按其自认的弱项做了两处修正）：

    [头部锚点 head_turns 轮] + [中间摘要（超限才压）] + [尾部 tail_turns 轮]

- **头部锚点**：承载身份/任务/约束，纯尾部窗口会挤掉它们；
- **尾部保留**：承载当前正在进行的话题；
- **中间摘要**：仅在总量超预算时生成，且要过「条数门槛 + 占比门槛」双条件
  （防止为了一点内容付一次 LLM 调用）；
- **递归摘要**：新摘要的 prompt 里带上旧摘要，保证跨多次压缩信息不丢；
- **裁剪提示**：裁剪后追加一句系统提示告知模型"历史已压缩"，
  成本近零，但能防「基于残缺历史编造」（open-agent 有，值得照抄）。

与参考项目的差异（有意为之）：
1. 参考项目用「条数 × maxFillRatio」控制，这里用**真实 token 预算**，
   并按消息粒度收缩尾部，避免"条数没超但 token 超了"；
2. 参考项目的估算偏乐观（±20% 且方向不利），这里的 `TokenEstimator` 取保守方向；
3. 摘要失败时**降级为直接丢弃中间段并追加上文提示**，不阻断生成。
"""

import logging
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.memory_token import token_estimator

logger = logging.getLogger(__name__)

# 裁剪提示（追加在系统提示末尾；与 open-agent 的 trimmed-notice 同思路）
TRIMMED_NOTICE = "（对话历史较长，早期部分已自动压缩为摘要）"

# 中间段摘要的准入门槛：占比低于此值不值得为它付一次 LLM 调用
_SUMMARY_MIN_RATIO = 0.3

_SUMMARY_PROMPT = """你是对话摘要助手。请用一段中文（不超过 200 字）总结以下对话片段中的关键信息：

规则：
1. 只提取事实、决策、约定、用户明确的偏好与约束，不描述对话过程
2. 不要编造原文没有的信息
3. 格式：「此前对话要点：...」

{prev_block}对话内容：
{content}

摘要："""


@dataclass
class TrimResult:
    """裁剪结果。"""

    messages: list[dict] = field(default_factory=list)
    was_trimmed: bool = False
    trimmed_count: int = 0          # 被丢弃/压缩的原始消息条数
    summary: str = ""               # 中间段摘要（未摘要则为空）
    summary_failed: bool = False    # 摘要生成失败、已降级为直接丢弃


def group_turns(messages: list[dict]) -> list[list[dict]]:
    """把扁平消息列表按「轮」分组（user + 紧邻 assistant 为一轮）。

    以「轮」为单位裁剪是参考项目的关键设计：单条裁剪会产生「只有用户问题、
    没有回答」的断头上下文，模型容易误以为自己在被追问。

    异常情况（无配对的 user 消息）单独成轮，不丢弃。

    公开（原 `_group_turns`）：M-3 相关性过滤（app/rag/history_filter.py）以「轮」
    为最小过滤单位，**复用同一份实现**——轮次分组出现两套逻辑必然漂移。
    """
    turns: list[list[dict]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user":
            pair = [msg]
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                pair.append(messages[i + 1])
                i += 2
            else:
                i += 1
            turns.append(pair)
        else:
            # 非 user 开头（历史脏数据）：单条成轮，保证不丢
            turns.append([msg])
            i += 1
    return turns


def _summarize(middle_turns: list[list[dict]], prev_summary: str) -> str | None:
    """用 LLM 把中间轮次压成摘要；失败返回 None（调用方降级为直接丢弃）。

    注意：此处是**同步**调用 LLM（裁剪发生在装配阶段，调用方已在 async 上下文，
    用 `asyncio.to_thread` 包）。不引入新的异步依赖，保持与本项目 llm 客户端一致。
    """
    lines: list[str] = []
    for turn in middle_turns:
        for msg in turn:
            role = "用户" if msg.get("role") == "user" else "AI"
            lines.append(f"{role}: {msg.get('content', '')}")
    if not lines:
        return None

    prev_block = f"之前对话摘要：\n{prev_summary}\n\n" if prev_summary else ""
    prompt = _SUMMARY_PROMPT.format(prev_block=prev_block, content="\n".join(lines))

    try:
        from app.core.llm import llm_client

        result = _call_llm_sync(llm_client, prompt)
        text = (result or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 —— 摘要失败必须降级，绝不阻断
        logger.warning("记忆中间段摘要失败，降级为直接丢弃中间段: %s", exc)
        return None


def _call_llm_sync(llm_client, prompt: str) -> str:
    """同步调用 LLM 拿摘要。

    独立成函数便于单测打桩（`monkeypatch` 本函数即可，不必 mock 整个 llm 客户端）。

    线程模型：本函数可能在「无事件循环」（单测直接调 trim_history）或
    「有事件循环」（agent_service 在 async 上下文里调）两种场景下执行。
    有循环时不能 `asyncio.run`（会报 loop already running），故统一放到
    独立线程里跑一个新循环——摘要只在超限时发生，频率低，线程开销可接受。
    """
    import asyncio

    async def _run() -> str:
        resp = await llm_client.chat([{"role": "user", "content": prompt}])
        return getattr(resp, "content", "") or ""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的循环（单测 / 同步上下文）：直接跑
        return asyncio.run(_run())

    # 有运行中的循环：起独立线程 + 独立循环
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _run())
        return future.result(timeout=60)


def trim_history(
    messages: list[dict],
    prev_summary: str = "",
    token_budget: int | None = None,
) -> TrimResult:
    """按 token 预算裁剪历史（双锚点 + 中间摘要）。

    Args:
        messages: 完整历史（时间升序，user/assistant 交替）
        prev_summary: 上一次的累积摘要（递归压缩用）
        token_budget: token 预算，缺省取 `settings.memory_token_budget`

    Returns:
        TrimResult；未超预算时 `was_trimmed=False` 且 `messages` 原样返回
    """
    budget = token_budget if token_budget is not None else settings.memory_token_budget
    if not messages:
        return TrimResult(messages=[])

    total = token_estimator.estimate_messages(messages)
    if total <= budget:
        # 未超预算：原样返回，**不触发摘要**（不白花 LLM 调用）
        return TrimResult(messages=list(messages))

    turns = group_turns(messages)
    head_n = max(0, int(settings.memory_head_turns))
    tail_n = max(0, int(settings.memory_tail_turns))

    # 头部锚点 + 尾部保留；两者重叠时直接返回全部（说明轮次很少、预算却很小，
    # 此时没有可裁的中间段，裁了反而丢信息）
    if len(turns) <= head_n + tail_n:
        return TrimResult(messages=list(messages))

    head = turns[:head_n]
    tail = turns[len(turns) - tail_n :] if tail_n else []
    middle = turns[head_n : len(turns) - tail_n] if tail_n else turns[head_n:]

    trimmed_count = sum(len(t) for t in middle)

    # 双门槛：条数够多 且 中间段占比够大，才值得摘要
    middle_msgs = [m for t in middle for m in t]
    middle_tokens = token_estimator.estimate_messages(middle_msgs)
    should_summarize = (
        len(middle_msgs) >= int(settings.memory_summary_min_turns) * 2
        and total > 0
        and (middle_tokens / total) >= _SUMMARY_MIN_RATIO
    )

    summary = ""
    summary_failed = False
    if should_summarize:
        got = _summarize(middle, prev_summary)
        if got:
            summary = got
        else:
            summary_failed = True

    # 重建：头部 + [摘要] + 尾部
    result: list[dict] = [m for t in head for m in t]
    if summary:
        result.append({"role": "system", "content": f"此前对话要点：{summary}"})
    result.extend(m for t in tail for m in t)

    # token 预算兜底：摘要本身可能仍偏大（LLM 没守字数上限）。
    # **锚点受保护**：头部是身份/约束、尾部是当前话题，两者都不参与丢弃；
    # 先裁摘要（它可再缩短），摘要没了才动中间段（此刻已无中间段可动）。
    result = _fit_budget(result, budget)

    # 裁剪提示：告知模型历史被压缩过（防基于残缺历史编造）
    result = _append_notice(result)

    logger.info(
        "记忆裁剪: %d 条 -> %d 条（丢弃 %d 条，摘要: %s）",
        len(messages), len(result), trimmed_count, bool(summary),
    )
    return TrimResult(
        messages=result,
        was_trimmed=True,
        trimmed_count=trimmed_count,
        summary=summary,
        summary_failed=summary_failed,
    )


def _fit_budget(messages: list[dict], budget: int) -> list[dict]:
    """确保装配结果不超预算。

    策略（**锚点优先于预算**）：
    1. 若总 token 已进预算 → 原样返回；
    2. 否则先尝试用更短的摘要（含摘要的 system 消息可能是超预算主因）；
    3. 仍超 → 保留非 system 消息（=头部+尾部锚点），丢掉摘要。

    为什么不丢锚点：头部承载用户身份/任务约束、尾部承载当前话题，
    丢了它们等于退回"纯尾部/纯头部"窗口，双锚点策略就失去意义。
    实测踩坑：早期版本在这里 `pop(0)` 丢最旧轮，结果把尾部锚点也丢光，
    只剩头部一条——正是本策略想避免的退化。
    """
    if token_estimator.estimate_messages(messages) <= budget:
        return messages

    non_system = [m for m in messages if m.get("role") != "system"]
    system_msgs = [m for m in messages if m.get("role") == "system"]

    # 先试：保留锚点 + 不保留摘要
    if token_estimator.estimate_messages(non_system) <= budget:
        return non_system

    # 锚点本身就超预算：保留锚点，摘要降级为一句极短提示（信息量最小的占位）
    if system_msgs:
        short = [{"role": "system", "content": "（早期历史已省略）"}]
        return short + non_system
    return non_system


def _append_notice(messages: list[dict]) -> list[dict]:
    """在首条 system 消息末尾追加裁剪提示；没有 system 消息则在最前插入一条。"""
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if TRIMMED_NOTICE in content:
                return messages
            updated = list(messages)
            updated[i] = {**msg, "content": f"{content}\n{TRIMMED_NOTICE}"}
            return updated
    return [{"role": "system", "content": TRIMMED_NOTICE}, *messages]

"""回复正文净化：确定性剥离模型在正文开头漏出的「思考过程」。

背景（DEV_ISSUES §6.56 / §6.57）：业务 / 知识问答正文由 LLM 生成，模型偶尔把推理过程
当正文开头吐出来，例如：

    I have everything available in the system. Here's the analysis.

    ## 一、D002 设备基础情况
    ...

提示词层（`prompts/biz_system.md` 回答风格）已明确要求「正文直接输出中文、
不输出思考过程」，但提示词是**软约束**——实测同一问题时而泄漏、时而不泄漏。
故此处再补一层**确定性兜底**。

铁律（宁缺毋滥）：**绝不误剪正文**。宁可漏掉一句杂质，也不能剪掉用户的答案。
因此判定条件层层收紧，且只作用于正文开头：

1. 只剥离**开头**；正文中间出现的英文一律不动（遇到第一个「不像思考句」的行立即停止）；
2. 判定分两步——**行首必须命中思考语模式**（Let me / I have / Here's / Based on / Now I …），
   再按行内是否含中文分两种处理：
   - 整行无中日韩字符（本系统正文恒为中文场景）且长度 ≤ 200 → 整行剥离；
   - 行内含中文（同行混排，如 `Here's the analysis. ## 一、…`）→ 从第一个中文字符起截断前缀；
3. Markdown 结构行（标题 / 表格 / 引用 / 列表 / 代码围栏 / 分隔线）一律不剥——它们是正文；
4. 剥离总量 ≤ 3 行 / 400 字符，超出即放弃（防误剪长正文）；
5. 剥离后正文为空 → 保留原文（交白卷比带瑕疵更糟）。

两条消费路径共用同一内核 `_analyze`（这是本模块的**核心设计约束**）：
- **同步整段**（`clean_reply`）：正文一次性到手，直接取净化结果；
- **流式逐字**（`LeadThoughtStreamGuard`）：正文逐字到达，靠 `_analyze` 多返回的
  「结论是否已确定」信号决定"继续缓冲"还是"可以吐给用户"。

两份判定逻辑各写一套是必然要烂的坑——单测已经证明过一次：守卫最初自己写了一套
"第一行结束就定稿"的判断，结果多行思考句只剥掉第一行、`chunk=1` 逐字时
"全段都是思考句"的兜底被误触发，10 项断言当场挂掉。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 中日韩字符：汉字（含扩展 A / 兼容）、中文标点、假名、谚文。
# 正文是中文场景，出现即视为「像正文」——这是最可靠的正向判据。
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)

# Markdown 结构行：此类行即便无中文也是正文的一部分，绝不剥离
_MD_STRUCT_RE = re.compile(r"^(?:#{1,6}\s|>|\||```|-{3,}|\*{3,}|[-*+]\s|\d+[.)]\s)")

# 行内 Markdown 结构标记（同行混排截断时，须连标记一起保留，
# 否则 "Here's the analysis. ## 一、…" 会变成 "一、…"，格式被破坏）
_MD_INLINE_RE = re.compile(r"(?:#{1,6}\s|\||```|[-*+]\s|\d+[.)]\s)")

# 思考语候选表：只收「过渡/引导」性质的英文开头，避免误伤英文正文。
# 显式列成元组（而非只写正则）是**流式守卫**的需要：文本逐字到达时，必须判断
# "当前缓冲是不是某个思考语的前缀"（如 `let m` 还看不出结论），见
# LeadThoughtStreamGuard。正则由本表拼接生成 —— 保证"守卫用的清单"与
# "剥离用的清单"物理上不可能漂移。
_THOUGHT_CANDIDATES = (
    "let me", "let's", "i have", "i've", "i will", "i'll", "i need", "i can",
    "i should", "i now", "i am", "i'm", "now i", "here's", "here is",
    "based on", "according to", "looking at", "the user", "we have", "we need",
    "to summarize", "in summary", "the data", "from the", "as per",
    "first,", "next,", "finally,",
)
_THOUGHT_RE = re.compile(
    r"^(?:" + "|".join(re.escape(c) for c in _THOUGHT_CANDIDATES) + r")(?![a-z])",
    re.IGNORECASE,
)

MAX_LEAD_LINES = 3      # 最多剥离 3 个思考行
MAX_LEAD_CHARS = 400    # 剥离总量上限（字符）
MAX_LINE_CHARS = 200    # 单个思考行长度上限


def _could_be_thought_prefix(text: str) -> bool:
    """这段文本是否可能是某个思考语**还没输完**的前缀（如 `let m`、`i`）。

    流式场景专用：`I h` 还看不出结论（可能输成 `I have …` 也可能是 `I hereby …`），
    此时既不能判定"是思考句"（剥早了会误剪），也不能判定"不是"（放早了会泄漏），
    只能继续缓冲。
    """
    low = text.lower()
    return any(len(low) < len(c) and c.startswith(low) for c in _THOUGHT_CANDIDATES)


def _thought_prefix_len(line: str) -> int:
    """行首思考语前缀长度；0 表示「不像思考句」（或该行是 Markdown 结构行）。"""
    s = line.strip()
    if not s or _MD_STRUCT_RE.match(s):
        return 0
    m = _THOUGHT_RE.match(s)
    return len(m.group(0)) if m else 0


def _analyze(text: str) -> tuple[str, int, bool]:
    """净化内核：返回 `(净化后文本, 剥离字符数, settled)`。

    `settled` =「结论是否已确定」，**只对流式调用有意义**：
    - `True`：按现有文本已能给出最终结论，即便后面还有内容也不会改变
      （因为判定只作用于开头，而"开头已确定不以思考句结束"是不可逆的）；
    - `False`：现有文本被判定为"整段都是思考句"。在**完整文本**里这等价于
      "正文为空 → 保留原文"，但在**流式增量**里它通常只意味着"后面的正文还没到"，
    调用方必须继续缓冲（这正是 §6.57 单测抓到的那个坑）。
    """
    if not text:
        return text, 0, True

    lines = text.split("\n")
    i = 0
    removed = 0
    cut_in_line = 0  # >0：该行从第 N 个字符起保留（同行混排截断）
    settled = True

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            # 开头空行（含「思考句 + 空行 + 正文」中间的空行）随思考句一并剥离
            if removed >= MAX_LEAD_CHARS or i >= MAX_LEAD_LINES + 2:
                break
            removed += len(raw) + 1
            i += 1
            continue
        if removed >= MAX_LEAD_CHARS or i > MAX_LEAD_LINES:
            break
        if not _thought_prefix_len(line):
            # 这一行不像思考句 → 正文。但若它可能只是思考语还没输完，结论就还没定。
            settled = not _could_be_thought_prefix(line)
            break
        m = _CJK_RE.search(raw)
        if m is None:
            # 整行都是英文：长度可控则剥离
            if len(line) > MAX_LINE_CHARS:
                break
            removed += len(raw) + 1
            i += 1
            continue
        if m.start() > 0:
            # 同行混排：仅当英文前缀**本身构成完整句**（含句末标点或足够长）才截断，
            # 避免把 "I have 3 台设备需要维护" 这类正常正文剪掉开头
            prefix = raw[:m.start()]
            if len(prefix.strip()) >= 12 or "." in prefix or "!" in prefix or "?" in prefix:
                cut = m.start()
                md = _MD_INLINE_RE.search(raw)
                if md and md.start() < cut:
                    cut = md.start()   # 保留行内 Markdown 标记（## / | / - 等）
                cut_in_line = cut
            break
        break  # 行首即中文 → 是正文，停止
    else:
        # 所有行都被剥离 → 正文为空，放弃净化（流式下结论未定）
        return text, 0, False

    if removed == 0 and not cut_in_line:
        return text, 0, settled

    if cut_in_line:
        tail = lines[i + 1:]
        rest = lines[i][cut_in_line:] + (("\n" + "\n".join(tail)) if tail else "")
    else:
        rest = "\n".join(lines[i:])
    rest = rest.lstrip()
    if not rest.strip():
        return text, 0, settled  # 剥离后为空 → 保留原文

    return rest, len(text) - len(rest), settled


def strip_lead_thought(text: str) -> tuple[str, int]:
    """剥离正文开头的英文思考句（**完整文本**入口）。

    返回 `(净化后文本, 剥离字符数)`；未改动时剥离数为 0（调用方据此判断是否需要留痕）。
    """
    out, removed, _ = _analyze(text)
    return out, removed


def clean_reply(text: str) -> str:
    """净化正文并记录日志（便于观测泄漏频率）。不改动时原样返回。"""
    cleaned, removed = strip_lead_thought(text)
    if removed > 0:
        logger.warning("正文开头思考句已剥离 %d 字符，原文起始: %r", removed, text[:60])
    return cleaned


# ========================= 流式逐字路径的首段守卫 =========================

MAX_STREAM_HOLD = MAX_LINE_CHARS + 60   # 260：缓冲上限，超过一律放行（宁可漏剥不卡输出）


class LeadThoughtStreamGuard:
    """流式逐字输出的首段净化守卫（DEV_ISSUES §6.57，补齐 §6.56 的遗留）。

    为什么需要它：RAG / 闲聊的流式分支是**真逐字**
    `async for delta in ...: yield delta`，出口拿不到完整文本，出口处的
    `clean_reply` 对它们无效 —— 泄漏会直达用户；更隐蔽的是整段未净化文本
    还会写回会话记忆，污染后续轮次（模型会跟着模仿）。

    策略：**把整段缓冲交给 `_analyze`，靠 `settled` 决定去留**——
    已确定结论就吐给用户，没确定就继续攒。因为 `_analyze` 只判定"开头"，
    所以"已确定"是不可逆的：一旦确定，后续内容一律直通，不再缓冲。
    与同步路径共用同一内核，不存在第二套规则（见模块 docstring 记的那个坑）。

    效果：
    - 中文 / 数字 / 符号 / Markdown 起头的正文 → **首个增量就放行**（零延迟）；
    - 疑似英文思考句开头 → 缓冲到"能看出正文从哪儿开始"（首个中文、首个结构标记，
      或某个不像思考句的行首）再一次性净化输出；
    - 纯英文思考句开头且**整段都是思考句** → 一直缓到流结束，交给 flush 用
      完整文本判定（保留原文，不交白卷）。

    另两条硬约束：
    - 缓冲超过 `MAX_STREAM_HOLD` 一律放行（宁可漏剥一句，不能卡住输出）；
    - 任何异常都按「原样输出」处理（兜底不得反过来弄挂主链路）。

    调用方在流结束（含异常中断）时**必须**调用 `flush()` 取回残留缓冲，
    否则被缓冲的正文会丢字。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._closed = False
        self.removed = 0      # 累计剥离字符数（>0 = 提示词没约束住、兜底接住了）

    @property
    def closed(self) -> bool:
        """已判定完毕，后续 delta 无需再缓冲。"""
        return self._closed

    def feed(self, delta: str) -> str:
        """投喂一个增量，返回本次**可以发给用户**的文本（可能为空串）。"""
        if self._closed:
            return delta
        if not delta:
            return ""
        self._buf += delta
        s = self._buf
        try:
            out, removed, settled = _analyze(s)
        except Exception as exc:  # noqa: BLE001 —— 兜底不得影响回答
            logger.warning("流式净化异常，按原文输出: %s", exc)
            return self._release(s)
        if settled:
            self.removed += removed
            return self._release(out)
        if len(s) >= MAX_STREAM_HOLD:
            # 兜不住就整段放行（不净化：这么长的缓冲已超出"句首杂质"的范畴）
            return self._release(s)
        return ""

    def flush(self) -> str:
        """流结束：取回残留缓冲（未判定过就必须调，否则丢字）。"""
        if self._closed:
            return ""
        s, self._buf = self._buf, ""
        self._closed = True
        if not s:
            return ""
        try:
            out, removed = strip_lead_thought(s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式净化异常，按原文输出: %s", exc)
            return s
        self.removed += removed
        return out

    # ---- 内部 ----

    def _release(self, text: str) -> str:
        """定稿：缓冲清空，后续增量直通。"""
        self._closed = True
        self._buf = ""
        return text

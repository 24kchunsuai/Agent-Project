"""回复正文净化（app/services/reply_clean.py）自测：正反例并重。

用法：
    cd agt_python && <python> scripts/reply_clean_selftest.py

为什么要有这个脚本（DEV_ISSUES §6.56）：
净化是"确定性兜底"，它的风险不是"没剥掉"而是"**误剪正文**"。
因此断言必须双向：既要证明该剥的剥了，更要证明**正文一个字都没动**
（含"正文中间的英文行不得被剪""正常正文以 I have 开头不得被剪"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.reply_clean import (  # noqa: E402
    MAX_STREAM_HOLD,
    LeadThoughtStreamGuard,
    strip_lead_thought,
)

# 真实泄漏样本（2026-09-19 实测 biz-058 的原始输出开头）
REAL_LEAK = (
    "I have everything available in the system. Here's the analysis.\n\n"
    "## 一、D002 设备基础情况\n\n| 项目 | 内容 |\n|---|---|\n| 编号/名称 | D002 测绘先锋 |"
)

# (说明, 原文, 剥离后的期望开头)
POSITIVE = [
    ("真实泄漏样本（整行）+ 中文 Markdown 正文", REAL_LEAK, "## 一、D002 设备基础情况"),
    ("两行英文思考句", "I have all the data I need.\nLet me summarize.\n\n设备 D002 状态为占用。",
     "设备 D002 状态为占用。"),
    ("开头空行 + 思考句", "\n\nBased on the query results, here's what I found.\n\nD007 告警处于待处置状态。",
     "D007 告警处于待处置状态。"),
    ("同行混排（英文句 + 中文正文）", "Here's the analysis. ## 一、D002 设备基础情况\n\n| 状态 | 占用 |",
     "## 一、D002 设备基础情况"),
    ("三行英文思考句", "Let me check the data.\nI have everything I need now.\n"
                       "The data shows the following.\n\n结论：D007 待处置。", "结论：D007 待处置。"),
    ("同行混排 + 表格（结构标记须保留）", "Based on the data I collected. | 编号 | 状态 |\n|---|---|\n| D002 | 占用 |",
     "| 编号 | 状态 |"),
]

# 必须**原样返回**的用例（误剪防护）
NEGATIVE = [
    ("中文标题开头", "# 设备盘点报告\n\n**盘点范围**：全系统共 9 台无人机设备"),
    ("表格开头", "| 编号 | 名称 |\n|---|---|\n| D001 | 航拍一号 |"),
    ("思考句在正文中间（关键）", "报告如下：\n\nBased on the data, 建议尽快处置。"),
    ("首行是结构行、次行是思考句", "## 结论\n\nLet me summarize the findings. 设备 D002 状态为占用。"),
    ("列表开头", "- 结论：D007 待处置\n- 建议：立即调机核查"),
    ("正常正文以 'I have' 开头（关键误剪防护）", "I have 3 台设备需要维护。"),
    ("空串", ""),
    ("业务标题开头", "D007 电子围栏越界（P0）告警 — 深度分析与处置建议\n\n## 一、告警事实"),
    ("全文都是英文思考句（剥完为空 → 保留原文）", "I have all the data I need.\nLet me summarize."),
    ("超长英文单行（>200 字符）", "I have " + "x" * 260 + "\n\n中文正文不变。"),
    ("引用块开头", "> 该结论来自告警详情接口\n\n处置建议：立即核查"),
    ("短英文前缀 + 中文正文（防误剪）", "Let me 查一下设备状态"),
]


def stream_run(text: str, chunk: int) -> tuple[str, int, int, int]:
    """按 chunk 大小逐块投喂守卫，模拟真实 delta 流。

    返回 `(拼接输出, 累计剥离数, 最大未输出缓冲, 首个可见输出时的已投喂字符数)`。
    """
    guard = LeadThoughtStreamGuard()
    parts: list[str] = []
    fed = 0
    max_hold = 0
    first_at = -1
    for i in range(0, len(text), chunk):
        piece = text[i:i + chunk]
        fed += len(piece)
        got = guard.feed(piece)
        if got:
            parts.append(got)
            if first_at < 0:
                first_at = fed
        max_hold = max(max_hold, fed - sum(len(p) for p in parts))
    tail = guard.flush()
    if tail:
        parts.append(tail)
    return "".join(parts), guard.removed, max_hold, first_at


# (说明, 原文, 期望开头 [None=必须原样], 是否要求首块即出)
STREAM_CASES: list[tuple[str, str, str | None, bool]] = [
    # ---- 正例：该剥的剥掉，且结果须与同步净化**逐字一致** ----
    ("真实泄漏样本", REAL_LEAK, "## 一、D002 设备基础情况", False),
    ("同行混排 + 表格", "Here's the analysis. ## 一、D002 设备基础情况\n\n| 状态 | 占用 |",
     "## 一、D002 设备基础情况", False),
    ("两行思考句", "I have all the data I need.\nLet me summarize.\n\n设备 D002 状态为占用。",
     "设备 D002 状态为占用。", False),
    ("开头空行 + 思考句", "\n\nBased on the query results, here's what I found.\n\nD007 告警处于待处置状态。",
     "D007 告警处于待处置状态。", False),
    # ---- 反例：一个字都不许动；中文/代号起头必须零延迟 ----
    ("中文标题开头", "# 设备盘点报告\n\n**盘点范围**：全系统共 9 台无人机设备", None, True),
    ("纯中文正文", "根据知识库检索结果，D002 设备当前状态为占用。", None, True),
    ("英文代号起头正文", "D002 开机自检失败 — 告警分析\n\n## 一、告警事实", None, True),
    ("表格开头", "| 编号 | 名称 |\n|---|---|\n| D001 | 航拍一号 |", None, True),
    ("短英文前缀 + 中文（防误剪）", "Let me 查一下设备状态", None, False),
    ("正文以 I have 开头（防误剪）", "I have 3 台设备需要维护。", None, False),
    ("全文皆思考句（不交白卷）", "I have all the data I need.\nLet me summarize.", None, False),
    ("思考句在正文中间（只动开头）", "报告如下：\n\nBased on the data, 建议尽快处置。", None, True),
    ("超长英文单行（须及时放行）", "I have " + "x" * 260 + "\n\n中文正文不变。", None, False),
    ("词边界不误判（I havea…）", "I havea lot of things to do here.", None, False),
    ("空串", "", None, False),
]


def check_stream() -> int:
    """流式守卫：与同步净化一致 + 不丢字 + 中文零延迟 + 缓冲有界。"""
    bad = 0
    for chunk in (1, 3, 7):
        print(f"\n=== 流式逐块投喂（每块 {chunk} 字符）===")
        for name, src, want_head, zero_delay in STREAM_CASES:
            joined, removed, max_hold, first_at = stream_run(src, chunk)
            sync_out, sync_removed = strip_lead_thought(src)
            problems = []
            if joined != sync_out:
                problems.append(f"与同步净化不一致(流式={joined[:24]!r} 同步={sync_out[:24]!r})")
            if removed != sync_removed:
                problems.append(f"剥离数不一致({removed} vs {sync_removed})")
            if want_head is not None:
                if joined == src:
                    problems.append("该剥的没剥")
                elif not joined.startswith(want_head):
                    problems.append(f"开头不对({joined[:24]!r})")
            elif joined != src:
                problems.append(f"误剪正文({joined[:24]!r})")
            if zero_delay and (first_at < 0 or first_at > chunk):
                problems.append(f"首字延迟过大(第 {first_at} 字符才出)")
            if max_hold > MAX_STREAM_HOLD + chunk:
                problems.append(f"缓冲失控({max_hold})")
            if src and first_at < 0 and chunk >= len(src) and not src.startswith("I have"):
                problems.append("整段未输出")
            ok = not problems
            bad += 0 if ok else 1
            flag = "OK " if ok else "**FAIL**"
            print(f"  [{flag}] {name}: 首出@{first_at} 剥离={removed} 峰值缓冲={max_hold}"
                  + ("" if ok else "  ← " + "；".join(problems)))
    return bad


def main() -> int:
    bad = 0
    print("=== 正例（应剥离）===")
    for name, src, want_head in POSITIVE:
        out, removed = strip_lead_thought(src)
        ok = out != src and out.startswith(want_head)
        bad += 0 if ok else 1
        print(f"  [{'OK ' if ok else '**FAIL**'}] {name}: removed={removed} 新开头={out[:34]!r}")

    print("\n=== 反例（必须原样返回，一个字都不许动）===")
    for name, src in NEGATIVE:
        out, removed = strip_lead_thought(src)
        ok = (out == src) and removed == 0
        bad += 0 if ok else 1
        print(f"  [{'OK ' if ok else '**FAIL**'}] {name}: removed={removed} 开头={out[:30]!r}")

    stream_bad = check_stream()
    bad += stream_bad

    total = len(POSITIVE) + len(NEGATIVE) + len(STREAM_CASES) * 3
    print(f"\n结果: {total - bad}/{total} " + ("全部通过" if bad == 0 else f"（{bad} 项不符）"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

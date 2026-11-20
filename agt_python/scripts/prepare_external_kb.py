#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第三方语料导入与质量筛选脚本

用途
----
把从 GitHub 拉取的第三方知识库（如 UAV-Stack-Knowledge-Base）转换为本项目
统一的知识库格式（Markdown + frontmatter），并在转换前用客观指标筛掉低质文档。

为什么需要自动筛选
------------------
第三方仓库里混有大量**文章链接聚合页**（wechat-articles.md / csdn-articles.md），
这类文件只有标题 + URL + 搜索摘要，不但对 RAG 无贡献，还会：
  1. 产生大量无意义 chunk，稀释向量空间
  2. 其搜索摘要含跨领域噪音（实测「法规汇编」聚合页里混入了不良资产、
     汇编语言语法等完全无关内容），直接污染检索结果

质量指标（三项都是可计算的，不靠人眼判断）
-----------------------------------------
  cn_chars        中文字符数      —— 过滤掉内容过薄的文件
  url_ratio       含 URL 行占比   —— 识别链接聚合页
  code_ratio      代码块行占比    —— 识别代码为主、正文稀少的文件

使用
----
  python scripts/prepare_external_kb.py --dry-run     # 只看筛选报告，不写文件
  python scripts/prepare_external_kb.py               # 实际转换
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 配置

ROOT = Path(__file__).resolve().parent.parent          # agt_python/
SRC_DIR = ROOT / "data/kb/external/_src"               # 原始仓库解压位置
OUT_DIR = ROOT / "data/kb/external/uav-stack"          # 转换产物
REPORT = ROOT / "data/kb/external/import-report.json"

SOURCE_REPO = "github:zhouzhupianbei/UAV-Stack-Knowledge-Base"
SOURCE_COMMIT_DATE = "2026-09-15"                      # 仓库最后 push 日期（快照标记）
LICENSE_NOTE = "unknown-no-license"

# 整类排除：文件名命中即丢弃
EXCLUDE_NAME_PATTERNS = [
    r"wechat-articles\.md$",
    r"csdn-articles\.md$",
    r"update_log\.md$",
    r"README\.md$",           # 模块导航页（含子目录），非知识正文
]

# 质量阈值
MIN_CN_CHARS = 600            # 中文字符下限
MAX_URL_RATIO = 0.35          # 含 URL 行占比上限
MAX_CODE_RATIO = 0.55         # 代码行占比上限

# 目录 → 业务分类映射（用于 frontmatter 的 category，检索时可按此过滤）
CATEGORY_MAP = {
    "00-QuickStart": "ext-uav-基础入门",
    "01-Policy-Standard": "ext-uav-政策法规",
    "02-Hardware-Systems": "ext-uav-硬件系统",
    "03-Protocols-Dev": "ext-uav-协议开发",
    "04-Streaming-AI": "ext-uav-算法与流媒体",
    "05-GIS-DigitalTwin": "ext-uav-GIS数字孪生",
    "06-Industry-Solutions": "ext-uav-行业方案",
    "07-OpenSource-Awesome": "ext-uav-开源生态",
    "08-Project-Analysis": "ext-uav-项目实战",
    "收集": "ext-uav-资讯收集",
}

# ---------------------------------------------------------------- 工具函数

CN_RE = re.compile(r"[\u4e00-\u9fff]")
URL_RE = re.compile(r"https?://|weixin\.sogou\.com|download\.csdn\.net")
CODE_FENCE_RE = re.compile(r"^\s*```")


def slugify(text: str, maxlen: int = 40) -> str:
    """把中文文件名转成稳定的 ASCII doc_id 片段。"""
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:maxlen]
    return f"{ascii_part}-{digest}" if ascii_part else digest


@dataclass
class DocStats:
    """单篇文档的质量指标。"""

    path: Path
    rel: str
    cn_chars: int = 0
    total_lines: int = 0
    url_lines: int = 0
    code_lines: int = 0
    ok: bool = False
    reason: str = ""
    category: str = ""

    @property
    def url_ratio(self) -> float:
        return self.url_lines / self.total_lines if self.total_lines else 1.0

    @property
    def code_ratio(self) -> float:
        return self.code_lines / self.total_lines if self.total_lines else 0.0


def analyze(path: Path, rel: str) -> DocStats:
    """计算单篇文档的质量指标并给出判定。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    st = DocStats(path=path, rel=rel)
    st.cn_chars = len(CN_RE.findall(text))
    st.total_lines = len(lines)
    st.url_lines = sum(1 for ln in lines if URL_RE.search(ln))

    # 统计代码块内的行数
    in_code = False
    for ln in lines:
        if CODE_FENCE_RE.match(ln):
            in_code = not in_code
            st.code_lines += 1
            continue
        if in_code:
            st.code_lines += 1

    # 分类
    top = rel.split("/")[0]
    st.category = CATEGORY_MAP.get(top, "ext-uav-未分类")

    # 判定
    if st.cn_chars < MIN_CN_CHARS:
        st.reason = f"中文字符过少({st.cn_chars}<{MIN_CN_CHARS})"
    elif st.url_ratio > MAX_URL_RATIO:
        st.reason = f"链接密度过高({st.url_ratio:.0%}>{MAX_URL_RATIO:.0%})——疑似聚合页"
    elif st.code_ratio > MAX_CODE_RATIO:
        st.reason = f"代码占比过高({st.code_ratio:.0%}>{MAX_CODE_RATIO:.0%})——正文信息少"
    else:
        st.ok = True
        st.reason = "通过"
    return st


FM_TEMPLATE = """---
doc_id: {doc_id}
title: {title}
category: {category}
source: {source}
source_type: third_party_github
source_commit_date: {commit_date}
license: {license}
owner: public
lang: zh
imported_by: scripts/prepare_external_kb.py
---

"""


def convert(st: DocStats, out_root: Path) -> Path:
    """转换单篇文档：加 frontmatter，输出到目标目录。"""
    text = st.path.read_text(encoding="utf-8", errors="ignore")

    # 原文可能自带 frontmatter/YAML 头，先剥掉避免嵌套
    if text.lstrip().startswith("---"):
        parts = text.lstrip().split("---", 2)
        if len(parts) >= 3:
            text = parts[2].lstrip("\n")

    # 去掉开头的推广语（该仓库每篇开头都有 Star 引导块）
    text = re.sub(
        r"^>\s*💡.*?(?=\n(?!>)|$)",
        "",
        text,
        count=1,
        flags=re.S | re.M,
    ).lstrip("\n")

    title = st.path.stem
    doc_id = f"ext-uav-{slugify(st.rel)}"

    out_dir = out_root / st.category.replace("ext-uav-", "")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{doc_id}.md"

    frontmatter = FM_TEMPLATE.format(
        doc_id=doc_id,
        title=title,
        category=st.category,
        source=SOURCE_REPO,
        commit_date=SOURCE_COMMIT_DATE,
        license=LICENSE_NOTE,
    )
    out_path.write_text(frontmatter + text, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只输出报告，不写文件")
    args = ap.parse_args()

    # 定位解压出来的仓库目录
    candidates = [d for d in SRC_DIR.glob("*/") if d.is_dir()]
    if not candidates:
        print(f"[X] 未在 {SRC_DIR} 找到解压后的仓库目录")
        return 1
    repo_dir = candidates[0]
    print(f"源仓库目录: {repo_dir}")

    all_md = sorted(repo_dir.rglob("*.md"))
    print(f"源 md 文件总数: {len(all_md)}\n")

    stats: list[DocStats] = []
    skipped_name: list[str] = []

    for md in all_md:
        rel = md.relative_to(repo_dir).as_posix()
        if any(re.search(p, rel) for p in EXCLUDE_NAME_PATTERNS):
            skipped_name.append(rel)
            continue
        stats.append(analyze(md, rel))

    ok = [s for s in stats if s.ok]
    bad = [s for s in stats if not s.ok]

    # ---------------- 报告
    print("=" * 78)
    print(f"整类排除（聚合页/元信息）: {len(skipped_name)} 篇")
    print("=" * 78)
    for r in skipped_name:
        print(f"  ✗ {r}")

    print()
    print("=" * 78)
    print(f"质量筛选通过: {len(ok)} 篇    未通过: {len(bad)} 篇")
    print("=" * 78)

    if bad:
        print("\n-- 未通过明细（按原因分组）--")
        from collections import defaultdict

        grouped: dict[str, list[DocStats]] = defaultdict(list)
        for s in bad:
            grouped[s.reason.split("(")[0]].append(s)
        for reason, items in grouped.items():
            print(f"\n  [{reason}]")
            for s in items:
                print(f"    {s.cn_chars:>6} 中文字  {s.rel}")

    print("\n-- 通过明细 --")
    by_cat: dict[str, list[DocStats]] = {}
    for s in ok:
        by_cat.setdefault(s.category, []).append(s)
    for cat in sorted(by_cat):
        items = by_cat[cat]
        cn = sum(i.cn_chars for i in items)
        print(f"\n  {cat}  ({len(items)} 篇, {cn:,} 中文字)")
        for s in items:
            print(f"    {s.cn_chars:>6} 中文字  url{s.url_ratio:>5.0%}  code{s.code_ratio:>5.0%}  {s.rel}")

    total_cn = sum(s.cn_chars for s in ok)
    print()
    print("=" * 78)
    print(f"可用中文字符合计: {total_cn:,}")
    print(f"预估 chunk 数（600 字/片）: {total_cn // 520} 个")
    print("=" * 78)

    # ---------------- 写文件
    if args.dry_run:
        print("\n[dry-run] 未写入任何文件")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for s in ok:
        p = convert(s, OUT_DIR)
        written.append(str(p.relative_to(ROOT)))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "source_repo": SOURCE_REPO,
                "source_commit_date": SOURCE_COMMIT_DATE,
                "license": LICENSE_NOTE,
                "excluded_by_name": skipped_name,
                "rejected_by_quality": [
                    {"rel": s.rel, "reason": s.reason, "cn_chars": s.cn_chars} for s in bad
                ],
                "imported": [
                    {
                        "rel": s.rel,
                        "category": s.category,
                        "cn_chars": s.cn_chars,
                        "url_ratio": round(s.url_ratio, 3),
                        "code_ratio": round(s.code_ratio, 3),
                    }
                    for s in ok
                ],
                "totals": {
                    "imported_docs": len(written),
                    "cn_chars": total_cn,
                    "est_chunks": total_cn // 520,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n[OK] 已写入 {len(written)} 篇 → {OUT_DIR}")
    print(f"[OK] 导入报告 → {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

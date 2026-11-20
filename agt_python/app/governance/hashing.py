"""语料指纹：L1 文件哈希 / L2 切片哈希（去重门的基础设施）。

规范化规则（L2，spec §4）
------------------------
1. **NFKC 统一**：全角 → 半角（`ＡＢＣ` ≡ `ABC`，`１２３` ≡ `123`）
2. **CJK 标点归一**：`。，、；：？！（）【】《》「」""` ≡ 对应英文标点
   （NFKC 没有 CJK 标点的兼容映射，必须显式归一）
3. **去除所有空白**：换行 / 空格 / 制表符 / 全角空格都不参与指纹
4. **英文大小写归一**

为什么哈希对象是"规范化正文"而不是原始文件字节
--------------------------------------------
同一份语料另存一次、加个页眉、改个文件名，字节就变了，但内容没变。
若按字节比对，这类"换个壳就重复入库"的冗余永远拦不住——而它正是向量库膨胀的
主要来源（参考实现实测历史切片 52% 是冗余）。按规范化正文比对才能真正去重。

注意：**L1 与 L2 的哈希对象不同**
- L1（文件级）：整篇正文的 SHA-256，用于"这篇文档整体已经存在"
- L2（切片级）：规范化正文的 SHA-256，用于"这段内容已经存在（哪怕换了文件名）"
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# 空白：含全角空格（NFKC 后变半角）、各类换行
_WS_RE = re.compile(r"\s+")

# CJK 标点 → 英文标点（NFKC 不覆盖这些码位）
_PUNCT_MAP = str.maketrans({
    "。": ".", "，": ",", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "…": ".", "·": ".",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "「": '"', "」": '"', "『": "'", "』": "'",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", "～": "~", "％": "%", "＃": "#", "＠": "@", "＆": "&",
})


def sha256_bytes(data: bytes) -> str:
    """字节级 SHA-256（文件指纹用）。"""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """文本 SHA-256（按 UTF-8 编码后计算）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_for_hash(text: str) -> str:
    """规范化正文：全半角统一 + CJK 标点归一 + 去空白 + 小写。

    只用于计算指纹，不改变入库的原始文本——检索展示的仍是原文。
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = s.translate(_PUNCT_MAP)
    s = _WS_RE.sub("", s)
    return s.strip().lower()


def chunk_fingerprint(text: str) -> str:
    """切片指纹（L2）：规范化正文的 SHA-256。"""
    return sha256_text(normalize_for_hash(text))

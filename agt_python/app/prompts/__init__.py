"""提示词模板集中管理。

所有提示词放本目录下的 .md 文件，业务代码只引用文件名，不硬编码文本。
启动后首次使用时加载并缓存（当前规模无需热更新，改提示词重启即可）。
"""

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """按文件名（不含扩展名）加载提示词模板。"""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"提示词模板不存在: {path}")
    text = path.read_text(encoding="utf-8")
    # 去掉首行 "# 标题（版本）" 元信息行，正文才是真正的提示词
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()

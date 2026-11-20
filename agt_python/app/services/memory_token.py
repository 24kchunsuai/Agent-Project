"""Token 计量（docs/specs/会话记忆规约.md §6 M-2，缺口 G11）。

为什么需要：原窗口按「条数」算（`window=10`），一条 3000 字的长回复与一条
"好的"等价占 1 条——10 条可能撑爆侧路上下文，也可能只用了 200 token 浪费预算。
改为按 token 预算装配后，需要一个**保守**的估算器。

为什么保守（乘安全系数）：参考项目 open-agent-master 的 `TokenEstimator`
自认误差 ±20%，且它的估算**偏小**（按字数直接当 token），真实 token 更多时
会击穿窗口。本项目反过来取保守方向——宁可少给历史，不可超窗口。

实现策略（按可用性降级）：
1. 装了 tiktoken 就用它精确计数（真实 BPE 切分）；
2. 否则用启发式：中文 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token；再乘安全系数。
"""

import logging
import re

logger = logging.getLogger(__name__)

# 安全系数：启发式估算偏乐观，乘上去留余量（对齐规约 §5.3 的保守原则）
_SAFETY_FACTOR = 1.2

# 偏小方向的误差最危险（会击穿窗口），所以取 1.2 而不是 1.0

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


class TokenEstimator:
    """文本 token 估算。优先 tiktoken，缺失时用保守启发式。"""

    def __init__(self) -> None:
        self._encoder = None
        self._mode = "heuristic"
        try:
            import tiktoken  # type: ignore

            self._encoder = tiktoken.get_encoding("cl100k_base")
            self._mode = "tiktoken"
        except Exception:
            # 缺依赖是常态（离线环境/未装），不算故障，debug 级留痕即可
            logger.debug("tiktoken 不可用，token 计量回退启发式估算")

    @property
    def mode(self) -> str:
        """当前计量模式：tiktoken / heuristic（观测用）。"""
        return self._mode

    def estimate(self, text: str) -> int:
        """估算一段文本的 token 数；空文本返回 0。"""
        if not text:
            return 0
        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception:
                # 运行期异常（超长/非法字符）也回退，不让计量拖垮主链路
                logger.debug("tiktoken 编码失败，本条回退启发式")

        chinese = len(_CJK.findall(text))
        rest = len(text) - chinese
        raw = chinese + rest / 4.0
        return max(1, int(raw * _SAFETY_FACTOR + 0.5))

    def estimate_messages(self, messages: list[dict]) -> int:
        """估算消息列表总 token（含每条的固定开销）。"""
        # 每条消息有 role/分隔符等固定开销，粗略按 4 token 计（保守）
        return sum(self.estimate(m.get("content", "")) + 4 for m in messages)


# 模块级单例：编码器初始化有成本，全局复用
token_estimator = TokenEstimator()

"""IntentRouter：意图识别路由（chitchat / rag / business）。

三级策略（参考 AgentCraft classifier.py 的「LLM 优先+关键词回退」，
但反过来用——关键词优先、LLM 兜底，让 90% 以上的输入不花模型钱）：

1. 关键词规则（intent_rules.yml）：rag 词命中且带疑问语气 → rag；
   business 词命中 → 先做「疑问语气仲裁」：带疑问语气且无祈使派活标记 → rag
   （业务词只是话题词：起飞流程/云台抖/飞手装备，真实意图是求答案；
     控制指令必然带祈使标记：帮我起飞 / 把无人机降落下来），否则 → business；
   chitchat 词命中且输入较短 → chitchat；
2. LLM 分类兜底（intent_classify.md 提示词，要求输出 JSON）；
3. LLM 也失败 → 默认 chitchat（闲聊兜底永远可用，业务宁可漏不可误拦）。
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from app.core.config import settings
from app.core.llm import LLMError, llm_client
from app.prompts import load_prompt

logger = logging.getLogger(__name__)

Intent = Literal["chitchat", "rag", "business"]

_RULES_PATH = Path(__file__).resolve().parent / "intent_rules.yml"
# chitchat 关键词直接放行的最大长度（字）：长输入即使带"你好"也可能是业务请求
_CHITCHAT_SHORT_LEN = 12
# rag 关键词必须搭配疑问语气才判 rag，避免「实名登记功能做一下」这类操作请求误入
_QUESTION_FEATURES = ("？", "?", "怎么", "什么", "哪些", "哪", "吗", "多少", "如何", "是不是", "能不能")
# 控制类动词：business 意图里出现即视为高危控制指令，回「要求确认」话术
_CONTROL_VERBS = ("起飞", "降落", "返航", "悬停")
# 系统资源名词：登录用户的查询对象落在这几个域时，倾向走业务工具而非知识库。
# 不含「飞手」——「飞手出门要带哪些装备」是知识题，飞手的系统化查询 P2 再收；
# 也不含「无人机」——几乎一切知识题都含这三个字，太泛。
_RESOURCE_NOUNS = ("设备", "任务", "告警", "围栏", "禁飞区", "限飞区", "媒体", "影像",
                   "规则", "运营", "总览", "概览", "整体情况", "仪表盘", "统计",
                   "仓库", "架次", "飞行小时", "维保", "维修单",
                   # 账号域 + 设备状态词（biz-051/biz-002 误路由修复，2026-09-18）：
                   # 「tomcat 这个账号现在是什么情况」「哪些无人机现在是空闲的」
                   # 此前因不含资源名词落进 LLM 兜底被判 rag。不收裸「用户」——
                   # 用户手册/用户体验等知识语境太泛；不收「视频/文件」——
                   # 拍视频卡顿等知识题易误伤（biz-060 靠"任务"名词+叫什么语气）。
                   "账号", "账户", "空闲", "停用")
# 系统查询语气：与资源名词同时出现 → 这是"查系统现状"，不是"问知识"
# 三类：疑问式、祈使式、是非式（「有 P0 级的告警吗？」的"吗"）
_SYSTEM_QUERY_PATTERNS = ("哪些", "哪台", "哪架", "哪个", "多少", "几台", "几架", "几个", "几条",
                          "什么状态", "状态怎么样", "怎么样", "什么类型", "什么情况",
                          "现在有", "当前有", "有没有", "列表",
                          "在维修", "空闲", "占用", "在飞", "讲一下", "说说", "介绍一下",
                          "查询", "查一下", "查查", "查下", "看看", "看下", "看一眼",
                          # 实体取名询问（biz-060 误路由修复）：「…视频文件叫什么名字」
                          "叫什么", "什么名字", "文件名")
# 是非问尾：单独出现也算语气（「都启用着吗？」「D005 能用吗？」）
_YES_NO_TAIL = ("吗", "么")
# 设备编号模式：D001/D007 这类编号直接指向业务实体
_DRONE_CODE_RE = re.compile(r"\bD\d{2,4}\b", re.IGNORECASE)
# 量词疑问强信号：「几台/几条/多少台」几乎只用于盘点系统资源
_QUANTITY_Q = ("几台", "几架", "几条", "多少台", "多少架", "多少条", "多少个")
# 知识追问后缀：出现即说明问的是概念解释而非系统现状，
# 即使带祈使动词也不走系统查询仲裁（「查询无人机的告警规则是什么意思」→ rag）。
# 不含裸「是什么」——避免误伤「查询任务是什么状态」这类系统查询。
_KNOWLEDGE_SUFFIXES = ("是什么意思", "什么意思", "为什么", "怎么办", "怎么定义",
                       "如何定义", "怎么算", "怎么解释", "怎么保养", "怎么维护",
                       "怎么选", "怎么充电", "怎么存放", "怎么处理", "怎么排查")


@dataclass
class IntentResult:
    intent: Intent
    confidence: float
    source: str  # rule | llm | fallback，便于排查


class IntentRouter:
    def __init__(self) -> None:
        self._business: list[str] = []
        self._chitchat: list[str] = []
        self._rag: list[str] = []
        self._action_markers: list[str] = []
        self._action_cancel: list[str] = []
        self._load_rules()

    def _load_rules(self) -> None:
        with open(_RULES_PATH, encoding="utf-8") as f:
            rules = yaml.safe_load(f) or {}
        self._business = [w for w in rules.get("business", []) if w]
        self._chitchat = [w for w in rules.get("chitchat", []) if w]
        self._rag = [w for w in rules.get("rag", []) if w]
        self._action_markers = [w for w in rules.get("action_markers", []) if w]
        self._action_cancel = [w for w in rules.get("action_cancel", []) if w]
        logger.info(
            "意图规则加载完成 business=%d chitchat=%d rag=%d action=%d",
            len(self._business), len(self._chitchat), len(self._rag),
            len(self._action_markers),
        )

    async def classify(self, message: str, identity: dict | None = None) -> IntentResult:
        text = message.strip()
        if not text:
            return IntentResult("chitchat", 1.0, "fallback")

        # ---- 第一级：关键词规则 ----
        # 0. 身份感知仲裁（最高优先）：系统资源名词 + 系统查询语气
        #    → 这是"查业务系统现状"（如「现在有哪些无人机设备？」），不是知识问答。
        #    登录用户进业务工具；匿名用户由上层给登录引导——
        #    都好过把问题甩给知识库答"未收录"。
        if self._is_system_query(text) and not any(s in text for s in _KNOWLEDGE_SUFFIXES):
            return IntentResult("business", 0.95, "rule")

        # rag（知识问答）最优先：rag 词 + 疑问语气，双条件压低误判
        rag_hit = sum(1 for w in self._rag if w in text)
        if rag_hit > 0 and any(f in text for f in _QUESTION_FEATURES):
            return IntentResult("rag", min(0.7 + 0.1 * rag_hit, 0.99), "rule")

        business_hit = sum(1 for w in self._business if w in text)
        if business_hit > 0:
            # 疑问语气仲裁：业务词 + 疑问语气 + 无祈使派活标记 → 是在问知识，
            # 不是在派任务（「起飞流程是什么」≠「帮我起飞」）。
            # 控制类知识问题误判成控制指令是最危险的误判方向，仲裁一律偏向 rag。
            if any(f in text for f in _QUESTION_FEATURES) and not self._has_action_marker(text):
                return IntentResult("rag", min(0.7 + 0.1 * business_hit, 0.99), "rule")
            return IntentResult("business", min(0.6 + 0.1 * business_hit, 0.99), "rule")

        chitchat_hit = any(w in text for w in self._chitchat)
        if chitchat_hit and len(text) <= _CHITCHAT_SHORT_LEN:
            return IntentResult("chitchat", 0.9, "rule")

        # ---- 第二级：LLM 分类兜底 ----
        try:
            return await self._classify_by_llm(text)
        except LLMError:
            logger.warning("LLM 意图分类失败，兜底为 chitchat")
            return IntentResult("chitchat", 0.0, "fallback")

    @staticmethod
    def _is_system_query(text: str) -> bool:
        """系统查询判定（登录/匿名通用）：强信号任一即可，弱信号需组合。

        强信号（单独即成立）：
        - 设备编号（D005 现在能用吗？）
        - 量词疑问（告警规则一共有几条？）
        - 资源名词 + 是非问尾（有 P0 级的告警吗？）
        弱信号组合：资源名词 + 查询语气（疑问式/祈使式）。
        """
        if _DRONE_CODE_RE.search(text):
            return True
        if any(q in text for q in _QUANTITY_Q):
            # 量词疑问单独成立（「告警规则一共有几条？」）；
            # 「怎么/为什么」类知识问法已由后缀排除兜底
            return True
        has_noun = any(n in text for n in _RESOURCE_NOUNS)
        if not has_noun:
            return False
        if any(p in text for p in _SYSTEM_QUERY_PATTERNS):
            return True
        if any(text.rstrip("？?").endswith(t) for t in _YES_NO_TAIL):
            return True
        return False

    def _has_action_marker(self, text: str) -> bool:
        """判断是否含祈使派活标记（先剔除「帮我看看」类求助式提问前缀）。"""
        for cancel in self._action_cancel:
            text = text.replace(cancel, "")
        return any(m in text for m in self._action_markers)

    async def _classify_by_llm(self, text: str) -> IntentResult:
        messages = [
            {"role": "system", "content": load_prompt("intent_classify")},
            {"role": "user", "content": text},
        ]
        result = await llm_client.chat(messages, temperature=0.0, max_tokens=64)
        parsed = self._parse_json(result.content)
        if parsed is None:
            logger.warning("LLM 意图输出无法解析: %r", result.content[:80])
            return IntentResult("chitchat", 0.0, "fallback")
        intent = parsed.get("intent")
        if intent not in ("chitchat", "rag", "business"):
            return IntentResult("chitchat", 0.0, "fallback")
        confidence = parsed.get("confidence", 0.5)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        return IntentResult(intent, min(max(confidence, 0.0), 1.0), "llm")

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """从模型输出里提取 JSON（容忍 ```json 包裹与前后杂文本）。"""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


intent_router = IntentRouter()


def business_placeholder() -> str:
    """business 意图 + 未登录的兜底回复：引导登录而非含糊其辞。"""
    return (
        "这是业务系统功能（设备/任务/告警/围栏等），需要登录后才能查询和操作。\n\n"
        "请先在前端右上角登录你的账号，登录后我就能直接帮你查真实数据、"
        "执行经你确认的写操作。知识问答（法规/操作/机型选购）无需登录，随时可以问我。"
    )


def control_confirm_placeholder() -> str:
    """控制指令（起飞/降落/返航/悬停）的确认话术：要求确认 + 说明当前状态，绝不假装已执行。"""
    return (
        "已识别这是一条无人机控制指令，涉及飞行安全，需要你确认后才会执行。\n\n"
        "当前状态：**没有正在执行的飞行任务，也未接入任何设备连接**，"
        "暂时无法真实执行——我不会假装已经操作。\n\n"
        "等你接入设备后，我会先复述指令请你二次确认（例如回复「确认起飞」），确认无误才会下发。"
    )


def business_reply(message: str) -> str:
    """business 意图的回复：控制类动词 → 确认话术；其余 → 通用占位。"""
    if any(v in message for v in _CONTROL_VERBS):
        return control_confirm_placeholder()
    return business_placeholder()

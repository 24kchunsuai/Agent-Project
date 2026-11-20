"""写操作安全护栏：危险白名单 + 复述二次确认 + 角色校验。

借鉴 open-agent-master 的护栏三件套，落在本项目业务工具链上：

1. **危险白名单**：`registry` 里 danger=True 的工具，AgentLoop 一律不当场执行，
   而是把「工具名 + 参数」冻结成 PendingAction，回一句复述确认话术；
2. **二次确认只认用户字面消息**：确认词必须由**用户下一条消息**给出
   （完全匹配，见 `is_confirmation`）。模型/工具返回内容里的任何"已确认"
   都不构成授权——这是防注入的关键：攻击者通过网页/知识库内容诱导模型
   自说自话执行删除，在此机制下只会得到一条待确认记录，而不是真删；
3. **每轮最多 1 个危险操作**：AgentLoop 挂到危险工具即终止本轮，
   执行前必须再走一次用户消息，天然限流；
4. **角色校验**：写操作要求 jsdstu-3 的 admin 权限（与后端 @RequireRole 对齐），
   非 admin 在**进入确认前**就被拒，避免"确认了也做不成"的坏体验。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.biz import BizError

logger = logging.getLogger(__name__)

# 写操作要求的角色（与 jsdstu-3 后端 @RequireRole(ADMIN) 一致）
WRITE_ROLE = "admin"
# 确认词：只认完全匹配（去空白/标点后），避免"好的帮我查一下设备"被误当确认
_CONFIRM_EXACT = {
    "确认", "确定", "确认执行", "确认操作", "执行", "是", "是的", "对", "好的", "好",
    "同意", "可以", "ok", "OK", "Ok", "yes", "YES", "y",
}
# 取消词：前缀匹配即可（"取消一下""算了不管了"都算放弃）
_REJECT_PREFIX = ("取消", "算了", "不要", "不用", "别", "否", "撤回", "停止", "先不", "no", "cancel", "n")
# 待确认动作有效期（秒）：超时视为放弃，避免陈旧动作被误确认
PENDING_TTL = 600


def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").strip() if ch not in " \t\r\n。，！？!?.,、~～")


def is_confirmation(text: str) -> bool:
    """用户消息是否构成「确认」。

    两级判定（防注入的"只认用户字面消息"原则不变）：
    1. 完全匹配白名单（"确认"/"好的"/"ok" 等短应答）；
    2. 以「确认/确定」开头的短句（<=10 字，如"确认创建"/"确认删除吧"）——
       覆盖自然说法，同时长度上限挡住"确认一下这个任务什么时候创建的"
       这类以确认开头的提问。
    """
    norm = _normalize(text)
    if norm in _CONFIRM_EXACT or norm.lower() in _CONFIRM_EXACT:
        return True
    if not norm.startswith(("确认", "确定")):
        return False
    if is_rejection(text):
        return False
    if any(ch in norm for ch in "吗么?？"):
        return False  # "确认了吗/确认么？"是提问不是应答（"确认删除吧"仍是应答）
    return len(norm) <= 10


def is_rejection(text: str) -> bool:
    """用户消息是否构成「取消」。"""
    norm = _normalize(text).lower()
    return any(norm.startswith(p) for p in _REJECT_PREFIX)


@dataclass
class PendingAction:
    """一个被拦下、等待用户确认的写操作。"""

    session_id: str
    tool: str
    arguments: dict[str, Any]
    description: str          # 人类可读复述（给用户看，也用于执行回执）
    user_id: int = 0
    username: str = ""
    permission: str = ""
    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL


class PendingActionStore:
    """按会话暂存的待确认动作（内存态，单实例部署够用；多实例需换 Redis）。"""

    def __init__(self) -> None:
        self._items: dict[str, PendingAction] = {}

    def put(self, action: PendingAction) -> None:
        self._items[action.session_id] = action
        logger.info(
            "待确认写操作已登记 session=%s tool=%s args=%s",
            action.session_id, action.tool, action.arguments,
        )

    def get(self, session_id: str) -> PendingAction | None:
        action = self._items.get(session_id)
        if action and action.expired():
            self._items.pop(session_id, None)
            logger.info("待确认写操作已超时作废 session=%s tool=%s", session_id, action.tool)
            return None
        return action

    def clear(self, session_id: str) -> PendingAction | None:
        return self._items.pop(session_id, None)


pending_store = PendingActionStore()


def ensure_can_write(identity: dict[str, Any] | None) -> None:
    """写操作角色校验（进确认前先拦，比事后失败体验好）。"""
    permission = (identity or {}).get("permission") or ""
    if permission != WRITE_ROLE:
        raise BizError(
            f"该操作需要管理员权限，当前账号权限为「{permission or '未知'}」，"
            "请联系管理员执行或改用查询类提问"
        )


_DESCRIBERS: dict[str, Any] = {
    "task_create": lambda a: f"创建飞行任务「{a.get('taskName', '')}」"
                             + (f"（优先级 {a['priority']}）" if a.get("priority") else "")
                             + (f"（飞手 {a['pilotName']}）" if a.get("pilotName") else ""),
    "task_assign": lambda a: f"下发任务 {a.get('taskId', '')}（会占用对应设备与飞手）",
    "task_delete": lambda a: f"删除任务 {a.get('taskId', '')}（不可恢复）",
    "task_audit": lambda a: f"审核任务 {a.get('taskId', '')}，结论「{a.get('result', '')}」"
                            + (f"（意见：{a['comment']}）" if a.get("comment") else ""),
    # ---- P1 任务 PILOT 动作三件（规约 §14）----
    "task_accept": lambda a: f"接受任务 ID={a.get('taskId', '')} 的派单（接单后任务进入执行中，会占用你的飞手档期）",
    "task_reject": lambda a: f"拒接任务 ID={a.get('taskId', '')}"
                             + (f"（原因：{a['reason']}）" if a.get("reason") else ""),
    "task_complete": lambda a: f"将任务 ID={a.get('taskId', '')} 标记为已完成",
    "drone_save": lambda a: f"保存设备 {a.get('code', '')}（{a.get('name', '')}）",
    "drone_delete": lambda a: f"删除设备 ID={a.get('droneId', '')}（不可恢复）",
    "drone_maintenance": lambda a: f"将设备 ID={a.get('droneId', '')} 的维保状态设为「{a.get('status', '')}」",
    "fence_save": lambda a: f"保存电子围栏「{a.get('name', '')}」"
                            + (f"（类型 {a['type']}）" if a.get("type") else ""),
    "fence_delete": lambda a: f"删除电子围栏 ID={a.get('fenceId', '')}（不可恢复）",
    "alarm_acknowledge": lambda a: f"确认告警 ID={a.get('alarmId', '')}（进入处置流程）",
    "alarm_resolve": lambda a: f"解决告警 ID={a.get('alarmId', '')}"
                               + (f"（说明：{a['note']}）" if a.get("note") else ""),
    "alarm_ignore": lambda a: f"忽略告警 ID={a.get('alarmId', '')}"
                              + (f"（原因：{a['note']}）" if a.get("note") else ""),
    # ---- P0 新增 8 条（规约 §5.2/§11.1：删除/重置类必须含"不可恢复"；二合一接口必须区分新建/更新） ----
    "user_save": lambda a: (
        (f"更新用户 {a.get('id', '')}（{a.get('username', '')}）的信息" if a.get("id")
         else f"创建新用户「{a.get('username', '')}」")
        + (f"，角色改为「{a['permission']}」（权限变更）" if a.get("permission") else "")
        + (f"，状态设为「{'启用' if str(a['status']) == '1' else '禁用'}」" if a.get("status") else "")
    ),
    "user_delete": lambda a: f"删除用户 ID={a.get('userId', '')}（不可恢复；若其有关联任务/告警会被系统拒绝）",
    "user_status_update": lambda a: (
        f"{'启用' if str(a.get('status')) == '1' else '禁用'}用户 ID={a.get('userId', '')} 的账号"
    ),
    "user_password_reset": lambda a: f"重置用户 ID={a.get('userId', '')} 的密码（原密码立即失效，不可恢复）",
    "alarm_escalate": lambda a: f"为告警 ID={a.get('alarmId', '')} 请求支援（升级给管理员处理）",
    "rule_update": lambda a: (
        f"更新告警规则 ID={a.get('id', '')}"
        + (f"，名称改为「{a['alarmName']}」" if a.get("alarmName") else "")
        + (f"，阈值改为 {a['thresholdValue']}" if a.get("thresholdValue") else "")
        + (f"，级别改为 {a['severity']}" if a.get("severity") else "")
        + (f"，{'启用' if str(a['enabled']) in ('1', 'true', 'True') else '停用'}该规则" if a.get("enabled") else "")
        + "（只更新列出的字段）"
    ),
    "media_delete": lambda a: f"删除媒体文件 ID={a.get('mediaId', '')}（不可恢复）",
    "media_upload": lambda a: (
        f"上传 {len(a.get('paths') or [])} 个媒体文件到任务 ID={a.get('taskId', '')}"
        + (f"（说明：{a['description']}）" if a.get("description") else "")
    ),
}


def describe_action(tool: str, arguments: dict[str, Any]) -> str:
    """把工具调用翻译成一句人话，用于向用户复述。"""
    describer = _DESCRIBERS.get(tool)
    if describer:
        return describer(arguments)
    return f"执行 {tool}（参数：{arguments}）"


def build_action(session_id: str, tool: str, arguments: dict[str, Any],
                 identity: dict[str, Any] | None) -> PendingAction:
    ident = identity or {}
    return PendingAction(
        session_id=session_id,
        tool=tool,
        arguments=arguments,
        description=describe_action(tool, arguments),
        user_id=int(ident.get("userId") or 0),
        username=str(ident.get("username") or ""),
        permission=str(ident.get("permission") or ""),
    )


def build_confirm_reply(action: PendingAction) -> str:
    """复述指令 + 要求确认的话术（带执行人身份，多一层防呆）。"""
    operator = action.username or f"用户{action.user_id}"
    return (
        f"⚠️ 这是一个**写操作**，需要你确认后才会执行。\n\n"
        f"**执行人**：{operator}（权限：{action.permission or '未知'}）\n"
        f"**将要执行**：{action.description}\n\n"
        f"确认请回复「**确认**」，放弃请回复「**取消**」。"
        f"（{PENDING_TTL // 60} 分钟内有效，未确认前不会改动任何数据）"
    )

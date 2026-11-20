"""业务工具注册表 + 首批查询类工具（P1 起点）。

ToolRegistry 借鉴 open-agent-master 的 ToolRegistry：
- 每个工具声明 name/description/parameters（JSON Schema），自动拼成
  OpenAI function-calling 的 tools 列表，LLM 按需选择；
- 工具实现是普通 async 函数，签名统一为 (identity: dict, **args)；
- identity 由 AgentLoop 注入（含用户自己的 droneToken），工具内部
  不接触任何凭据细节。

分级约定（见规划）：
- 查询类：GET，直接执行，无护栏；
- 操作类：POST，P2 落地，须走 danger 白名单 + 二次确认；
- 流程类：不做独立工具，由 AgentLoop 多步编排。
"""

import json
import logging
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.paths import DATA_DIR
from app.services.biz import BizAuthError, BizError, jsdstu_client

logger = logging.getLogger(__name__)


def _page_params(pageNum: int = 0, pageSize: int = 0) -> dict[str, Any]:
    """分页参数归一化：不传=全量（jsdstu-3 约定，见规约坑位 4）。"""
    params: dict[str, Any] = {}
    if pageNum:
        params["pageNum"] = pageNum
    if pageSize:
        params["pageSize"] = pageSize
    return params

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema（properties/required）
    handler: Callable[..., Awaitable[Any]]
    danger: bool = False                # True=写操作：AgentLoop 拦截转二次确认，不直接执行


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"工具重名: {tool.name}")
        self.tools[tool.name] = tool
        logger.info("业务工具已注册: %s", tool.name)

    def to_openai_schema(self) -> list[dict[str, Any]]:
        """拼成 OpenAI function-calling 的 tools 列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools.values()
        ]

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def is_dangerous(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.danger)

    async def execute(self, name: str, identity: dict[str, Any], arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise BizError(f"未知工具: {name}")
        return await tool.handler(identity, **arguments)


registry = ToolRegistry()


# ---------------------------------------------------------------------------
# 查询类工具（P1 首批：设备 / 任务 / 告警）
# ---------------------------------------------------------------------------

async def drone_query(
    identity: dict[str, Any],
    keyword: str = "",
    status: str = "",
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询无人机设备列表。"""
    params: dict[str, Any] = _page_params(pageNum, pageSize)
    if keyword:
        params["keyword"] = keyword
    if status:
        params["status"] = status
    data = await jsdstu_client.get("/v1/drone/select", token=identity.get("droneToken"), params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "drones": [
            {
                "id": r.get("id"),
                "code": r.get("code"),
                "name": r.get("name"),
                "model": r.get("model"),
                "type": r.get("type"),
                "status": r.get("status"),
                "location": r.get("location"),
                "flightCount": r.get("flightCount"),
                "flightHours": r.get("flightHours"),
            }
            for r in records[:20]
        ],
    }


async def task_query(
    identity: dict[str, Any],
    status: str = "",
    taskName: str = "",
    priority: str = "",
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询飞行任务列表。"""
    params: dict[str, Any] = _page_params(pageNum, pageSize)
    for key, val in (("status", status), ("taskName", taskName), ("priority", priority)):
        if val:
            params[key] = val
    data = await jsdstu_client.get("/v1/task/list", token=identity.get("droneToken"), params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "tasks": [
            {
                "id": r.get("id"),
                "taskName": r.get("taskName"),
                "priority": r.get("priority"),
                "status": r.get("status"),
                "pilotName": r.get("pilotName"),
                "droneName": r.get("droneName"),
                "startTime": r.get("startTime"),
                "endTime": r.get("endTime"),
            }
            for r in records[:20]
        ],
    }


async def alarm_query(
    identity: dict[str, Any],
    status: str = "",
    severity: str = "",
    alarmType: str = "",
    keyword: str = "",
    startTime: str = "",
    endTime: str = "",
    onlyDeviceAlarm: bool = False,
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询告警列表（多维度过滤；时间参数原样透传，见规约坑位 5）。"""
    params: dict[str, Any] = _page_params(pageNum, pageSize)
    for key, val in (("status", status), ("severity", severity), ("alarmType", alarmType),
                     ("keyword", keyword), ("startTime", startTime), ("endTime", endTime)):
        if val:
            params[key] = val
    if onlyDeviceAlarm:
        params["onlyDeviceAlarm"] = True
    data = await jsdstu_client.get("/v1/alarm/list", token=identity.get("droneToken"), params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "alarms": [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "alarmType": r.get("alarmType"),
                "alarmTypeName": r.get("alarmTypeName"),
                "severity": r.get("severity"),
                "status": r.get("status"),
                "droneCode": r.get("droneCode"),
                "taskId": r.get("taskId"),
                "triggeredAt": r.get("triggeredAt"),
            }
            for r in records[:20]
        ],
    }


async def dashboard_query(identity: dict[str, Any]) -> dict[str, Any]:
    """查询运营总览仪表盘（全局统计）。"""
    return await jsdstu_client.get("/v1/dashboard/summary", token=identity.get("droneToken"))


async def fence_query(
    identity: dict[str, Any],
    keyword: str = "",
    type: str = "",
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询电子围栏列表。"""
    params: dict[str, Any] = _page_params(pageNum, pageSize)
    if keyword:
        params["keyword"] = keyword
    if type:
        params["type"] = type
    data = await jsdstu_client.get("/v1/fence/select", token=identity.get("droneToken"), params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "fences": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "type": r.get("type"),
                "status": r.get("status"),
                "description": r.get("description"),
            }
            for r in records[:20]
        ],
    }


async def media_query(
    identity: dict[str, Any],
    taskId: int = 0,
    fileType: str = "",
    grouped: bool = False,
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询媒体文件列表 / 按任务分组概览。"""
    token = identity.get("droneToken")
    page = _page_params(pageNum, pageSize)
    if grouped:
        params: dict[str, Any] = dict(page)
        if fileType:
            params["fileType"] = fileType
        data = await jsdstu_client.get(
            "/v1/media/selectGroupedByTask", token=token, params=params
        )
        records = data if isinstance(data, list) else (data.get("list") or [])
        return {
            "grouped": True,
            "total": len(records),
            "groups": [
                {
                    "taskId": r.get("taskId"),
                    "taskName": r.get("taskName"),
                    "taskStatus": r.get("taskStatus"),
                    "mediaCount": r.get("mediaCount"),
                    "imageCount": r.get("imageCount"),
                    "videoCount": r.get("videoCount"),
                    "pilotName": r.get("pilotName"),
                }
                for r in records[:20]
            ],
        }
    params = dict(page)
    if taskId:
        params["taskId"] = taskId
    if fileType:
        params["fileType"] = fileType
    data = await jsdstu_client.get("/v1/media/select", token=token, params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "medias": [
            {
                "id": r.get("id"),
                "fileName": r.get("fileName") or r.get("name"),
                "fileType": r.get("fileType"),
                "taskId": r.get("taskId"),
                "pilotId": r.get("pilotId"),
                "createTime": r.get("createTime") or r.get("createdAt"),
            }
            for r in records[:20]
        ],
    }


async def rule_query(identity: dict[str, Any], pageNum: int = 1, pageSize: int = 10) -> dict[str, Any]:
    """查询告警规则列表（分页）。"""
    data = await jsdstu_client.get(
        "/v1/alarm/rule/list",
        token=identity.get("droneToken"),
        params={"pageNum": pageNum, "pageSize": pageSize},
    )
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "rules": [
            {
                "id": r.get("id"),
                "alarmName": r.get("alarmName"),
                "alarmType": r.get("alarmType"),
                "thresholdValue": r.get("thresholdValue"),
                "severity": r.get("severity"),
                "escalateSeconds": r.get("escalateSeconds"),
                "enabled": r.get("enabled"),
            }
            for r in records[:20]
        ],
    }


# ---------------------------------------------------------------------------
# 操作类工具（P2）：全部 danger=True，AgentLoop 拦截转二次确认后才会真正执行
# 字段以 jsdstu-3 各 SaveParam/接口签名为准（见 DEV_ISSUES 6.28）
# ---------------------------------------------------------------------------

async def task_create(
    identity: dict[str, Any],
    taskName: str,
    priority: str = "",
    pilotName: str = "",
    routeInfo: str = "",
    description: str = "",
) -> dict[str, Any]:
    """创建飞行任务（草稿，创建后还需下发）。"""
    body: dict[str, Any] = {"taskName": taskName}
    for key, val in (("priority", priority), ("pilotName", pilotName),
                     ("routeInfo", routeInfo), ("description", description)):
        if val:
            body[key] = val
    await jsdstu_client.post("/v1/task/save", token=identity.get("droneToken"), json_body=body)
    return {"ok": True, "message": f"任务「{taskName}」已创建"}


async def task_assign(identity: dict[str, Any], taskId: int) -> dict[str, Any]:
    """下发/派发任务（占用设备与飞手）。"""
    await jsdstu_client.post(f"/v1/task/assign/{taskId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"任务 {taskId} 已下发"}


async def task_delete(identity: dict[str, Any], taskId: int) -> dict[str, Any]:
    """删除任务。"""
    await jsdstu_client.post(f"/v1/task/delete/{taskId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"任务 {taskId} 已删除"}


async def task_audit(identity: dict[str, Any], taskId: int, result: str, comment: str = "") -> dict[str, Any]:
    """审核任务（result=通过/驳回，以系统枚举为准）。"""
    body: dict[str, Any] = {"taskId": taskId, "result": result}
    if comment:
        body["comment"] = comment
    await jsdstu_client.post("/v1/task/audit", token=identity.get("droneToken"), json_body=body)
    return {"ok": True, "message": f"任务 {taskId} 审核结果：{result}"}


# ---- P1 工具补齐（规约 §14）：drone/detail + 任务 PILOT 动作三件 ----

async def drone_detail(identity: dict[str, Any], droneId: int) -> dict[str, Any]:
    """查询设备详情（比列表字段更全：位置/维保信息等）。"""
    return await jsdstu_client.get(
        f"/v1/drone/detail/{droneId}", token=identity.get("droneToken")
    )


async def task_accept(identity: dict[str, Any], taskId: int) -> dict[str, Any]:
    """接单（PILOT 角色动作；admin 全通场景也要，服务端按角色校验 4011 兜底）。"""
    await jsdstu_client.post(f"/v1/task/accept/{taskId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"任务 {taskId} 已接单"}


async def task_reject(identity: dict[str, Any], taskId: int, reason: str = "") -> dict[str, Any]:
    """拒单（PILOT 角色动作；reason 拼在路径里，需 URL 编码）。"""
    path = f"/v1/task/reject/{taskId}"
    if reason:
        path += f"/{quote(reason)}"
    await jsdstu_client.post(path, token=identity.get("droneToken"))
    return {"ok": True, "message": f"任务 {taskId} 已拒单" + (f"（原因：{reason}）" if reason else "")}


async def task_complete(identity: dict[str, Any], taskId: int) -> dict[str, Any]:
    """完成任务（PILOT 角色动作）。"""
    await jsdstu_client.post(f"/v1/task/complete/{taskId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"任务 {taskId} 已完成"}


async def drone_save(
    identity: dict[str, Any],
    code: str,
    name: str,
    model: str = "",
    type: str = "",
    location: str = "",
) -> dict[str, Any]:
    """新增/更新无人机设备（无 id 为新增）。"""
    body: dict[str, Any] = {"code": code, "name": name}
    for key, val in (("model", model), ("type", type), ("location", location)):
        if val:
            body[key] = val
    await jsdstu_client.post("/v1/drone/save", token=identity.get("droneToken"), json_body=body)
    return {"ok": True, "message": f"设备 {code}（{name}）已保存"}


async def drone_delete(identity: dict[str, Any], droneId: int) -> dict[str, Any]:
    """删除无人机设备。"""
    await jsdstu_client.post(f"/v1/drone/delete/{droneId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"设备 {droneId} 已删除"}


_DRONE_STATUS_MAP = {"0": "0", "1": "1", "2": "2", "空闲": "0", "占用": "1", "使用中": "1", "维修": "2", "维修中": "2", "送修": "2"}


async def drone_maintenance(identity: dict[str, Any], droneId: int, status: str) -> dict[str, Any]:
    """设置设备维保状态。status 兼容中文（空闲/占用/维修），统一映射为系统枚举 0/1/2。"""
    mapped = _DRONE_STATUS_MAP.get(str(status).strip())
    if not mapped:
        raise BizError(f"未知的设备状态「{status}」，可选：0 空闲 / 1 占用 / 2 维修")
    await jsdstu_client.post(
        f"/v1/drone/maintenance/{droneId}/{mapped}", token=identity.get("droneToken")
    )
    label = {"0": "空闲", "1": "占用", "2": "维修中"}[mapped]
    return {"ok": True, "message": f"设备 {droneId} 状态已设为「{label}」"}


async def fence_save(
    identity: dict[str, Any],
    name: str,
    type: str = "",
    points: str = "",
    description: str = "",
) -> dict[str, Any]:
    """新增/更新电子围栏（form 绑定接口）。"""
    body: dict[str, Any] = {"name": name}
    for key, val in (("type", type), ("points", points), ("description", description)):
        if val:
            body[key] = val
    await jsdstu_client.post("/v1/fence/save", token=identity.get("droneToken"), data=body)
    return {"ok": True, "message": f"围栏「{name}」已保存"}


async def fence_delete(identity: dict[str, Any], fenceId: int) -> dict[str, Any]:
    """删除电子围栏。"""
    await jsdstu_client.post(f"/v1/fence/delete/{fenceId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"围栏 {fenceId} 已删除"}


async def alarm_acknowledge(identity: dict[str, Any], alarmId: int) -> dict[str, Any]:
    """确认告警（开始处置）。"""
    await jsdstu_client.post(f"/v1/alarm/acknowledge/{alarmId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"告警 {alarmId} 已确认"}


async def alarm_resolve(identity: dict[str, Any], alarmId: int, note: str = "") -> dict[str, Any]:
    """解决告警。"""
    body = {"note": note} if note else None
    await jsdstu_client.post(
        f"/v1/alarm/resolve/{alarmId}", token=identity.get("droneToken"), json_body=body
    )
    return {"ok": True, "message": f"告警 {alarmId} 已解决"}


async def alarm_ignore(identity: dict[str, Any], alarmId: int, note: str = "") -> dict[str, Any]:
    """忽略告警（仅 P2/P3 低优先级可忽略，系统会校验）。"""
    body = {"note": note} if note else None
    await jsdstu_client.post(
        f"/v1/alarm/ignore/{alarmId}", token=identity.get("droneToken"), json_body=body
    )
    return {"ok": True, "message": f"告警 {alarmId} 已忽略"}


# ---------------------------------------------------------------------------
# P0 工具层补齐（规约 §5.2）：查询类 6 个 + 操作类 8 个
# ---------------------------------------------------------------------------

# ---- 查询类（danger=False） ----

async def user_query(
    identity: dict[str, Any],
    username: str = "",
    status: str = "",
    permission: str = "",
    pageNum: int = 0,
    pageSize: int = 0,
) -> dict[str, Any]:
    """查询用户列表（admin 可看全部；非 admin 服务端收敛为本人）。"""
    params: dict[str, Any] = _page_params(pageNum, pageSize)
    for key, val in (("username", username), ("status", status), ("permission", permission)):
        if val:
            params[key] = val
    data = await jsdstu_client.get("/v1/user/select", token=identity.get("droneToken"), params=params)
    records = data.get("list") if isinstance(data, dict) else data
    records = records or []
    return {
        "total": data.get("total", len(records)) if isinstance(data, dict) else len(records),
        "users": [
            {
                "id": r.get("id"),
                "username": r.get("username"),
                "age": r.get("age"),
                "gender": r.get("gender"),
                "phone": r.get("phone"),
                "email": r.get("email"),
                "status": r.get("status"),
                "permission": r.get("permission"),
            }
            for r in records[:20]
        ],
    }


async def user_detail(identity: dict[str, Any], userId: int) -> dict[str, Any]:
    """查询用户详情。"""
    return await jsdstu_client.get(
        f"/v1/user/detail/{userId}", token=identity.get("droneToken")
    )


async def permission_matrix(identity: dict[str, Any]) -> dict[str, Any]:
    """查询角色权限矩阵（admin 能做什么、各角色边界）。"""
    return await jsdstu_client.get(
        "/v1/user/permission-matrix", token=identity.get("droneToken")
    )


async def alarm_detail(identity: dict[str, Any], alarmId: int) -> dict[str, Any]:
    """查询告警详情（含 detail 遥测与处理时间线）。"""
    return await jsdstu_client.get(
        f"/v1/alarm/detail/{alarmId}", token=identity.get("droneToken")
    )


async def alarm_stats(identity: dict[str, Any]) -> dict[str, Any]:
    """查询告警分级统计。"""
    return await jsdstu_client.get("/v1/alarm/stats", token=identity.get("droneToken"))


async def alarm_dashboard(identity: dict[str, Any]) -> dict[str, Any]:
    """查询告警看板（趋势 + 设备/飞手排行，ADMIN）。"""
    return await jsdstu_client.get("/v1/alarm/dashboard", token=identity.get("droneToken"))


# ---- 操作类（danger=True，全部走确认门） ----

async def user_save(
    identity: dict[str, Any],
    username: str,
    id: int = 0,
    email: str = "",
    phone: str = "",
    age: int = 0,
    gender: str = "",
    status: str = "",
    permission: str = "",
) -> dict[str, Any]:
    """新建/编辑用户（表单绑定接口，字段走 query string，见规约坑位 1）。

    id=0 为新建；id>0 为更新（permission 变更影响账号权限，复述必须显式）。"""
    fields: dict[str, Any] = {"username": username}
    if id:
        fields["id"] = id
    for key, val in (("email", email), ("phone", phone), ("gender", gender),
                     ("status", status), ("permission", permission)):
        if val:
            fields[key] = val
    if age:
        fields["age"] = age
    await jsdstu_client.post("/v1/user/save", token=identity.get("droneToken"), data=fields)
    action = "更新" if id else "创建"
    return {"ok": True, "message": f"用户「{username}」已{action}"}


async def user_delete(identity: dict[str, Any], userId: int) -> dict[str, Any]:
    """删除用户（有关联任务/告警会被系统 3010 拒绝）。"""
    await jsdstu_client.post(f"/v1/user/delete/{userId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"用户 {userId} 已删除"}


async def user_status_update(identity: dict[str, Any], userId: int, status: str) -> dict[str, Any]:
    """启用/禁用用户。status：1=启用，0=禁用。"""
    mapped = {"1": "1", "0": "0", "启用": "1", "禁用": "0", "disable": "0", "enable": "1"}.get(
        str(status).strip().lower()
    )
    if not mapped:
        raise BizError(f"未知的用户状态「{status}」，可选：1 启用 / 0 禁用")
    await jsdstu_client.post(
        f"/v1/user/update/status/{userId}/{mapped}", token=identity.get("droneToken")
    )
    label = "启用" if mapped == "1" else "禁用"
    return {"ok": True, "message": f"用户 {userId} 已{label}"}


async def user_password_reset(identity: dict[str, Any], userId: int) -> dict[str, Any]:
    """重置用户密码（高危：复述必须点名"重置谁的密码"）。"""
    await jsdstu_client.post(
        f"/v1/user/update/password/{userId}", token=identity.get("droneToken")
    )
    return {"ok": True, "message": f"用户 {userId} 的密码已重置"}


async def alarm_escalate(identity: dict[str, Any], alarmId: int) -> dict[str, Any]:
    """为告警请求支援（升级给管理员；飞手角色动作，admin 全通场景也要）。"""
    await jsdstu_client.post(f"/v1/alarm/escalate/{alarmId}", token=identity.get("droneToken"))
    return {"ok": True, "message": f"告警 {alarmId} 已升级请求支援"}


async def rule_update(
    identity: dict[str, Any],
    id: int,
    alarmType: str = "",
    alarmName: str = "",
    enabled: str = "",
    thresholdValue: float = 0,
    severity: str = "",
    escalateSeconds: int = 0,
) -> dict[str, Any]:
    """更新告警规则（PUT + JSON，见规约坑位 2）。id 必填，其余按需传。"""
    body: dict[str, Any] = {"id": id}
    for key, val in (("alarmType", alarmType), ("alarmName", alarmName),
                     ("enabled", enabled), ("severity", severity)):
        if val:
            body[key] = val
    if thresholdValue:
        body["thresholdValue"] = thresholdValue
    if escalateSeconds:
        body["escalateSeconds"] = escalateSeconds
    await jsdstu_client.put("/v1/alarm/rule/update", token=identity.get("droneToken"), json_body=body)
    return {"ok": True, "message": f"告警规则 {id} 已更新"}


async def media_delete(identity: dict[str, Any], mediaId: int) -> dict[str, Any]:
    """删除媒体文件（DELETE 方法，id 在 query，不可恢复）。"""
    await jsdstu_client.delete(
        "/v1/media/delete", token=identity.get("droneToken"), params={"id": mediaId}
    )
    return {"ok": True, "message": f"媒体 {mediaId} 已删除"}


# 上传文件的暂存目录（相对 DATA_DIR）：media_upload 只允许读这里，
# 防止模型借工具读任意本地路径（P2 前端文件选择联动前的过渡方案）
MEDIA_UPLOAD_DIR = "media_upload"

_MEDIA_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".avi": "video/x-msvideo", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


async def media_upload(
    identity: dict[str, Any],
    taskId: int,
    paths: list[str] = [],
    description: str = "",
) -> dict[str, Any]:
    """上传任务成果媒体（multipart：taskId+files+description）。

    paths 为**暂存目录内**的文件名列表（防任意路径读取）；
    类型白名单 image/video，大小上限透传 jsdstu-3 校验（100MB）。"""
    if not paths:
        raise BizError("media_upload 需要至少一个文件名（paths）")
    upload_root = (DATA_DIR / MEDIA_UPLOAD_DIR).resolve()
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for p in paths:
        fpath = (upload_root / p).resolve()
        if not str(fpath).startswith(str(upload_root)):
            raise BizError(f"文件 {p} 不在允许的上传目录内")
        if not fpath.is_file():
            raise BizError(f"文件不存在：{p}（请先放入上传暂存目录）")
        mime = _MEDIA_MIME.get(fpath.suffix.lower())
        if not mime:
            raise BizError(f"不支持的文件类型：{fpath.suffix}（仅允许图片/视频）")
        files.append(("files", (fpath.name, fpath.read_bytes(), mime)))
    fields: dict[str, Any] = {"taskId": taskId}
    if description:
        fields["description"] = description
    await jsdstu_client.upload(
        "/v1/media/upload", token=identity.get("droneToken"), fields=fields, files=files
    )
    names = ", ".join(paths)
    return {"ok": True, "message": f"{len(paths)} 个媒体文件已上传到任务 {taskId}：{names}"}


# ---------------------------------------------------------------------------
# 工具声明与实现解耦（manifest 化，2026-09-18）
#
# 「给模型看的说明书」（name/description/parameters/danger）按业务域存放于
# manifests/*.json（drone/task/alarm/user/fence/media/dashboard）；
# 「实现」（handler）留在本模块。启动时双向校验：清单与 handler 必须一一
# 对应，多一个少一个都 fail-fast 拒绝启动，防止"改了清单忘改实现"的静默漂移。
# 修改 manifest 后重启服务生效（无热加载：registry 在 import 时构建，
# 白名单/指纹熔断均依赖它，保持启动期一次性装配最稳）。
# ---------------------------------------------------------------------------

_MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"


def _load_manifest_decls() -> dict[str, dict[str, Any]]:
    """读取全部 manifest，返回 工具名 -> 声明(dict)。重名即抛错。"""
    decls: dict[str, dict[str, Any]] = {}
    for f in sorted(_MANIFESTS_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for item in data.get("tools") or []:
            name = str(item.get("name") or "")
            if not name:
                raise ValueError(f"manifest {f.name} 存在缺 name 的工具声明")
            if name in decls:
                raise ValueError(f"工具重名（manifest）: {name}")
            if not item.get("description") or not isinstance(item.get("parameters"), dict):
                raise ValueError(f"manifest {f.name} 中 {name} 缺 description/parameters")
            decls[name] = item
    if not decls:
        raise RuntimeError(f"manifests 目录为空: {_MANIFESTS_DIR}")
    return decls


def _register_default_tools() -> None:
    """清单（manifests/*.json）+ 实现（本模块 handler）拼装注册表。

    handlers 的键是"代码侧清单"：与 manifest 双向校验，两边必须完全一致。
    新增工具 = manifest 加声明 + 这里加一行 handler + 写实现函数。"""
    handlers: dict[str, Callable[..., Awaitable[Any]]] = {
        "alarm_acknowledge": alarm_acknowledge,
        "alarm_dashboard": alarm_dashboard,
        "alarm_detail": alarm_detail,
        "alarm_escalate": alarm_escalate,
        "alarm_ignore": alarm_ignore,
        "alarm_query": alarm_query,
        "alarm_resolve": alarm_resolve,
        "alarm_stats": alarm_stats,
        "dashboard_query": dashboard_query,
        "drone_delete": drone_delete,
        "drone_detail": drone_detail,
        "drone_maintenance": drone_maintenance,
        "drone_query": drone_query,
        "drone_save": drone_save,
        "fence_delete": fence_delete,
        "fence_query": fence_query,
        "fence_save": fence_save,
        "media_delete": media_delete,
        "media_query": media_query,
        "media_upload": media_upload,
        "permission_matrix": permission_matrix,
        "rule_query": rule_query,
        "rule_update": rule_update,
        "task_accept": task_accept,
        "task_assign": task_assign,
        "task_audit": task_audit,
        "task_complete": task_complete,
        "task_create": task_create,
        "task_delete": task_delete,
        "task_query": task_query,
        "task_reject": task_reject,
        "user_delete": user_delete,
        "user_detail": user_detail,
        "user_password_reset": user_password_reset,
        "user_query": user_query,
        "user_save": user_save,
        "user_status_update": user_status_update,
    }
    decls = _load_manifest_decls()
    missing = sorted(set(decls) - set(handlers))
    extra = sorted(set(handlers) - set(decls))
    if missing or extra:
        raise RuntimeError(
            "工具清单与实现不一致，拒绝启动："
            f"清单有而代码无 handler={missing}；代码有而清单无声明={extra}"
        )
    for name, item in decls.items():
        registry.register(Tool(
            name=name,
            description=str(item["description"]),
            parameters=item["parameters"],
            handler=handlers[name],
            danger=bool(item.get("danger", False)),
        ))


# import 时一次性装配（清单校验失败将阻断服务启动，见上方案注）
_register_default_tools()

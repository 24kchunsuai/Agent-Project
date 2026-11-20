"""v1 路由聚合。新增模块时在这里挂载，main.py 不需要改动。"""

from fastapi import APIRouter

from app.api.v1.endpoints import agent, health, kb, kb_governance, ops, profile, rag

api_router = APIRouter()
api_router.include_router(health.router)
# 运维健康聚合（E-3）：/v1/ops/health——受 X-Agent-Key 保护（不在豁免清单里）
api_router.include_router(ops.router)
api_router.include_router(agent.router, prefix="/agent")
api_router.include_router(rag.router, prefix="/rag")
api_router.include_router(kb.router, prefix="/kb")
# 治理接口与 kb 同前缀：/v1/kb/governance/*（spec §6.1）
api_router.include_router(kb_governance.router, prefix="/kb")
# 用户画像（M-5）：/v1/profile/*——查看与删除，抽取开关在配置层
api_router.include_router(profile.router, prefix="/profile")

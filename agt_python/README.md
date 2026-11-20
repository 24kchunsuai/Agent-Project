# agt_python — Drone Agent 智能体服务

架构分层中的 **agent 层**：只负责智能，不碰数据库、不碰权限。

```
Vue (9090)  ──HTTP──>  Java 后端 (8080)  ──HTTP──>  Python agent (8000)
   界面与交互              业务/鉴权/持久化              LLM 编排与推理
```

## 目录职责

| 路径 | 职责 |
|---|---|
| `app/main.py` | 应用入口，中间件、lifespan、路由与异常处理注册 |
| `app/core/config.py` | 全部配置项，只从 env / `.env` 读取 |
| `app/core/response.py` | 统一响应体（code / message / data），与 Java 侧 JsonResult 一致 |
| `app/core/exception.py` | 全局异常处理，保证错误响应也是 JsonResult 结构 |
| `app/api/v1/router.py` | v1 路由聚合，新增模块在此挂载 |
| `app/api/v1/endpoints/` | 路由层：只做参数校验与响应包装 |
| `app/schemas/` | Pydantic 契约，字段对外用 camelCase |
| `app/services/` | 业务编排层，真正的 agent 逻辑写在这里 |
| `tests/` | 冒烟测试 |

**铁律**：编排逻辑不写进 `endpoints/`，一切放 `services/`，否则后面接 LLM、加多轮记忆、加工具调用时会失控。

## 响应约定（与 Java 侧完全对齐）

| code | 含义 |
|---|---|
| 2000 | 操作成功 |
| 3000 | 操作失败 |
| 3001 | 参数校验失败 |
| 3002 | 请求数据不存在 |
| 3010 | 智能体服务不可用 |
| 3011 | 智能体调用失败 |

**所有响应（含错误）HTTP 状态码恒为 200**，业务结果由 body 的 `code` 表达 ——
这样 Java 侧只需要一套解析逻辑，不会因为 422/500 而解析失败。

## 运行环境

**统一使用本机 conda 环境 `python311`（Python 3.11.15），不另建 venv。**

| 项 | 值 |
|---|---|
| 环境名 | `python311` |
| 解释器 | `D:\install\python\anaconda\envs\python311\python.exe` |
| PyCharm 配置 | `.idea/misc.xml` → Black SDK 已指向 `Python 3.11 (python311)` |

## 快速开始

```bash
cd agt_python

# 1. 依赖（已装好，换机器或加依赖时才需要）
conda activate python311
pip install -r requirements.txt

# 2. 配置
copy .env.example .env

# 3. 启动（两种写法等价，任选）
conda activate python311 && python -m uvicorn app.main:app --reload --port 8000
D:\install\python\anaconda\envs\python311\python.exe -m uvicorn app.main:app --reload --port 8000

# 4. 验证
curl http://localhost:8000/v1/health
```

- Swagger 文档：http://localhost:8000/docs
- 健康探针：http://localhost:8000/v1/health

## 接口

### GET /v1/health

```json
{ "code": 2000, "message": "操作成功", "data": { "status": "up", "version": "0.1.0", "llmConfigured": false } }
```

### POST /v1/agent/chat

请求：

```json
{
  "sessionId": "s-001",
  "userId": 1,
  "message": "你好",
  "history": [{ "role": "user", "content": "上一句" }],
  "metadata": {}
}
```

响应：

```json
{
  "code": 2000,
  "message": "操作成功",
  "data": {
    "sessionId": "s-001",
    "content": "[agent 骨架] ...",
    "model": "placeholder",
    "usage": { "promptTokens": 0, "completionTokens": 0 },
    "traceId": "3f8a1c2d9e4b7a06"
  }
}
```

## 测试

```bash
conda activate python311
cd agt_python
python -m pytest -q
```

## 当前状态

骨架阶段：接口链路已通，`AgentService._generate()` 为占位实现。
未完成项：接入 LLM、多轮记忆、工具调用、流式输出（SSE）、RAG 检索。

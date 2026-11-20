---
doc_id: proj-arch-201
title: drone-agent001 项目架构总览（三层分离设计）
category: project-doc
source: self
version: v1.0
updated: 2026-09-16
owner: default
---

# drone-agent001 项目架构总览

## 一、设计理念

项目的核心设计原则是**分层解耦**：业务与鉴权归 Java，智能与编排归 Python，交互归 Vue。三层各司其职，通过 HTTP 接口通信，任一层可以独立替换或扩容。

```
┌─────────────────────────┐
│  agt_web  (Vue 3)       │  端口 9090
│  聊天界面 / 会话侧栏     │  开发代理 /v1 → :8080
└───────────┬─────────────┘
            │ HTTP / SSE
┌───────────▼─────────────┐
│  agt_backend (Java)     │  端口 8080
│  业务逻辑 / 鉴权 / 持久化 │  Spring Boot 3.3.3
└───────────┬─────────────┘
            │ HTTP / SSE
┌───────────▼─────────────┐
│  agt_python (FastAPI)   │  端口 8000
│  意图路由 / LLM / 记忆    │  Python 3.11
└─────────────────────────┘
```

## 二、各层职责

### 2.1 agt_web（前端）

- 技术栈：Vue 3 + axios + marked + DOMPurify + highlight.js
- 端口 9090，开发环境下通过代理把 `/v1` 转发到后端 8080
- 核心页面：`AgentChatView.vue`（聊天主页面）
- 关键组件：`SessionSidebar.vue`（会话侧边栏，新建/切换/删除会话）
- 工具模块：`utils/markdown.js`（渲染与净化）、`utils/sessionStore.js`（localStorage 持久化）
- 流式实现：`fetch` + `ReadableStream` 按 `data:` 行解析，支持 `AbortController` 中断

### 2.2 agt_backend（后端）

- 技术栈：Spring Boot 3.3.3，包名 `cn.tedu.agt`，端口 8080
- 职责边界：**只管业务、鉴权、持久化，不碰 LLM 逻辑**
- 关键类：
  - `AgentController`（`/v1/agent/*` 对外接口）
  - `AgentService` / `AgentServiceImpl`（编排，含流式 SseEmitter 与独立线程池）
  - `AgentClient`（调用 Python 服务：RestClient 同步 + JDK HttpClient 流式）
  - `base/` 包（`JsonResult`、`StatusCode`、`ServiceException`、全局异常处理、跨域配置、Knife4j 文档配置）

### 2.3 agt_python（智能体）

- 技术栈：FastAPI + httpx + Pydantic
- 端口 8000
- 职责边界：**只管智能，不碰业务数据与权限**
- 关键模块：
  - `core/config.py`（.env 配置加载）
  - `core/llm.py`（LLM 调用，含 `chat` 与 `chat_stream` 异步实现）
  - `core/response.py`（统一响应体与状态码）
  - `schemas/agent.py`（请求/响应模型）
  - `services/agent_service.py`（对话编排：意图分派）
  - `services/intent/`（意图路由：规则 → LLM → 兜底三级策略）
  - `services/memory.py`（会话记忆，内存版滑动窗口）
  - `prompts/`（提示词模板：人设、意图分类）

## 三、数据流（一次流式对话）

1. 前端 `POST /v1/agent/chat/stream`（经代理到 8080）
2. Java `AgentController` 接收，交给 `AgentServiceImpl.chatStream`
3. Java 通过 JDK HttpClient 调用 Python `POST /v1/agent/chat/stream`
4. Python 做意图识别 → 分派到闲聊或业务处理 → 流式调用 LLM
5. Python 以 SSE 事件推回：`meta`（元信息，含 intent）→ `delta`（增量文本）* → `done` / `error`
6. Java 逐事件转发给前端（SseEmitter）
7. 前端按 `data:` 行解析，逐字渲染，markdown 实时渲染

## 四、核心约定

| 项 | 约定 |
|---|---|
| 接口前缀 | `/v1`（不带 `/api`） |
| 成功响应码 | `2000` |
| 失败响应码 | `3000` 段（3010 智能体不可用、3011 调用失败） |
| SSE 事件协议 | `meta` → `delta`* → `done` \| `error` |
| 意图类型 | `chitchat`（闲聊）、`business`（业务） |
| 配置管理 | Python 用 `.env`（已 gitignore），Java 用 `application.yml` |

## 五、为什么要分三层

1. **职责单一**：LLM 相关逻辑变更只改 Python，业务规则变更只改 Java，互不影响
2. **技术选型自由**：Python 生态在 AI 方向更成熟，Java 生态在企业级业务与鉴权上更成熟
3. **独立演进**：Python 侧的 RAG、多 Agent 编排升级不需要动 Java
4. **可替换性**：理论上任一层都可以换实现（如把 Python 换成别的推理服务），只要接口契约不变

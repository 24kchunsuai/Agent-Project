---
doc_id: proj-api-202
title: 智能体接口文档（对话、流式对话、健康探测）
category: project-doc
source: self
version: v1.0
updated: 2026-09-16
owner: default
---

# 智能体接口文档

## 一、接口清单（Java 层，端口 8080）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/agent/chat` | 同步对话，一次性返回完整回答 |
| POST | `/v1/agent/chat/stream` | 流式对话（SSE），逐字返回 |
| GET | `/v1/agent/health` | 智能体服务健康探测 |

## 二、POST /v1/agent/chat

**请求体**：

```json
{
  "message": "用一句话介绍你自己",
  "sessionId": "可选的会话标识"
}
```

**响应体**（统一包装）：

```json
{
  "code": 2000,
  "msg": "操作成功",
  "data": {
    "content": "我是小飞……",
    "intent": "chitchat",
    "intentSource": "rule",
    "traceId": "…"
  }
}
```

**字段说明**：

| 字段 | 含义 |
|---|---|
| `content` | 模型生成的回答文本 |
| `intent` | 意图类型：`chitchat`（闲聊）/ `business`（业务） |
| `intentSource` | 意图判定来源：`rule`（规则命中）/ `llm`（模型判定）/ `fallback`（兜底） |
| `traceId` | 链路追踪标识，便于定位问题 |

## 三、POST /v1/agent/chat/stream

**请求体**：与同步接口一致。

**响应**：`text/event-stream`（SSE），事件协议为：

```
meta → delta* → done | error
```

### 3.1 meta 事件

连接建立后首先推送，携带本次请求的元信息：

```
data: {"type":"meta","intent":"chitchat","intentSource":"rule","traceId":"…"}
```

### 3.2 delta 事件

正文增量，可能推送多次，每次是一小段文本：

```
data: {"type":"delta","content":"我"}
data: {"type":"delta","content":"是"}
```

### 3.3 done 事件

生成结束时推送，表示正常收尾：

```
data: {"type":"done"}
```

### 3.4 error 事件

发生异常时推送，携带错误码与提示：

```
data: {"type":"error","code":3011,"msg":"智能体调用失败"}
```

**错误码**：`3010` 表示智能体不可用，`3011` 表示调用失败。

## 四、GET /v1/agent/health

返回 Python 智能体服务的可用性信息，用于健康探测与联调排查。响应中包含 Python 侧版本等基础信息。

## 五、前端如何消费流式接口

前端不使用 `EventSource`（因为它只支持 GET），而是用 `fetch` + `ReadableStream`：

1. `fetch` 发起 POST 请求，获取 `response.body` 的 reader
2. 逐块解码为文本，按行切分
3. 过滤出以 `data:` 开头的行，`JSON.parse` 后按 `type` 分派处理
4. 通过 `AbortController` 支持中途取消

**注意**：SSE 数据可能跨 chunk 被截断（半帧），解析时需要**缓存未完成的行**，不能假设每次 `read()` 都能拿到完整行。

## 六、Python 层接口（端口 8000）

Python 侧提供与 Java 层对应的接口，供 Java 调用：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/agent/chat` | 同步对话 |
| POST | `/v1/agent/chat/stream` | 流式对话（SSE） |
| GET | `/v1/health` | 健康检查（暴露 Python 版本） |

**重要约定**：Java 侧使用 JDK `HttpClient` 调用 Python 的 uvicorn 服务时，**必须显式指定 HTTP/1.1**（`version(HTTP_1_1)`）。原因是 HttpClient 默认会尝试 h2c（HTTP/2 明文升级），而 uvicorn 不接受该升级，会导致请求体丢失、Pydantic 报 `Field required`。这是本项目踩过的坑，务必遵守。

---
doc_id: proj-lessons-205
title: 项目开发踩坑记录（跨语言链路与工程化问题）
category: project-doc
source: self
version: v1.0
updated: 2026-09-16
owner: default
---

# 项目开发踩坑记录

本项目在搭建三层 Agent 系统过程中踩过的真实问题，按类别整理，供排查同类问题参考。

## 一、跨语言调用问题

### 1.1 JDK HttpClient 调 uvicorn 必须指定 HTTP/1.1 ⭐

**现象**：Java 调用 Python 接口，Python 侧 Pydantic 报 `Field required`，即请求体丢失。

**原因**：JDK `HttpClient` 默认会尝试 h2c（HTTP/2 明文升级），而 uvicorn 不接受该升级，导致请求体在协商过程中丢失。

**解决**：显式指定协议版本 `HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)`。

**排查启示**：跨语言调用出现「字段丢失」时，先怀疑协议协商层，而非序列化层。

### 1.2 Java 侧 VO 字段未同步导致数据静默丢弃

**现象**：Python 返回了 `intent` 字段，Java 转发给前端后字段消失。

**原因**：Jackson 反序列化时，目标 VO 类没有对应字段，默认**静默忽略**未知字段，不报错。

**解决**：跨层新增字段时，必须同步补齐链路上每一层的模型定义（Python schema → Java DTO/VO → 前端解析）。

### 1.3 SSE 数据跨 chunk 截断（半帧问题）

**现象**：前端偶发解析失败，报 JSON 解析错误。

**原因**：`ReadableStream` 每次 `read()` 返回的 chunk 边界与 SSE 事件边界不一致，一行 `data:` 可能被切成两半。

**解决**：解析时缓存未完成的行，与下一块拼接后再解析，不能假设每次读取都拿到完整行。

## 二、Windows 环境问题

### 2.1 打包前必须停止运行中的 jar ⭐

**现象**：`mvn package` 因无法覆盖 jar 文件而失败。

**原因**：Windows 有文件锁机制，运行中的 jar 被 JVM 持有，无法覆盖。

**解决**：先停止 Java 进程，再打包，最后重启。

### 2.2 Maven Wrapper 在 Git Bash 下失效

**现象**：`./mvnw` 无法正常执行。

**解决**：直接调用 Maven 主类执行打包，或改用 PowerShell/CMD 终端，也可在 IDEA 的 Maven 面板操作。

### 2.3 长驻服务不要用 `&` 挂后台再跑验证

**现象**：启动的服务在命令结束后被回收。

**原因**：主命令结束后进程树被回收，连坐杀掉子进程。

**解决**：使用专门的后台任务机制启动服务，不要用 shell 的 `&` 挂起长驻进程。

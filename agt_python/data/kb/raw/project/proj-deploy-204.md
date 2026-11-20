---
doc_id: proj-deploy-204
title: 三端项目启动与部署指南
category: project-doc
source: self
version: v1.0
updated: 2026-09-16
owner: default
---

# 项目启动与部署指南

## 一、启动顺序

必须先启动下游服务，再启动上游服务：

```
1. agt_python (8000)  ← 智能体，最底层
2. agt_backend (8080) ← 业务层，依赖 Python
3. agt_web (9090)     ← 前端，依赖 Java
```

**原因**：Java 启动时会调用 Python 的健康探测；前端开发代理指向 Java。顺序颠倒会导致启动阶段报「智能体不可用」（`3010`）。

## 二、agt_python 启动

**环境**：conda 环境 `python311`（Python 3.11）

```bash
# 激活环境
conda activate python311

# 进入目录
cd agt_python

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**配置文件**：`agt_python/.env`（已被 `.gitignore` 排除，**严禁提交到版本库**）

```
APP_NAME=agt-python-agent
DEBUG=true
HOST=0.0.0.0
PORT=8000

LLM_PROVIDER=deepseek
LLM_API_KEY=<your-key>
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_TIMEOUT=60

EMBEDDING_API_KEY=<your-key>
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_MODEL=embedding-3
```

**依赖安装**：

```bash
pip install -r requirements.txt
```

## 三、agt_backend 启动

**环境要求**：JDK 17+

**打包与运行**：

```bash
cd agt_backend
./mvnw package -DskipTests
java -jar target/agt_backend-0.0.1-SNAPSHOT.jar
```

**⚠️ Windows 环境注意事项**：

1. **打包前必须先停止正在运行的 jar 进程**——Windows 存在文件锁，进程未退出时打包会因无法覆盖 jar 而失败
2. **Git Bash 下 Maven Wrapper（`./mvnw`）可能失效**，此时可直接调用 Maven 主类执行，或改用 IDEA 的 Maven 面板/CMD 终端
3. 开发阶段可直接在 IDEA 中运行 `AgtBackendApplication`，避免反复打包

**接口文档**：项目集成了 Knife4j，启动后可通过文档页面查看与调试接口。

## 四、agt_web 启动

```bash
cd agt_web
npm install       # 首次或依赖变更后执行
npm run serve     # 启动开发服务器，端口 9090
```

**开发代理**：`vue.config.js` 中把 `/v1` 代理到 `http://localhost:8080`，因此前端代码中的请求路径写 `/v1/...` 即可。

**⚠️ 注意**：新增 npm 依赖后，dev server **必须重启**才能识别，热更新不会重新加载依赖图。

**前端构建**：

```bash
npm run build     # 产出 dist/ 目录
```

## 五、服务验证

| 检查项 | 命令 / 方式 |
|---|---|
| Python 健康 | `curl http://localhost:8000/v1/health` |
| Java 健康 | `curl http://localhost:8080/v1/agent/health` |
| 前端可访问 | 浏览器打开 `http://localhost:9090` |
| 端口占用检查 | `netstat -ano \| grep LISTENING`（Windows） |

## 六、常见启动问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 打包失败，提示文件被占用 | 旧 jar 进程仍在运行 | 先停进程再打包 |
| Java 报 `3010` 智能体不可用 | Python 未启动或端口不通 | 先启动 Python，确认 8000 监听 |
| 前端请求 404 | 代理未生效或路径写错 | 检查 `vue.config.js` 代理配置；路径前缀为 `/v1` |
| Python 报 `Field required` | Java 调用时协议升级问题 | 确保 Java 侧 HTTP 客户端指定 HTTP/1.1 |
| 新装依赖报模块找不到 | dev server 未重启 | 重启前端服务 |
| 端口被占用 | 有残留进程 | 查端口 PID 后终止进程 |

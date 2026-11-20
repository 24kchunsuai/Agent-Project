# AgentOS 1.0 — 项目说明与达成路径

> 版本：v1.2 ｜ 日期：2026-09-24 ｜ 状态：执行中
> 仓库/目录名仍为 `drone-agent001`；**「AgentOS 1.0」指本项目的 1.0 目标版本**——
> 把现有三端 AI Agent 系统从「演示级」推进到「可交付的内部生产版本」。
> 若后续正式改名，本文档整体按同一目标维护。
> 本文档是**达成路径与任务拆解的唯一入口**；细节以 `docs/specs/` 下各专项规约与
> `DEV_ISSUES.md`（73 章开发台账）为准，冲突时以规约为准。
> **当前版本号以仓库根 `VERSION` 为准**（现为 `1.0.0`），变更记录见 `CHANGELOG.md`。

---

## §1 项目是什么

**一句话**：用自然语言驱动一个无人机运维平台——用户在网页上问一句话，系统自己决定
「查知识库 / 调业务接口 / 派子代理」，把答案连引用一起答出来，并把全过程记成可追溯的数据。

| 维度 | 内容 |
|---|---|
| 业务场景 | 无人机运维（机队、任务、告警、围栏、媒体、用户、规则） |
| 使用方 | 运维人员（自然语言问答）+ 管理员（危险操作二次确认 + 语料治理 + 数据看板） |
| 业务后端 | `jsdstu-3`（无人机主控，:8060）——外部依赖，**只调用、不改代码** |
| 技术栈 | Vue3(:9090) + Spring Boot 3.3.3(:8080) + FastAPI(:8000) + Milvus + SQLite |
| 架构形态 | 三端分层单体（非微服务）：前端管交互、Java 管鉴权与业务、Python 只负责智能 |

**三端职责铁律**：Python 侧**不碰数据库、不碰权限**——它只做 LLM 编排与推理；
身份与授权由 Java 注入；前端永不直连 Python。

```
Vue3 前端 :9090  ──HTTP──▶  Spring Boot :8080  ──HTTP──▶  FastAPI agent :8000
  界面与交互                 业务 / 鉴权 / 持久化              LLM 编排与推理
                                     │                              │
                                     └──────▶ jsdstu-3 :8060 ◀──────┘
                                              无人机业务后端（外部）
```

---

## §2 能力全景

五块主干能力，全部已有实现与验收记录：

| # | 能力域 | 核心内容 | 关键实现 |
|---|---|---|---|
| 1 | **RAG 知识库** | 混合检索（BM25+dense 双路 RRF 融合）→ rerank → 证据充分性评审 → 生成；含查询改写、跨域拒答、引用溯源 | `app/rag/`、`app/services/rag_service.py` |
| 2 | **多智能体编排** | 意图识别 → 复杂度路由 → Plan-and-Execute / ReAct 双引擎 → 专家委派 / 子代理（并行 + 黑板）→ 32 业务工具 | `agent_loop.py`、`planning.py`、`delegation_router.py`、`subagent.py` |
| 3 | **会话记忆** | M-1 持久化 / M-2 token 双锚点裁剪 / M-3 相关性过滤 / M-4 埋点 / M-5 用户画像 + TTL 定时清理 | `memory_sqlite.py`、`memory_token.py`、`memory_trim.py`、`history_filter.py`、`user_profile.py` |
| 4 | **数据中台** | ODS 五类事件表 → DWS 预聚合 → ADS 九页看板；trace 七段链路还原（四色状态）+ 知识缺口聚类 | `telemetry.py`、`docs/specs/数据中台开发规约.md` |
| 5 | **语料治理** | 生命周期状态机 / 回收站 / TTL / 版本链 / 向量对账 / 巡检工单 / 审计账本 | `governance_service.py`、`inspection_service.py`、`reconcile_service.py` |

**我们比同类参照项目多出来的（差异化，别因"别人没写"而怀疑价值）**：
数据中台、语料治理、知识缺口聚类、用户画像、链路阶段还原。
（对照口径见 `docs/specs/Agentic模式对照-drone现状与落地建议.md`：21 个 Agentic 设计模式中
✅ 9 项已对等或更强、🟡 6 项部分实现、⬜ 6 项缺口，覆盖率 71%。）

---

## §3 当前状态总览（截至 2026-09-24）

### 3.1 代码规模

| 端 | 规模 | 说明 |
|---|---|---|
| `agt_python` | 62 个 py 文件 / 43 个路由 | 智能体编排、RAG、记忆、中台埋点、治理、运维聚合 |
| `agt_backend` | 39 个 java 文件 / 6 个 Controller | 鉴权、透传、统一响应体（`JsonResult`）、版本与运维聚合 |
| `agt_web` | 6 个主页面 + 9 个看板页 + 9 个组件 | 聊天、知识库、画像、治理面板、数据看板 |
| 工具 | 7 个 manifest / 41 条登记 / 注册表 37 个 | 覆盖 jsdstu-3 的 41 个端点中 37 个；危险工具走确认门 |
| 测试 | **Python 419 项**（415 过 / 4 项既有基线已在 `pytest.ini` 显式 deselect）+ **Java 174 项全绿**（19 个测试类，E-5 从 0 到 1） | 一条命令跑三端门禁：`bash scripts/ci.sh`（**五作业** ≈73s）；Java 侧覆盖鉴权 / 透传契约 / 响应体 / 审计 / 指标五组；Python 侧含备份恢复一致性 18 项、审计 41 项、依赖扫描 54 项 |

### 3.2 已达成的关键指标（均有验收记录）

| 域 | 指标 | 数值 |
|---|---|---|
| RAG 检索 | hit@1 / hit@3（查询改写前后） | **52.5% → 65.0%**（+12.5pp）/ **80.0% → 87.5%**（+7.5pp）；40 题有标注集，收益来自口语 / 多轮指代类受损样本（属上限侧） |
| RAG 端到端 | 52 题评估 pass_rate | **0.981**，意图识别 100% |
| 业务问答 | 60 题评估均分 | **2.0 / 2.0**（满分口径 2.0） |
| 多智能体 | 业务 e2e | **34 / 34** 全绿 |
| 容错 | 指纹熔断 e2e / 全链路容错 e2e | **7/7 · 11/11** |
| 语料治理 | G1–G4 验收断言 | **311 项**（45+104+85+77），三端 16 端点全通 |
| 数据中台 | 事件表 / 看板页 / 规约版本 | 5 表 / 9 页 / 演进至 **v5** |
| 知识库 | 集合 / 切片 / 文档 | `kb_drone` / 1998 片 / ~80 篇 |

### 3.3 一句话状态

> **功能面已完整闭环（五块主干全部落地并有验收数字）；
> 工程面正在从「演示级」升到「企业级可交付」——S-1 鉴权、E-1 版本基线、E-2 一键启停已落地。**

按四档口径（详见 `docs/specs/企业级落地规约.md` §1）：

| 档位 | 定义 | 现状 |
|---|---|---|
| L1 演示级 | 单机、自己人点开看 | ✅ 已达标 |
| L2 内部生产 | 小团队真实使用：可信内网、数据不丢、故障有人知道 | 🟡 S-1 已落地（密钥 + 回环），S-2~S-10 未做 |
| **L3 企业级可交付** | **陌生人照手册能部署、坏了能自己修、问得出账** | ✅ **已收官（2026-09-25）**：**E-P0 可交付最小集已收官**（E-1 ✅ / E-2 ✅ / E-3 ✅ / E-4 ✅ / E-5 ✅）→ **E-P1 已收官**：**E-7 数据备份 + 恢复演练 ✅**（判据 C4 从"零备份"变为"演练过的可恢复"）、**E-6 日志规范与轮转 ✅**（判据 C3 从"裸文本无轮转"变为"JSON lines + traceId 三端贯通 + 脱敏"）、**E-9 轻量指标端点 ✅**（判据 C3 再补上"机器可抓的 Prometheus 文本"，零新依赖）、**E-8 操作审计留痕 ✅**（判据 C5 从"只有治理动作有审计"变为"一张表回答谁做了什么"）→ **E-P2 已收官**：**E-10 密钥轮换 + 权限矩阵 ✅**（判据 C5 再补上"**不停服换密钥**"与"**每行权限可 grep**"）、**E-11 依赖与漏洞扫描 ✅**（判据 C1 从"交付包里带已知 Critical 漏洞"变为"**Critical 阻断发布**"——上线即抓到并处置 **7 条真实 Critical**）、**E-12 前端运行时报错上报 ✅**（判据 C3 补上"**浏览器侧**"这一体——此前前端崩了在三端都没有记录，现在看板上一页三问可答）→ **E-P3 已收官**：**E-13 成本计量与配额 ✅**（判据 C7 从"答不出花了多少钱"变为"**一个端点 + 一页看板答三问**"——成本查询期折算、流式 usage 补洞、配额超限降级为提示；**七条判据至此全部落地**）→ **E-14 容器化交付 ✅**（判据 C1 双轨的第二条轨道实测可跑——三 Dockerfile + compose + nginx SSE 三件套）→ **E-15 验收清单与演示脚本 ✅**（C1 收口：`docs/验收清单.md` 逐条命令与期望输出 + `scripts/demo.py` 核心链路 **21/21 实测通过**；**七判据 C1~C7 全部落地，L3 企业级可交付达成**）；**S-8 PII 脱敏补收口**（2026-09-25：手机号/身份证/邮箱与 E-6 同出口叠加，Python 415 / Java 174 全绿，存量库复扫 0 命中） |
| L4 规模化生产 | 多实例、公网、SLA | ⏸ 只划边界（企业级规约 §8） |

> **起服务、看状态、排查故障**：直接看 `docs/部署手册.md`（§4 启停 / §5 验证 / §7 故障对照表）；
> 一键命令在 §6.2。

---

## §4 达成路径（主线）

以「从演示级到内部生产，再到有据可依的工程资产」为主线，五个阶段：

```
Phase 0  功能闭环                     ✅ 已完成
   └ RAG · 编排 · 记忆 · 中台 · 治理 —— 五块主干 + 验收数字

Phase 1  内部生产下限（P0，4 项各半天，一天可全落）        ⬜ 当前
   └ S-1 信任边界 · S-2 前端构建 · S-3 进程守护 · S-4 数据备份
   └ 完成后：可信内网可交付真实用户

Phase 2  生产增强（P1，约 4 天）                          ⬜
   └ S-5 HTTPS+限流 · S-6 告警出口 · S-7 注入防护 · S-8 PII 脱敏

Phase 3  工程资产与可度量（收益确定、无依赖）              ⬜
   └ CI 流水线 · 轨迹评分口径 · 成本视图 · 澄清追问 · 工具统一重试
   └ 关键动作：跑一次压测 + 一次评估集，把数字沉淀成可引用的基线

Phase 4  能力扩展（P2/P3，等条件触发）                    ⏸
   └ MCP 接入 · 整栈容器化 · 程序记忆 · 无人访问巡检 · 优先级排序 · A2A

Phase 5  规模化生产（只划边界，当前不做）                 ⏸
   └ SQLite→MySQL · 缓存与调度外置 · 多 worker · Milvus 高可用 · RBAC 审计
```

**为什么是这个顺序**：Phase 1 的四项是所有后续工作的前提（服务能被伪造 / 进程会静默消失 /
数据会丢，其余优化都无意义）；Phase 3 是关键分水岭——它把「我做了个系统」变成
「我做了个**可度量**的系统」，且不需要再写产品代码。

---

## §5 详细任务拆解

状态标记：✅ 已完成 ｜ ⬜ 待办 ｜ ⏸ 等待触发条件
「期」沿用各规约既有编号（S-* 出自生产级改进规约，T-* 为本 README 补齐项）。

### Phase 1 — 内部生产下限（P0）

| 编号 | 任务 | 落点 | 依赖 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| **S-1** | 服务间信任边界：Python 侧新增 `X-Agent-Key` 共享密钥中间件；`agent_shared_key` 为空**拒绝启动**；Java 出站统一加头；启动改绑 `127.0.0.1` | `agt_python` 中间件 + `config.py` + `AgentClient.java` | 无 | 无 key/错 key → 4010 并落 `fault_event`；空密钥拒绝启动；`netstat` 确认只监听 127.0.0.1；Java 全链路不受影响 | ⬜ **最先做** |
| **S-2** | 前端生产构建 + nginx 托管：`npm run build` → `deploy/nginx.conf`（`/api` 反代 8080、**SSE `proxy_buffering off`**、history fallback） | `agt_web` + `deploy/` | 无 | 生产构建页面可登录/聊天（流式逐字）/翻页/画像；SSE 首事件 < 2s | ⬜ |
| **S-3** | 进程守护与开机自启：四服务改由 Windows 服务承载（NSSM 优先），`AppExit → Restart` + 自动启动 + 日志滚动 | 宿主环境（**8060 只包装不改**） | 无 | 手杀任一进程 30s 内自动恢复；重启机器后四服务自动在位 | 🟡 **部分完成**：**E-2 补启停/自检脚本 + S4U 计划任务**（替代 NSSM，实测 SessionId=0 不随会话死）、**E-6 补日志轮转**；开机自启用 `install-tasks.ps1 -StartAtLogon`，`AppExit → Restart` 未做 |
| **S-4** | 数据备份：`scripts/backup_dbs.py` 备份四库（含 `-wal/-shm`）+ `PRAGMA integrity_check`，保留 14 份，挂入 `scheduler` 每日任务 | `agt_python/scripts` + `scheduler.py` | 无 | 手动触发产出当日备份且校验 ok；超 14 份滚动清理且只动备份目录 | ✅ **升级为 E-7 已完成**（2026-09-24）——且**从"备份成功"提升到"恢复演练通过"**；快照改走 SQLite 在线备份 API（有意偏离，已记档） |

### Phase 2 — 生产增强（P1）

| 编号 | 任务 | 落点 | 依赖 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| **S-5** | HTTPS 终结 + 接口限流：nginx 配 TLS；登录 IP 维度 10 次/分、聊天用户维度限流；超限 429 并落 `fault_event` | `deploy/nginx` + `agt_backend` 过滤器 | S-2 | https 全功能正常；压测 20 次/分登录后 10 次收 429 | ⬜ |
| **S-6** | 最小告警出口：降级计数 / 熔断 / 任务连续失败 → webhook（企微/钉钉）；fire-and-forget；同故障 30 分钟去重 | `telemetry.py` + env | 无 | 人为制造 Milvus 不可用收到 1 条告警且 30 分钟内不重复；URL 置空零外发 | ⬜ |
| **S-7** | 间接注入防护：RAG 证据 / 工具返回进 prompt 前加统一边界标记；注入模式检测命中打标 + 落库 + 降权（不硬删） | `app/rag` + `agent_service.py` | 无 | 注入样本不改变答案行为且命中落库；既有 questions.jsonl 抽样无劣化 | ⬜ |
| **S-8** | 埋点 / 日志 PII 脱敏：写入前统一 `sanitize()`（手机号 / 身份证 / 邮箱） | `telemetry.py` | 无 | 三类 PII 单测；真机抽查 `agent_chat_log` 无明文 PII | 🟡 **部分完成**：**E-6 已收口密钥类**（key/token/Bearer，双侧正则 + 字面量两防线）与"**完整用户输入只记长度**"；⚠️ **手机号/身份证/邮箱三类 PII 的正则仍未实现**，S-8 **不可勾选** |

### Phase 3 — 工程资产与可度量

| 编号 | 任务 | 落点 | 依赖 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| **T-1** | **压测 + 评估集复跑**：单机 QPS、P95、SSE 首字延迟；`questions.jsonl` 全量评估复跑对齐 hit 基线 | `agt_python/scripts` | 服务在位 | ✅ **压测完成（2026-09-25）**：`scripts/loadtest.py` 六场景实测 3050 读请求 0 错误 / RPS 348~545 / 压后 10 组件全 up（部署手册 §5 第 17 条）。评估复跑由 demo.py RAG 步承担。**容量规划口径压测（真实流量画像定标）归 L4** |
| **T-2** | CI 流水线：pytest（对齐既有基线口径）+ Maven package + 前端 build | 仓库根 `.github/workflows` 或 Gitee CI | 无 | push 触发全绿一次 | ⬜ |
| **T-3** | 轨迹评分口径：`trace_detail` 增加「理想轨迹 vs 实际轨迹」比对评分（按序匹配 + precision / recall） | `telemetry.py` + 看板 | 人工标注若干理想轨迹 | 评分可计算、可展示、与人工判读一致 | ⬜ |
| **T-4** | 成本视图：按模型单价把 `prompt/completion tokens` 折算成成本，DWS 加日粒度聚合 + 看板一卡 | `telemetry.py` + `agt_web` | 无 | 看板显示日/周成本与分模型占比 | ⬜ |
| **T-5** | 澄清追问（轻量 HITL）：模糊/过短问题先反问再答，返回 `requires_input` | `app/services/intent/` | 无（复用短查询判据，禁第二套阈值） | 模糊问法返回追问；正常问法不受影响；有单测 | ⬜ |
| **T-6** | 工具统一重试语义：工具 manifest 加 `idempotent` 标记，幂等工具失败按策略重试并计数 | `tools/manifests` + `agent_loop.py` | 无 | 读工具可自动重试、写工具不重试；重试计数可观测 | ⬜ |

### Phase 4 — 能力扩展（P2 / P3，等条件触发）

| 编号 | 任务 | 触发条件 | 落点 | 状态 |
|---|---|---|---|---|
| **T-7** | MCP 外部工具接入 | 有外部工具需求（**须先做 T-6**，注册表语义先定稳） | 工具注册表加 provider | ⬜ |
| **T-8** | 整栈容器化（3 个 Dockerfile + compose） | 需要一键交付 / 换机部署 | `deploy/` | ⬜ |
| **T-9** | 空结果 vs 失败区分 | 工具上报需要细分 | `agent_tool_call` 加结果分类 | ⬜ |
| **T-10** | 程序记忆（Reflection 自改系统提示） | **评估集能证明有用** | 画像库旁增「指令记忆」 | ⬜ |
| **T-11** | 无人访问文档巡检（doc_usage 差集） | 命中埋点积累 ≥7 天 | `inspection_service.py` | ⏸ |
| **T-12** | M-3 相关性过滤阈值调参 | M-4 埋点攒够分布 + 全量评估复跑 | `history_filter.py` | ⏸ |
| **T-13** | 优先级排序（任务队列 + 动态重排） | 真实出现排队 / 资源争抢场景 | 新增调度层 | ⏸ |
| **T-14** | A2A（跨框架智能体互操作） | 出现跨系统对接的真实需求 | 新增协议层 | ⏸ |
| **T-15** | 历史向量持久化 / Skill 插件化 | 收益评估 / 工具规模翻倍 | `history_filter.py` / 路由层 | ⏸ |

### Phase 5 — 规模化生产（只划边界，当前不做）

SQLite → MySQL/PG（三库迁移 + 迁移脚本）· 进程内缓存与 scheduler 外置（Redis / 独立 worker）·
uvicorn 多 worker + 负载均衡 · Milvus 高可用 · 完整 RBAC 与操作留痕 · SLA 定义。

### 存量遗留（不新开阶段，随手上做）

| 编号 | 事项 | 来源 | 触发 / 说明 |
|---|---|---|---|
| L-1 | `Java 侧 4011`（登录非管理员）自动化覆盖 | 治理规约 §19 | 缺长期存在的非管理员账号 |
| L-2 | `QUERY_LIMIT=16384` → `query_iterator` | 治理规约 §19 | 切片数接近 1.5 万时（当前 2038） |
| L-3 | 工单批量处理 | 治理规约 §19 | OPEN 工单常态超 30~50 条（当前 7 条） |
| L-4 | TTL 对 `role=history` 快照单独设期限 | 治理规约 §19 | 产生第一个历史快照后（当前 0 个） |
| L-5 | `agent_feedback` 90 天 TTL | 数据库规约 §6 | 挂 scheduler，与四表 TTL 同批 |
| L-6 | `agent_chat_log` 无 session 索引 | 数据库规约 §3 | 视数据量待定 |
| L-7 | 登录身份对模型不可见（identity 未进 messages） | DEV_ISSUES §6.49 | 未实施，需排期 |
| L-8 | Milvus schema 四份复制 → 收敛为单点 | DEV_ISSUES §6.39 | 4 个脚本改为 import 单点定义 |
| L-9 | `scripts/` 下 8 个脚本未加数据隔离守卫 | DEV_ISSUES §6.37 | 防止误写产品库 |
| L-10 | 产品库 80 条测试污染数据清理 | DEV_ISSUES §6.64/6.66 | **等用户确认后清理** |
| L-11 | `test_agent_api.py` 4 项失败断言修复 | 历史基线 | 断言早期占位话术，业务已变 |
| L-12 | 裸 jar 启动被绑到 59065 端口 | DEV_ISSUES §6.26 | 需显式 `--server.port=8080`，低优先 |
| L-13 | `schema_version` 版本行重复累计 | DEV_ISSUES §6.67 | 后续加 UNIQUE 索引清理 |

---

## §6 快速开始

### 6.1 环境要求

| 组件 | 要求 | 备注 |
|---|---|---|
| Python | **3.11（conda env `python311`）** | 必须用该环境，隔离环境缺 `pydantic_settings` |
| Node.js | 16+ / 22+ | 前端 dev server 与构建 |
| JDK | 17 | Spring Boot 3.3.3 |
| Docker | Desktop（含 compose） | 跑 Milvus |
| 外部依赖 | `jsdstu-3` 无人机主控 | 由 IDEA 启动 `DroneApplication`，端口 8060 |

### 6.2 启动（推荐：一键脚本，E-2）

```powershell
# ⓪ 部署前自检（只读；退出码非 0 就别继续，start-all 也会据此拒绝启动）
python deploy\scripts\preflight.py

# ① 注册计划任务（只需一次；S4U 非交互主体 → 服务跑在 session 0，无窗口）
powershell -ExecutionPolicy Bypass -File deploy\scripts\install-tasks.ps1

# ② 一键启动（8060 → 8000 → 8080 → 9090，逐个等就绪）
powershell -ExecutionPolicy Bypass -File deploy\scripts\start-all.ps1

# ③ 状态 / 停止 / 重启
powershell -ExecutionPolicy Bypass -File deploy\scripts\status.ps1
powershell -ExecutionPolicy Bypass -File deploy\scripts\stop-all.ps1
powershell -ExecutionPolicy Bypass -File deploy\scripts\start-all.ps1 -Restart
```

**手动逐步**（调试用；完整步骤、故障对照表见 `docs/部署手册.md` §4.6 与 §7）：

```bash
# ⓪ 版本打戳（E-1）——改了 VERSION / 首次 clone 后执行一次
D:/install/python/anaconda/envs/python311/python.exe scripts/stamp_version.py

# ① Milvus（向量库）
cd deploy/milvus && docker compose -p milvus up -d   # etcd / minio 先于 standalone

# ② Python agent（8000）——改代码必须重启，非 reload
#    绑 127.0.0.1（S-1）：只服务本机 Java，外网与内网其它机器不可达
#    AGENT_SHARED_KEY 必须已配（空值会拒绝启动，见 6.4）
cd agt_python
D:/install/python/anaconda/envs/python311/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ③ Java 后端（8080）——打包前必须先停旧进程（Windows 文件锁）
#    用包装脚本启动：它负责注入 AGENT_SHARED_KEY（S-1）
#    产物名固定为 agt_backend.jar（E-2 统一，不带版本号）
cd deploy && cmd /c start-backend.cmd

# ④ 前端（9090）
cd agt_web && npm run serve

# ⑤ jsdstu-3（8060）——在 IDEA 中启动，本仓库不管理
```

> 生产/长期运行请用 **S4U 非交互计划任务**（`drone-agent-{端口}-{服务}`），即上面的 ① ② 两步：
> 交互式计划任务会为每个服务弹出可见 cmd 窗口，窗口被关即整棵进程树一起死。

### 6.3 验证

```powershell
# 一键看四服务状态（端口 / SessionId / 计划任务 / 探针；完整验证清单见 docs\部署手册.md §5）
powershell -ExecutionPolicy Bypass -File deploy\scripts\status.ps1
```

```bash
curl -s --noproxy '*' http://127.0.0.1:8000/v1/health              # Python 健康 + 版本
curl -s --noproxy '*' http://localhost:8080/v1/version             # Java 侧版本
curl -s --noproxy '*' http://localhost:8080/v1/ops/health          # E-3：整套系统健康（含组件明细）
netstat -ano | grep LISTENING | grep -E ":(8000|8080|9090|8060) "  # 四端口在位

# 版本一致性门禁（VERSION / CHANGELOG / 三端产物必须同版本）
D:/install/python/anaconda/envs/python311/python.exe scripts/stamp_version.py --check

# 单测（Python 419 项，预期 415 passed / 4 项既有基线 deselect）
cd agt_python && D:/install/python/anaconda/envs/python311/python.exe -m pytest tests/ -q

# 五作业门禁（Python 单测 + Java 单测 + 前端构建 + 权限矩阵 + 依赖漏洞扫描；本地实测 ≈73s，E-4 / E-10 / E-11）
# E-12 起 web 作业多一条产物存在性断言（前端三处收口是否真的进了 bundle）
# 也可只跑一个：bash scripts/ci.sh python java
# 改权限相关代码后先单跑矩阵（秒级）：bash scripts/ci.sh matrix
# 改了依赖后先单跑依赖作业（秒级，离线）：bash scripts/ci.sh deps
bash scripts/ci.sh

# 依赖漏洞与许可证（E-11）：Critical 阻断 / High 告警；退出码 0 通过、1 有 Critical、2 快照过期
D:/install/python/anaconda/envs/python311/python.exe scripts/scan_deps.py
D:/install/python/anaconda/envs/python311/python.exe scripts/scan_deps.py --refresh   # 联网刷新快照（security/*.json）
```
```

默认管理员账号：`admin / 123456`（前端 token 存 `localStorage` 的 `agt_token` / `agt_user`）。

### 6.4 核心配置（`agt_python/.env.example`）

| 分组 | 关键项 |
|---|---|
| 应用 | `HOST`（默认 `127.0.0.1`）/ `PORT` / `DATA_ROOT`（测试隔离用） |
| 大模型 | `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`；备用链 `LLM_BACKUP_*` / `LLM_COOLDOWN_SECONDS` |
| RAG | `RAG_JUDGE_ENABLED` / `RAG_REWRITE_ENABLED` / `RAG_REWRITE_TEMPERATURE=0.0` |
| 编排 | `AGENT_PLANNING_*` / `AGENT_SUBAGENT_PARALLELISM=4` / `AGENT_LOOP_SAME_CALL_MAX=3` / `AGENT_TOOL_TIMEOUT=15` |
| 治理 | `GOV_ENABLED` / `GOV_TTL_DAYS=30` / `GOV_RECONCILE_INTERVAL_SEC=300` / `GOV_INSPECTION_INTERVAL_SEC=3600` / `GOV_LOW_QUALITY_CHARS=50` |
| 记忆 | `MEMORY_TTL_DAYS=30` / `MEMORY_TTL_INTERVAL_SECONDS=86400` / `PROFILE_ENABLED` |
| 埋点 | `TELEMETRY_ENABLED` / `TELEMETRY_FLUSH_INTERVAL=5` |
| **服务间信任（S-1）** | `AGENT_SHARED_KEY`——**留空则拒绝启动**；Java 侧须同值（环境变量，本机经 `deploy/secrets/agent.key`） |
| 待新增（S-6） | `ALERT_WEBHOOK_URL`（空 = 关闭告警） |

> 生成服务间密钥：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。
> 两侧必须同值，否则 Java 每个请求都会拿到 `4010`。

### 6.5 数据面

| 存储 | 位置 | 内容 |
|---|---|---|
| Milvus | 集合 `kb_drone` | 语料向量（约 2037 片 / ~80 篇） |
| `session_memory.db` | `agt_python/data/` | 会话消息（30 天 TTL） |
| `user_profile.db` | 同上 | 用户画像（无 TTL，独立库） |
| `observability.db` | 同上 | 数据中台五类事件表 |
| `governance.db` | 同上 | 语料治理账本（docs/chunks/audit/tickets） |
| 语料目录 | `data/kb/`（raw / external / recycle） | 原文与回收站备份 |

---

## §7 文档索引

### 专项规约（`docs/specs/`）

| 文档 | 内容 | 状态 |
|---|---|---|
| `企业级落地规约.md` | **L3 企业级可交付**：七条判据（C1~C7）、E-1~E-15 分期任务、与 S/T 编号的映射 | **v1.18 已收官**（S-1 ✅、E-1~E-15 ✅、S-8 ✅、T-1 ✅ 全部完成，E-P1/E-P2/E-P3 收官，**C1~C7 全部落地**） |
| `验收清单.md` | **企业交付逐条验收项**（E-15）：§0 前置 + §1~§8 每条含命令与期望输出 + §9 结论登记表；含"快速路径"（起服务 + demo.py 两步覆盖核心链路） | **v1.0**（配合 `scripts/demo.py` 21/21 实测通过） |
| `生产级改进规约.md` | S-1~S-10 分期任务、接口契约、fail-closed 铁律（L2 内部生产） | v1.1（S-1 已落地） |
| `改进路线图.md` | 参照 AgentCraft / open-agent 的差距盘点（P1/P2/P3 + 不做清单） | v1.0 待评审 |
| `Agentic模式对照-drone现状与落地建议.md` | 21 个 Agentic 设计模式 × 现状对照（✅9/🟡6/⬜6） | v1.0 待评审 |
| `会话记忆规约.md` | M-1~M-5、G10~G15 验收项、阈值参数 | v1.1 已落地 |
| `数据中台开发规约.md` | ODS/DWS/ADS 分层、五类事件表、看板、演进至 v5 | v1.0 |
| `语料治理规约.md` | G1~G4 全生命周期治理、13 个子系统、311 项验收 | 已验收 |
| `业务操作全能力规约.md` | 工具补齐、双模式引擎、多智能体、41 端点映射 | v1.0 |
| `全链路容错规约.md` | 重试/降级/兜底/熔断四概念与分层 | v1.0 |
| `数据库设计规约.md` | 四库表结构、迁移约定、MySQL 映射 | v1.0 |
| `知识库回收站与分页.md` | 软删 + 回收站 + 分页契约 | 已实现 |
| `权限感知RAG规约.md` | 多部门/密级文档检索（**仅设计，未立项**） | v1.0 待评审 |

### 交付与版本（仓库根）

| 文件 | 内容 |
|---|---|
| `VERSION` | **版本号唯一来源**。改版本只改这里（同时补 CHANGELOG），其余由脚本派生 |
| `CHANGELOG.md` | 变更记录（Keep a Changelog），每个 `[x.y.z]` 必须与 `VERSION` 对应 |
| `docs/交付物清单.md` | 交付包内容、部署前置、验收步骤、回滚方式、已知限制 |
| `docs/部署手册.md` | **怎么装**：环境要求 → 密钥 → 构建 → Milvus → 自检 → 启停 → 验证清单 → 故障对照表 → **§9 CI 门禁**（E-2 / E-4） |
| `docs/运维手册.md` | **怎么保住数据**：四库清单 → 备份在哪/怎么跑/保留策略 → **恢复演练（四步断言）** → 真出事的分场景恢复步骤 → 演练频率 → 排障（E-7） |
| `docs/日志规范.md` | **怎么读日志**：字段契约（九字段逐字同名）+ `event` 命名法 + 双轨分工 + 轮转口径 + **traceId 三端链路** + 脱敏清单（E-6 密钥类 + **S-8 PII 三类**）+ **按 traceId 一次 grep 串三端**的用法（E-6） | **v1.1**（2026-09-25：S-8 收口，§7 补 PII 规则与顺序/环视契约） |
| `docs/审计清单.md` | **审计留痕全表**：哪些动作必留痕 + 动作码全表 + 七维契约（`actor/action/target/result/ip/traceId/at`）+ 五处写点 + 保留与权限 + **§10 已知边界**（E-8） |
| `docs/密钥与配置管理.md` | **密钥怎么管**：密钥清单 7 项（名称/用途/存放位置/轮换周期）+ 存放与保护 6 条 + **共享密钥双密钥轮换（顺序论证 / 逐步命令 / 三个坑）** + 其余密钥要点 + 泄漏处置五步（E-10） |
| `docs/权限矩阵.md` | **谁能调什么**：角色定义（按实测：admin / 非 admin 两档）+ 三层判定结构 + **52 行端点 × 权限矩阵（每行带 `文件:行` + 代码原文）** + Python 侧兜底 + 已知边界（**含"唯一匿名写入端点"为什么必须匿名**；E-10 / E-12 / E-13） |
| `docs/依赖清单.md` | **依赖与许可证**：三端闭包（Python 41 / Java 47 / npm 57）+ 直接依赖逐条列 license + 已知限制（企业采购会问 license；E-11） |
| `docs/依赖与漏洞处置.md` | **漏洞怎么处置**：阈值表（Critical 阻断 / High 告警）+ 退出码表 + 五步处置流程 + `tomcat 10.1.28→10.1.60` 实测案例 + 豁免台账格式 + 四个坑（E-11） |
| `deploy/scripts/` | 运维脚本：`preflight.py` 自检 / `_services.ps1` 服务清单 / `install-tasks.ps1` / `start-all.ps1` / `stop-all.ps1` / `status.ps1` / `trim-logs.ps1`（stdout 捕获日志轮转，被占用跳过、绝不影响启动，E-6） |
| `scripts/ci.sh` | **本地 CI 门禁**：Python 单测 + Java 单测 + 前端构建 + **权限矩阵** + **依赖漏洞扫描**五作业（≈73s），退出码可挂 pre-push（E-4 / E-10 / E-11） |
| `scripts/scan_deps.py` | **依赖与漏洞扫描**：三端闭包 + OSV 单接口查漏洞 + CVSS 分级 + 许可证；**零第三方依赖**、离线快照可跑，Critical 阻断（E-11） |
| `security/*.json` | 安全基线快照：`sbom.json` 闭包 + `vuln-snapshot.json` 漏洞 + `licenses.json` 许可证 + `deps-allowlist.json` 豁免台账（台账自身有校验；E-11） |
| `scripts/check_permission_matrix.py` | **权限矩阵门禁**：校验 `docs/权限矩阵.md` 与代码一致（端点覆盖 / 证据真实 / 约束语义 / 反向完整 / 匿名行结构五项），退出码可挂 CI（E-10） |
| `agt_web/src/utils/clientError.js` | **前端报错上报器**（E-12）：三处收口 + 采样/忽略规则/单页限流 + `sendBeacon` 卸载兜底；**零第三方依赖**、纯 `fetch`（刻意不复用 axios 实例，理由见代码注释）；`window.__agtClientErrorSelfTest()` 供验收自检 |
| `agt_python/app/services/costing.py` | **成本计量与配额**（E-13）：`LLM_PRICE_JSON` 解析（配错收敛不拒启）+ 成本折算单点 `model_cost`（未知模型记 0、单列未配价）+ 超限提示文案；所有成本数字都从这一处出 |
| `agt_web/src/views/metrics/MetricsCostsView.vue` | **成本与配额看板页**（E-13）：KPI 四卡 + 按模型 BarList + 按用户/按部门表 + 日趋势 + 口径页脚（成本查询期折算 / 部门=画像声明键 / 估算行如实标注） |
| `agt_python/Dockerfile` `agt_backend/Dockerfile` `agt_web/Dockerfile` | **三端镜像**（E-14）：构建上下文统一仓库根（Python 才能 `COPY VERSION`）；Java/前端两阶段；密钥/数据不进镜像 |
| `deploy/docker-compose.app.yml` | **容器编排**（E-14）：healthcheck 驱动启动顺序、external 接入 `milvus-net`、agt-python 无宿主端口（S-1 容器等价物）、命名卷 `agt-data` |
| `deploy/nginx/nginx.conf` | **浏览器唯一入口的反代**（E-14，与 S-2 同一份）：`/v1`→8080 带 **SSE 三件套**、SPA 回退、静态长缓存 |
| `scripts/backup_dbs.py` | **数据备份**：四库一致快照 + `integrity_check` + 清单落 `data/backups/`（E-7） |
| `scripts/restore_drill.py` | **恢复演练**：恢复最新备份到临时目录 → 校验/逐表行数比对/应用真读 → 出报告（E-7） |
| `agt_python/pytest.ini` | 测试口径单一来源（testpaths / 4 项既有基线的 `--deselect` / 标记），本地与 CI 共用 |
| `scripts/stamp_version.py` | 版本打戳：`--check` 门禁校验 / `--print` 查看 / 无参则写产物 |
| `scripts/release.sh` | 发布编排：打戳 → 门禁 → 构建 → 归档+SHA256 → 打 tag（`--dry-run` 演练） |

> 版本信息的三种暴露方式：`GET :8000/v1/health`（agent 端）、
> `GET :8080/v1/version`（Java 端，含 Java 运行时）、
> `GET :9090/version.json`（前端，页面「关于」页底部展示）。

### 设计与学习文档（`docs/`）

`Agentic智能体设计模式-精读笔记.md`（全书 21 章 + 附录 A~G）·
`Agentic设计模式-三章深读.md`（第 8/19/20 章）·
`AgentCraft可借鉴优势清单.md` · `RAG方案对比与选型建议.md` ·
`切片方案与元数据设计.md` · `检索参数标定与rerank模型选型.md` ·
`链路设计-前置分类风险与自适应改造.md` · `链路设计-检索过滤与证据充分性评审.md` ·
`知识库数据准备方案.md`

### 开发台账

`DEV_ISSUES.md`——83 章（§6.1~§6.83），逐条记录问题、根因、修法、验证与遗留。
**排查任何历史问题时先查这份文件**，多数"疑似 bug"已在其中销案。

---

## §8 工程铁律

> 这些是踩坑后确立的，改动时不要绕过。

1. **判定以代码为准**：凭印象下结论会把已完成的工作重复开发（先 grep，再下判断）。
2. **服务间信任单向**：身份只由 Java 注入；`ChatRequest.userId` 不得被信任（S-1）；确认词只认**用户字面消息**（防间接注入授权）。
3. **fail-closed 只用于鉴权，fail-open 只用于增强**：密钥校验失败必须拒绝；告警 / 检测 / 脱敏异常只降级计数，绝不阻断主链路。
4. **埋点非阻塞、绝不抛异常**：内存队列 + 5s 批量落库；**内容不落库**（只记长度 / 摘要）。
5. **降级必须计数**：每个降级 / 熔断 / 过滤路径一个计数点 + WARN 留痕。
6. **数据库只加列不重建表**：加列幂等 ALTER，`schema_version` 登记；失败 fail-fast 拒启。
7. **判定逻辑只许有一份**：多写一份必然烂（同步 / 流式净化、历史过滤分组都复用同一实现）。
8. **"操作未执行"绝不用成功码返回**；断言"零 / 空 / 未调用"一律不用 `or`（`int(x or -1)` 会误判合法 0）。
9. **以后置条件为准**：以结果状态（文件是否真删 / 库是否真写）判定，不看调用是否报错。
10. **新增能力需双门槛**：① 真实需求出现；② 评估集能证明变好。缺一不动。
11. **8060（jsdstu-3）只包装不改**；清理 / 删除类动作先列清单请人确认。
12. **git 提交等明确指令**：开发过程只改工作区文件。

---

## §9 明确不做的事（已论证，留痕防返工）

| 事项 | 理由 |
|---|---|
| 用户管理 / 公告管理 | 用户体系在 jsdstu-3，自建即双写用户 |
| Redis 多级缓存 | 单实例部署收益为零，可靠性反而更差 |
| 问答日志页 / 执行记录页 | 会话明细 + trace 追溯已覆盖且更强 |
| ragas 类重评估 | 已有 `questions.jsonl` 静态评估集 + 知识缺口闭环 |
| 反思重写循环（Agentic 第 4 章） | 单轮评审 + 拒绝答已够；重写使成本延迟翻倍且收益未验证 |
| 学习与适应 / DPO（第 9 章） | 无训练数据规模、无算力、无业务诉求 |
| 探索与发现（第 21 章） | 科研辅助场景，与无人机运维无关 |
| GraphRAG | 语料为结构化机型/故障文档，跨文档综合需求未出现 |
| ToT / CoD / RLVR | 成本高、场景不明 |
| 框架迁移（LangChain/ADK 等） | 自研栈已在线上，迁移是负收益 |
| 数据中台迁 MySQL | 用户 2026-09-18 明确本期不做 |
| 权限感知 RAG 落地 | 需真实多部门文档需求出现才立项 |

---

## §10 本机开发环境注意事项

> 这些坑会伪装成"产品缺陷"，排查前先看一眼。

- **工作目录不跨调用保持**：命令里显式带绝对路径；跑测试用「绝对路径 + PYTHONPATH」。
- **后台进程不跨工具调用存活**：长驻服务需用计划任务 / 独立终端，`nohup &` 会被回收。
- **沙箱快照视图**：跨调用比对文件列表会"删了又出现"，判定文件事实须在同一调用内完成，或走接口 / 账本口径。
- **安全删除守卫**：每回合 50 次删除上限、全进程共享，超限抛 `SystemExit`（非 `OSError`）；构建类命令需 `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR=`。
- **`curl` 走系统代理**：一律加 `--noproxy '*'`。
- **Maven**：`./mvnw` 不可用，需用 IDEA 内置 Maven 手工启动 classworlds（含 `-Dmaven.multiModuleProjectDirectory`）。
- **Windows 文件锁**：重新打包 jar 前必须先停 Java 进程。
- **同一文件禁止并发编辑**：一条消息里两次编辑同一文件，后一次会覆盖前一次且回执仍显示成功；改完用 grep / 编译 / 断言核验。
- **uvicorn 非 reload**：改代码必须重启 8000；三层任一跑旧代码都会"字段被吞"。
- **`taskkill`**：Git Bash 下 `/PID` 会被路径转换吃掉，用 PowerShell `Stop-Process`。

---

## §11 下一步

当前在推 **L3 企业级可交付**（`docs/specs/企业级落地规约.md`）。**E-P0 可交付最小集已收官**：
**S-1** 服务间信任边界 → **E-1** 版本与交付物基线 → **E-2** 一键启停 + preflight + 部署手册
→ **E-3** 健康聚合入口（顶栏三态状态灯）→ **E-4** CI 门禁 + **E-5** Java 测试 0→1（71 项全绿）。
**E-P1 进行中**：
**E-7 数据备份 + 恢复演练** ✅（判据 C4 —— 四库从"零备份"变为"每天备份 + 演练过的可恢复"，见 `docs/运维手册.md`）；
**E-6 日志规范与轮转** ✅（判据 C3 —— 四服务从"裸文本无轮转"变为"JSON lines 九字段 + 双侧 50MB×10 轮转 +
**traceId 三端贯通**（一次 grep 串三端同一请求）+ token 脱敏"，见 `docs/日志规范.md`）；
**E-9 轻量指标端点** ✅（判据 C3 —— 补上"机器可抓"的口径：`GET /v1/ops/metrics` 输出 Prometheus 文本格式，
**零新依赖手写**，8080 双段合并 Java + Python，Python 不可达时降级不冒 500，见 `docs/部署手册.md` §7.8）；
**E-8 操作审计留痕** ✅（判据 C5 —— 从"只有治理动作有审计"变为"**一张表回答谁在这台机器上做了什么**"：
七个统一维度（新增 `target`/`ip`/`traceId`，只加列不重建）、覆盖面扩到账号事件/确认门/备份/配置变更、
`docs/审计清单.md` 给出动作码全表与已知边界，见 `docs/部署手册.md` §7.9）。
**E-P1 四项全部收官（2026-09-24）。**

**E-P2 已开工**：
**E-10 密钥轮换 + 权限矩阵** ✅（判据 C5 —— ① **双密钥过渡窗口**：轮换期 `AGENT_SHARED_KEY` 与
`AGENT_SHARED_KEY_PREV` 两把都收，把"无缝轮换"从**调度问题**变成**配置问题**；旧密钥放行**只计数不落
fault_event**（请求是成功的），计数 `auth_key_legacy_used` 作为"能不能清掉 PREV"的机器判据；
② **权限矩阵 49 行**每行带 `文件:行` 与代码原文，并做成 `scripts/check_permission_matrix.py`
**五项自动校验**（端点覆盖 / 证据真实 / 约束语义 / 反向完整 / 匿名行结构）；
③ **角色按实测纠正**为 `admin / 非 admin` 两档——规约原文的 `admin/operator/viewer` 在本系统无对应物。
见 `docs/密钥与配置管理.md`、`docs/权限矩阵.md`）；
**E-11 依赖与漏洞扫描** ✅（判据 C1 —— 从"交付包里可能带已知 Critical 漏洞"变为"**Critical 阻断发布**"：
`scripts/scan_deps.py` **零第三方依赖**，一套 **OSV** 接口覆盖 PyPI / npm / Maven 三端闭包（Python 41 / Java 47 / npm 57）；
**Critical 阻断 / High 告警**（阈值按用户裁定，非规约原文的"高危都阻断"）；豁免台账**自身有校验**（缺字段 / 过期 / 日期格式错 → 阻断）；
CI 加第五作业 `deps`、`release.sh` 加 3.5 步发布前阻断。**上线即抓到并处置 7 条真实 Critical**
（`tomcat-embed-core 10.1.28` → 显式覆盖 `<tomcat.version>` 到 `10.1.60`，Critical 7→0、High 25→11）。
见 `docs/依赖清单.md`、`docs/依赖与漏洞处置.md`）；
**E-12 前端运行时报错上报** ✅（判据 C3 —— 把"前端炸了"从**三端都没有记录**变成
"**看板上一页三问可答**：哪一页最容易炸 / 是不是这一版引入的 / 具体报了什么、什么时候、谁碰到的"）：

* **三处收口**：`app.config.errorHandler`（Vue 3 的等价物；规约原文写的 `Vue.config.errorHandler` 是 Vue 2 API）
  \+ `window.onerror` + `unhandledrejection`。装上位置在 `mount()` **之前**——装在 mounted 之后会漏掉
  首屏渲染阶段的异常，而那恰好是"用户看到一片空白、连错误提示都没有"的那一段。
  前端侧 `src/utils/clientError.js`（**零第三方依赖**，纯 `fetch`）。
* **刻意不复用 `src/api/request.js` 的 axios 实例**，两个具体理由（写在代码注释里）：
  ① 它的响应拦截器把非 2000 一律 reject，网络失败会造出**新的 unhandledrejection** → 被本模块再上报 → **自我放大的循环**；
  ② 它对 `4010` 调 `clearAuth()` —— 等于"会话过期期间每次前端报错都把用户登出"。用一个观测能力改用户登录态，不可接受。
* **身份是结构性约束、不是约定**：上报体模型里**没有 `userId` 字段**，身份只能由 Java 从登录态注入
  `user_id` 查询参数。真机验收里浏览器上报的 `userId=1`（admin）由 Java 注入、body 里塞的 `999` 无效。
* **风控两层**：前端"指纹 + 单页上限 30"（防**网络**风暴：一个渲染循环能在 3 秒内触发几千次 handler）；
  服务端"`(指纹, 1 分钟桶)` 合并 + `count` 累加"（防**表**爆炸）。
  由此 `total` 是真实抛错次数的**下界**——这条口径写进看板页脚与代码注释，**别当 bug 去修**。
* **永远返回 2000**：上报失败绝不影响用户（失败只在 `data.stored/reason` 里如实说）。
  空请求体也收（页面卸载中 `sendBeacon` 只带 Content-Type 不保证带体；`fetch` 会被 `keepalive` 截断）——
  首轮实机验收正是被这条挡住的：空体吃 422 → `3001`，**恰恰是最难复现的那类错误永远收不到**。
* **看板**：新增「数据中台 · 观测 → 🐞 前端错误」页（KPI + 按页面 + 按版本 + 最新报错表 + 口径页脚），
  并在「总览」加一张前端错误卡（**独立请求、独立失败**：浏览器真炸的时候恰好看不到总览，最需要看的时候看不到）。
* **测试基线**：Python **368 项**（+30）、Java **164 项**（+13）、权限矩阵 **51 行**；
  CI 的 `web` 作业加了一条**产物存在性断言**（前端没有单测框架，**刻意不为它引 jest**——
  门禁工具自身跑不起来比没有门禁更糟），行为正确性由**实机浏览器验收**覆盖：
  控制台 `__agtClientErrorSelfTest()`（三条收口各发包一次）+ 主动抛错 → 看板可见。
* 实机验收抓到并修掉一个**静默丢字段** bug：pydantic v2 的 `model_dump()` 默认输出**字段名**
  （`app_version`）而非**别名**（`appVersion`），服务层按 camelCase 取键 → 前端版本号与 UA **一直存成空串**
  （接口照样 2000、库里照样有行，只有"按版本聚合"永远是「(未知版本)」）。见 `DEV_ISSUES.md`。

**E-13 成本计量与配额** ✅（判据 C7 —— 把"这个月大模型花了多少钱、哪个部门用的"从**答不出**变成
"**一个端点 + 一页看板答三问**：花了多少钱 / 谁在花 / 用量有没有失控"）：

* **计量原料本就在库**（`agent_chat_log.prompt/completion_tokens/model/user_id`），本轮补齐三件事：
  ① **流式 usage 补洞**——E-13 之前流式（主用链路）token 恒记 0（`done.usage={}`），现带
  `stream_options.include_usage` 取**真实用量**（真机实测 `source:"api"`）；上游不支持则
  `TokenEstimator` 本地估算，并落 **`usage_source` 列**（v8 迁移）如实标注"量的还是估的"——
  **估算值可看趋势、不可拿去对账**。
* **成本 = 查询期折算**（`LLM_PRICE_JSON`，**元/百万 token**，与供应商定价页同口径），
  **不在 DWS 冻结**——改价后历史按新价重算，回答的是"按现在的价格值多少钱"；
  未配价模型 token 照记、成本记 0 并单列 `unpricedTokens`（**缺价 ≠ 免费照标**）。
* **配额**：`QUOTA_USER_DAILY_TOKENS`（单用户日 token 上限，0=不限），超限**降级为提示**
  （规约原文）而非拒绝——SSE 发独立 `quota` 事件 / 非流式响应挂 `quota` 字段（前端黄条），
  并落 `quota_exceeded` fault（**每用户每日至多一条**，防刷）。真机验收：`cap=1` 时真实对话
  照常返回答案 + quota 事件文案 `34,414 / 1 tokens` + fault_event 留痕。
* **口径三条**（与 `costing.py` / `telemetry.costs_summary` / 看板页脚四处一致，改一处同步另一处）：
  部门 = 用户画像里**声明过的** `department` 键（读取期派生，本系统**无组织架构**）；
  成本不在 DWS 冻结；估算行单列计数。看板新增「数据中台 · 观测 → 💰 成本与配额」页
  （KPI + 按模型 BarList + 按用户/按部门表 + 日趋势）+ 总览 KPI 卡（独立请求、独立失败）。
* **顺手修掉一个存量 bug**：`schema_version` 表**没有 UNIQUE 约束**、`INSERT OR IGNORE` 从来不
  IGNORE——每次服务启动都把 v1..vN 重插一遍（存量库已堆数百行重复）。改为逐版本判存 + 启动清重。
* **测试基线**：Python **389 项**（+22，`tests/test_cost_quota.py`）、Java **167 项**（+3，
  AgentChatVO quota 字段 Jackson 反序列化回归——守 `AgentHealthVO` 踩过的同一个静默丢字段坑）、
  权限矩阵 **52 行**。

**E-14 容器化交付** ✅（判据 C1 —— 双轨交付的第二条轨道从"规划中"变为**实测可跑**）：

* **6 个交付物**：三个 Dockerfile（Python 直装 / Java 两阶段 maven→jre / 前端两阶段 node→nginx）、
  `deploy/docker-compose.app.yml`、`deploy/nginx/nginx.conf`、`.dockerignore` + Maven 镜像源。
  构建上下文统一为**仓库根**——Python 侧 `COPY VERSION`，容器里 `/v1/health` 照样答得出版本号（E-1）。
* **与 Milvus 不合并**：既有 `deploy/milvus` 栈保持独立，应用 compose 以 **external 网络
  `milvus-net`** 接入，`MILVUS_URI=http://milvus-standalone:19530`。
* **端口策略 = S-1 的容器等价物**：`agt-web 9090:80`（浏览器唯一入口，`/v1` 反代 8080）、
  `agt-backend 8080:8080`（运维直连）、**agt-python 无宿主端口**——宿主机与浏览器都摸不到 8000，
  只有同网络带 `X-Agent-Key` 的 agt-backend 能访问。8060 主控在无人机主控机上，经
  `host.docker.internal` 回宿主机网络，不在场时走既有降级路径。
* **SSE 三件套进 nginx**（`proxy_buffering off` + `read_timeout 300s` + HTTP/1.1）——实测经 nginx
  逐字 **19 个 delta** + `done.usage` 真实 API 用量；漏配的表现是"卡半天一次性蹦全段 + 504"。
* **密钥/数据不进镜像**：`.env` 经 compose `env_file` 注入（镜像里没有密钥）；数据落命名卷
  `agt-data`，**与形态一互不相通**，实测 `down/up` 后探针行存活。
* **据实记档一处偏离**：规约原文"`/api` 反代 8080"——前端既有约定是同源 `/v1/*`，改 `/api`
  要动全部前端请求路径，不值；意图（浏览器摸不到 8000）不变（`DEV_ISSUES.md` §6.86）。
* **国内网络四坑沉淀**（部署手册 §8.4）：Docker Hub 拉镜像卡死 → daocloud 预拉重打标签；
  清华 pypi 的 `/packages/` CDN 对容器 403 → 阿里云源；Maven settings.xml **注释里不能有 `#`
  也不能有 `--`**（XML 两连坑，实测各挂一次）；npm 走 npmmirror。

**E-15 验收清单与演示脚本** ✅（判据 C1 收口，**L3 全案收官**）：

* **`docs/验收清单.md`**：企业交付时的逐条验收项，§0 前置 + §1 服务探活 / §2 鉴权安全 /
  §3 版本发布 / §4 CI 门禁 / §5 核心链路 / §6 可观测 / §7 备份恢复 / §8 容器化 + §9 结论登记表；
  每条含**命令与期望输出**，陌生人照跑即可得出"通过/不通过"，无歧义项；开头给"快速路径"
  （§1 起服务 + §5 demo 两步覆盖核心链路），赶时间验收可用。
* **`scripts/demo.py`**：一键核心链路演示/回归**双用**——登录 → RAG 问答 → 业务工具 →
  确认门 → 看板数据，六步 **21 项硬断言**；打真实三层（8080→8000→8060）、与产品代码零耦合
  （不 import 项目代码，交付包里单文件即可跑）。RAG 问句取评测集 eval-002（确定性答案
  UOM 平台）；确认门覆盖**拦截（数据零改动）/ 确认执行（8060 REST 直查落库）/ 取消 /
  收尾自清理**四条路径，可反复重跑。
* **实测**：`demo.py` **21/21 通过、退出码 0**。首跑抓到 RAG `degraded`——根因是 Milvus
  依赖容器未起，`/v1/ops/health` 组件级当场定位，按 etcd/minio→standalone 顺序恢复后全绿
  ——**验收现场本身就是 E-3 可观测性的一次成功演示**。

按规约 §6 的顺序，下一步：

1. ~~E-15 验收清单与演示脚本~~ ✅（2026-09-25 完成，**L3 企业级可交付全案收官**）；
2. ~~S-8 PII 脱敏~~ ✅（2026-09-25 补收口，见下）；
3. **待 push 后补**：远端 Gitee Go 流水线首次全绿 + 分支保护配置（部署手册 §9.3 有步骤）；
4. **遗留待办**：清理/决策一个早先遗留的 `agt_python_8000` 计划任务（绑 `0.0.0.0`、Interactive 主体，
   与 S-1 回环口径冲突，已临时 Disable，成因未完全查清）；`deepseek-flash` 历史模型名**未配价**
   （token 照记、成本记 0，拿到实际单价往 `LLM_PRICE_JSON` 补一行即可）；
   T-1 压测 + 评估复跑（规约归入 E-15/后续，属 C1 支撑材料，建议作为 L3 后的第一个增强项）。

**S-8 PII 脱敏** ✅（2026-09-25 补收口，L3 收官后的遗留清账）：

* **三类 PII（手机号/身份证/邮箱）在与 E-6 同一个出口叠加实现**：规则单点
  `agt_python/app/core/pii.py`（`redact_pii()`，开关 `PII_REDACT_ENABLED` 默认开），
  Java `LogRedactor` 同规则镜像。覆盖四类出口：双格式日志、`client_error` message/stack、
  埋点文本摘要（`_digest` **先脱敏再截断**）、看板原句展示侧。
* **三条正则契约**（单测钉死）：邮箱最先（`13812345678@139.com` 先被手机号规则吃掉就救不回来）；
  数字类用 **hex 感知环视**——真机存量扫描实证 16 位 hex traceId 内嵌 11 位十进制段会被
  纯数字环视误伤；身份证不做校验位验证（宁误脱 18 位订单号，不放过真身份证）。
* **边界**：`feedback_content` 表原文是规约 §4.2 口子的内容表（开关 + TTL 治理）——
  库内存原文、出口处一律脱敏。
* **实测**：Python **415** / Java **174** 全绿（新增 25+7 双向用例）、验红一次
  （13 项 PII 断言全红、反例全绿后还原）、真机上报落库核验 `PII_MASKED=True`、
  存量库新口径复扫 **0 命中**。

> 本文档是活文档：每完成一项任务，同步更新 §3 指标与 §5 状态列，
> 并在 `DEV_ISSUES.md` 追加对应章节留痕。

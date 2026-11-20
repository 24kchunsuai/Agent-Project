# Changelog

本项目的所有重要变更都记录在此文件。

格式遵循 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

> **版本号唯一来源是仓库根 `VERSION` 文件。**
> 本文件每个 `## [x.y.z]` 标题必须与 `VERSION` 中已出现的版本对应；
> 新增版本时先改 `VERSION`，再在本文件顶部补条目（顺序不能反，否则
> `scripts/stamp_version.py --check` 会报不一致）。
>
> 变更分类：`Added` 新增 ｜ `Changed` 变更 ｜ `Deprecated` 弃用 ｜
> `Removed` 移除 ｜ `Fixed` 修复 ｜ `Security` 安全。

## [Unreleased]

企业级落地任务（E-2 起）的在研改动，每完成一项即在此追加。

### Added

**T-1 轻量压测**（C1 支撑材料，2026-09-25 补收口）
- 新增 `scripts/loadtest.py`（与 demo.py 同款零耦合，零新依赖）：六只读场景
  （8080 登录/健康/版本/指标看板 + 8060 无人机看板 + 9090 静态）并发压测 +
  对话链路小流量单列（外部 LLM 主导不与本地读混算），输出 RPS / p50/p95/p99 /
  错误率并**压后自动复核 `ops/health`**
- 实测 PASS：3050 读请求 0 错误、RPS 348~545、p95 ≤135ms、压后 10 组件全 up；
  登录 p50 ≈1.4s（密码哈希成本）、对话 p50 ≈5.7s（外部 LLM）属预期
- 判定线：错误率 < 1% 且压后服务健康；口径为「并发不塌方」验证，非容量规划（归 L4）

**S-8 PII 脱敏**（与 E-6 同源单点，2026-09-25 补收口）
- 新增 `agt_python/app/core/pii.py`（规则单点，`PII_REDACT_ENABLED` 默认开）：
  手机号 / 身份证 / 邮箱三类 PII 出口统一替换 `***`；Java `LogRedactor` 同规则镜像
- 接线四处：`logging_setup.redact()`（双格式日志 + `client_error` message/stack）、
  `telemetry._digest()`（埋点摘要**先脱敏再截断**）、缺口视图原句展示侧
- 三条契约（两侧单测钉死）：邮箱最先（防 `13812345678@139.com` 吃剩 `***@139.com` 残渣）、
  数字类 **hex 感知环视**（真机存量扫描实证 hex traceId 内嵌 11 位十进制段会被
  纯数字环视误伤）、身份证不做校验位验证（宁误脱 18 位订单号，不放过真身份证）
- 边界：`feedback_content` 原文属内容表（开关 + TTL 治理），库内存原文、出口脱敏
- **实测**：Python **415** / Java **174** 全绿（新增 25+7 双向用例）；验红一次
  （13 项 PII 断言全红、反例全绿后还原）；真机上报落库核验 `PII_MASKED=True`；
  存量库新口径复扫 **0 命中**

**E-15 验收清单与演示脚本**（判据 C1 可交付收口，**L3 全案收官**）
- `docs/验收清单.md`：企业交付逐条验收项（§0 前置 + §1 服务探活 / §2 鉴权安全 / §3 版本发布 /
  §4 CI 门禁 / §5 核心链路 / §6 可观测 / §7 备份恢复 / §8 容器化 + §9 结论登记表），
  每条含**命令与期望输出**，全可勾选、无歧义项；开头给"快速路径"（起服务 + demo 两步覆盖核心链路）
- `scripts/demo.py`：一键核心链路演示/回归**双用**——登录 → RAG 问答 → 业务工具 → 确认门 →
  看板数据，六步 **21 项硬断言**；打真实三层（8080→8000→8060）、**不 import 项目代码**
  （交付包单文件可跑）；RAG 问句取评测集 eval-002（确定性答案 UOM）；确认门覆盖
  拦截（数据零改动）/ 确认执行（8060 REST 直查落库）/ 取消 / 收尾自清理，可反复重跑
- **实测**：`demo.py` **21/21 通过、退出码 0**；首跑 RAG `degraded` 当场抓出 Milvus 依赖容器未起
  （`/v1/ops/health` 组件级定位，按 etcd/minio→standalone 顺序恢复后全绿）

**E-14 容器化交付**（判据 C1 可交付，双轨的第二条轨道）
- 三个 Dockerfile：`agt_python`（python:3.11-slim，依赖层缓存 + 容器内 0.0.0.0 + python 一行式健康检查）、
  `agt_backend`（两阶段 maven→jre-17，`--agent.base-url` 命令行覆盖而非依赖 Spring 环境变量松散绑定）、
  `agt_web`（两阶段 node→nginx:1.27-alpine，生产构建进镜像）
- `deploy/docker-compose.app.yml`：healthcheck 驱动启动顺序（python 健康 → backend 起 → backend 健康 → web 起）、
  **external 网络 `milvus-net`** 接入既有 Milvus 栈（不复制编排）、命名卷 `agt-data`（`DATA_ROOT=/app/data`，
  实测跨 `down/up` 数据存活）、**agt-python 无宿主端口**（S-1"8000 只服务本机"的容器等价物）
- `deploy/nginx/nginx.conf`：浏览器唯一入口——`/v1` → 8080 带 **SSE 三件套**
  （`proxy_buffering off` + `read_timeout 300s` + HTTP/1.1 长连接）、SPA history 回退、
  指纹产物长缓存 / index.html 不缓存；**S-2 与 E-14 同一份配置**
- `.dockerignore` + `deploy/docker/maven-settings.xml`：上下文瘦身（密钥/数据/缓存不进镜像）+
  Maven 阿里云镜像源（国内网络适配，另有 pip 阿里云源、npm npmmirror、基础镜像经 daocloud 预拉，
  四坑沉淀在部署手册 §8.4）
- `docs/部署手册.md` §8 由"规划中"改写为完整实测内容（§8.1 交付物 / §8.2 拓扑与启动顺序 /
  §8.3 七条实测验收 / §8.4 国内网络坑）

**E-13 成本计量与配额**（判据 C7 可计量）
- `agt_python/app/services/costing.py`：`LLM_PRICE_JSON` 解析（元/百万 token，**配错收敛不拒启**：
  非法 JSON/行结构丢行、负价收敛 0）+ 成本折算单点 `model_cost`（未知模型记 0，调用方单列未配价）
  + 超限提示文案；**所有成本数字都从这一处出**
- `observability.db` v8 迁移：`agent_chat_log.usage_source` 列——token 数是**量的还是估的**的
  如实标注（`api` / `estimate`；估算值可看趋势、不可拿去对账）
- **流式 usage 补洞**：`llm_client.chat_stream` 带 `stream_options.include_usage` 取真实用量
  （usage chunk 的 `choices` 为空，必须先取 usage 再判 choices）；上游 400 自动降级重试并交
  上层 `TokenEstimator` 估算。E-13 之前流式（主用链路）token **恒记 0**，成本看板会系统性漏记
- **DWS 扩展**：`recompute_daily` 增 `tokens_prompt` / `tokens_completion`（按 model）；
  成本**不在 DWS 冻结**——查询期按当前价目折算，改价后历史按新价重算
- `GET :8000/v1/agent/metrics/costs`（需密钥）+ `GET :8080`（匿名，与其他 metrics 读端点同锁）：
  窗口总量 / byModel / byUser / **byDept**（画像声明的 `department` 键，读取期派生）/ 日趋势 + 配额状态
- **配额**：`QUOTA_USER_DAILY_TOKENS`（单用户日 token 上限，0=不限），超限**降级为提示**而非拒绝——
  SSE 独立 `quota` 事件 / 非流式响应 `quota` 字段（`AgentChatVO` 同步声明，防 Jackson 静默丢字段）/
  前端黄条；`quota_exceeded` fault **每用户每日至多一条**（detail 以 `用户 <id> |` 前缀做去重键）
- 前端：`MetricsCostsView.vue`「数据中台 · 观测 → 💰 成本与配额」页（KPI 四卡 + 按模型 +
  按用户/按部门 + 日趋势 + 口径页脚）+ 总览 KPI 卡（独立请求、独立失败）+ 聊天页超限黄条
- 配置：`.env.example` 增 `LLM_PRICE_JSON` / `QUOTA_USER_DAILY_TOKENS`
- 单测：Python `tests/test_cost_quota.py` **22 项**（价表收敛 / 手工核算一致性 / 队列也算数 /
  fault 去重 / DWS 拆分 / 端点鉴权）+ Java 3 项（quota 字段反序列化回归）

**E-2 一键启停 + preflight + 部署手册**（判据 C1 可交付）
- `deploy/scripts/preflight.py`：部署前自检 22 项（工具链 / 配置与密钥 / 构建产物 / 端口 / 外部依赖），
  退出码非 0 即拒绝启动；`--json` 供 CI 与看板消费
- `deploy/scripts/_services.ps1`：四服务的**单一真相来源**（端口、启动命令、探针、日志路径集中一处）
- `deploy/scripts/install-tasks.ps1`：幂等注册四个 S4U 非交互计划任务（`-Uninstall` / `-StartAtLogon`）
- `deploy/scripts/start-all.ps1`：自检 → 按依赖顺序启动 → **逐个等就绪** → 汇总（幂等，`-Restart`）
- `deploy/scripts/stop-all.ps1`：倒序停止 + 清理残留监听进程 + 复查端口
- `deploy/scripts/status.ps1`：端口 / **SessionId** / 任务状态 / 探针 / 日志时间（`-Json` 供监控）
- `docs/部署手册.md`：环境要求 → 密钥 → 构建 → Milvus → 自检 → 启停 → 验证清单 → 故障对照表（§1~§10）

**E-3 运维健康聚合**（判据 C3 可观测）
- `GET :8000/v1/ops/health`：聚合 Python 自身 / Milvus（**真实连接**，非 ping）/ SQLite 四库
  / scheduler 各任务 `last_run`·`last_error` / 降级计数器 / 8060 可达性；
  单项失败只降级**不外抛**；3s TTL 缓存（`?fresh=true` 绕过）
- `GET :8080/v1/ops/health`：聚合顶层 = Java 自身 + 转发 8000 组件，**一个 URL 回答整套系统**
- 前端顶栏状态灯改**三态**（绿=正常 / 黄=降级 / 红=异常）+ 悬停显示不健康组件明细 + 30s 轮询
- 文档：部署手册新增 §7.6「状态灯怎么读」（含两个最易读错的口径）
- 单测 13 项（全部打桩，不连真实 Milvus/SQLite/8060，断网可跑）

**E-4 CI 门禁**（判据 C2 可复现 / C6 可回归）
- `scripts/ci.sh`：**本地 CI 门禁**（规约原文未列，实施时补上的最有用的产出）——
  Python 单测 / Java 单测 / 前端构建三作业，可 `bash scripts/ci.sh python java` 分作业跑；
  日志落 `deploy/ci-logs/<job>.log`；退出码可直接挂 pre-push 钩子。**本地实测 ≈49s**（12+16+21）
- `.workflow/ci.yml`：Gitee Go 流水线（仓库在 Gitee，故为主力）；
  另附 `.github/workflows/ci.yml` 备选（将来迁移时三作业逐条对应）
- `agt_python/pytest.ini`：统一 testpaths / 4 项既有基线的 `--deselect` 清单 / 标记，
  **本地与 CI 共用同一份**（「CI 绿本地红」最能消耗对门禁的信任）
- `agt_python/requirements.lock`：环境全量快照（77 包）

**E-5 Java 侧测试 0→1**（判据 C6 可回归）
- `agt_backend/src/test`：**9 个测试类 / 71 项测试全绿**（14.8s，离线 `-o` 可跑），四组：
  - **透传契约**（9 项）：每个出站口的 `X-Agent-Key`、路径、**流式单独加头**、metrics 路径白名单
  - **鉴权**（47 项）：4010/4011 判定、只认 `Authorization: Bearer`、会话惰性过期、
    jsdstu-3 token 静默续期与降级匿名、**越权不下发下游**
  - **响应体**（11 项）：`JsonResult` 包装一致、异常不裸抛 500、确认门 `detail` 不丢
  - **配置**（4 项）：`AgentAuth` fail-closed、头名契约、日志掩码
- 透传契约组**不用 mock**：起 JDK 自带 `com.sun.net.httpserver` 读原始请求——
  mock 掉 HTTP 层等于把要证明的东西（「线路上到底有没有那个头」）一起 mock 掉了
- `application-test.properties`：占位密钥 + 下游指向不可达端口
  （任何「忘了 mock、真去连了下游」的测试会立刻连接被拒而失败，而不是静默走通）

**E-7 数据备份 + 恢复演练**（判据 C4 可恢复 —— 此前四个 SQLite 库**零备份**）
- `app/services/backup_service.py`：四库一致快照 → `PRAGMA integrity_check` → 清单落
  `data/backups/YYYY-MM-DD/`，滚动保留 **14 份**；清理只删「日期名 + 内含 `manifest.json`」的目录（三条护栏）
- 快照走 **SQLite 在线备份 API**（`Connection.backup`），而非规约原文的"连同 `-wal`/`-shm` 一起复制"：
  裸拷在服务正在写时可能读到不同时刻的 db 与 wal → 得到「`integrity_check` 过但丢数据」的备份；
  源库 `-wal`/`-shm` 现状仍记入清单供排查（有意偏离已记档）
- `scripts/backup_dbs.py`：命令行备份/查清单（**服务没起也能备份**——这是它存在的理由）
- `scripts/restore_drill.py`：**恢复演练** —— 恢复最新备份到临时目录 → 四步断言
  （恢复 / integrity+sha256 / **逐表行数与清单比对** / 用应用自己的 store 真读一遍）→ 出报告
- `POST :8000/v1/ops/backup/run`（手动触发）、`GET :8000/v1/ops/backup/status`（清单与保留策略）；
  默认受 `X-Agent-Key` 保护（会写盘，不该匿名可触发）
- 挂入 `scheduler.default_tasks()` 的 `db_backup` 条目：每天一轮、启动后延迟 300s，
  复用既有任务表机制（开关 / 手动触发 / 状态上报）；**每日幂等**——
  当天已有且校验通过的备份不再重写（`force=true` / `--force` 才强制重备）。
  理由是实测发现的：服务每次重启都会让 `initial_delay` 再走一遍，不幂等则每次重启都换掉
  当天备份、把 `drill-report.json` 一起冲掉，"演练过的备份"永远立不住
- `docs/运维手册.md`（新增）：四库清单 → 备份在哪/怎么跑/保留策略 → 恢复演练 →
  **真出事的分场景恢复步骤** → 演练频率（月度）→ 排障对照表
- 单测 18 项，含 **WAL 一致性**（T1「只在 `-wal` 里的数据能被备走」）与
  T2 反面证明（只拷主库文件连表都读不到）、每日幂等（T6b）、`force` 语义（T6c）；
  **已验红**：实现换回裸拷后 T1 立刻红、其余 17 项仍绿
- `.gitignore` 补 `data/backups/`，并顺带补上 `data/memory/`（**库内是用户对话原文与画像**）
  与 `data/metrics/`——`data/governance/` 早在此列，这三处是同类缺口

**E-6 日志规范与轮转**（判据 C3 可观测 —— 此前四服务日志是**裸文本且无轮转**）
- `docs/日志规范.md`（新增，v1.0）：字段契约**唯一来源** —— §2 九字段逐字同名表
  （`ts/level/service/traceId/userId/event/durationMs/msg/err`，比规约多一个 `msg`：人读排障必需）
  + 三条硬规则（字段恒在 / 不许改名删字段 / `ts` 带时区）；§3 `event` 命名法 `域.动作`；
  §4 双轨分工；§5 轮转；§6 三端链路图（含两个跨线程坑）；§7 脱敏清单；§8 按 traceId grep 用法
- **双轨分工（有意设计，非冗余）**：`.jsonl` 供查询与对账（应用自写自轮转）；
  `deploy/logs/*.log` 是计划任务 cmd 重定向的控制台流，**是"进程根本没起来"的唯一线索**
  （此时应用还没能力写日志），保留但由本项收口轮转
- Python `app/core/logging_setup.py`：`JsonFormatter`（九字段 JSON 单行）+ `ConsoleFormatter`
  （带 `[traceId]` 前缀且过脱敏）；`RotatingFileHandler` **50MB × 10**；`setup_logging()` 幂等；
  跑在隔离目录（单测）时自动把日志改到 `DATA_DIR/logs`，不污染源码树
- Java `base/log/`：`JsonLogLayout`（**手写 JSON**，不为一个 layout 引入 logstash-encoder）、
  `LogRedactor`、`TraceIdFilter`、`TraceHeaders`、`MdcScope`、`StructuredLog`；
  `logback-spring.xml`（新增）`FixedWindowRollingPolicy` **50MB × 10**，**两侧备份文件名规律一致**
- **traceId 三端贯通**（生成方**只有 Java**，前端不生成，避免多源 id 打架）：
  Java `TraceIdFilter` 生成/采纳 → MDC → 响应头 `X-Trace-Id`；出站 RestClient 经
  `AgentAuth.withKey` 拦截器透传；Python `TraceIdMiddleware`（纯 ASGI）**只采纳入站、不自造**，
  经 `ContextVar` 写入并回写响应头；`AuthInterceptor` 解析身份后 `MDC.put(userId)`
- ⚠️ **两个必须记档的坑**：① 流式走 **JDK HttpClient**，既不共享 RestClient 的 defaultHeader、
  也不走 `AuthInterceptor`/拦截器 → 必须 `TraceHeaders.apply(...)` 单独加；
  ② MDC **不跨线程** → 流式段必须 `STREAM_POOL.execute(MdcScope.wrap(...))`，
  否则整条链路只有流式那一段没 traceId
- ⚠️ **入站 traceId 头要白名单校验**（Java `sanitize` / Python `read_trace_header`）：
  否则调用方可塞任意串污染、伪造日志
- **脱敏两防线**（与 S-8 同源，同一处收口不写两套）：字面量（`register_secret` 登记真实密钥值）
  + 正则（Java `LogRedactor` / Python `redact`）；完整用户输入**只记长度**（`qlen()`）
- ⚠️ **脱敏正则顺序敏感**：`Bearer` 规则必须排在 `Authorization` 规则**之前**。反例已固化为单测
  `LogRedactorTest.authorizationBearerIsFullyMasked`——顺序颠倒时
  `Authorization: Bearer eyJ...` 会先被 authorization 规则替成 `Authorization *** eyJ...`，
  **token 残留在日志里**（本次真实踩到的缺陷，Python 与 Java 两侧同步修正）
- **stdout 捕获收口**：`deploy/scripts/trim-logs.ps1`（UTF-8 BOM + CRLF）只处理 `deploy/logs/`
  **直接子级**的 `*.log`，超 20MB 轮转 `.1`、超 14 天删除、**被占用则跳过**、任何情况 `exit 0`
  （**绝不影响启动**）；由 `start-all.ps1` 在启动段前以独立进程调用，失败只告警
- `deploy/start-backend.cmd` 注入 `AGT_LOG_DIR`（**绝对路径**——该脚本会切到 `agt_backend/target`，
  相对路径会失效）
- **第三方噪音压制**：一次对话命中 23 行，其中 14 行是 httpcore 连接/TLS 细节（被 formatter
  染上 traceId，**看起来像业务日志**）→ `setup_logging` 把 `httpcore/httpx/urllib3/openai/pymilvus`
  等压到 `WARNING`，同类链路从 23 行降到 **9 行**
- 单测：Python `tests/test_logging.py` **33 项**（全量 170 → **203 passed / 4 deselected**）；
  Java 新增 **37 项**（`LogRedactorTest` 8 + `JsonLogLayoutTest` 11 + `TraceIdFilterTest` 11 +
  `AgentClientContractTest` 扩充 4 + `AuthInterceptorTest` 扩充 3），全量 71 → **108 项全绿**；
  全量门禁 `bash scripts/ci.sh` **PASS**（合计 46s）
- **已验红**：摘掉 `AgentClient.chatStream` 的 `TraceHeaders.apply(...)` 后，
  `chatStreamCarriesTraceHeadersOnItsOwnClient` **恰好变红**、其余 12 项仍绿 —— 证明该断言
  真能拦住"流式漏传链路头"；验后已还原

**E-9 轻量指标端点**（判据 C3 可观测 —— 有了端点，还要有"机器可抓"的口径，**零新依赖**）
- `GET :8000/v1/ops/metrics`（**受 `X-Agent-Key` 保护**，与备份端点同向，会读全库）：
  手写 Prometheus 文本格式 `text/plain; version=0.0.4`，**21 个必需族** —— 按路由的请求量/延迟分位、
  LLM 调用与 token 数、RAG 判定分布（kept/cross_domain/no_hit/insufficient_evidence）、降级计数、
  scheduler 任务状态、自述信息；`?window=<小时>` 调窗口、`?fresh=true` 绕缓存
- `GET :8080/v1/ops/metrics`（**匿名**，与前端可达性同向）：**双段合并** —— Java 自产 `agt_java_*`
  （up / info / heap / nonheap / threads / uptime / processors / free memory）+ **原样透传** Python 段 `agt_*`；
  两段命名空间隔离，不会撞名
- ⚠️ **Python 不可达时降级而非 500**：只返回 Java 段 + `agt_java_python_section_ok 0` + 一行 `#` 注释写明原因
  （已做故障注入实测：停 8000 后 HTTP 200、32 行、Java 段完整、**无残留 Python 样本**；恢复后自动回 1）
- ⚠️ **`_total` 是建表以来累计（单调），窗口滑动量一律标 gauge** —— 否则 `rate()` 全错，是这类端点最常见的
  静默错误。降级计数器因 `fault_counters.json` **只写不读、进程重启归零**，单列
  `agt_degrade_events_since_start_total`，并**不**与 `agt_faults_total` 做一致性检查（两者口径不同）
- ⚠️ **分位用最近秩法（nearest-rank）**，取第 `ceil(q·n)` 个**真实观测**、不插值 —— 避免输出虚构精度
- 数据源**全部复用既有 ODS 四表 + 内存降级计数器 + `scheduler.status()`**，**不新增埋点通道**；
  SQLite 一律 `mode=ro` 且先判文件存在，**不创建库文件**（不接 Prometheus 时**零影响**）
- `/v1/ops/metrics` 已加入 Java 与 Python **两侧 quiet paths**（实测连抓 30 次 `?fresh=true`，两侧结构化日志增量为 0）
- 单测：Python `tests/test_ops_metrics.py` **30 项**（**自写最小 Prometheus 文本解析器**作为验收口径 —— 严格校验
  HELP/TYPE 成对、重复序列、标签名/值合法性与转义、数值无科学计数法）；Java **25 项**
  （`PrometheusTextTest` 12 + `OpsMetricsServiceTest` 9 + `OpsMetricsControllerTest` 4）
- 全量门禁 `bash scripts/ci.sh` **PASS**：python **233 passed / 4 deselected**、java **133 项全绿**、web 构建通过
- **已验红**：① Python 摘掉 RAG 空值 → `none` 的映射，`test_rag_decision_empty_maps_to_none` **恰好 1 红**、其余 29 绿；
  ② Java 摘掉 Python 段拼接，合并 / 原样透传 / 不缓存 **3 项变红**、其余 22 绿。验后均已还原

**E-8 操作审计留痕**（判据 C5 可追溯 —— 从"只有治理动作有审计"变为**一张表回答谁在这台机器上做了什么**）
- **复用治理域既有 `audit()` 机制，不新建第二套**：写点从 1 处扩到 **5 处** ——
  治理门面 `audit()`（既有）、确认门 `_audit_confirm()`、备份包装层 `run_backup()` /
  配置留痕 `audit_config_change()`、Java 侧 `AuditReporter`（账号事件经
  `POST /v1/kb/governance/audit` **补录**进同一张表）
- **统一维度补全**：`actor≡operator`、`at≡ts`（历史命名保留并声明等价），
  新增 `target` / `ip` / `trace_id` 三列 —— 走 `ALTER TABLE ADD COLUMN`（**只加列不重建**），
  由 `_ensure_audit_columns()` 幂等补齐；`target` 统一前缀 `doc:` / `tool:` / `user:` /
  `backup` / `config:` / `service:`，老调用点由 `doc_id` **自动派生**、不需要逐个改造
- ⚠️ **老行的 `target` 是读取期派生的，不回填历史数据**：审计表"只增不改"，故用
  `COALESCE(target, 'doc:'||doc_id)`，且**筛选与显示用同一个表达式**（否则会出现
  "筛出来了但对象列写着别的"）。真机实测：475 行历史数据里有 `doc_id` 的现在也能被
  `target=doc:` 筛到（439 条），**库里仍是 NULL**
- **覆盖面**：`LOGIN_OK` / `LOGIN_FAIL` / `LOGOUT`（含**上游不可达**也留痕，否则"认证服务挂了"
  会表现成"压根没人登录"）、`TOOL_CONFIRM_ALLOW` / `TOOL_CONFIRM_DENY`（带 `reason`）、
  `BACKUP_RUN`、`CONFIG_CHANGE`
- ⚠️ **`result` 三值必须分开**：`SUCCESS` / `FAILED`（做了没成）/ `DENIED`（按规则不该做）。
  混成一个"失败"会让一个小权限问题看起来像一次系统故障
- ⚠️ **审计失败永不抛出**：Python 侧吞异常只打 `[GOV-WARN]`、Java 侧 `AuditReporter` 吞异常只打 WARN。
  审计是事后能力，**不能反过来变成"登录失败"的原因**。真机故障注入实测：**停 8000 后登录仍返回 2000**，
  只留下 1 条 WARN，且故障期间那次登录**没有**审计行（宁可丢一条审计，也不拒绝登录）
- **配置变更留痕**（`app/services/config_audit.py`）：本系统**无运行时改配置入口**，
  只能在**启动时**比对配置指纹 → `data/ops/config_snapshot.json`；指纹**不含 `buildTime`**
  （每次打包都变，算进去会把"重新构建"记成"改配置"→ 告警疲劳），含 `version` + `gitCommit`
- ⚠️ **密钥永不进审计表**：配置留痕按**键名**判定敏感（`key`/`secret`/`password`/`token`/
  `credential`/`api_key`），命中只记 `sha256:<12 位>`；真机实测快照 137 键，明文密钥泄漏检测**通过**
- `GET /v1/kb/governance/audit` 加 `target` 前缀过滤；`GET /v1/kb/governance/audit/export`
  导出 CSV（utf-8 **带 BOM**、上限 2 万行且超限**如实标 `truncated`**、**防 CSV 公式注入**：以
  `=`/`+`/`-`/`@` 开头加 `'` 前缀）；`POST /v1/kb/governance/audit` 供 Java 补录
- 前端治理面板「审计流水」页扩展（**不另起一页**）：新增**对象(target)筛选**与**时间范围**、
  对象列改显 `target`、展开看 IP 与 traceId、新增**导出 CSV**（服务端导出，不前端拼表 ——
  分页单页上限 200，前端循环翻页一次失败就会产出"看不出残缺的残缺证据"）
- 新增 `docs/审计清单.md`（11 节）：动作码全表、七维契约、五处写点、保留与权限、
  **§10 已知边界 7 条如实记档**（如"只有账号事件带 ip"、"配置变更只知变了不知谁改的"、
  "Python 不可达时审计会丢"）
- 单测：Python `tests/test_audit.py` **41 项**（含**升级路径**：手工造一个"E-8 之前"的旧表，
  验证 `ensure_schema()` 补列且**老数据不丢**；**导出专项 7 项**覆盖 BOM + 固定表头、
  公式注入两向（掐 `=`/`+`/`@` 但**保留纯负数** `-5`）、超限`truncated` 如实上报、
  导出与列表**同过滤**、无密钥 4010、库不可用降级返回不抛 500）；
  Java **15 项**（`AuditReporterTest` 10 + `AuthServiceTest` 新增 5）
- **验红（mutation test）**：三处关键实现各改坏一次，**红的都是当为之事**——
  ① 去掉 `target` 的 `COALESCE` 派生 → `test_legacy_rows_derive_target_without_touching_data` 1 红；
  ② 去掉 `_csv_cell` 的公式前缀拦截 → 注入相关的 2 条红；
  ③ 去掉 `audit()` 里 `trace_id` 的上下文兜底 → `test_audit_picks_up_trace_id_from_request_context` 1 红。
  其余全绿，验后均已逐字还原（另跑 AST 扫描确认**无重复定义/编辑残留**）
- 全量门禁 `bash scripts/ci.sh` **PASS**：python **274 passed / 4 deselected**、java **151 项全绿**、web 构建通过
- **真机验收**（2026-09-24，见 `DEV_ISSUES.md §6.81`）：登录 → 危险操作（创建任务经确认门）→ 停用文档，
  审计按 id 序查出 **476 / 477 / 478** 三条；`target=tool:` / `doc:` / `user:` 前缀筛选正确；
  CSV 导出 2 行、带 BOM、表头含 traceId；无令牌读审计与导出均 4010

**E-10 密钥轮换 + 权限矩阵**（判据 C5 —— 从「一把密钥改不动、权限靠看代码猜」变为**不停服轮换 + 每行权限可 grep**）
- **共享密钥双密钥过渡窗口**：新增 `AGENT_SHARED_KEY_PREV`，轮换期本服务**同时接受新旧两把**
  （`app/core/auth.py` 的 `accepted_keys()` + 中间件双密钥校验）。
  ⚠️ 本服务是**非 reload** 部署、Java 侧同理，只认一把密钥时轮换**必然**出现
  "一边已换、一边未换"的窗口，那个窗口里全链路 4010 —— 双密钥把"无缝轮换"从**调度问题**变成**配置问题**
- ⚠️ **轮换顺序是「先消费者后生产者」**：8000 先两把都收 → 再改 Java → 最后清 PREV。
  反过来做会在中间稳定 4010（生产者拿着新钥匙敲一把还不认它的门）。
  完整步骤与顺序论证见 `docs/密钥与配置管理.md` §3
- ⚠️ **`AGENT_SHARED_KEY_PREV` 与当前密钥同值 → 拒绝启动**：两把一样说明"以为在轮换其实没换"，
  操作者会看到 legacy 计数恒为 0 并据此"停用旧密钥"——而真相是**旧密钥从未被停用**。
  静默的安全债比启动失败危险
- ⚠️ **旧密钥放行只计数、不落 `fault_event`**：请求是**成功处理完的**，把它算进看板故障数就是让看板说谎。
  走新增的 `telemetry.count_event()`（只计数 + 指标，无 fault_event 行），
  计数落在 `agt_degrade_events_since_start_total{path="auth_key_legacy_used"}` ——
  它是"能不能清掉 PREV"的**机器可判**放行条件，而不是凭感觉
- ⚠️ **`AGENT_SHARED_KEY_PREV` 同样必须脱敏**（Python `register_secret` + Java `LogRedactor`）：
  过渡窗口内它**仍是有效凭据**，漏掉它等于让一把还能开门的钥匙以明文进日志
- `deploy/scripts/preflight.py` 密钥检查升级为**轮换期感知**：分辨"轮换进行中（Java 仍在用旧密钥）"、
  "轮换①已完成（Java 已用新密钥，可以着手清 PREV）"、"三把都不对"三种状态，
  并新增"PREV 与当前密钥同值"的 FAIL
- **权限矩阵**（`docs/权限矩阵.md`）：**49 行端点 × 权限**，每行带 `文件:行` + 代码原文
- ⚠️ **角色定义按实测纠正**：真实取值是 `admin / pilot / repair`（来自 jsdstu-3），
  且代码里**只比较过 `admin`** —— `pilot` 与 `repair` 在本系统**完全等价**。
  规约原文假设的 `admin/operator/viewer` 在本系统**没有对应物**，按它写会让每一行都指向不存在的校验点
- **把它做成了可执行的门禁**（`scripts/check_permission_matrix.py`）而非一份说明文档，五项检查：
  A 端点覆盖双向 ｜ B 证据真实（`文件:行` 必须包含所引用的代码片段）｜ C 约束语义（标管理员的证据行必须真含判定）
  ｜ **D 反向完整**（代码里每个判定调用点必须被矩阵某行的方法区间覆盖）｜
  **E 匿名行结构**（标匿名的证据必须是 Controller 映射注解）。实测 49 行 / 49 端点 / 22 处判定点全覆盖
- 新增 `docs/密钥与配置管理.md`（6 节）：密钥清单 7 项（含轮换周期）、存放与保护 6 条（逐条带代码证据）、
  双密钥轮换（顺序论证 / 逐步命令 / 三个坑 / 残余风险）、其余密钥要点、泄漏应急处置五步
- 单测：`tests/test_auth_shared_key.py` **+10 项**（双密钥放行 / 第三把被拒 / 清 PREV 后即刻失效 /
  legacy 计数且不算故障 / 每进程一次告警 / 非 ASCII 头 / 同值拒启动）
- **权限矩阵门禁接进 CI**：`scripts/ci.sh` 由**三作业扩为四作业**（新增 `matrix`，秒级、零依赖、断网可跑），
  可单跑 `bash scripts/ci.sh matrix`；四作业全量实测 **≈64s**（python 284 passed / java 151 passed /
  web 构建通过 / matrix 49 行全覆盖）。自此"改端点忘了改矩阵"会被直接拦住

**E-11 依赖与漏洞扫描**（判据 C1 —— 从"交付包里可能带已知 Critical 漏洞"变为**Critical 阻断发布**）
- `scripts/scan_deps.py`：**零第三方依赖**（仅标准库）的依赖闭包 + 漏洞 + 许可证扫描器，三端一次覆盖 ——
  **Python**（`importlib.metadata` 传递闭包，**展开 `uvicorn[standard]` 这类 extras** 但不收未被请求的 extras）、
  **Java**（从 fat jar 的 `BOOT-INF/lib/*.jar` 取闭包，版本回查 `~/.m2` 的**目录结构反查 groupId**——离线可跑）、
  **npm**（`package-lock.json` 里 `dev != true`）
- **一套接口覆盖三个生态**：查漏洞只走 **OSV**（`api.osv.dev`），PyPI / npm / Maven 同一个 query API，
  不必为三个生态装三个工具
- ⚠️ **为何不用规约点名的 `pip-audit` / `npm audit` / `dependency-check`**（据实记档）：
  ① 三个工具 + 三条联网路径，任一挂了门禁就整体失效；② 实测 `npm audit` 在本机**淘宝镜像上直接不可用**
  （`registry.npmmirror.com` 未实现 `/-/npm/v1/security/*`，返回 404 `NOT_IMPLEMENTED`，
  加 `--registry=https://registry.npmjs.org` 才通）；一个"跑不起来"的门禁工具比"没有门禁"更糟
  ——它会让"绿灯"失去意义
- **阈值按裁定实现为 Critical 阻断 / High 告警**（非规约原文的"高危都阻断"）：真机扫出 **25 条 High**，
  多数落在 Spring 托管、不升大版本就消不掉的 Jackson 栈内；全阻断会让门禁长期红灯而**被绕过**
- ⚠️ **不扫描解释器环境本身**：本机共享 conda env 的 77 个包里扫出 15 条命中，**全部**来自与本项目无关的
  jupyter 生态（accelerate / Django / sqlparse / pip 自身）。扫环境会把无关漏洞算到自己头上，
  只会训练出"看见红灯就忽略"的习惯
- **Java 真实收益**：`tomcat-embed-core 10.1.28` 携带 **7 条 Critical**。仅升 Spring Boot 3.3.3 → 3.3.13
  只能到 10.1.42（仍 6 条 Critical），**必须显式覆盖** `pom.xml` 的 `<tomcat.version>`；
  改到 **10.1.60** 后 Critical **7 → 0**、High **25 → 11**、总命中 **74 → 41**，Java 151 项测试仍全绿
- **离线优先**：`security/sbom.json` + `vuln-snapshot.json` + `licenses.json` 入库，
  CI 用快照**断网可跑**；`--refresh` 才联网刷新（含 HTTP 重试，避免一次超时把包永久钉成 UNKNOWN）
- **豁免台账自身也有门禁**（`security/deps-allowlist.json`）：缺必填字段 / 缺过期日 / 日期格式错 → **直接阻断**；
  有到期日的豁免过期即失效 —— 台账不设门禁就会变成"把漏洞永久静音"的地方
- 其余：`docs/依赖清单.md`（闭包 145 包 + 许可证 144/145 已识别）、`docs/依赖与漏洞处置.md`
  （阈值 / 退出码 / 五步处置 / tomcat 案例 / 四个坑）；`scripts/ci.sh` 加**第五作业** `deps`；
  `scripts/release.sh` 加 **3.5 步发布前阻断**；`agt_python/tests/test_scan_deps.py` **54 项全离线**
  （CVSS v3.1 已知向量 10.0/9.8/7.1/5.9、四级 severity 回退链、台账校验、scan() 结论）
- ⚠️ **Severity 回退链**：`database_specific.severity` → GHSA 别名 → **自写 CVSS v3.1 公式算分**
  → `UNKNOWN`（**显式列出、不静默丢弃**）—— PYSEC 条目常缺 GHSA 分级，不回退就会"一片未分级"

**E-12 前端运行时报错上报**（判据 C3 —— 把"前端炸了"从**三端都没有记录**变成"看板一页三问可答"）
- `agt_web/src/utils/clientError.js`：上报器，**零第三方依赖**、纯 `fetch`
  - **三处收口**：`app.config.errorHandler` + `window.onerror` + `unhandledrejection`。
    ⚠️ 规约原文写的 `Vue.config.errorHandler` 是 **Vue 2 API**，本项目 Vue 3 的等价物是**实例上**的
    `app.config.errorHandler`（据实记档）。三处缺一不可：`errorHandler` 收不到 `addEventListener` 里
    异步回调抛的异常与未处理的 Promise，而那恰是真实项目里最多的一类
  - 装在 `mount()` **之前**：装在 mounted 之后会漏掉**首屏渲染阶段**的异常，而那正是
    "用户看到一片空白、连错误提示都没有"的那一段
  - **刻意不复用 `src/api/request.js` 的 axios 实例**，两个具体理由：① 它的响应拦截器把非 2000
    一律 reject，网络失败会造出**新的 unhandledrejection** → 被本模块再上报 → **自我放大的循环**；
    ② 它对 `4010` 调 `clearAuth()` —— 等于"**会话过期期间每次前端报错都把用户登出**"。用观测能力改
    用户登录态不可接受
  - **绝不吞掉线索**：`onerror` 里**不 `return true`**（返回真值会吞掉浏览器默认的控制台输出）
  - 卸载期（`pagehide` / `beforeunload`）切 `sendBeacon`：`fetch` 在导航开始后会被中断，
    而崩溃/离开页面的那一刻正是错误最多的时候（代价：`sendBeacon` 不能带请求头 → 那条上报无 `userId`，
    归匿名；取舍是"宁可不带身份也要把错误送出去"）
  - **风控第一层**：指纹去重（同一页面加载内同一错误只报一次）+ 单页上限 30 + 忽略规则（`ResizeObserver`
    这类浏览器噪音，**启动时打印规则清单**，不做静默丢弃）+ 采样率
  - 验收钩子：`window.__agtClientErrorSelfTest()`（三条收口各发包一次；`vue` 那条是直接调用已安装的
    handler——浏览器不允许外部伪造一次组件渲染异常，故它验的是"handler 装上且能发包"）与
    `window.__agtClientErrorStats()`
- `POST :8000/v1/ops/client-error` + `POST :8080/v1/ops/client-error`（Java 转发）
  - **身份是结构性约束而不是约定**：上报体模型里**没有 `userId` 字段**，只能由 Java 从登录态注入
    `user_id` 查询参数（与 S-1 同一条铁律）。真机验收：浏览器上报 `userId=1`（admin）由 Java 注入，
    body 里塞的 `999` 无效
  - **永远返回 2000**：上报失败绝不影响用户，是否入库只在 `data.stored` + `reason` 里如实说
  - **空请求体也收**：页面卸载中 `sendBeacon` 只带 `Content-Type` 不保证带体、`fetch` 会被 `keepalive`
    截断；若端点把请求体声明为必填，这两种会吃 422 → `3001`，**恰恰是最难复现的那类错误永远收不到**
  - Java 侧 **8KB 体量闸门** + **匿名可达**（唯一匿名写入端点：崩溃最常发生在未登录时，要求先登录就
    等于在最需要上报的时刻收不到；防护靠体量闸门 + 服务端合并 + 采样率三重，且它读不到任何东西）
- `observability.db` 新表 `client_error`（第六张事件表，`schema_version` 登记 `v7`）
  - **合并写**：`UNIQUE(fingerprint, bucket)` + `ON CONFLICT DO UPDATE SET count = count + 1` ——
    同一错误在同一时间桶内**只占一行**、`count` 累加。防的是"一个渲染循环里的报错几秒内刷出上千条"把表
    刷爆、把看板变成滚动字幕
  - **固定桶**（`floor(now/window)*window`）而不是滑动窗口：无状态、可重启、可单测、多 worker 下确定；
    代价是跨桶边界（10:00:59 与 10:01:01）会各占一行，在"看频率趋势"这个用途上无所谓
  - `message` / `stack` 入库前**截断**（长度可配）并过 `redact()`；`page` 丢掉 `?query` 与 `#hash`
    （那里常有 id / token / 用户输入）；`trace_id` 用**只读**的 `trace_ctx.get_trace_id()` 而不是会
    "凭空造 id"的 `current_trace_id()`
- `GET :8000/v1/agent/metrics/client-errors` + `GET :8080/v1/agent/metrics/client-errors`：总量 / 类数 /
  按页面 / 按版本 / 最新明细；窗口起点**在 Python 侧算好**（`_now()` 写 T 分隔、SQLite `datetime()`
  用空格分隔，直接字符串比较会让"同一天的行永远算在窗口内"且不报错）
- 看板：新增「数据中台 · 观测 → 🐞 前端错误」页（KPI + 按页面 + 按版本 + 最新报错表 + 口径页脚），
  并在「总览」加同一张卡的紧凑形态（**独立请求、独立失败**——浏览器真炸的时候恰好看不到总览，
  最需要看的时候看不到）
- `docs/权限矩阵.md`：新增 2 行（含"**唯一匿名写入端点为什么必须匿名**"的说明），49 → **51 行**
- `.env.example`：新增五个配置项 `CLIENT_ERROR_ENABLED` / `_SAMPLE_RATE` / `_DEDUP_SECONDS` /
  `_MAX_MESSAGE` / `_MAX_STACK`
- 两侧 quiet path：Python `app/core/trace.py` 的 `_QUIET_PATHS` 与 Java `TraceIdFilter.QUIET_PATHS`
  都加了上报路径，避免上报风暴刷满结构化日志
- `scripts/ci.sh` 的 `web` 作业加**产物存在性断言**（断言三处收口标记在 bundle 里）——
  前端**刻意不引入 jest**（门禁工具自身跑不起来比没有门禁更糟），行为正确性由实机浏览器验收覆盖
- 单测：Python `tests/test_client_error.py` **30 项**（策略纯函数 / report 策略 / 合并写与聚合 / 端点），
  Java `OpsClientErrorControllerTest` **6 项** + `OpsClientErrorServiceTest` **7 项**（真 HttpServer 读原始请求，
  断言 `user_id` 走查询参数、body 里没有 `userId`、`X-Agent-Key` 在位）

### Changed

- **`pom.xml` 显式覆盖 `<tomcat.version>` 到 `10.1.60`**（E-11 发现）：Spring Boot 3.3.3 默认托管的
  `tomcat-embed-core 10.1.28` 带 7 条 Critical；仅升 Spring Boot 到 3.3.13 只能到 10.1.42（仍 6 条），
  故显式覆盖到 10.1.60。**这是"改一个版本号就消掉 7 条 Critical"的性价比最高路径**；
  重新打包后 `BOOT-INF/lib/tomcat-embed-core-10.1.60.jar` 在位、Java 151 项测试全绿、8080 重启后
  `GET /v1/version` 正常
- **本地门禁 `scripts/ci.sh` 由四作业扩为五作业**（新增 `deps`，秒级、零依赖、断网可跑），
  五作业全量实测 **≈73s**（python 338 passed / java 151 passed / web 构建通过 / matrix 49 行 / deps 2s）
- **`scripts/release.sh` 增加 3.5 步发布前阻断**：依赖扫描非 0 退出即"正式执行会在此中断"，
  与既有的版本一致性门禁同向（刻意不提供 `--force`）
- **日志字段口径统一**（含 `msg` 字段）后，`ConsoleFormatter` 亦过脱敏：控制台与结构化文件
  **不再可能一个脱敏、一个漏**（早前只有结构化侧脱敏，控制台侧是裸文本）
- **健康探测与业务超时分离**：新增 `agent.ops-read-timeout=5000`。业务读超时 240s 是为覆盖
  LLM 重试序列，但健康探测本身就是"还活着吗"的判据——沿用 240s 会让 Python 僵死时
  前端状态灯 4 分钟才变红，等于没有告警
- **后端产物名统一为 `agt_backend.jar`**（`pom.xml` 加 `<finalName>`）：jar 名是部署契约，
  不带版本号可消除"产物名里的 `0.0.1-SNAPSHOT` 与 `VERSION` 不一致"这类运维困惑；
  真实版本看 `GET :8080/v1/version`
- `deploy/start-backend.cmd` 启动前检查 jar 是否存在，缺失时直接给出构建命令
- **`requirements.txt` 补齐上限约束**（`fastapi>=0.115.0,<1.0` 等 9 项）：此前只有下限，
  同一份源码在不同时间安装会得到不同依赖树，「可复现」不成立。上限取**下一个主版本**
  （而非死锁到当前小版本——后者会把安全补丁一起挡在门外）
- **测试基线口径统一**：`pytest.ini` 用 `--deselect` 逐条列出 4 项既有失败，
  取代此前「看到 4 failed 属正常」的口头约定；这 4 项之外任何失败都会让门禁退出码非 0
- `docs/部署手册.md` **§9.1 增告警「让它在前台跑完」**：同一条门禁命令，被挂到后台执行时
  web 作业实测 2903s（≈48 分钟），而前台执行 16s——是执行环境节流，**不是构建配置问题**；
  同时按最新实测刷新 §9 耗时口径（Python 12s / Java 11s / Web 16s，合计约 40s）

### Fixed

- ⚠️ **两个既有测试隔离地雷（traceId contextvar 泄漏）**（S-8 轮次乱序复跑时抓到）：
  `test_logging.py` 的 `test_logging_and_telemetry_share_one_trace_id` 用 `set_trace_id()`
  后不清理、`test_ensure_trace_id_reuses_existing` 里 `ensure_trace_id()` 自建的新 id
  绑进 contextvar 后也不清理——默认按字母序跑（client_error 在 logging 前）侥幸不炸；
  一旦文件顺序变化，`test_client_error.py::test_trace_id_is_empty_without_request_context`
  假失败。修法：两处 finally 里显式还原/清空 contextvar（注意 reset 语义下不能用
  `bind("")+unbind()` 还原，要 `set_trace_id("")`）。

- ⚠️ **`schema_version` 表重复插行**（E-13 真机验收抓到的存量 bug）：这张表**没有 UNIQUE 约束**，
  `INSERT OR IGNORE` 从来不 IGNORE——每次服务启动都把 v1..vN 再插一遍，存量库已堆积数百行重复
  （实测 v1 重复 53 行、v2 重复 53 行…）。修复 = 启动时先清历史重复（保留最早 `applied_at`）
  再逐版本判存插入（幂等）；不改表结构——加 UNIQUE 需重建表，"插入幂等"已根除问题
- ⚠️ **`scripts/ci.sh` 的 `job_web` 用裸 `cd` 导致工作目录泄漏给后续作业**（接第四个作业时抓出）：
  全量跑时顺序为 `python → java → web → matrix`，web 把 cwd 留在 `agt_web/`，
  于是 matrix 去 `agt_web/scripts/...` 找脚本、报 `[Errno 2]`、退出码 2。
  **单独跑 `matrix` 完全正常，只有全量跑才暴露**。已改为子 shell 内 `cd`
  （`job_web` / `job_matrix` / `job_deps` 三处同改，后续作业不再受前一个作业的 cwd 影响）
- `.env` 的 `HOST=0.0.0.0` 是 S-1 改造前的残留（运行中的实例靠命令行参数兜住，
  但照手册手敲命令的人会绑到全网卡）；已改回 `127.0.0.1`
- 9090 健康探针只探 `/`，于是任意 HTTP 服务都被判成"agt_web 已在运行"，
  使整套 fail-fast 失效；改用 E-1 的 `/version.json` 作为特征
- `preflight.py` 输出在 PowerShell 管道下中文乱码（`encoding="utf-8"` 与 PowerShell 5.1 的
  GBK 解码错位）；改为只补 `errors="replace"`，不动 encoding
- ⚠️ **`GovernancePanel.vue` 三个方法只存在于模板、脚本里从未实现**（E-8 排查前端时发现）：
  `reloadAudit` / `reloadTickets` / `reloadRegistry` —— 点"查询"或切换筛选下拉会抛
  `xxx is not a function`，界面表现为"点了没反应"，故长期无人上报（默认值都是"全部"，
  不主动切换就不触发；"刷新"走的是另一条 `reloadSub` 路径）。三个方法已补齐
- ⚠️ **`ensure_schema()` 直接调用时会崩**（E-8 单测抓出）：该分支新建的连接没有
  `row_factory`，而 `_ensure_audit_columns` 按列名取值 → `TypeError: tuple indices must be
  integers`。真实影响面是**任何"先建表再读数据"的调用方**（迁移脚本 / 预检）。
  已显式对齐 `row_factory`，并把列名改为按下标取（不依赖行对象形态）
- ⚠️ **前端上报的 `appVersion` / `userAgent` 被静默存成空串**（E-12 实机验收抓出）：
  pydantic v2 的 `model_dump()` 默认输出**字段名**（`app_version` / `user_agent`），而服务层
  `client_error.report` 按**上报体（camelCase）**取键 `appVersion` / `userAgent` —— 于是接口照样返回
  2000、库里照样有行，**只有"按版本聚合"永远是「(未知版本)」**，UI 上完全看不出来。
  已改为 `model_dump(by_alias=True)`，并补了一条**别名回归断言**（去掉 `by_alias` 只有它变红，
  其余全绿——验红已实测）
- ⚠️ **上报端点把请求体声明为必填，导致"空体上报"吃 422 → `3001`**（E-12 首轮实机验收抓出）：
  浏览器在页面**正在卸载**时发 `sendBeacon` 只带 `Content-Type`、不保证带体；`fetch` 被 `keepalive`
  截断也会得到空体。端点改成 `report: ClientErrorReport | None = None`（空体按 `{}` 处理）。
  **上报端点一旦有前置条件，就有一整类错误永远收不到，而那恰恰是最难复现的一类**
- **E-12 单测断言姿势错误**：原先断言 `telemetry._q_client_error[-1]`，但后台 flush 循环
  会在"请求发出"与"断言执行"之间把队列排空 —— "上报成功但队列为空"是**设计使然**，
  报的却是断言失败，**红得极有误导性**。改为断言**库里的行**（并在 `TestClient` 上下文内 flush）

## [1.0.0] - 2026-09-24

首个受版本管理的版本：三层架构的**功能闭环**，加上企业级交付的第一步（服务间信任边界）。

> 说明：企业级落地任务（E-1 ~ E-15，见 `docs/specs/企业级落地规约.md`）仍在推进。
> 本条目记录的是 1.0.0 版本号**基线化**时的既有能力与已完成的加固项。

### Added

**三层架构**
- 前台 `agt_web`（Vue 3 + vue-router + vuex，端口 9090）：登录、智能体对话、知识库、数据看板、个人画像、关于
- 后端 `agt_backend`（Spring Boot 3.3.3，端口 8080）：业务编排、鉴权、持久化，统一 `JsonResult` 响应体
- 智能体端 `agt_python`（FastAPI + uvicorn，端口 8000）：意图识别、RAG 检索、多智能体编排、工具调用、埋点与中台聚合
- 无人机主控 `jsdstu-3`（端口 8060）：既有外部系统，由后端代理鉴权与查询

**RAG 知识库**
- 结构优先切片：按标题层级与段落边界切分，双向合并、递归下探、尾部短片并入前片（不丢弃信息）
- 混合召回：BM25 与 dense 向量双路 **RRF 融合**，再经 rerank 重排与预算制精选
- 四值判定口径：`kept` / `cross_domain` / `no_hit` / `insufficient_evidence`，如实拒答不再伪装成正常回答
- 知识缺口聚类：规则切段 + 重叠系数聚类（不调 LLM），按"证据不足 / 无召回"给出补语料优先级

**语料治理（G1~G4）**
- 治理账本（SQLite）与 `schema_version` 迁移机制：只加列、不重建表
- 回收站与停用区、文档版本管理、知识库整体备份与恢复
- 治理审计留痕（覆盖文档治理动作）

**记忆与画像（M-1 ~ M-5）**
- 会话记忆持久化（SQLite）与双锚点裁剪：保头保尾、中间按相关性取舍
- 裁剪后摘要补偿：摘要生成失败落 `memory_summary_failed` 埋点，可查账
- TTL 过期清理挂入调度器（默认 24h 周期，可配）
- 用户画像：从对话中抽取长期偏好，支持查看/删除自己的画像

**数据中台（ODS / DWS / ADS）**
- 四类事件表（`agent_chat_log` / `agent_tool_call` / `agent_delegation` / `fault_event`）只增不改
- 反馈两表（`agent_feedback` / `feedback_content`）与 RAG 判定列（`rag_decision` / `rag_docs`）
- `metrics_daily` 预聚合与九页可视化看板，前端一律走 API，不直连明细
- 埋点铁律：非阻塞、绝不抛异常；元数据全量记、**内容不落库**

**容错与可观测**
- 超时分层（LLM 重试序列 / 工具序列 / 整链），失败重试与熔断、降级计数快照
- 轨迹还原：按真实控制流还原 RAG / 业务 / 闲聊三条链路的阶段，四色标注（ok / warn / gray 如实拒答 / error 真故障）
- 降级与拒答**分开呈现**：灰是设计内行为，红才是系统故障

**工程资产**
- Python 侧 130 项单测；治理 G1~G4 端到端验收脚本；容错与数据中台端到端验收脚本
- 评估集：RAG 检索（hit@1 / hit@5）与 52 题问答、60 题业务链路

### Security

- **S-1 服务间信任边界**：智能体端（8000）除探针外全部要求 `X-Agent-Key`，
  常量时间比对、失败返 `4010` 并落 `auth_key_rejected` 埋点；
  密钥为空时**拒绝启动**（fail-closed）
- 智能体端绑回环 `127.0.0.1`，只服务本机后端：外网与内网其它机器不可达
- Java 侧三个出站客户端（Agent / Kb / Governance）统一经 `AgentAuth` 加头，避免漏配
- 密钥只经环境变量注入，本机密钥文件 `deploy/secrets/` 已 gitignore；日志只留指纹（长度 + 首尾）

### Fixed

- 切片器尾部短段由"直接丢弃"改为"并入前一片"，消除信息丢失
- 记忆 TTL 清理此前未挂调度、生产不触发，已接入调度器
- 智能体端在 `.env` 缺失关键配置时静默运行的问题，改为启动即校验

[Unreleased]: https://gitee.com/IS24Kchunshuai/drone-agent001/compare/v1.0.0...HEAD
[1.0.0]: https://gitee.com/IS24Kchunshuai/drone-agent001/releases/tag/v1.0.0

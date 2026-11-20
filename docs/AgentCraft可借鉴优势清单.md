# AgentCraft 可借鉴优势清单（面向 drone-agent001 数据中台）

> 生成时间：2026-09-19
> 调研对象：`D:/install/java/javaworkspace/AgentCraft`（前端 11 个管理页 / 后端 15 个 Controller / 19 张表）
> 对比基准：`drone-agent001` 三端现状（agt_python:8000 · agt_backend:8080 · agt_web:9090）
> 结论性质：**决策清单**（含「建议做 / 不建议做 / 已有，勿重复建设」三类），不是开工令

---

## 一、先说结论：真正值得搬的只有三件

AgentCraft 的管理端有 11 个页面，但用户要的是「数据中台优势部分」——**数据中台的价值不在页面多，而在"数据闭环能不能自我改进"**。
按这个标准筛完，AgentCraft 相对 drone 的**真实增量**只有三件：

| # | 能力 | AgentCraft 现状 | drone 现状 | 增量价值 |
|---|---|---|---|---|
| **A** | **RAG 未命中问题分析** | `qa_unanswered` 表 + 聚类 + 补库建议（含优先级） | ❌ **完全没有**——`decision` 字段算完就丢 | ⭐⭐⭐ 最高：把"答不出来"变成可运营的知识缺口清单 |
| **B** | **知识库巡检的"使用度"维度** | 重复/低质量/过期 + **无人访问文档** | ✅ 已有 重复/低质量/过期；❌ 缺"无人访问" | ⭐⭐ 中：补一个维度，且需先有访问埋点 |
| **C** | **报表页的时间区间 + 趋势图形态** | 日期区间选择 + 近 7 天柱状趋势 | ✅ 有窗口切换 + 趋势条（已对等） | ⭐ 低：drone 已具备，仅样式可借鉴 |

**其余 8 个页面（用户管理 / 公告 / Agent 执行记录 / 问答日志 / 点赞统计 …）不建议搬**，理由逐条列在第四节。

---

## 二、A 项详解：RAG 未命中问题分析（最该做的一件事）

### 2.1 AgentCraft 怎么做的

**数据来源**：`qa_unanswered` 表，字段极简——

```sql
CREATE TABLE `qa_unanswered` (
  `question` varchar(500) NOT NULL COMMENT '问题内容',
  `count`    int DEFAULT '1'    COMMENT '提问次数',
  `create_time` datetime        COMMENT '首次提问时间',
  `update_time` datetime        COMMENT '最后更新时间',
  UNIQUE KEY `uk_question` (`question`)   -- 同一问题只留一行，count 累加
)
```

**后端分析**（`KnowledgeInspectionServiceImpl.java`，616 行）四步：

1. **拉未命中问题** → 按时间区间 + `minCount`（至少被问几次）过滤；
2. **关键词聚类** → `clusterQuestions()`：提取中文/英文关键词，用**公共字符数**判断相似（`countCommonCharacters`），超过 `clusterThreshold` 归为一簇（**不调 LLM，纯规则**）；
3. **生成主题** → 每簇取最高频关键词作为主题名（`summarizeTopic`）+ 提取关键词列表；
4. **补库建议** → `generateSuggestions()`：按簇产出建议，分三档优先级——

```javascript
const suggestionTypeLabels = {
  '紧急补库': '紧急',   // 高频 + 多问题聚成一簇
  '建议补库': '建议',
  '观察':     '观察',
};
const priorityColors = { '高': '#ff4d4f', '中': '#faad14', '低': '#52c41a' };
```

**前端展示**（`KnowledgeInspection.jsx` 的「未命中问题分析」tab）：
- 分析概览：未命中问题总数 / 独立问题数 / 主题聚类数
- 高频未命中主题：表格（主题、关键词、问题数、代表性提问）
- **补库建议**：按优先级着色 + 建议类型 + 建议内容
- 一键导出 CSV

### 2.2 drone 现状：原材料被丢掉了

**这是最关键的一条发现。** drone 的 RAG 链路**已经算出了**拒答判定，但**没有落库**：

`app/rag/retriever.py:190` 产出 `decision`，取值四类：

| decision | 含义 | 是否是"知识缺口" |
|---|---|---|
| `kept` | 正常返回证据（3~8 条） | 否 |
| `cross_domain` | 完全跨域，证据为空 → 拒答 | **部分**（问的根本不是本领域，不算缺口） |
| `no_hit` | 检索无任何召回 | **是**（知识库真没有） |
| `insufficient_evidence` | 证据不足 `min_keep` 条 | **是**（有但不够，最值得补） |

而 `telemetry.record_chat()` 的参数里**没有 decision**（`agent_chat_log` 表也没有该列）：

```python
def record_chat(
    self, *, session_id, user_id, permission,
    intent, intent_source, mode, model,       # ← mode 只有 REACT/PLAN（编排模式）
    prompt_tokens, completion_tokens, duration_ms,
    ok, degraded, content_chars, error,
) -> None:
```

**实测验证**（查 `data/metrics/observability.db`，1067 行会话）：

```
mode 分布：  'REACT' 895  |  'PLAN' 172      ← 没有一次拒答判定
intent 分布：'business' 790 | 'rag' 240 | 'chitchat' 37
```

**后果**：drone 现在**答不出来多少、答不出来什么，完全看不见**。`intent='rag'` 的 240 条里，有多少是 `no_hit`/`insufficient_evidence`？无从得知。这正是"数据中台闭环"缺的一环——

```
现在：  会话 → 埋点(无 decision) → 看板（只能看到"有多少次对话"）
应该：  会话 → 埋点(含 decision) → 未命中分析 → 补库建议 → 补语料 → 命中率上升
                                              └──── 闭环在这里断掉 ────┘
```

### 2.3 建议方案（drone 版，比 AgentCraft 更省）

**核心取舍：不新建表，只加一列。**

AgentCraft 用独立 `qa_unanswered` 表 + `uk_question` 去重累加。但 drone 有 AgentCraft 没有的东西——**`agent_chat_log` 已有 `intent` 字段**，且 `decision` 天然属于"这一次会话"的属性。所以：

```sql
-- agent_chat_log 增量迁移（数据库规约 §2.6：只加列，不重建表）
ALTER TABLE agent_chat_log ADD COLUMN rag_decision TEXT DEFAULT '';
```

**为什么不照搬独立表**：
- AgentCraft 建独立表是因为它的 `qa_log` 只存问答原文，没有"链路判定"维度；
- drone 已有会话级明细表 + trace 追溯，**再加一张表会让"一次对话"分散在两处**，违背规约 §2.2 的"事件表优于层级表"取舍；
- 加了这一列后，**未命中分析 = 对 `agent_chat_log` 的一个 GROUP BY 查询**，不需要新增采集点、不需要改采集链路，只在 `record_chat` 多传一个参数。

**聚类用规则而非 LLM**（照搬 AgentCraft 的判断）：未命中问题通常几十到几百条，用关键词+公共字符聚类足够，且**零 token 成本、结果稳定可复现**（LLM 聚类同一批数据两次结果可能不同，不便给运维看）。

**建议落地为三件**：

| 交付物 | 内容 |
|---|---|
| 埋点增量 | `agent_chat_log` 加 `rag_decision` 列（`schema_version` 升 v4）；`record_chat` 补参数 |
| 接口 | `GET /v1/agent/metrics/unanswered`（Python）+ Java 同名透传：返回未命中总数/独立问题数/聚类及补库建议（照 §6 契约） |
| 前端 | 新增「知识缺口」页（AdminLayout 的"运营"分组下），复用现有 `DataPage`/`StatCards`/`BarList`/`PaginationBar` |

**优先级判定建议**（比 AgentCraft 的三档更贴 drone 场景）：

| 优先级 | 判定条件 | 运营动作 |
|---|---|---|
| 高 | `insufficient_evidence` 且同一主题 ≥3 次 | 语料确实缺 → 补文档 |
| 中 | `no_hit` 且同一主题 ≥3 次 | 检查是否该建新分类 |
| 观察 | 出现 1~2 次 | 记录观察，不动 |

**`cross_domain` 要单独排除**——用户问"今天天气"落在跨域拒答里，它不是知识缺口，混进来会让补库建议变成噪音。这是 AgentCraft 没有的细分（它的 `qa_unanswered` 不区分拒答原因）。

---

## 三、B 项详解：巡检补"使用度"维度

### 3.1 对比

| 巡检维度 | AgentCraft | drone | 落差 |
|---|---|---|---|
| 重复文档 | ✅ `detectDuplicateDocs`（算文档间相似度） | ✅ `_detect_duplicate` | 持平 |
| 低质量 Chunk | ✅ `detectLowQualityChunks` | ✅ `_detect_low_quality` | 持平 |
| 过期文档 | ✅ `detectOutdatedDocs`（`outdatedDays`） | ✅ `_detect_stale`（`gov_stale_days`） | 持平 |
| **无人访问文档** | ✅ `detectUnaccessedDocs`（`unaccessedDays`） | ❌ **无** | **drone 缺** |

### 3.2 但先别急着做——前置条件不满足

AgentCraft 能做"无人访问"，是因为它有 **`doc_view_log` 表**：

```sql
CREATE TABLE `doc_view_log` (
  `doc_id` bigint NOT NULL,
  `user_id` bigint DEFAULT NULL,
  `create_time` datetime COMMENT '查看时间',
  KEY `idx_doc_id` (`doc_id`), KEY `idx_create_time` (`create_time`)
)
```

**drone 没有"文档被查看"的埋点**。要注意区别：

- drone **有** `agent_tool_call`（记录了检索工具被调用），但那是**工具级**——只知道"调了 kb_search"，不知道"命中了哪几篇文档"；
- 文档级的访问，需要**检索命中后回写命中 doc_id**。

**所以 B 项的正确做法是两步，且第二步依赖第一步**：

1. 先给 RAG 检索加"命中文档埋点"（`rag_decision=kept` 时记下 top-N 的 `docId`）；
2. 积累一段时间后，才能做"无人访问文档"检测。

**建议**：**B 项排在 A 项之后**，且第 1 步可以和 A 项的埋点改造**同批做**（都在 `record_chat` 附近，一次改完省一次回归）。

⚠️ 另需注意：drone 的知识库有 `owner` 鉴权（public/private 语料），"无人访问"要按 **owner 维度**统计才有意义——某文档对 A 无人访问、对 B 可能天天用。AgentCraft 是单租户的，直接照搬会算错。

---

## 四、不建议搬的部分（附理由）

| AgentCraft 页面 | 不建议理由 |
|---|---|
| 用户管理（135 行） | drone 的用户体系在 **jsdstu-3（8060）**，是外部管控平台；drone 自己建一套等于**双写用户**，权限边界会分叉。现有 `admin`+角色（`permission`）已够用 |
| 公告管理（143 行） | 与数据中台无关，属通用后台功能；本项目无此业务需求 |
| Agent 执行记录（298 行） | drone 的 `agent_tool_call` + `trace 追溯`**已覆盖且更强**——AgentCraft 是 run/step 两级层级表，drone 是"四类事件表 + trace 串起来"，规约 §2.2 已论证过事件表更适合两种读模式 |
| 问答日志（201 行） | 与 drone「会话明细」页**功能重复**，且 drone 版多了 tokens/降级/工具步数 |
| 点赞统计（`message_feedback_stats`） | drone 的 `agent_feedback` 已实现且**更完整**（二值评分 + 内容 TTL + 金标集候选导出），AgentCraft 只有计数 |
| 报表页（581 行） | drone 已对等（窗口切换 + 五个 CSV view + 口径速查）。**唯一可借的是"日期区间选择器"**，但 drone 用 1/7/30 天预设更省事，改成任意区间会引出时区/边界问题，性价比低 |
| 五层架构 / 四级记忆 | 属 Agent 架构而非数据中台；drone 的架构已定型，且规约 §2.2 已说明"不照搬"的取舍理由 |

---

## 五、最终建议：一张推进表

> **落地状态（2026-09-19 下午更新）**：P0 / P1 / P2 **已全部实施并端到端验证**，
> 详见 `DEV_ISSUES.md` §6.60；口径已写入 `docs/specs/数据中台开发规约.md` §4.1/§5/§6。

| 优先级 | 事项 | 落点 | 依赖 | 建议时机 | 状态 |
|---|---|---|---|---|---|
| **P0** | `agent_chat_log` 加 `rag_decision` 列 + `record_chat` 补参数 | agt_python | 无 | **下一批做**，最小改动、最大收益 | ✅ 2026-09-19 |
| **P0** | `GET /metrics/unanswered` 接口 + Java 透传 | agt_python + agt_backend | 上一项 | 同批 | ✅ 2026-09-19 |
| **P1** | 前端「知识缺口」页（未命中聚类 + 补库建议 + CSV） | agt_web | 上一项 | 同批（数据一起来才有得看） | ✅ 2026-09-19 |
| **P2** | RAG 命中文档埋点（为"无人访问"铺路） | agt_python | 可并入 P0 同批改造 | 建议与 P0 合并，省一次回归 | ✅ 2026-09-19（`rag_docs` 列 + `/metrics/docs/usage`） |
| **P3** | 「无人访问文档」巡检维度 | agt_python + 前端 | P2 数据积累 ≥7 天 | 之后 | ⏸ 待数据积累（另需按 owner 维度统计） |
| ❌ | 用户管理 / 公告 / 执行记录 / 问答日志 / 点赞统计 | — | — | **不做** | — |

**一句话总结**：AgentCraft 真正值得搬的是 **「让拒答可运营」** 这一件事——drone 的 RAG 已经会拒答，拒答理由却随水流走了。把它接住（加一列 + 一个接口 + 一页），数据中台才算真正闭环。

### 实施期发现的三处"照搬会出错"的地方（对照 AgentCraft）

1. **不建独立表**：AgentCraft 用 `qa_unanswered` 独立表；drone 加列即可——
   判定本就是"一次对话"的属性，独立成表会让同一轮轨迹拆两处（规约 §2.2）；
2. **聚类不能直接用 bigram + Jaccard**：AgentCraft 的 `isSimilar` 用公共字符数判相似，
   实测中文问句下同主题只能到 0.43，换个长句就掉破 0.25 → 漏聚约 25%。
   改为「实词切段 + 重叠系数」后同主题 0.5~0.88 / 异主题 ≈0；
3. **`cross_domain` 必须排除**：AgentCraft 单租户、问题域窄，无所谓；
   drone 会接闲聊（"今天天气"），不排除的话缺口清单会被闲聊淹掉。

---

## 附：本次调研的核实方式

- **AgentCraft 侧**：读 `AgentCraft学习指南.md`、`frontend/src/pages/admin/*`（11 页共 5091 行）、`src/main/java/**/KnowledgeInspection*`（616 行）、`sql/init.sql` 全部 19 张表结构
- **drone 侧**：读 `app/api/v1/endpoints/{agent,kb,kb_governance,rag}.py` 全部端点、`app/services/telemetry.py` 建表与 `record_chat` 签名、`app/services/inspection_service.py` 巡检三类判定、`app/rag/retriever.py` 的 decision 定义
- **实测**：直查 `data/metrics/observability.db` 确认 `mode` 仅 REACT/PLAN、无 decision 字段（1067 行）

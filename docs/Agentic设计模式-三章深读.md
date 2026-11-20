# 《Agentic 智能体设计模式》三章深读：记忆 · 评估 · 优先级

> 版本：v1.0 ｜ 日期：2026-09-20 ｜ 状态：读书笔记（供选型参考，**不代表立项**）
> 选章理由：这三章对应智能体系统的三个"元能力"——**状态（记忆）、度量（评估）、调度（优先级）**，也是 drone-agent001 当前最有增量空间的三处
> 配套：全书笔记 `docs/Agentic智能体设计模式-精读笔记.md`；差距盘点 `docs/specs/Agentic模式对照-drone现状与落地建议.md`
> 本文件每章末的「对 drone 的可用点」是**候选建议**，立项仍须满足双门槛：真实需求出现 + 评估集能证明变好

---

# 第 8 章 记忆管理 Memory Management

## 1. 章节结构（书中实际小节）

`记忆分类体系 → 实际应用与用例 → 实操代码：Google ADK（Session / State / Memory 三小节）→ 实操代码：LangChain 和 LangGraph → 速览 → 可视化摘要 → 关键要点 → 结论 → 参考文献`

## 2. 记忆的分类体系（本章骨架）

### 2.1 两大类

| 类别 | 存什么 | 存在哪 | 生命周期 |
|---|---|---|---|
| **短期记忆 / 上下文记忆** | 最近消息、智能体回复、工具调用结果、当前轮次的反思 | LLM 上下文窗口（框架层面：ADK `Session`/`State`，LangGraph checkpointer） | 会话内，结束即失 |
| **长期记忆 / 持久记忆** | 跨交互/任务/时间的事实、经历、规则 | 智能体环境**之外**：数据库、知识图谱、**向量数据库** | 持久，靠语义相似度检索回上下文 |

### 2.2 长期记忆的三种细分（本章最有价值的一节）

| 类型 | 记什么 | 书中给的实现方式 |
|---|---|---|
| **语义记忆 Semantic** | 事实、用户偏好、领域知识 | 持续更新的用户**画像（JSON profile）**，或事实文档集合 |
| **情景记忆 Episodic** | 经历、过去的事件与行动；"上次这种任务怎么完成的" | **少样本示例提示（few-shot example prompting）** |
| **程序记忆 Procedural** | 规则、如何执行任务的指令（通常在系统提示里） | **反思（Reflection）**——用当前指令 + 最近交互，让 LLM 提示自己改写指令 |

### 2.3 框架 API 对照（书中出现的具体类名）

| 框架 | 组件 | 作用与关键方法 |
|---|---|---|
| **Google ADK** | `Session`（`google.adk.sessions.Session`） | 独立聊天线程，记录 Event（消息与操作）+ 临时 State |
| | `session.state` | 临时数据字典；前缀 `user:`（跨会话用户级）/ `app:`（应用全局共享）/ `temp:`（仅当前轮次，不持久） |
| | `SessionService` | 会话生命周期：`create_session` / `get_session` / `append_event` / `delete_session`。实现：`InMemorySessionService`（测试）/ `DatabaseSessionService(db_url="sqlite:///./my_agent_data.db")`（持久）/ `VertexAiSessionService`（GCP 生产） |
| | `MemoryService` / `BaseMemoryService` | 长期知识：`add_session_to_memory`（从会话提取入库）/ `search_memory`（检索）。实现：`InMemoryMemoryService`（测试）/ `VertexAiRagMemoryService(rag_corpus=…, similarity_top_k=5, vector_distance_threshold=0.7)` / `VertexAiMemoryBankService` |
| **LangChain** | `ChatMessageHistory` | 链外手动管理：`add_user_message` / `add_ai_message` / `.messages` |
| | `ConversationBufferMemory` | 链内自动注入：`memory_key="history"`（默认）/ `chat_history`，`return_messages`（标准 LLM 用 False，Chat Model 用 True） |
| **LangGraph** | `BaseStore` / `InMemoryStore` | `store.put(namespace, key, value)` / `store.get(...)` / `store.search(namespace, filter=…, query=…)`；`(namespace, key)` 类似文件夹与文件名；支持向量相似度排序（需 `index={"embed": fn, "dims": n}`） |
| | checkpointer | 持久化线程状态，实现**线程范围**的短期记忆恢复 |
| **Vertex AI Memory Bank** | `VertexAiMemoryBankService` | 托管长期记忆：用 Gemini **异步**分析对话历史、提取关键事实与偏好；**智能更新（整合新数据、解决矛盾）**；新会话经全量召回或嵌入相似度检索。每条记忆标记 `USER_ID` + `APP_NAME` |

## 3. 短期记忆（上下文）管理策略

- **问题**：完整历史会挑战上下文窗口，导致错误或性能下降。
- **书中给的做法**：在有限空间内保留最相关信息 —— **总结旧对话片段** + **突出关键细节**。
- **关键澄清**：**长上下文窗口 ≠ 持久记忆**——它只是把短期记忆容量做大，上下文仍临时、会话结束即丢，且每次处理更贵、更低效。
- LangGraph 把它作为 Agent 状态的一部分，用 checkpointer 持久化，可按线程恢复。

## 4. 长期记忆的存取机制

- **存**：外部存储——数据库 / 知识图谱 / 向量数据库（信息转向量，按语义相似度而非精确关键字检索）。
- **检索参数**：ADK `SIMILARITY_TOP_K = 5`、`VECTOR_DISTANCE_THRESHOLD = 0.7`；LangGraph `search(namespace, filter, query)` 内容过滤 + 向量排序；Memory Bank 用全量召回或嵌入相似度搜索。
- **更新**：Memory Bank 会**整合新数据 + 解决矛盾**（书中未展开去重/遗忘算法，属"书中未展开"）。

## 5. 书中代码示例清单

| 示例 | 框架 | 做什么 |
|---|---|---|
| Session 选择 | ADK | `InMemorySessionService` / `DatabaseSessionService(sqlite:///./my_agent_data.db)` / `VertexAiSessionService` 三种持久化取舍 |
| State 更新（`output_key`） | ADK | `LlmAgent(output_key="last_greeting")` → `runner.run()` → `get_session()` 看 `updated_session.state`，Runner 自动在 `append_event` 时用 `state_delta` 落状态 |
| State 更新（工具内） | ADK | `log_user_login(tool_context: ToolContext)` 内改 `user:login_count` / `task_status` / `user:last_login_ts` / `temp:validation_needed` |
| 长期知识 | ADK | `InMemoryMemoryService`（测试）vs `VertexAiRagMemoryService(similarity_top_k=5, vector_distance_threshold=0.7)`（生产） |
| 手动历史 | LangChain | `ChatMessageHistory` 手动 add_user/add_ai |
| 自动记忆 | LangChain | `ConversationBufferMemory(memory_key="chat_history", return_messages=True)` + `LLMChain.predict()` 多轮 |
| **程序记忆（反思改指令）** | LangGraph | `update_instructions(state, store)` 节点：取 `("instructions",)` 当前指令 → 让 LLM 反思对话生成新指令 → `store.put(("agent_instructions",), "agent_a", {...})`；`call_model` 节点 `store.get()` 后格式化 prompt |
| 存取搜索 | LangGraph | `InMemoryStore(index={"embed": embed, "dims": 2})` + `put/get/search(filter, query="language preferences")` |
| Memory Bank | ADK | `VertexAiMemoryBankService(project, location, agent_engine_id)` → `add_session_to_memory(session)` 自动存储 |

## 6. 坑与边界（逐条）

1. **绝不直接改 `session.state` 字典**：会绕过事件机制 → 不写入事件历史、可能不被持久化、并发问题、时间戳不更新。必须用 `output_key`（仅限最终文本响应）或 `append_event` 时带 `EventActions.state_delta`；`session.state` 只用于读。
2. **长上下文 ≠ 持久记忆**（见 §3）。
3. **State 设计原则**：保持简单、用基本类型、键名清晰并用对前缀、避免深嵌套、始终通过 `append_event` 更新。
4. **`InMemory*` 只适合测试**：应用重启即丢数据；生产必须换 `DatabaseSessionService` / `VertexAiRagMemoryService` / 数据库后端 store。
5. **`return_messages` 取值**：标准 LLM 用 False（格式化字符串），Chat Model 用 True（消息对象列表）。
6. **Memory Bank 跨框架**：与 ADK 开箱即用，LangGraph/CrewAI 需直接调 API（书中未给代码）。

## 7. Key Takeaways（12 条浓缩）

1. 记忆对处理事务、学习、个性化都关键。
2. 对话式 AI = 单会话即时上下文的**短期记忆** + 跨会话持久知识的**长期记忆**。
3. 短期记忆临时，受上下文窗口或框架传递方式限制。
4. 长期记忆用外部存储（向量库等）跨会话保存，靠**搜索**访问。
5. ADK 的组件：`Session`（聊天线程）/ `State`（临时数据）/ `MemoryService`（可搜索长期知识）。
6. `SessionService` 管会话全生命周期（events + state）。
7. `session.state` 前缀 `user:`/`app:`/`temp:` 决定归属与是否持久。
8. 状态更新要用 `EventActions.state_delta` 或 `output_key`，**不要直接改字典**。
9. `MemoryService` 负责写入长期存储并支持检索，通常以工具形式暴露。
10. LangChain 的 `ConversationBufferMemory` 自动把历史注入提示。
11. LangGraph 的 store 能存**语义事实、情景经验、甚至可更新的程序规则**。
12. Memory Bank 是托管服务，自动提取/存储/召回用户特定信息，跨 ADK / LangGraph / CrewAI 提供个性化。

## 8. 对 drone 的可用点

| 可用点 | 我们的现状 | 建议 |
|---|---|---|
| **记忆三分类的"分类学"** | M-5 画像 = **语义记忆** ✅；会话轮次 ≈ 情景记忆（但是原文存储，非 few-shot） | 不必改。可在 `会话记忆规约.md` 注明我们自己的一层映射，便于对外解释 |
| **程序记忆（Reflection 改指令）** | **没有** | 候选：新增"指令记忆"——把用户/运营给 Agent 的偏好与修正（如"3 号机相关回答必须给处置步骤"）存为指令补丁，每轮注入 system 区。**需评估集证明优于纯画像注入** |
| **State 更新禁忌** | 我们无 `state` 概念（自研 store） | 无需适配；但"**不要绕过事件机制直接改存储**"这条原则我们已遵守（所有写入走 `UserProfileStore.upsert` / `record_turn`），可在代码注释里点明这条依据 |
| **检索参数（top_k / 距离阈值）** | M-5 是 KV 全量注入（画像条数少），无相似度检索 | 画像条数增长后（>50 条）可考虑按相似度筛选后注入，**当前不需要** |
| **"长上下文 ≠ 持久记忆"** | 我们的定位本就是"持久化 + 裁剪"，书上这条印证了 M-1/M-2 的设计前提 | 记为设计依据 |

---

# 第 19 章 评估和监控 Evaluation and Monitoring

## 1. 章节结构

`章首导言（区别于第 11 章目标设定、第 17 章推理机制：本章侧重持续、外部的效能/效率/合规测量）→ 实际应用与用例 → 实践代码示例（四个子主题：响应评估 / 延迟监控 / token 追踪 / LLM-as-a-Judge 评"有用性"）→ 评估方法对比表 → **智能体轨迹 → 多智能体 → 从智能体到高级承包商** → Google 的 ADK → 速览 → 可视化摘要 → 关键要点 → 结论 → 参考文献`

## 2. 为什么智能体评估比传统软件测试难（逐条）

1. **概率性/非确定性**：传统代码是可预测的通过/失败，智能体要同时做**定性**评估（最终输出 + 轨迹）。
2. **多智能体系统持续变化（constantly in flux）**：要超越个体性能，去衡量沟通与团队协作有效性；环境非静态，测试用例须随时间自适应。
3. **因素众多**：完整评估框架"复杂性堪比一门学术学科"（模型性能、用户交互、道德、社会影响）。
4. **问题多在部署后才出现**：数据漂移、意外交互、工具调用异常、偏离目标。
5. **实时环境要求**：传统测试无法保障动态环境下的可靠性。

## 3. 评估维度与指标清单

| 指标 | 衡量什么 | 书中方法/细节 |
|---|---|---|
| **响应准确性** | 是否给出期望答案 | 大小写无关 + 去空格的精确匹配，返回 1.0/0.0；**书中明确指出它不考虑语义等价**（"Paris is the capital of France." vs 反向表述会误判 0.0） |
| **字符串相似度** | 文本相近程度 | Levenshtein 距离、Jaccard 相似性 |
| **关键词分析** | 关键内容有无 | 特定关键词存在/缺失 |
| **语义相似度** | 意思是否相近 | 嵌入向量 + 余弦相似度 |
| **LLM-as-a-Judge** | 细微正确性、主观品质（helpfulness） | 用 LLM 按评分标准（rubric）打分 |
| **RAG 专属** | 答案是否忠于资料、是否切题 | 忠实性 faithfulness、相关性 relevance |
| **质量子项** | 答案综合质量 | 事实正确性、流畅性、语法精确性、对用户目的的遵守 |
| **延迟监控** | 处理+生成耗时 | 必须**持久化**到 JSON 日志 / 时序库（InfluxDB、Prometheus）/ 数仓（Snowflake、BigQuery、PostgreSQL）/ 可观测平台（Datadog、Splunk、Grafana Cloud） |
| **token 用量** | 成本 | 跟踪输入/输出 token 以控成本、优化提示词 |

**评估方法对比（书中给的权衡）**：LLM-as-a-Judge 可能**忽略中间步骤**且受评估模型自身能力限制；人工评估**难扩展、贵、耗时**；自动化指标**覆盖能力有限**。

## 4. 智能体轨迹评估（本章重点）

**评什么**：轨迹 = 智能体"**达到解决方案所采取的步骤序列**"。评估覆盖决策质量、推理过程与整体结果；具体看**工具选择、策略、任务效率**。与"只看最终答案"的区别在于——**只评结果无法暴露步骤层面的错误与低效**。

**做法**：把实际操作序列与**理想/真实（expected / ground truth）轨迹**逐环节比对。

**书中示例的理想轨迹**（客户产品查询）：意图确定 → 数据库搜索工具使用 → 结果审查 → 报告生成。

**全部匹配方法与指标**：

| 方法 | 判什么 | 怎么用 |
|---|---|---|
| **精确匹配 exact match** | 与理想序列完全一致 | 任何偏差都判错；**高风险场景**用 |
| **按序匹配 in-order match** | 正确操作按正确顺序出现 | **允许额外步骤** |
| **任意顺序匹配 any-order match** | 正确操作出现即可 | **允许额外步骤**，更灵活 |
| **精确度 precision** | 预测出的操作有多少是相关的 | — |
| **召回率 recall** | 捕获了多少个**基本/必要操作** | — |
| **单工具使用 single-tool use** | 是否出现了某个特定操作 | 检查关键工具是否被调用 |

指标选择取决于具体需求：高风险场景要求精确匹配，灵活场景可用按序/任意顺序匹配。

**多智能体场景评什么（四条）**：

1. **是否有效合作** —— 如"航班预订智能体"是否把正确日期/目的地传给"酒店预订智能体"（否则酒店订错周）
2. **是否制定了好计划并坚持执行** —— 如酒店在航班确认前订房即偏离计划；或卡住无休止搜索"完美"租车而停滞
3. **是否为任务选对了智能体** —— 问天气却走了"通用知识智能体"（给泛化答案而非实时数据）
4. **增加更多智能体是否真提升性能** —— 新增角色让规划更好，还是造成冲突、拖慢系统（暴露可扩展性问题）

**两级测试文件**：

| | test 文件 | evalset 文件 |
|---|---|---|
| 粒度 | 单会话、多回合 | 多会话（可很长） |
| 用途 | **单元测试**（活跃开发期，快速执行） | **集成测试**（模拟复杂多轮对话） |
| 内容 | 用户查询 + 预期工具使用轨迹 + 中间响应 + 最终响应 | 多个 eval，每个是一次会话、含多个 turn |
| 例 | "关闭卧室 device_2" → `set_device_info(location=Bedroom, device_id=device_2, status=OFF)` → 预期响应"I have set the device_2 status to off." | 先"你能做什么？"再"掷两次 10 面骰子并检查 9 是否质数" → 预期 `roll_die` + `check_prime` 调用 |

**一句话**：**单会话用 test 文件，多会话用 evalset 文件。**

## 5. 评估工具与实践

| 工具/平台 | 干什么 |
|---|---|
| `adk web` | Web UI 交互式评估 + 数据集生成（可把会话保存进 eval 集，显示评估状态） |
| `pytest` 集成 | 调 `AgentEvaluator.evaluate` 指定智能体模块与 test 文件路径，作为集成测试**进 CI/CD** |
| `adk eval` | 命令行批量评估（适合定期构建/验证），可指定配置文件、打印详细结果、只跑指定 evals |
| 可观测落地 | JSON 日志 / InfluxDB / Prometheus / Snowflake / BigQuery / PostgreSQL / Datadog / Splunk / Grafana Cloud |
| LLM-as-a-Judge | 用 `google.generativeai` 接 Gemini 做评估者 |

## 6. 书中代码示例

| 示例 | 库 | 做什么 |
|---|---|---|
| `evaluate_response_accuracy` | 纯 Python | 归一化后精确比对，返回 1.0/0.0；书中自陈缺陷：不解语义等价 |
| `LLMInteractionMonitor` | 纯 Python（占位） | 累计 input/output token 计数；真实场景应用 LLM 的 tokenizer |
| `LLMJudgeForLegalSurvey` | `google.generativeai`（Gemini） | 按 5 维 rubric（清晰与精确 / 中立与偏见 / 相关与焦点 / 完整性 / 受众适当性，各 1~5 分）给法律调查问题打分，`temperature=0.2`，要求 JSON 输出（overall_score / rationale / detailed_feedback / concerns / recommended_action）；含三种错误处理（安全过滤空响应 / JSON 解析失败 / 其他异常） |

## 7. 坑（逐条）

1. 精确字符串匹配对**释义/语义等价**失效。
2. 简单比较不足以评估语义相似，需要更高级 NLP 技术。
3. LLM-as-a-Judge 可能忽略**中间步骤**，且受评估模型能力限制。
4. 人工评估难扩展、贵、耗时；纯自动化指标覆盖不全。
5. 环境非静态，测试用例须随时间调整。
6. **延迟只打控制台不落库 = 没有监控**。
7. token 示例是概念占位（`split()` 计数），真实要用 API 的 tokenizer。

## 8. Key Takeaways 与"承包商"模型

**Key Takeaways**：① 评估超越传统测试，要在真实环境持续测有效性/效率/合规；② 应用含实时性能跟踪、A/B 测试、合规审计、漂移与异常检测；③ 除响应准确性还需延迟与 token 监控；④ **轨迹对评估至关重要**（操作 vs 理想路径）；⑤ ADK 用 test（单元）/ evalset（集成）文件提供结构化评估；⑥ 执行方式有三：Web UI / pytest 进 CI / 命令行自动化；⑦ 高风险任务需要从"提示词"走向"正式合约"；⑧ 多智能体重点转向评估协作成功。

**⭐ 承包商模型（范式论断）**——作者引 Agent Companion 提出智能体应演化为受**正式合约**约束的**承包商**：

1. **形式化合约**：交付物、规格、可接受数据源、范围、预期成本与完成时间 → 结果客观可验证
2. **协商与反馈生命周期**：合约是对话起点，智能体可分析条款、标记歧义与风险
3. **质量优先的迭代执行**：优先正确性，自我验证（如代码合约先出多方案、跑合约内单测、按性能/安全/可读性评分，只提交全通过版本）
4. **子合约的层次化分解**：主承包商当项目经理，把大目标拆成子合约分配给专门智能体

作者对现状的诊断（原文）："当今常见的 AI 智能体以**简短的、规格不明确的指令**运行，使其适合简单演示，**但在生产中很脆弱，因为歧义会导致失败**。"

## 9. 对 drone 的可用点

| 可用点 | 我们的现状 | 建议 |
|---|---|---|
| **轨迹评分口径**（精确/按序/任意序 + precision/recall/单工具） | `trace_detail` 已还原七段链路与四色标注，但**只呈现不评分** | **最有价值的一条**：给 trace 加"理想轨迹 vs 实际轨迹"比对。理想轨迹可由人工标注若干典型问题（如"3 号机告警查询"应走 意图→查询工具→回答）。可先离线跑，避免污染在线链路 |
| **"允许额外步骤"的匹配策略** | — | 我们的链路允许多次工具调用，用**按序匹配**比精确匹配更贴合；高风险操作（写类）才用精确匹配 |
| **延迟与 token 落库** | 已在四类事件表落 tokens 与耗时 ✅ | 书上这条印证我们的做法；可补"延迟 P95"到 DWS 聚合 |
| **LLM-as-a-Judge 的 rubric 与 JSON 输出范式** | RAG 评审员已用类似模式（judge 判 verdict） | 可借鉴其**五维 rubric + JSON 结构 + temperature=0.2** 的参数选择（评估要确定性） |
| **test / evalset 两级** | 有 `questions.jsonl` 单轮评估集 | 可补 **evalset 式多轮会话集**（多轮追问下的记忆与过滤效果，正是 M-2/M-3 的验收盲区） |
| **多智能体四问** | 子代理/专家委派已上线，**未评过协作质量** | 可作为委派质量的检查清单（交接参数是否正确、是否选对专家、加子代理是否真变快） |
| **承包商模型** | 与我们无关（面向高风险交付） | 仅作知识储备；其中"**规格不明确的指令在生产中脆弱**"这条对我们的系统提示与工具描述写法有借鉴 |

---

# 第 20 章 优先级排序 Prioritization

## 1. 章节结构

`章首导言 → 优先级排序模式概述 → 实际应用和用例 → 实践代码示例 → 速览（What/Why/Rule of thumb/可视化摘要）→ 关键要点 → 结论 → 参考文献`

## 2. 定义与要解决的问题（原文表述）

- **定义**："智能体使用优先级排序来有效管理任务、目标和子目标，指导后续行动……在处理多个需求时做出明智决策，优先处理重要或紧急的活动。"
- **问题**：复杂动态环境中智能体面对"**大量潜在行动、相互冲突的目标和有限的资源**"；没有确定下一步行动的明确方法，"可能变得低效且效果不佳，甚至导致严重的操作延迟或完全无法完成主要目标"。
- **Rule of thumb**：当智能体必须在**资源约束**下自主管理**多个（常相互冲突的）目标**、且环境动态时才需要它。

## 3. 六个判据

| 判据 | 书中定义 |
|---|---|
| **紧急性 urgency** | 任务的时间敏感性 |
| **重要性 importance** | 对主要目标（primary objective）的影响 |
| **依赖关系 dependencies** | 该任务是否是其他任务的前提 |
| **资源可用性 resource availability** | 必要工具或信息是否就绪 |
| **成本/收益分析** | 投入 effort 与预期成果 expected outcome 的对比 |
| **用户偏好** | 面向个性化智能体 |

## 4. 三个作用层级

1. **高层次目标优先级排序**（选总体目标）
2. **子任务优先级排序**（计划内排序步骤）
3. **行动选择**（从可用选项中选下一个即时行动）

书中补充："这反映了人类团队的组织方式，其中管理者通过考虑所有成员的输入来优先处理任务。"

## 5. 实现四要素

1. **标准定义**：建立评估规则或指标（见 §3 的六判据）
2. **任务评估**：按标准评估每个潜在任务，方法是"**从简单规则到 LLM 的复杂评分或推理**"
3. **调度或选择逻辑**：选最佳下一步的算法，可能用**队列**或**高级规划组件**
4. **动态重新优先级排序**：新关键事件出现、截止日期临近时重排，确保适应性与响应能力（书中未给触发阈值清单）

## 6. 代码示例细节

- **框架**：Python + **LangChain**（`create_react_agent` + `AgentExecutor` + `ChatPromptTemplate` + `Tool` + `ChatOpenAI(gpt-4o-mini, temperature=0.5)` + `ConversationBufferMemory(memory_key="chat_history", return_messages=True)`）
- **智能体角色**：Project Manager（项目经理智能体）——负责"任务的创建、优先级排序和分配给团队成员"
- **Pydantic 模型**：
  - `Task`：`id` / `description` / `priority: Optional[str]`（P0/P1/P2）/ `assigned_to: Optional[str]`
  - `CreateTaskArgs(description)`、`PriorityArgs(task_id, priority)`（校验 ∈ {P0,P1,P2}）、`AssignWorkerArgs(task_id, worker_name)`
- **工具清单（4 个）**：`create_new_task`（先建任务拿 `task_id`）→ `assign_priority_to_task`（校验优先级）→ `assign_task_to_worker` → `list_all_tasks`
- **底层实现**：`SuperSimpleTaskManager`（内存字典 `Dict[str, Task]`，O(1) 查找，`create_task` / `update_task`（用 `model_copy`）/ `list_all_tasks`），任务 ID 形如 `TASK-001`
- **优先级等级与默认策略**：P0/P1/P2；紧急词（urgent / ASAP / critical）映射 P0；信息缺失时**合理补全**为 P1 + 'Worker A'；可用 worker：Worker A / Worker B / Review Team
- **流程**：① 建任务取 id → ② 分析请求定优先级并分派 → ③ 信息缺失按默认补全 → ④ `list_all_tasks` 输出最终状态；示例用 `run_simulation`（异步 `ainvoke`）跑两个场景（紧急新登录系统派 Worker B；非紧急"审查营销网站内容"信息少）

## 7. 用例（逐条）

| 场景 | 排序逻辑 |
|---|---|
| 自动化客户支持 | 系统停机报告优先于密码重置；高价值客户优先 |
| 云计算 | 高峰把资源给关键应用，非紧急批处理挪到非高峰以优化成本 |
| **自动驾驶** | **为避免碰撞而刹车** 优先于保持车道纪律、优化燃油效率 |
| 金融交易 | 按市场条件、风险承受、利润率、实时新闻优先执行 |
| 项目管理 | 按截止日期、依赖关系、团队可用性、战略重要性排序 |
| 网络安全 | 按威胁严重性、潜在影响、资产关键性分级告警 |
| 个人助理 | 按用户定义重要性、临近截止、当前上下文组织提醒 |

## 8. 坑与边界

- 书中未设专门"陷阱"小节，可提炼的边界：① **无约束/单任务场景下该模式价值有限**（只在资源受限、时间有限、目标冲突时"尤为相关"）；② **没有触发机制的动态重排会失效**（须响应新事件与截止期）；③ **工具参数必须校验**（示例用 Pydantic 限制优先级取值，否则返回 "Invalid priority"）；④ 其他常见问题（优先级惯性、指标博弈）**书中未展开**。

## 9. Key Takeaways 与独特论断

**Key Takeaways**：① 使智能体在复杂多维环境有效运作；② 依据紧急性、重要性、依赖等标准评估排序；③ **动态重新优先级排序**让智能体随实时变化调整焦点；④ 排序发生在从战略目标到即时战术的各层级；⑤ 有效排序提升效率与操作稳健性。

**⭐ 独特论断（结论段）**："这种**自我管理工作流程**的能力，正是**真正的智能体系统与简单自动化脚本的区别所在**。" 另称优先级排序是"有效智能体 AI 的基石"，使系统从被动执行升为主动战略决策者。

## 10. 对 drone 的可用点

| 可用点 | 我们的现状 | 建议 |
|---|---|---|
| **六判据** | 无排序概念 | 作为未来设计清单；其中"**依赖关系**"与"**资源可用性**"两条对我们的批处理场景（批量语料入库 / 巡检工单）最相关 |
| **"用户偏好"入判据** | M-5 画像已存在 | 若做排序，用户偏好可直接取画像字段（现成的数据源，书里没有） |
| **LLM 评分 vs 规则** | — | 建议**规则优先**（紧急词表 + 优先级字段），LLM 只用于歧义补全；纯 LLM 打分引入成本与不确定性 |
| **示例的 Pydantic 参数校验** | 我们的工具已有参数校验惯例 | 印证我们的做法（`app/services/tools/__init__.py` 的 parameters 定义） |
| **是否立项** | — | **第二档（需先决条件）**：等真实出现"多任务并发 + 资源冲突"（如批量入库与在线问答争抢 LLM 配额）。当前单轮问答为主，**不立项** |
| **动态重排的标志性论断** | — | 引用价值：日后向他人解释"为什么要做任务调度"时，可用"自我管理工作流是 agent 与脚本的分界"这一表述 |

---

# 附：三章合看的一条主线

```
第 8 章 记忆  →  状态：系统记得什么（短期/长期、语义/情景/程序）
第 19 章 评估 →  度量：系统做得对不对（答案 + 轨迹 + 延迟 + 成本）
第 20 章 优先级 → 调度：系统先做什么（多目标 + 资源约束下的自主排序）
```

这三者是**同一层能力的三个面**：没有记忆就没有被评估的"历史"，没有评估就无法判断排序策略是否变好，没有排序则在多任务时无法自主运行。

**对 drone-agent001 的映射**：

- 记忆 → M-1~M-5 已完整，仅差"程序记忆"（候选）
- 评估 → 数据中台已提供原料（四类事件表 + trace），差"轨迹评分"（低成本、建议近期做）
- 优先级 → 完全空缺，但**当前无场景**，列为第二档

**一句话**：三章里最值得立刻动手的只有一件——**给 trace 加轨迹评分口径**；其余两条要么等评估集证明、要么等真实场景，都不该因为"书上写了"就开工。

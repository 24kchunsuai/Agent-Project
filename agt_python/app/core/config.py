"""全局配置。

所有可变参数都从环境变量 / .env 读取，禁止在业务代码里硬编码。
新增配置项时同步更新 .env.example。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.build_info import read_version_file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_name: str = "agt-python-agent"
    # 版本号唯一来源是仓库根 VERSION（企业级落地规约 E-1）。
    # 默认值在此求值，仅调试时可被环境变量 APP_VERSION 覆盖；
    # 更完整的构建画像（gitCommit / buildTime / 工作区是否脏）见
    # app/core/build_info.py，并经 /v1/health 对外暴露。
    app_version: str = read_version_file()
    debug: bool = True
    # 只服务本机 Java（企业级落地规约 S-1）：8000 绑回环，外网不可达。
    # 需要从别的机器直连调试时**临时**用命令行覆盖（--host 0.0.0.0），
    # 但那样 X-Agent-Key 就是唯一防线，且必须同步改计划任务。
    host: str = "127.0.0.1"
    port: int = 8000
    # 与 Java 侧保持一致，不带 /api 前缀
    api_prefix: str = "/v1"

    # ---------- 服务间信任边界（企业级落地规约 S-1 / E-10）----------
    # Java(8080) → 本服务的**全部**请求必须带请求头 X-Agent-Key，值与本项全等。
    # **留空则服务拒绝启动**（fail-closed）：防"忘了配密钥就上线"——那种情况下
    # 鉴权中间件会形同虚设，而报错要到运维发现异常流量时才会暴露。
    # 生成：python -c "import secrets; print(secrets.token_urlsafe(32))"
    # 两侧必须同值：Python 写 .env，Java 用环境变量 AGENT_SHARED_KEY 注入。
    agent_shared_key: str = ""

    # 上一把密钥（企业级落地规约 E-10 密钥轮换的**过渡窗口**）。
    #
    # 语义：轮换期间本服务**同时接受** `agent_shared_key` 与 `agent_shared_key_prev`
    # 两把；轮换结束后把本项清空并重启，旧密钥立刻失效。
    #
    # ⚠️ 为什么必须有这一项：本服务是**非 reload** 部署（改配置必须重启），
    # 而 Java 侧重启也要时间。若只支持一把密钥，轮换时必然出现
    # "一边已换、一边未换" 的窗口——那个窗口里全链路 4010。双密钥把
    # "无缝轮换" 变成配置问题，而不是调度问题。
    #
    # ⚠️ 轮换顺序是**先消费者后生产者**（见 `docs/密钥与配置管理.md`）：
    #   ① 本服务先配 (新=agent_shared_key, 旧=agent_shared_key_prev) 并重启 → 两把都收
    #   ② 再改 Java 的 AGENT_SHARED_KEY 为新值并重启 → Java 开始用新密钥
    #   ③ 观察 `agt_degrade_events_since_start_total{path="auth_key_legacy_used"}`
    #      不再增长后，清空本项并重启 → 旧密钥失效
    # 反过来做（Java 先换）会在第 ②、③ 步之间稳定 4010。
    agent_shared_key_prev: str = ""

    # ---------- 数据目录 ----------
    # 落盘数据（治理账本 / 回收站与停用区备份 / 上传源文件）的根目录由**环境变量
    # `DATA_ROOT`** 决定，默认 `data`（即 agt_python/data，产品目录）。
    #
    # 它刻意**不是** Settings 字段，也**不能写在 .env 里**（本文件的 .env 由 pydantic 解析，
    # 而 DATA_ROOT 只被 `app/core/paths.py` 从进程环境变量读取）——路径必须在 import app
    # 之前就定下来，才能实现测试隔离。正确用法：
    #     DATA_ROOT=/tmp/xxx python -m uvicorn app.main:app        # 临时换数据目录
    #     python scripts/serve_isolated.py --port 8001            # 起隔离服务（推荐）
    # 验收脚本的数据目录守卫见 `app/core/test_guard.py`。

    # ---------- 跨域 ----------
    # Java 后端(8080) 是服务端调用，不涉及跨域；这里是为 Vue(9090) 直连调试准备的
    cors_origins: list[str] = [
        "http://localhost:8080",
        "http://localhost:9090",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:9090",
    ]

    # ---------- 大模型（对话）----------
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60
    # E-13 成本计量：价目表 JSON，单位 **元 / 百万 token（CNY）**，键 = 模型名。
    # 例：{"deepseek-chat": {"prompt": 2.0, "completion": 8.0}}
    # 解析失败/行不合法按收敛处理（丢行或置 0），**不会拒绝启动**——见 costing.parse_prices。
    llm_price_json: str = ""
    # E-13 配额：单用户**每日** token 上限（prompt+completion，本地日）；0 = 不限。
    # 超限不拒绝服务，只在响应挂提示 + 落 fault_event（每用户每日至多一条）。
    quota_user_daily_tokens: int = 0

    # ---------- 向量模型（RAG 阶段启用）----------
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "embedding-3"
    embedding_dim: int = 2048          # GLM embedding-3 实测维度
    embedding_batch: int = 32          # 单批条数（过大易触发 429）
    embedding_sleep: float = 0.6       # 批次间隔秒数，限流保护

    # ---------- 重排模型 ----------
    # 必须用 rerank-pro：基础版 rerank 给无关文档打 0.86~0.95 分，
    # 分数挤在 0.99999~1.0，任何阈值都失效（实测见 DEV_ISSUES 6.6）
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = "rerank-pro"

    # ---------- 向量库（Milvus）----------
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "kb_drone"

    # ---------- 语料治理（Corpus Governance，见 docs/specs/语料治理规约.md）----------
    # 治理元数据库（SQLite）：私有状态，只有 agt_python 读写；空 = data/governance/governance.db
    gov_enabled: bool = True
    gov_db_path: str = ""
    # L1（文件指纹）/ L2（切片指纹）去重门
    gov_l1_enabled: bool = True
    gov_l2_enabled: bool = True
    # L2 命中时复用已有切片的 dense 向量，跳过 embedding（需同 embedding 模型）
    gov_reuse_vector: bool = True
    # L3 语义查重阈值（G2 恢复确认门 / G4 巡检）
    gov_l3_threshold: float = 0.92
    # 回收站 TTL（天）：超期自动彻底删除（G4）
    gov_ttl_days: int = 30
    # 回填时源文件搜索根目录（相对 agt_python/，逗号分隔）
    gov_source_roots: str = "data/kb,data/kb/raw,data/kb/external"

    # ---- 常驻任务总闸（对账 / 巡检 / TTL 的"定时"执行）----
    # false = 不挂载三个常驻循环；**功能本身照旧可用**（手动 API 与 Java 透传不受影响）。
    # 它刻意与 gov_reconcile_enabled / gov_inspection_enabled / gov_ttl_enabled 分开——
    # 那三个是"功能总开关"，关掉会连手动接口一起停用；本开关只停"自动跑"。
    # 用途：验收/演示要确定性结果时，避免后台任务在断言窗口里自己开单或清理
    # （G4 实测：隔离服务启动 120s 后巡检首次执行，把"dryRun 前后 openCount 不变"打成假失败）。
    gov_scheduler_enabled: bool = True

    # ---- G2 生命周期强化 ----
    # 停用/启用（DISABLED 状态）：停用同样物理删向量，备份落 data/kb/disabled/
    gov_disable_enabled: bool = True
    # 恢复前的 L3 语义查重确认门：命中相似文档要求 confirm=true
    gov_restore_l3_gate: bool = True
    # 彻底删除只允许管理员（Java 层注入 is_admin，Python 侧再兜一道）
    gov_purge_require_admin: bool = True
    # 彻底删除的二次确认：必须 confirm=true 且 confirmDocName 与文档名一致
    gov_purge_require_confirm: bool = True
    # 备份是否随身带 dense 向量（恢复/启用走"回灌"，零 embedding；关闭则回落重新切片）
    gov_backup_include_dense: bool = True

    # ---- 向量对账任务（G2）----
    gov_reconcile_enabled: bool = True
    gov_reconcile_interval_sec: int = 300          # 5 分钟一轮（spec §7）
    gov_reconcile_initial_delay_sec: int = 60      # 启动后先等 1 分钟，避开冷启动
    gov_reconcile_batch: int = 50                  # 单轮处理的残留文档上限（防雪崩）

    # ---- G3 版本链 ----
    # 版本链总开关：关闭后 /versions 与 /rollback 均不可用（同名上传行为回到 G2 的"直接覆盖"）
    gov_version_chain_enabled: bool = True
    # 同名覆盖时是否把旧版自动快照入回收站（关掉则旧版仍直接蒸发，仅保留 VERSION_REPLACE 审计）
    gov_version_snapshot_enabled: bool = True
    # 回滚确认门：必须 confirm=true 且 confirmDocName 与文档名一致（与彻底删除同形）
    gov_rollback_require_confirm: bool = True
    # 回滚只允许管理员：回滚会换掉**在库的可检索内容**（影响全体使用者，属治理动作）
    gov_rollback_require_admin: bool = True
    # 版本快照保留上限（每文档）：超出的最老快照自动清理，防"每天覆盖一次"无限堆积
    gov_version_keep: int = 10

    # ---- G4 巡检工单化 ----
    # 巡检总开关（关闭则 /inspection/scan 与定时巡检均不产生工单，已有工单仍可处理）
    gov_inspection_enabled: bool = True
    gov_inspection_interval_sec: int = 3600        # 1 小时一轮（spec §7）
    gov_inspection_initial_delay_sec: int = 120    # 启动后先等 2 分钟，避开冷启动
    # STALE 判据：updated_at 早于"现在 - N 天"的 ACTIVE 文档
    gov_stale_days: int = 180
    # LOW_QUALITY 判据：存在 char_len < 此值的切片
    # 必须落在切片器的 min_section(40) 与 min_chunk(100) 之间，否则对真实上传永不命中：
    # 切片器对 <40 字符的段落并入前一片（更短片不会产生）、对 <100 的段落先尝试双向合并，
    # 故真实语料里**不存在** <40 的切片——阈值取 10 时该检查等于空转（实测 char_len<10 恒为 0 条）。
    # 取 50 才能检出"合并后仍偏短"的段落（2026-09-18 由 10 调整为 50，见规约 §17.3）。
    gov_low_quality_chars: int = 50
    # 单轮巡检的文档上限（L3 语义巡检要逐篇取向量做检索，防一轮扫太多拖垮服务）
    gov_inspection_docs: int = 300
    # L3 巡检每篇取样的切片数（越多越准、越慢）
    gov_inspection_samples: int = 3

    # ---- G4 TTL 清理 ----
    gov_ttl_enabled: bool = True
    gov_ttl_interval_sec: int = 3600               # 1 小时一轮（spec §7）
    gov_ttl_initial_delay_sec: int = 180
    gov_ttl_batch: int = 20                        # 单轮彻底删除的文档上限（防雪崩）

    # ---------- 切片参数（实测标定，见 docs/切片方案与元数据设计.md）----------
    rag_chunk_size: int = 400          # 最终片长硬上限（预算制）
    rag_chunk_overlap: int = 80        # 相邻片重叠字符数
    rag_chunk_min: int = 100           # 低于此长度的段落尝试合并

    # ---------- 检索参数 ----------
    rag_recall_topk: int = 20          # 混合检索候选数
    rag_rerank_topn: int = 5           # 重排后取前 N（收益递减点）
    rag_min_keep: int = 3              # 过滤保底条数
    rag_max_keep: int = 8              # 过滤封顶条数
    rag_score_floor: float = 0.01      # top1 低于此值判为「完全跨域」→ 硬兜底

    # ---------- RAG 问答 ----------
    rag_judge_enabled: bool = True     # LLM judge 评审证据充分性（领域内无答案兜底）

    # ---------- 查询改写（Query Rewrite，DEV_ISSUES 6.37）----------
    # 检索前把用户口语/带指代的消息改写成规范查询。总开关关掉即完全退回原文检索。
    rag_rewrite_enabled: bool = True        # 总开关（本地词典 + LLM 两层）
    rag_rewrite_llm_enabled: bool = True    # 是否允许 LLM 层（关掉只留本地词典，零成本）
    rag_rewrite_temperature: float = 0.0    # 改写要确定性，不要创造力
    rag_rewrite_max_tokens: int = 120       # 改写输出很短（一句查询），给足即可
    rag_rewrite_max_chars: int = 80         # 改写结果字符上限，超限视为不合格回退原文
    rag_rewrite_history_turns: int = 4      # 送给改写模型的历史条数（user+assistant 计）

    # ---------- 业务系统（jsdstu-3 无人机后端）----------
    # 智能体业务工具调用的目标系统；鉴权由 Java 层完成（用户自己的 token 注入），
    # Python 侧不做登录，token 过期时把 4010 原样抛回给用户提示重新登录
    jsdstu_base_url: str = "http://localhost:8060"
    jsdstu_timeout: int = 10           # 业务接口读超时（查询类接口应当很快）
    jsdstu_upload_timeout: int = 120   # 媒体上传专用读超时（文件可达 100MB，见规约 §5.1）
    agent_loop_max_steps: int = 6      # AgentLoop 单轮最多工具调用次数（熔断）
    # ---- 容错（docs/specs/全链路容错规约.md §10）----
    agent_loop_same_call_max: int = 3  # 同指纹（同工具+同参数）第 N 次拒绝执行，第 N-1 次提醒
    agent_tool_fail_max: int = 3       # 单工具连续失败熔断阈值（会话级连续计数，成功清零）
    agent_tool_timeout: int = 15       # 工具执行 wait_for 上限（秒）；media_upload 覆盖为 jsdstu_upload_timeout
    agent_fingerprint_window: int = 0  # 指纹滚动窗口大小，0=会话级全窗口（跨消息生效）

    # ---- P2 容错：LLM failover 链（规约 §6.2，G8）----
    # 备用 provider（OpenAI 兼容）；三项任一为空 = 无备用，行为退化为单主
    llm_backup_base_url: str = ""
    llm_backup_api_key: str = ""
    llm_backup_model: str = ""
    llm_cooldown_seconds: int = 300    # provider 持续故障后的冷却期，到期半开探测

    # ---- P2 容错：业务查询重试（规约 §6.5，G7/G9）----
    # 仅 GET（幂等）在网络错误/5xx 时重试；写操作（POST/PUT/DELETE）绝不自动重试
    jsdstu_query_retry: int = 1        # 查询类额外重试次数
    jsdstu_retry_backoff: float = 0.5  # 重试退避（秒）

    # ---- 数据中台埋点 + 降级计数（容错规约 §8、业务规约 §10）----
    fault_counters_enabled: bool = True        # 降级计数开关（铁律 1：降级必须计数）
    telemetry_enabled: bool = True             # 埋点总开关（关闭则四表不写、overview 只出计数器）
    telemetry_flush_interval: float = 5.0      # 后台批量落库间隔（秒）
    telemetry_db_path: str = ""                # 空 = DATA_ROOT/metrics/observability.db
    metrics_recompute_seconds: int = 600       # DWS(metrics_daily) 定时重算间隔（秒，规约 §4：近 2 天幂等重算）

    # ---- E-12 前端运行时报错上报（企业级落地规约 E-12，判据 C3）----
    # 前端报错是**低频**事件（一次会话几条），默认全收；采样率存在的意义是
    # "前端出了循环报错、把接口当洪水灌"时不至于被灌满——它不是常规限流的主力，
    # 主力是下面的合并窗口（同错同窗口只占一行）。
    client_error_enabled: bool = True          # 上报总开关；false=接口仍返回成功但明确说不入库
    client_error_sample_rate: float = 1.0      # 采样率 0~1（1.0=全收；配错值会收敛而非拒启）
    client_error_dedup_seconds: int = 60       # 合并窗口（秒）：同一错误在该窗口内只占一行、count 累加
    client_error_max_message: int = 500        # message 截断长度（字符）
    client_error_max_stack: int = 2000         # stack 截断长度（字符；完整栈常有数 KB，逐条存会让表暴涨）

    # ---- S-8 PII 脱敏（企业级落地规约 S-8，与 E-6 同源）----
    # 手机号/身份证/邮箱三类 PII 在日志/埋点/展示出口统一替换为 ***（实现单点 app/core/pii.py）。
    # 默认开启；关闭仅限受控调试，平时保持开启（合规要求，不是增强项）。
    pii_redact_enabled: bool = True

    # ---- 数据中台 P2：feedback 反馈闭环（数据中台规约 §6/§8，开放问题 2 拍板=二值）----
    feedback_capture_content: bool = True      # 问题/回复原文捕获开关（独立表 feedback_content，非四表）
    feedback_content_ttl_days: int = 30        # feedback_content 保留天数（规约 §4.2：内容类必须带 TTL）
    feedback_ttl_days: int = 90                # agent_feedback 事件表保留天数（规约 §4.2；事件表比内容表长，
                                               # 理由：反馈是评测金标集的起点，候选池要活得更久）

    # ---- 数据中台 v4：知识缺口分析（数据中台规约 §5「未命中问题分析」）----
    # 问题原文来源：埋点四表不存正文（规约 §10 铁律），缺口视图的原始问句只能取自
    # 会话记忆（近期）与 feedback_content（点踩时捕获）。记忆是易失的（重启/淘汰即失），
    # 所以缺口视图必须**允许只有主题词、没有原句**——聚类与计数不依赖原句。
    kb_gap_min_occurrences: int = 3            # 同主题 ≥N 次才算「值得补库」（低于此只列「观察」）
    kb_gap_max_questions: int = 200            # 单次分析纳入的缺口会话上限（防大窗口爆炸）
    kb_gap_hours_default: int = 24 * 7         # 默认分析窗口（7 天：缺口要看趋势，一天样本太少）

    # ---------- 会话记忆（docs/specs/会话记忆规约.md §5.3，M-1/M-2）----------
    # M-1 持久化：存储介质可切换。sqlite 异常会自动回退 memory，主链路不中断
    memory_store: str = "sqlite"               # memory(内存) / sqlite
    memory_ttl_days: int = 30                  # 会话消息保留天数（清理走产品内定时任务）
    memory_ttl_interval_seconds: int = 86400   # TTL 清理周期（默认每天一次，规约 §5.1）
    memory_max_sessions: int = 500             # 会话数上限（沿用内存版现值）
    memory_sync_round_threshold: int = 10      # 增量落盘：累计轮次阈值（三条件之一）
    memory_sync_interval_seconds: int = 60     # 增量落盘：最长间隔（三条件之一）
    # M-2 裁剪：token 预算 + 双锚点（头部身份/约束 + 尾部当前话题）
    memory_token_budget: int = 3000            # 单次注入历史的 token 预算
    memory_head_turns: int = 2                 # 头部锚点轮数（会话开头的身份/任务/约束）
    memory_tail_turns: int = 6                 # 尾部保留轮数（当前进行中的话题）
    memory_summary_min_turns: int = 8          # 触发中间段摘要的最小轮数（防为一点内容付 LLM 调用）
    # ---- M-3 相关性过滤（docs/specs/会话记忆规约.md §6 M-3）----
    memory_filter_enabled: bool = True         # 总开关；关闭后退回 M-2 行为（历史不过滤）
    memory_filter_min_turns: int = 8           # 少于该轮数不触发过滤（短会话白付 embedding 成本）
    memory_filter_top_k: int = 6               # 中间候选最多保留轮数（锚点轮不占此额；上限非保底）
    memory_filter_similarity_weight: float = 0.9   # 相似度权重；关键词项未接中文分词前打分按 1.0 计
    memory_filter_min_similarity: float = 0.35     # 低于此相似度直接归零（防低分堆叠挤占 topK）
    memory_filter_min_score: float = 0.2           # 综合分阈值，不达标不保留

    # ---- M-5 用户画像（docs/specs/会话记忆规约.md §6 M-5）----
    # 增强能力不是主链路：抽取失败静默降级；关闭后完全不抽取（规约硬要求）
    profile_enabled: bool = True               # 总开关；false=不抽取也不注入
    profile_extract_interval_seconds: int = 90 # 同一用户两次抽取最小间隔（控 LLM 成本）
    profile_min_message_chars: int = 6         # 问题短于该字数不抽取（"在吗"无信息量）

    # ---------- 业务执行引擎（见 docs/specs/业务操作全能力规约.md §6/§7/§8/§12）----------
    agent_planning_enabled: bool = True    # Plan-and-Execute 总开关（false=全部走 REACT）
    agent_planning_max_plan_steps: int = 6 # 计划步数上限
    agent_router_mode: str = "rule_llm_fallback"  # 复杂度路由：rule_only / llm_only / rule_llm_fallback
    agent_router_intent_keywords: str = "报告,总结,汇总,分析,对比,统计,规划,评估,生成,巡检报告,运营周报"
    agent_subagent_enabled: bool = True    # 多智能体总开关（P1 起默认打开）
    agent_subagent_max_calls_per_session: int = 10
    agent_subagent_parallelism: int = 4
    # 确定性委派（DEV_ISSUES §6.55）：规则命中的设计用例在进循环前先委派一次，
    # 消除"委派与否由 LLM 掷骰子"的抖动；false=退回改造前的纯 LLM 自主
    agent_delegation_rule_enabled: bool = True

    # ---------- E-7 数据备份与恢复演练（企业级落地规约 §4 E-7，判据 C4）----------
    # 备份总开关：false = 定时循环不跑，**手动 API 仍可用**（与治理任务同一条语义：
    # 功能开关与"定时跑"分开，关掉自动不影响排障时手动补一份）。
    backup_enabled: bool = True
    # 每天一轮：这四个库都是"天级变化"，备份更密只增加 IO 不增加安全性
    backup_interval_sec: int = 86400
    # 启动后先等 5 分钟：避开冷启动与首轮对账/巡检（那会儿库正忙）
    backup_initial_delay_sec: int = 300
    # 保留份数（规约 §4 E-7 指定 14 份）。滚动清理只删 backup_dir 下"形如 YYYY-MM-DD
    # 且内含 manifest.json"的目录，见 backup_service._prune 的三条护栏。
    backup_keep_days: int = 14
    # 空 = DATA_ROOT/backups。**刻意放在 DATA_ROOT 之内**：备份跟数据一起被搬走，
    # 不会出现"迁了数据目录、备份还留在旧盘"的错配。
    backup_dir: str = ""
    # SQLite 拿锁等待上限（毫秒）。备份循环不能因为某个库被长事务占住就永久挂死；
    # 超时按"该库本轮失败"处理，其余库照常备份。
    backup_busy_timeout_ms: int = 5000

    # ---------- E-6 结构化日志（企业级落地规约 §4 E-6，判据 C3）----------
    # 契约（字段名 / 文件命名 / 轮转）见 docs/日志规范.md，**改这里要同步改那份文档**
    # 与 Java 侧 logback-spring.xml + JsonLogLayout。
    # 日志目录：空 = <仓库根>/deploy/logs；数据目录被隔离（测试/脚本）时自动改到
    # 隔离目录下，避免跑单测就往仓库写日志（见 app/core/logging_setup.py::log_dir）。
    log_dir: str = ""
    # 服务名，进日志的 service 字段（两侧各一个固定值：agt_python / agt_backend）
    log_service: str = "agt_python"
    # 结构化文件名：空 = <PORT>-<LOG_SERVICE>.jsonl
    log_json_file: str = ""
    # 50MB × 10 份（规范 §5 统一口径；Java 侧 logback 里是写死的同值）
    log_max_bytes: int = 52_428_800
    log_backup_count: int = 10
    # 空 = debug 时 DEBUG、否则 INFO
    log_level: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

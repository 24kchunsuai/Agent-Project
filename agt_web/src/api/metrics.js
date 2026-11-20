import request from './request'

/**
 * 数据中台看板 API（数据中台开发规约 §6/§7，P0）
 * 全部走 Java :8080 同名透传，前端不直连明细。
 */

/** 可观测总览：降级计数 + 近 24h 四表聚合 */
export function fetchMetricsOverview() {
  return request.get('/v1/agent/metrics/overview')
}

/**
 * 会话明细列表（会话视图数据源）
 * @param {{limit?: number, hours?: number, intent?: string, sessionId?: string}} params
 */
export function fetchMetricsChats(params = {}) {
  return request.get('/v1/agent/metrics/chats', { params })
}

/**
 * 单次对话全轨迹（trace 追溯）
 * @param {string} traceId
 * @returns {Promise<{found: boolean, chat: object|null, tools: Array, delegations: Array, faults: Array}>}
 */
export function fetchMetricsTrace(traceId) {
  return request.get(`/v1/agent/metrics/traces/${encodeURIComponent(traceId)}`)
}

/**
 * 按天聚合序列（P1，读 DWS 预聚合）
 * @param {{metric: string, from: string, to: string, groupBy?: string}} params
 *        metric ∈ chat_total/chat_failed/chat_degraded/tool_total/tool_failed/
 *                 delegation_total/delegation_done/fault_total/duration_avg/
 *                 duration_p95/tokens_total；groupBy ∈ ''/intent/model/tool/agent/fault_path
 * @returns {Promise<{enabled: boolean, series: Array<{day, dim, value}>}>}
 */
export function fetchMetricsTimeseries(params) {
  return request.get('/v1/agent/metrics/timeseries', { params })
}

/**
 * 工具 TopN（P1）
 * @param {{orderBy?: 'calls'|'fail_rate'|'duration', limit?: number, hours?: number}} params
 */
export function fetchMetricsToolsTop(params = {}) {
  return request.get('/v1/agent/metrics/tools/top', { params })
}

// ---- P2：feedback 反馈闭环 + 告警处置联动 + 报表导出（数据中台规约 §6/§8）----

/**
 * 提交会话反馈（二值 up/down，挂 trace）
 * @param {{traceId: string, rating: 'up'|'down', sessionId?: string, comment?: string}} data
 */
export function submitMetricsFeedback(data) {
  return request.post('/v1/agent/metrics/feedback', data)
}

/**
 * 反馈列表（按 trace 关联会话轨迹摘要）
 * @param {{hours?: number, limit?: number, rating?: ''|'up'|'down'}} params
 */
export function fetchMetricsFeedback(params = {}) {
  return request.get('/v1/agent/metrics/feedback', { params })
}

/**
 * 评测金标集候选导出（JSONL 文本，P2 验收项）
 * @returns {Promise<{count: number, jsonl: string}>}
 */
export function fetchMetricsFeedbackExport(params = {}) {
  return request.get('/v1/agent/metrics/feedback/export', { params })
}

/**
 * CSV 报表导出（view=chats/tools/delegations/faults/feedback）
 * @returns {Promise<{filename: string, content: string}>}
 */
export function fetchMetricsExport(params = {}) {
  return request.get('/v1/agent/metrics/export', { params })
}

/**
 * 告警处置链路联动（委派↔jsdstu 告警近似关联，P2）
 * @returns {Promise<{enabled: boolean, linked: boolean, delegations: Array, alarms: Array, reason: string}>}
 */
export function fetchMetricsAlarmLink(params = {}) {
  return request.get('/v1/agent/metrics/alarms/link', { params })
}

// ---- v4：知识缺口分析（数据中台规约 §5「未命中问题分析」）----

/**
 * 知识缺口分析：未命中问题聚类 + 补库建议
 *
 * 只统计 no_hit（完全无召回）/ insufficient_evidence（证据不足）；
 * kept（答上了）与 cross_domain（问的不属知识域）都会被排除——
 * 把 cross_domain 算进来会让视图被"今天天气"这类闲聊淹掉。
 *
 * `questionAvailable=false` 是**正常状态**：埋点不落正文（规约 §10），
 * 问题原文只能从会话记忆（易失）或 feedback_content 捞，捞不到时
 * 接口只返回主题聚类与计数，并给出 reason——不会伪造原句。
 *
 * @param {{hours?: number, limit?: number}} params hours 默认 7 天（缺口要看趋势）
 * @returns {Promise<{enabled, hours, total, gapTotal, distinctTopics, byDecision,
 *                    topics: Array, suggestions: Array, questionAvailable, reason}>}
 */
export function fetchMetricsUnanswered(params = {}) {
  return request.get('/v1/agent/metrics/unanswered', { params })
}

/**
 * 命中文档使用度（B 类「无人访问文档」巡检的已访问侧）
 *
 * 只统计 kept 会话的引用——拒答会话即使召回到文档也不计入（它没答上，
 * 说明那份文档答不了这个问题）。与知识库文档清单做差集 = 从未被命中的文档。
 *
 * @param {{hours?: number, limit?: number}} params
 * @returns {Promise<{enabled, hours, docTotal, rows: Array<{docId, hits, lastHitAt}>, reason}>}
 */
export function fetchMetricsDocUsage(params = {}) {
  return request.get('/v1/agent/metrics/docs/usage', { params })
}

/**
 * LLM 成本与配额聚合（E-13，判据 C7）
 *
 * 成本 = ODS 明细按**当前价目表**查询期折算（token × 单价，元/百万 token）。
 * 口径：改价后历史按新价重算（看"现在的钱"）；未配价模型单列 unpricedTokens；
 * 部门 = 用户画像里声明过的 department 键（读取期派生，没声明的归 unknown）。
 *
 * @param {{days?: number}} params 统计窗口天数，默认 7
 * @returns {Promise<{enabled, days, pricesConfigured, quota: {capTokens, exceededEventsToday},
 *   totals: {chats, promptTokens, completionTokens, tokens, costCny, unpricedTokens, estimatedRows},
 *   byModel, byUser, byDept, daily}>}
 */
export function fetchMetricsCosts(params = {}) {
  return request.get('/v1/agent/metrics/costs', { params })
}

/** 把导出接口返回的文本内容触发浏览器下载（CSV/JSONL 通用） */
export function downloadTextFile(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

import request from './request'

/**
 * 知识库管理接口。
 *
 * 全部经由 Java 后端（8080）转发到 Python（8000）：
 * 前端不直连 Python，也就不需要关心跨域与 token 透传。
 * owner 由后端从登录态注入，这里传了也不作数。
 */

/** 列表分页参数默认值，与 Python 端 kb_service 常量一致 */
export const DEFAULT_PAGE_SIZE = 20
export const MAX_PAGE_SIZE = 100
export const PAGE_SIZE_OPTIONS = [10, 20, 50]

/**
 * 上传文档并入库
 * @param {{fileName: string, content: string, category?: string, visibility?: string}} params
 * @returns {Promise<{docId, docTitle, category, visibility, owner, chunkCount, replacedChunks, charCount, elapsedSec}>}
 */
export function uploadDocument(params) {
  // 入库要跑切片 + 向量化，耗时明显长于普通请求，单独放宽超时
  return request.post('/v1/kb/documents', params, { timeout: 240000 })
}

/**
 * 文档列表（分页）
 * @param {{page?: number, pageSize?: number}} [params]
 * @returns {Promise<{total, page, pageSize, totalPages, items: Array, truncated}>}
 */
export function fetchDocuments(params = {}) {
  return request.get('/v1/kb/documents', { params: toPageQuery(params) })
}

/**
 * 移入回收站（软删除）：切片立即从向量库移除，可恢复
 * @param {string} docId 文档标识（中文需编码）
 * @returns {Promise<{docId, docTitle, removedChunks, recycled}>}
 */
export function deleteDocument(docId) {
  return request.delete(`/v1/kb/documents/${encodeURIComponent(docId)}`)
}

/**
 * 回收站列表（分页）。需登录
 * @param {{page?: number, pageSize?: number}} [params]
 * @returns {Promise<{total, page, pageSize, totalPages, items: Array}>}
 */
export function fetchRecycleBin(params = {}) {
  return request.get('/v1/kb/recycle', { params: toPageQuery(params) })
}

/**
 * 从回收站恢复（重新切片 + 向量化，耗时与上传同级）
 *
 * 命中在库近重复语料时后端返 **3001 + data.requireConfirm**（本次并未执行恢复），
 * 调用方用 `utils/confirmGate` 的 `withConfirmGate` 拿到用户确认后带 `confirm` 重发。
 *
 * @param {string} docId
 * @param {{confirm?: boolean}} [opts]
 * @returns {Promise<{docId, docTitle, chunkCount, restored}>}
 */
export function restoreDocument(docId, { confirm = false } = {}) {
  return request.post(
    `/v1/kb/recycle/${encodeURIComponent(docId)}/restore`,
    {},
    { timeout: 240000, params: confirm ? { confirm: true } : {} }
  )
}

/**
 * 彻底删除：清理回收站记录与源文件，不可恢复
 *
 * 双确认（管理员 + `confirm=true` + 复述文档名一致），缺一即返 3001 +
 * `data.expectedDocName`（**不执行任何删除**）。前端必须把这两个参数真的发出去——
 * 早先只传 docId，管理员点确认后必然被拒、永远删不掉（P0-1，2026-09-18 修）。
 *
 * @param {string} docId
 * @param {{confirm?: boolean, confirmDocName?: string}} [opts]
 * @returns {Promise<{docId, docTitle, purged}>}
 */
export function purgeDocument(docId, { confirm = false, confirmDocName = '' } = {}) {
  return request.delete(`/v1/kb/recycle/${encodeURIComponent(docId)}`, {
    params: confirm ? { confirm: true, confirmDocName } : {},
  })
}

/**
 * 知识库统计
 * @returns {Promise<{docCount, chunkCount, charCount, publicDocCount, privateDocCount, byCategory, collection}>}
 */
export function fetchKbStats() {
  return request.get('/v1/kb/stats')
}

/**
 * 检索预览：验证刚入库的内容能否被检索命中（与问答链路同一个检索器）
 * @param {string} query
 */
export function searchKb(query) {
  return request.post('/v1/kb/search', { query }, { timeout: 120000 })
}

// ============================================================================
// 语料治理（G1-G4）
//
// 端点全部在 `/v1/kb/governance/*`，由 Java 层做权限收口：除 disable / enable
// （登录用户可操作自己的文档）外，**一律管理员专属**，非管理员返回 4011。
//
// 重操作统一放宽超时：Java 侧对停用/启用/回滚/对账/巡检/TTL/工单执行走的是
// 180s 长超时客户端。前端若沿用默认 90s 会先一步断开，把一次正常的慢操作
// 显示成失败——用户随即再点一次，而那时工单已 DONE、备份已回灌，
// 第二次必然被拒，体验退化成"点了没反应"。
// ============================================================================

/** 停用 / 启用 / 对账 / 巡检 / TTL 等重操作的读超时 */
const SLOW_TIMEOUT = 180000
/** 要真写/真删向量的操作再宽一档（回滚可能回落到重新切片 + embedding） */
const VECTOR_TIMEOUT = 240000

/** 工单类型 → 中文名 */
export const TICKET_TYPE_TEXT = {
  DUPLICATE: '近重复',
  STALE: '陈旧',
  LOW_QUALITY: '劣质切片',
}

/** 工单状态 → 中文名（表里预留的 CONFIRMED 不单独使用，见 inspection_service） */
export const TICKET_STATUS_TEXT = {
  OPEN: '待处理',
  DISMISSED: '已忽略',
  DONE: '已完成',
}

/** 治理文档状态 → 中文名 */
export const GOV_STATE_TEXT = {
  ACTIVE: '在库',
  DISABLED: '停用',
  RECYCLED: '回收站',
}

/**
 * 审计动作 → 中文名。
 * 未列出的动作（如将来的新动作）由调用方回落显示原始英文，不隐藏。
 */
export const AUDIT_ACTION_TEXT = {
  UPLOAD: '入库',
  DEDUP_INTERCEPT: '去重拦截',
  VERSION_REPLACE: '同名覆盖',
  DISABLE: '停用',
  ENABLE: '启用',
  SOFT_DELETE: '移入回收站',
  RESTORE: '恢复',
  PURGE: '彻底删除',
  TTL_PURGE: 'TTL 清理',
  TICKET_OPEN: '巡检开单',
  TICKET_CONFIRM: '工单执行',
  TICKET_DISMISS: '工单忽略',
  BACKFILL: '历史补录',
  RECONCILE: '向量对账',
  ROLLBACK: '版本回滚',
  // E-8 覆盖面扩展：非文档类动作与治理动作同表同序，审计页才是一条完整时间轴
  LOGIN_OK: '登录成功',
  LOGIN_FAIL: '登录失败',
  LOGOUT: '登出',
  TOOL_CONFIRM_ALLOW: '危险操作确认',
  TOOL_CONFIRM_DENY: '危险操作拒绝',
  BACKUP_RUN: '数据备份',
  CONFIG_CHANGE: '配置变更',
}

/**
 * 审计「结果」文案。
 *
 * `DENIED` 与 `FAILED` 必须分开：前者是"按规则不该做"（角色不足 / 用户拒绝），
 * 后者是"做了但没成"。混成一个"失败"会让排查方向走偏——一个小权限问题
 * 看起来像一次系统故障。
 */
export const AUDIT_RESULT_TEXT = {
  SUCCESS: '成功',
  FAILED: '失败',
  DENIED: '已拒绝',
}

/**
 * 审计「对象」筛选（target 前缀）——E-8 引入的统一指向。
 *
 * 值即前缀，服务端按 `LIKE '<值>%'` 匹配，所以 `doc:` 能一次捞出所有文档类动作。
 * 与后端 `AUDIT_EXTRA_COLUMNS` 注释里的写法必须保持一致。
 */
export const AUDIT_TARGET_OPTIONS = [
  { value: '', label: '全部对象' },
  { value: 'doc:', label: '文档' },
  { value: 'tool:', label: '工具确认' },
  { value: 'user:', label: '账号' },
  { value: 'backup', label: '备份' },
  { value: 'config:', label: '配置' },
]

/**
 * 治理概览：文档状态分布 + 切片指纹冗余率 + 工单待办 + 最近审计 + 治理开关
 */
export function fetchGovernanceOverview() {
  return request.get('/v1/kb/governance/overview')
}

/** 治理配置（只读）：去重门开关与阈值。排查"为什么没拦住"先看这里 */
export function fetchGovernanceConfig() {
  return request.get('/v1/kb/governance/config')
}

/** 常驻任务状态：对账 / TTL / 巡检各跑了几轮、上轮耗时与错误 */
export function fetchGovernanceTasks() {
  return request.get('/v1/kb/governance/tasks')
}

/**
 * 审计流水（分页）
 * @param {{operator?: string, action?: string, docId?: string, target?: string,
 *          from?: string, to?: string, page?: number, pageSize?: number}} [params]
 */
export function fetchGovernanceAudit(params = {}) {
  const { operator, action, docId, target, from, to } = params
  const query = toPageQuery(params)
  if (operator) query.operator = operator
  if (action) query.action = action
  if (docId) query.docId = docId
  if (target) query.target = target
  if (from) query.from = from
  if (to) query.to = to
  return request.get('/v1/kb/governance/audit', { params: query })
}

/**
 * 审计流水导出 CSV（E-8）
 *
 * 与 `metrics/export` 同一约定：服务端返回 JSON 信封，
 * `data.content` 是 CSV 文本（utf-8 带 BOM）、`data.filename` 是建议文件名，
 * 前端只用 `downloadTextFile` 落盘。**导出走服务端而不是前端拼表格**：
 * 分页单页上限 200，前端循环翻页一旦中途失败，用户会拿到看不出残缺的残缺证据。
 */
export function fetchGovernanceAuditExport(params = {}) {
  const { operator, action, docId, target, from, to } = params
  const query = {}
  if (operator) query.operator = operator
  if (action) query.action = action
  if (docId) query.docId = docId
  if (target) query.target = target
  if (from) query.from = from
  if (to) query.to = to
  return request.get('/v1/kb/governance/audit/export', { params: query })
}

/**
 * 文档治理登记列表（分页）——治理账本视角，**含回收站中的文档**
 * @param {{state?: 'ACTIVE'|'DISABLED'|'RECYCLED', page?: number, pageSize?: number}} [params]
 */
export function fetchGovernanceDocs(params = {}) {
  const query = toPageQuery(params)
  if (params.state) query.state = params.state
  return request.get('/v1/kb/governance/docs', { params: query })
}

/**
 * 停用区列表（分页）。停用不是删除：无 TTL、随时可启用，与回收站是两个视图
 */
export function fetchDisabledDocs(params = {}) {
  return request.get('/v1/kb/governance/disabled', { params: toPageQuery(params) })
}

/**
 * 停用文档：向量物理移除（立即不可检索）+ 备份落停用区（含向量）
 * @param {string} docId
 */
export function disableDocument(docId) {
  return request.post(
    `/v1/kb/governance/doc/${encodeURIComponent(docId)}/disable`,
    {},
    { timeout: VECTOR_TIMEOUT }
  )
}

/**
 * 启用文档：从停用备份回灌向量（零 embedding，与停用前逐字节一致）
 * @param {string} docId
 */
export function enableDocument(docId) {
  return request.post(
    `/v1/kb/governance/doc/${encodeURIComponent(docId)}/enable`,
    {},
    { timeout: VECTOR_TIMEOUT }
  )
}

/**
 * 手动跑一轮向量对账（与定时任务同一条代码路径）
 * @param {{dryRun?: boolean}} [params]
 */
export function runReconcile(params = {}) {
  return request.post(
    '/v1/kb/governance/reconcile/run',
    {},
    { params: { dryRun: !!params.dryRun }, timeout: SLOW_TIMEOUT }
  )
}

/**
 * 版本链：当前版本 + 历史快照
 * 每个条目带 isCurrent / live / hasVectors —— 只有 hasVectors=true 的历史版本
 * 才该显示"瞬时回滚"（能零 embedding 回灌）。
 * @param {string} docId
 */
export function fetchVersions(docId) {
  return request.get(`/v1/kb/governance/versions/${encodeURIComponent(docId)}`)
}

/**
 * 回滚到历史版本（管理员 + 确认门）
 *
 * 必须先不带 confirm 调用一次拿到需复述的文档名，再带 confirm=true + confirmDocName 重发。
 * 这里不做"自动填好名字"的便利处理：确认门的语义就是让操作人**自己复述一遍**，
 * 自动填等于把这道门删掉。
 *
 * @param {string} docId
 * @param {{versionNo?: number, confirm?: boolean, confirmDocName?: string}} [params]
 */
export function rollbackDocument(docId, params = {}) {
  const query = { confirm: !!params.confirm }
  if (params.versionNo) query.versionNo = params.versionNo
  if (params.confirmDocName) query.confirmDocName = params.confirmDocName
  return request.post(
    `/v1/kb/governance/doc/${encodeURIComponent(docId)}/rollback`,
    {},
    { params: query, timeout: VECTOR_TIMEOUT }
  )
}

/**
 * 巡检工单列表（分页）。OPEN 永远排最前，返回值带 openCount 供角标直接使用
 * @param {{status?: 'OPEN'|'DISMISSED'|'DONE', type?: 'DUPLICATE'|'STALE'|'LOW_QUALITY', page?: number, pageSize?: number}} [params]
 */
export function fetchTickets(params = {}) {
  const query = toPageQuery(params)
  if (params.status) query.status = params.status
  if (params.type) query.type = params.type
  return request.get('/v1/kb/governance/tickets', { params: query })
}

/**
 * 确认工单并执行：DUPLICATE→软删（可恢复）、STALE / LOW_QUALITY→停用（可启用）
 *
 * 执行的是**可回退动作**——巡检判定带主观性，误判的代价必须是一键可还原。
 * 执行失败时工单保持 OPEN（原因写入备注），可直接重试。
 * @param {number} ticketId
 */
export function confirmTicket(ticketId) {
  return request.post(
    `/v1/kb/governance/tickets/${ticketId}/confirm`,
    {},
    { timeout: SLOW_TIMEOUT }
  )
}

/**
 * 忽略工单（不执行任何动作）。
 * 忽略**有记忆**：文档自那以后未变化时，下轮巡检不会为同一问题重复开单。
 * @param {number} ticketId
 * @param {string} [note] 忽略原因（可选，写入工单备注）
 */
export function dismissTicket(ticketId, note) {
  const params = {}
  if (note) params.note = note
  return request.post(`/v1/kb/governance/tickets/${ticketId}/dismiss`, {}, { params })
}

/**
 * 手动触发一轮巡检：三类检测 → 工单去重 → 开单（与定时任务同一路径）
 *
 * 报告里 `errors` 非空表示**某类没查成**，与"这类没有问题"是两件事——展示时必须区分。
 * @param {{dryRun?: boolean}} [params]
 */
export function runInspectionScan(params = {}) {
  return request.post(
    '/v1/kb/governance/inspection/scan',
    {},
    { params: { dryRun: !!params.dryRun }, timeout: SLOW_TIMEOUT }
  )
}

/**
 * TTL 到期彻底删除（与定时任务同一路径）
 *
 * 只清 RECYCLED：停用区无 TTL（停用备份是"启用"的唯一数据来源）；
 * 账本无记录的孤儿备份只在报告里列出，绝不自动删。
 * @param {{dryRun?: boolean}} [params]
 */
export function purgeExpired(params = {}) {
  return request.post(
    '/v1/kb/governance/recycle-bin/purge-expired',
    {},
    { params: { dryRun: !!params.dryRun }, timeout: SLOW_TIMEOUT }
  )
}

/** 统一收敛分页参数：page 从 1 起，pageSize 夹在 [1, MAX_PAGE_SIZE] */
function toPageQuery({ page, pageSize }) {
  const query = {}
  const p = Number(page) || 1
  const ps = Number(pageSize) || DEFAULT_PAGE_SIZE
  query.page = Math.max(1, p)
  query.pageSize = Math.min(Math.max(1, ps), MAX_PAGE_SIZE)
  return query
}

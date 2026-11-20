/**
 * 看板公共逻辑：时间窗口、格式化、窗口文案。
 *
 * 拆分前的看板页把这十几行格式化塞在组件 methods 里，拆成八个页面后
 * 每个页面都要用同一套口径——必须收敛到一处，否则「近 24 小时」在不同
 * 页面可能出现不同写法（原来页面内就已经有 days===1 与 days*24 两套表达）。
 */

/** 时间窗口选项：值是天数，label 供下拉与文案复用 */
export const WINDOW_OPTIONS = [
  { value: 1, label: '近 24 小时', suffix: '24h' },
  { value: 7, label: '近 7 天', suffix: '7 天' },
  { value: 30, label: '近 30 天', suffix: '30 天' },
]

/** 窗口下拉文案：days=1 → 「近 24 小时」，其余 → 「近 N 天」 */
export function windowText(days) {
  const hit = WINDOW_OPTIONS.find((o) => o.value === Number(days))
  return hit ? hit.label : `近 ${days} 天`
}

/** 窗口短文案：用于卡片副标题（「24h」/「7 天」） */
export function windowShort(days) {
  const hit = WINDOW_OPTIONS.find((o) => o.value === Number(days))
  return hit ? hit.suffix : `${days} 天`
}

/** 毫秒可读化：>=1000 显示 s（1 位小数），否则 ms */
export function fmtMs(v) {
  if (v === null || v === undefined || v === '') return '-'
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`
}

/** 时间戳截断展示：把 ISO 的 T 换成空格 */
export function shortTime(ts) {
  return ts ? String(ts).replace('T', ' ') : '-'
}

/** 本地日期 yyyy-MM-dd（timeseries 的 from/to 参数用） */
export function fmtDay(d) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 百分比：分母为 0 返回 '-'（避免出现 Infinity%） */
export function pct(numerator, denominator, digits = 0) {
  if (!denominator) return '-'
  return `${((numerator / denominator) * 100).toFixed(digits)}%`
}

/** 委派状态标签样式：DONE 绿、FAILED 红、TIMEOUT 橙 */
export function statusClass(status) {
  return {
    'ok-tag': status === 'DONE',
    'err-tag': status === 'FAILED',
    'warn-tag': status === 'TIMEOUT',
  }
}

/** 告警级别样式：P0/P1 红、P2 橙、其余灰 */
export function severityClass(severity) {
  const s = String(severity || '').toUpperCase()
  if (s === 'P0' || s === 'P1') return 'sev-high'
  if (s === 'P2') return 'sev-mid'
  return 'sev-low'
}

/** 意图中文名（看板与对话页共用同一映射） */
export const INTENT_TEXT = {
  metric: '指标查询',
  fault: '故障诊断',
  task: '任务管理',
  alarm: '告警处置',
  rca: '根因分析',
  biz: '业务分析',
  knowledge: '知识问答',
  chitchat: '闲聊',
  unknown: '未识别',
}

export function intentText(intent) {
  if (!intent) return '未识别'
  return INTENT_TEXT[intent] || intent
}

// ---- 列表分页（约定见 docs/specs/数据中台开发规约.md §7.2）----

/**
 * 每页条数默认值：**按页面构成分流**。
 *
 * - `PAGE_SIZE_WITH_CONTENT`（10）：页面上还有 KPI 卡片 / 趋势图 / 条形榜等
 *   可视化内容占位，列表只是页面的一部分 —— 一屏再塞 20 行会看不到图表，
 *   也会让页面重新变回"数据坟场"，所以取一半。
 * - `PAGE_SIZE_LIST_ONLY`（20）：页面主体就是这张列表，20 行刚好一屏多一点，
 *   与知识库页的 `DEFAULT_PAGE_SIZE` 保持一致口径。
 */
export const PAGE_SIZE_WITH_CONTENT = 10
export const PAGE_SIZE_LIST_ONLY = 20

/** 每页条数下拉选项：两档默认值都必须在候选里，否则下拉框显示不出当前值 */
export const METRICS_PAGE_SIZE_OPTIONS = [10, 20, 50]

/**
 * 前端内存分页切片。
 *
 * metrics 系列的 Python 接口**只支持 limit、不支持 page/offset**（见规约 §6），
 * 所以看板的分页是"一次多取 + 前端切片"，而不是知识库那样由后端返回
 * `{total, page, pageSize, totalPages}`。这里把切片与页数计算收敛成一处，
 * 八个页面共用，避免每个页面各写一份 `slice()` 各错一遍边界。
 *
 * @param {Array} rows 全量行（已按业务顺序排好）
 * @param {number} page 当前页，从 1 开始
 * @param {number} pageSize 每页条数
 * @returns {{items: Array, total: number, page: number, pageSize: number, totalPages: number}}
 */
export function paginate(rows, page, pageSize) {
  const list = Array.isArray(rows) ? rows : []
  const size = Math.max(1, Number(pageSize) || PAGE_SIZE_LIST_ONLY)
  const total = list.length
  // 至少 1 页：空列表也要显示「1 / 1 页」，否则分页条上会出现「1 / 0 页」
  const totalPages = Math.max(1, Math.ceil(total / size))
  // 页码夹紧：过滤条件变化后原页码可能越界，夹紧比让用户看到空表好
  const current = Math.min(Math.max(1, Number(page) || 1), totalPages)
  const start = (current - 1) * size
  return { items: list.slice(start, start + size), total, page: current, pageSize: size, totalPages }
}

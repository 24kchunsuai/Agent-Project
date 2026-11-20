import request from './request'

/**
 * 运维健康聚合（企业级落地规约 E-3，判据 C3）。
 *
 * 一个 URL 回答"整套系统现在健康吗"：Java(8080) 自身 + 转发 Python(8000) 侧的
 * 组件明细（Milvus / SQLite 四库 / 定时任务 / 降级计数 / 8060 可达性）。
 *
 * 返回结构：
 * {
 *   status: 'up' | 'degraded' | 'down',
 *   checkedAt, durationMs, summary: {up, degraded, down},
 *   components: [{name, status, latencyMs, detail, required}],
 *   pythonReachable, pythonError?, scheduler: [], degradeCounters: {}
 * }
 *
 * 注意 status 与组件 status 是两件事：Milvus 挂了会显示
 * `components[].status='down'` 而整体 `status='degraded'`（服务仍在，能力降级）。
 * 顶栏状态灯只认顶层 status；要看"哪一环坏了"读 components。
 *
 * @param {boolean} [fresh] true = 跳过 Python 侧 3s 缓存强制重新采集（排障用）
 */
export function fetchOpsHealth(fresh = false) {
  return request.get('/v1/ops/health', { params: fresh ? { fresh: true } : {} })
}

/**
 * 前端报错看板数据源（企业级落地规约 E-12，判据 C3）。
 *
 * ⚠️ 这里只有**读**接口。写（上报）刻意不走本文件、也不走 axios 实例：
 * 见 `src/utils/clientError.js` 的 `send()` 注释——那个拦截器会在 4010 时
 * `clearAuth()`，等于"会话过期期间每次前端报错都把用户登出"。
 *
 * 返回结构（口径见 `README.md` 的「E-12 前端运行时报错上报」小节 + `docs/权限矩阵.md` §3.1）：
 * {
 *   enabled, hours, total, distinct,
 *   byPage:    [{name, value}],   // 按页面聚合（按上报次数降序）
 *   byVersion: [{name, value}],   // 按前端版本聚合
 *   latest:    [{fingerprint, page, source, message, stack, appVersion,
 *                userId, count, firstAt, lastAt, traceId}],
 *   reason
 * }
 *
 * `total` 是上报次数之和，`distinct` 是不同错误的**类数**——两者不是一回事，
 * 且 `total` 是真实抛错次数的**下界**（前端同一页面加载内同一错误只报一次）。
 *
 * @param {{hours?: number, limit?: number}} params hours ∈ [1, 2160]，默认 24
 */
export function fetchClientErrors(params = {}) {
  return request.get('/v1/agent/metrics/client-errors', { params })
}

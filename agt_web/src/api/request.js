import axios from 'axios'
import { getToken, clearAuth } from '../utils/auth'

// Java 后端统一响应体：{ code, message, data }
// code === 2000 为成功，其余一律按业务失败处理
export const SUCCESS_CODE = 2000
// 会话过期/未登录：清除本地登录态，由页面引导重新登录
export const AUTH_EXPIRED_CODE = 4010
// 确认门（语料治理）：操作需要用户二次确认，**本次并未执行**。
// data 里带确认依据——彻底删除给 expectedDocName，恢复给命中的相似文档与相似度。
export const CONFIRM_REQUIRED_CODE = 3001

/**
 * 业务错误：保留 code 与 data。
 *
 * 为什么不直接 `new Error(message)`：确认门（3001）与普通失败共用一套响应体，
 * 区分它们的唯一依据就是 code 与 data。早先只抛 message，调用方拿到"该文档与在库
 * 语料高度相似(0.957)，确认仍要恢复？"这句提示，却没有任何字段可以据此弹框重发，
 * 于是流程直接卡死（P0-2，2026-09-18 修）。
 */
export class BizError extends Error {
  constructor(message, code, data) {
    super(message)
    this.name = 'BizError'
    this.code = code
    this.data = data
  }
}

const service = axios.create({
  // 开发环境走 vue.config.js 的代理，生产环境由反向代理处理
  baseURL: process.env.VUE_APP_BASE_API || '',
  // LLM 生成较慢，超时给足
  timeout: 90000
})

service.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

service.interceptors.response.use(
  (response) => {
    const body = response.data
    if (!body || typeof body.code === 'undefined') {
      return Promise.reject(new Error('响应格式异常：缺少 code 字段'))
    }
    if (body.code === AUTH_EXPIRED_CODE) {
      clearAuth()
      return Promise.reject(
        new BizError(body.message || '登录已过期，请重新登录', body.code, body.data)
      )
    }
    if (body.code !== SUCCESS_CODE) {
      // 带上 code / data：确认门（3001）要靠它们弹确认框并重发，只留 message 就会卡死
      return Promise.reject(new BizError(body.message || '请求失败', body.code, body.data))
    }
    // 业务代码只关心 data，解包在这里统一完成
    return body.data
  },
  (error) => {
    // 非 2xx 时后端同样可能返回统一响应体，先尝试按业务错误解包，再退化成 HTTP 层提示
    const body = error.response && error.response.data
    if (body && typeof body.code !== 'undefined' && body.code !== SUCCESS_CODE) {
      return Promise.reject(new BizError(body.message || '请求失败', body.code, body.data))
    }
    const message = error.response
      ? `请求失败（HTTP ${error.response.status}）`
      : '无法连接后端服务，请确认 Java 后端已在 8080 端口启动'
    return Promise.reject(new Error(message))
  }
)

export default service

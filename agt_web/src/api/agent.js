import request from './request'
import { getToken } from '../utils/auth'

/**
 * 智能体对话（非流式）
 * @param {{message: string, sessionId?: string, userId?: number, history?: Array, metadata?: object}} params
 * @returns {Promise<{sessionId: string, content: string, model: string, usage: object, traceId: string}>}
 */
export function chat(params) {
  return request.post('/v1/agent/chat', params)
}

/**
 * 智能体对话（SSE 流式）
 *
 * 后端事件协议：meta（意图/traceId）-> rag?（RAG 检索结果：decision/judged/citations）
 *              -> biz?（业务执行过程：toolTrace/mode/plan，规约 §9）
 *              -> delta*（增量文本）-> done | error
 * 用 fetch + ReadableStream 逐行解析（axios 不支持流式响应）。
 *
 * @param {object} params 同 chat()
 * @param {{onMeta?: Function, onRag?: Function, onBiz?: Function, onDelta?: Function, onDone?: Function, onError?: Function}} handlers
 * @param {{signal?: AbortSignal}} [options] 传入 signal 可中断（停止生成）
 * @returns {Promise<void>} 流结束时 resolve
 */
export async function chatStream(params, handlers = {}, options = {}) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch('/v1/agent/chat/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify(params),
    signal: options.signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`流式接口异常: HTTP ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按 SSE 约定以空行分隔事件，逐行取 data: 载荷
    const lines = buffer.split('\n')
    buffer = lines.pop() // 最后一段可能不完整，留到下一轮
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      let event
      try {
        event = JSON.parse(payload)
      } catch {
        continue // 半截 JSON 丢弃，下一轮会补全
      }
      if (event.type === 'meta') handlers.onMeta && handlers.onMeta(event)
      else if (event.type === 'rag') handlers.onRag && handlers.onRag(event)
      else if (event.type === 'biz') handlers.onBiz && handlers.onBiz(event)
      else if (event.type === 'delta') handlers.onDelta && handlers.onDelta(event)
      else if (event.type === 'quota') handlers.onQuota && handlers.onQuota(event)
      else if (event.type === 'done') handlers.onDone && handlers.onDone(event)
      else if (event.type === 'error') handlers.onError && handlers.onError(event)
    }
  }
}

/**
 * 探测智能体服务（Python 端）健康状态
 */
export function fetchAgentHealth() {
  return request.get('/v1/agent/health')
}

// ---- 用户画像（M-5，会话记忆规约 §6 G15）----
// 画像 = 跨会话的偏好记忆（如"回答要简洁""只看3号机"），由模型从问答中
// 异步抽取。知情权：这里提供查看与删除；猜错的画像删掉即可纠偏。

/**
 * 查看当前登录用户的画像条目
 * @returns {Promise<Array<{key,value,source,confidence,updatedTs}>>}
 */
export function fetchProfile() {
  return request.get('/v1/agent/profile')
}

/**
 * 删除单条画像
 * @param {string} key 画像键名（preference_style / topics）
 */
export function deleteProfileKey(key) {
  return request.delete(`/v1/agent/profile/${encodeURIComponent(key)}`)
}

/** 清空当前用户画像 */
export function clearProfile() {
  return request.delete('/v1/agent/profile')
}

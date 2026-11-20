/**
 * 会话历史持久化（localStorage）
 *
 * 存储设计：
 * - agt_chat_sessions_v1: 会话数组 [{ id, title, sessionId, createdAt, updatedAt, messages }]
 *   - id: 前端本地会话 id（与后端 sessionId 是两个概念）
 *   - sessionId: 后端会话记忆 id，切换回旧会话时继续服务端记忆
 *   - messages: [{ role, content, traceId?, intent?, intentSource?, model?, ts }]
 * - agt_chat_active_v1: 当前激活的本地会话 id
 *
 * 容量控制：
 * - 最多保留 50 个会话，单会话最多 200 条消息（超出丢最旧的）
 * - setItem 做了 try/catch：超容量只告警，不炸页面
 */

const SESSIONS_KEY = 'agt_chat_sessions_v1'
const ACTIVE_KEY = 'agt_chat_active_v1'
const MAX_SESSIONS = 50
const MAX_MESSAGES_PER_SESSION = 200

function safeParse(json, fallback) {
  try {
    const v = JSON.parse(json)
    return v === null || v === undefined ? fallback : v
  } catch {
    return fallback
  }
}

/** @returns {Array} 会话列表（旧 -> 新顺序由调用方维护） */
export function loadSessions() {
  const list = safeParse(localStorage.getItem(SESSIONS_KEY), [])
  return Array.isArray(list) ? list : []
}

export function saveSessions(sessions) {
  const trimmed = (sessions || [])
    .slice(0, MAX_SESSIONS)
    .map((s) => ({
      ...s,
      messages: (s.messages || []).slice(-MAX_MESSAGES_PER_SESSION),
    }))
  try {
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(trimmed))
  } catch (e) {
    // 大概率是容量超限（~5MB），不影响当前页面使用
    console.warn('[sessionStore] 会话保存失败，可能超出 localStorage 容量:', e)
  }
}

export function loadActiveId() {
  return localStorage.getItem(ACTIVE_KEY) || ''
}

export function saveActiveId(id) {
  if (id) localStorage.setItem(ACTIVE_KEY, id)
  else localStorage.removeItem(ACTIVE_KEY)
}

/** 新建一个空会话记录 */
export function createSessionRecord() {
  return {
    id: `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    title: '新会话',
    sessionId: '',
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: [],
  }
}

/** 用首条用户消息截断生成会话标题 */
export function buildTitle(text) {
  const t = String(text || '').replace(/\s+/g, ' ').trim()
  if (!t) return '新会话'
  return t.length > 20 ? `${t.slice(0, 20)}…` : t
}

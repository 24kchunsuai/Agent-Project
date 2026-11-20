/**
 * 登录态存取（localStorage 持久化，刷新不丢）
 *
 * 后端契约（agt_backend /v1/auth/*）：
 * - token: 不透明会话令牌（8h），放在 Authorization: Bearer <token>
 * - user: { userId, username, realName, role }
 * 会话过期由后端 4010 标识，见 request.js 响应拦截器
 */
const TOKEN_KEY = 'agt_token'
const USER_KEY = 'agt_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export function setUser(user) {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function isLoggedIn() {
  return !!getToken()
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 当前用户昵称（登录态展示用） */
export function displayName() {
  const u = getUser()
  if (!u) return ''
  return u.realName || u.username || `用户${u.userId}`
}

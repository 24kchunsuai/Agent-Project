import request from './request'
import { getToken, setToken, setUser, clearAuth, getUser } from '../utils/auth'

/**
 * 登录（代理 jsdstu-3 用户体系，不建第二套账号）
 * 后端返回的 user 是 jsdstu-3 原始结构（id/permission），这里归一化成
 * 前端统一结构 {userId, username, realName, role}，raw 保留原字段。
 * @param {{username: string, password: string}} params
 * @returns {Promise<{token: string, user: object, expiresIn: number}>}
 */
export async function login(params) {
  const data = await request.post('/v1/auth/login', params)
  const raw = data.user || {}
  const user = {
    userId: raw.id,
    username: raw.username,
    realName: raw.realName || raw.username,
    role: raw.permission || raw.role,
    raw,
  }
  setToken(data.token)
  setUser(user)
  return { ...data, user }
}

/** 当前登录用户信息（token 失效时后端返回 4010） */
export function fetchMe() {
  return request.get('/v1/auth/me')
}

/** 退出登录（后端销毁会话，前端清存储） */
export async function logout() {
  try {
    await request.post('/v1/auth/logout')
  } finally {
    clearAuth()
  }
}

/** 对话链路用的身份载荷：登录时带服务端 userId（前端传的不算数） */
export function chatIdentity() {
  const u = getUser()
  return u ? { userId: u.userId, username: u.username, role: u.role } : null
}

export { getToken, getUser, clearAuth }

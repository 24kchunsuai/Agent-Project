/**
 * 确认门（语料治理）前端共用工具。
 *
 * 为什么单独抽一个文件
 * --------------------
 * 同一个契约在项目里有两处实现：治理 tab 的"版本回滚"接对了，回收站的"恢复"与
 * "彻底删除"却没接（2026-09-18 三层审计发现，P0-1/P0-2/P0-3）。根因是每个页面各写
 * 一套、漏一处无人察觉——而当时所有验收脚本都只打 Python（8000），前端与 Java 谁
 * 都不碰，所以谁都没发现。这里把「判定 / 复述名校验 / 带确认重发」收敛成一份。
 *
 * 后端口径（Python `_kb_fail` 与 Java `ConfirmRequiredException` 完全一致）：
 *   HTTP 200，code = 3001，data = { requireConfirm: true, gate, ... }
 *   - gate = "confirm"（彻底删除）：data.expectedDocName —— 要用户复述文档名
 *   - gate = "L3"   （从回收站恢复）：data.hits / maxSimilarity / threshold
 *     —— 命中了在库的近重复语料，要用户看着相似度和命中文档决定是否继续
 *   关键语义：**操作本次并未执行**，带上 confirm 重发即可，不需要先撤销什么。
 */

/** 是否"确认门"拦截（与普通失败的区别：未执行 + 带确认后可原样重发） */
export function isConfirmRequired(e) {
  return !!e && e.code === 3001 && !!(e.data && e.data.requireConfirm)
}

/** 确认门携带的结构化依据（原样来自后端） */
export function confirmPayload(e) {
  return (e && e.data) || {}
}

/**
 * 复述名校验：与后端 `_confirm_name_matches` 同口径——**候选名之一、严格相等、去首尾空白**。
 *
 * 后端接受"文档标题 / docId / 文件名主干"三者之一（界面上显示哪个就允许复述哪个），
 * 所以这里也收候选集合而不是单个期望值；只放松到"三选一"，不做模糊匹配——
 * 这道的语义是"用户确实看清了要删的是哪一篇"，模糊匹配等于没确认。
 *
 * @param {string} input 用户输入
 * @param {string[]|string} candidates 允许的名字集合
 */
export function nameRepeated(input, candidates) {
  const given = String(input == null ? '' : input).trim()
  if (!given) return false
  const list = Array.isArray(candidates) ? candidates : [candidates]
  return list.some((c) => c && String(c).trim() === given)
}

/** 确认门里"需要用户复述的文档名"（彻底删除 / 回滚用，取主推显示名） */
export function expectedName(payload) {
  return (payload && (payload.expectedDocName || payload.docTitle)) || ''
}

/**
 * 带确认门重试：先按原样调一次；被确认门拦下就交给回调弹框，用户同意后带 confirm 重发。
 *
 * @param {(extra: {confirm?: boolean, confirmDocName?: string}) => Promise<any>} call
 *        实际发请求的函数（extra 会透传成 query 参数）
 * @param {(payload: object) => Promise<{confirmDocName?: string}|null|undefined>} onConfirmRequired
 *        确认门回调：弹框拿用户决定；返回假值表示用户取消（原错误继续抛出）
 * @returns {Promise<any>}
 */
export async function withConfirmGate(call, onConfirmRequired) {
  try {
    return await call({})
  } catch (e) {
    if (!isConfirmRequired(e) || typeof onConfirmRequired !== 'function') throw e
    const decided = await onConfirmRequired(confirmPayload(e))
    if (!decided) throw e
    // 只重发一次：再被拦说明条件仍不满足，不该无限弹框
    return await call({ confirm: true, confirmDocName: decided.confirmDocName })
  }
}

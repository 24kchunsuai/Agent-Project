<template>
  <div class="profile-page">
    <header class="page-head">
      <div>
        <h2>我的画像</h2>
        <p class="sub">
          模型会从对话中悄悄记下你的偏好（跨会话生效）。这里一切透明：
          记了什么、多可靠、什么时候记的，都能看、能删。
        </p>
      </div>
      <div class="head-actions">
        <button class="btn ghost" :disabled="loading" @click="load">刷新</button>
        <button class="btn danger" :disabled="loading || !entries.length" @click="clearAll">
          清空画像
        </button>
      </div>
    </header>

    <p v-if="notice" class="notice" :class="{ warn: noticeWarn }">{{ notice }}</p>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!entries.length" class="empty">
      还没有画像。和智能体多聊几句，它记住的偏好会出现在这里。
    </div>

    <ul v-else class="entry-list">
      <li v-for="e in entries" :key="e.key" class="entry">
        <div class="entry-main">
          <span class="key">{{ keyLabel(e.key) }}</span>
          <span class="value">{{ prettyValue(e) }}</span>
        </div>
        <div class="entry-meta">
          <span class="badge" :class="e.source">来源：{{ sourceLabel(e.source) }}</span>
          <span class="badge">置信度：{{ Math.round((e.confidence || 0) * 100) }}%</span>
          <span class="time">{{ formatTs(e.updatedTs) }}</span>
          <button class="btn tiny danger" @click="removeOne(e.key)">删除</button>
        </div>
      </li>
    </ul>

    <footer class="foot-note">
      画像只影响回答风格与关注点，不会替你执行操作；关闭总开关（配置
      <code>profile_enabled</code>）后模型停止记录，但已记内容仍需在这里手动删除。
    </footer>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { clearProfile, deleteProfileKey, fetchProfile } from '../api/agent'

const entries = ref([])
const loading = ref(false)
const notice = ref('')
const noticeWarn = ref(false)

function flash(message, warn = false) {
  notice.value = message
  noticeWarn.value = warn
  if (message) setTimeout(() => { notice.value = '' }, 3000)
}

const KEY_LABELS = {
  preference_style: '回答风格',
  topics: '关注主题',
}
const keyLabel = (k) => KEY_LABELS[k] || k

function prettyValue(e) {
  if (e.key !== 'topics') return e.value
  try {
    const list = JSON.parse(e.value)
    return Array.isArray(list) ? list.join('、') : e.value
  } catch { return e.value }
}

function sourceLabel(s) {
  return { agent: '模型抽取', user: '本人设置' }[s] || (s || '未知')
}

function formatTs(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function load() {
  loading.value = true
  try {
    const data = await fetchProfile()
    entries.value = Array.isArray(data) ? data : []
  } catch (err) {
    flash(`画像加载失败：${err.message}`, true)
  } finally {
    loading.value = false
  }
}

async function removeOne(key) {
  try {
    const r = await deleteProfileKey(key)
    flash(`已删除「${keyLabel(key)}」（${(r && r.removed) || 0} 条）`)
    await load()
  } catch (err) {
    flash(`删除失败：${err.message}`, true)
  }
}

async function clearAll() {
  if (!window.confirm('确定清空全部画像？清空后模型立刻回到"不认识你"状态。')) return
  try {
    const r = await clearProfile()
    flash(`已清空（${(r && r.removed) || 0} 条）`)
    await load()
  } catch (err) {
    flash(`清空失败：${err.message}`, true)
  }
}

onMounted(load)
</script>

<style scoped>
.profile-page { max-width: 760px; margin: 0 auto; padding: 24px 20px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-head h2 { margin: 0 0 6px; font-size: 20px; }
.sub { margin: 0; color: var(--text-secondary, #6b7280); font-size: 13px; line-height: 1.6; }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.notice { margin: 14px 0 0; padding: 8px 12px; border-radius: 8px; background: var(--bg-soft, #f0fdf4); color: #15803d; font-size: 13px; }
.notice.warn { background: var(--bg-warn, #fef2f2); color: #b91c1c; }

.entry-list { list-style: none; margin: 18px 0 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.entry { border: 1px solid var(--border, #e5e7eb); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.entry-main { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.key { font-weight: 600; font-size: 14px; }
.value { font-size: 14px; color: var(--text-primary, #111827); }
.entry-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary, #6b7280); }
.badge { background: var(--bg-soft, #f3f4f6); border-radius: 6px; padding: 2px 8px; }
.badge.user { background: #ecfdf5; color: #047857; }
.badge.agent { background: #eff6ff; color: #1d4ed8; }
.time { margin-left: auto; }

.btn { border: 1px solid var(--border, #d1d5db); background: #fff; border-radius: 8px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.danger { color: #b91c1c; border-color: #fecaca; }
.btn.danger:hover:not(:disabled) { background: #fef2f2; }
.btn.tiny { padding: 2px 10px; font-size: 12px; }
.btn.ghost:hover:not(:disabled) { background: #f9fafb; }

.empty { margin: 40px 0; text-align: center; color: var(--text-secondary, #9ca3af); font-size: 14px; }
.foot-note { margin-top: 22px; font-size: 12px; color: var(--text-secondary, #9ca3af); line-height: 1.7; }
.foot-note code { background: var(--bg-soft, #f3f4f6); border-radius: 4px; padding: 1px 5px; }
</style>

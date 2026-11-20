<template>
  <div class="chat-page">
    <SessionSidebar
      :sessions="sessions"
      :active-id="activeId"
      @select="switchSession"
      @create="newSession"
      @remove="removeSession"
    />

    <section class="chat-main">
      <header class="chat-header">
        <div class="title-block">
          <h1>Drone Agent 智能体对话</h1>
          <p class="subtitle">Vue 9090 → Java 8080 → Python 8000</p>
        </div>
        <div class="header-right">
          <div class="health" :class="healthClass">
            <span class="dot"></span>
            <span>{{ healthText }}</span>
          </div>
          <!-- 登录态展示已收归左侧导航（AdminLayout 的侧栏底部统一管理
               昵称/角色/退出），本页不再重复一个登录按钮：
               同一屏出现两个「登录/退出」入口会让人不知道该点哪个。 -->
        </div>
      </header>

      <main class="chat-body" ref="bodyRef">
        <div v-if="!messages.length" class="empty">
          <p class="empty-title">还没有对话</p>
          <p class="empty-desc">输入一句话试试，链路是否打通立刻见分晓。</p>
        </div>

        <div
          v-for="(item, index) in messages"
          :key="index"
          class="row"
          :class="item.role"
        >
          <div class="bubble">
            <!-- 助手消息：markdown 渲染；用户消息：纯文本 -->
            <template v-if="item.role === 'assistant'">
              <p v-if="item.ragNote && !item.content" class="rag-note">{{ item.ragNote }}</p>
              <div class="md-body" v-html="renderMd(item.content)"></div>
              <span v-if="item.streaming" class="cursor">▌</span>

              <!-- E-13 配额提示：仅超限时后端才会发 quota 事件；黄条不拦截任何操作 -->
              <p v-if="item.quotaNotice" class="quota-note">{{ item.quotaNotice }}</p>

              <!-- 确认卡片：写操作待确认时给快捷按钮（按钮只发送字面"确认/取消"，规约 §9 防注入不变式） -->
              <div v-if="isPendingConfirm(item.content)" class="confirm-card">
                <span class="confirm-tip">写操作等待您的确认（{{ confirmTtl(item.content) }}）</span>
                <div class="confirm-btns">
                  <button class="btn-confirm" type="button" :disabled="loading" @click="replyLiteral('确认')">确认执行</button>
                  <button class="btn-cancel" type="button" :disabled="loading" @click="replyLiteral('取消')">取消</button>
                </div>
              </div>

              <!-- 来源卡片：RAG 知识问答的回答下方列出引用文档 -->
              <div v-if="item.citations && item.citations.length" class="citations">
                <button class="cite-toggle" type="button" @click="item.citeOpen = !item.citeOpen">
                  来源资料 · {{ item.citations.length }} 条
                  <span class="arrow">{{ item.citeOpen ? '▲' : '▼' }}</span>
                </button>
                <ul v-show="item.citeOpen" class="cite-list">
                  <li v-for="(c, ci) in item.citations" :key="ci" class="cite-item">
                    <span class="cite-score">{{ citeScore(c.score) }}</span>
                    <span class="cite-title" :title="c.docId">{{ c.docTitle || c.docId }}</span>
                    <span v-if="c.category" class="cite-cat">{{ c.category }}</span>
                  </li>
                </ul>
              </div>

              <!-- 执行过程：业务回合的工具调用轨迹（可折叠），PLAN 模式附计划清单 -->
              <div v-if="item.toolTrace && item.toolTrace.length" class="tooltrace">
                <button class="cite-toggle" type="button" @click="item.traceOpen = !item.traceOpen">
                  执行过程 · {{ item.toolTrace.length }} 步<template v-if="item.mode === 'PLAN'"> · 计划模式</template>
                  <span class="arrow">{{ item.traceOpen ? '▲' : '▼' }}</span>
                </button>
                <div v-show="item.traceOpen" class="trace-list">
                  <ol v-if="item.plan && item.plan.length" class="plan-steps">
                    <!-- biz 事件在执行完成后到达，展示即"已完成的计划复盘"：全部打勾 -->
                    <li v-for="(p, pi) in item.plan" :key="'p' + pi" class="plan-step done">
                      <span class="tick">✓</span>{{ p }}
                    </li>
                  </ol>
                  <div v-for="(t, ti) in item.toolTrace" :key="ti" class="trace-item" :class="t.ok ? 'ok' : 'fail'">
                    {{ traceText(t) }}
                  </div>
                </div>
              </div>
            </template>
            <template v-else>
              <p class="content">{{ item.content }}</p>
            </template>

            <p v-if="item.traceId" class="meta">
              <!-- 子智能体徽标（规约 §9）：轨迹里有成功的 delegate_subagent 即标注 -->
              <span v-if="helperAgents(item).length" class="subagent-badge">
                由 {{ helperAgents(item).join('、') }} 协助完成
              </span>
              <span v-if="item.intent" class="intent-tag" :class="item.intent">{{ intentText(item.intent) }}</span>
              <span
                v-if="item.ragJudged === 'insufficient'"
                class="intent-tag rag-refused"
              >知识库未收录</span>
              trace: {{ item.traceId }}<template v-if="item.model"> · model: {{ item.model }}</template>
            </p>
          </div>
        </div>

        <div v-if="loading" class="row assistant">
          <div class="bubble loading">正在思考…</div>
        </div>

        <p v-if="errorText" class="error">{{ errorText }}</p>
      </main>

      <footer class="chat-footer">
        <textarea
          ref="input"
          v-model="draft"
          class="input"
          rows="3"
          placeholder="输入内容，Enter 发送，Shift + Enter 换行"
          :disabled="loading"
          @keydown.enter.exact.prevent="send"
        ></textarea>
        <div class="actions">
          <button class="ghost" :disabled="loading || !messages.length" @click="clear">清空</button>
          <button class="primary" :disabled="loading || !draft.trim()" @click="send">
            {{ loading ? '发送中' : '发送' }}
          </button>
        </div>
      </footer>
    </section>
  </div>
</template>

<script>
import { chatStream, fetchAgentHealth } from '../api/agent'
import { isLoggedIn, displayName, clearAuth } from '../utils/auth'
import { renderMarkdown } from '../utils/markdown'
import {
  loadSessions,
  saveSessions,
  loadActiveId,
  saveActiveId,
  createSessionRecord,
  buildTitle,
} from '../utils/sessionStore'
import SessionSidebar from '../components/SessionSidebar.vue'

export default {
  name: 'AgentChatView',
  components: { SessionSidebar },
  data() {
    return {
      draft: '',
      messages: [],
      loading: false,
      errorText: '',
      sessionId: '', // 后端会话记忆 id
      abortController: null,
      healthState: 'unknown', // unknown | up | down
      sessions: [], // 本地会话列表（旧 -> 新）
      activeId: '', // 当前本地会话 id
      loggedIn: false, // 登录态（auth.js localStorage 读取）
      userLabel: '', // 登录用户昵称
    }
  },
  computed: {
    healthClass() {
      return `health-${this.healthState}`
    },
    healthText() {
      if (this.healthState === 'up') return '智能体在线'
      if (this.healthState === 'down') return '智能体离线'
      return '状态探测中'
    },
    currentUserId() {
      const u = JSON.parse(localStorage.getItem('agt_user') || 'null')
      return u ? u.userId : undefined
    },
  },
  mounted() {
    this.restoreSessions()
    this.checkHealth()
    this.refreshAuth()
    this.applyPrefill()
  },
  beforeUnmount() {
    // 页面离开时中断进行中的流式请求，并落盘当前会话
    if (this.abortController) this.abortController.abort()
    this.persistActiveSession()
  },
  methods: {
    /**
     * 接收外部入口的预填消息（数据看板「让 Agent 处置」等）。
     *
     * 设计要点：**只填进输入框，绝不自动发送**。
     * 处置是写操作，按 guard.py 的铁律「确认只认用户下一条字面消息」——
     * 若这里自动 send，用户就失去了"看到复述再决定"的机会，
     * 等于替用户点了确认。填入后由用户自己确认发送，链路才完整。
     *
     * 预填文本同时从地址栏清除（replace），避免刷新页面时重复填入。
     */
    applyPrefill() {
      const prefilled = this.$route && this.$route.query && this.$route.query.prefill
      if (!prefilled) return
      this.draft = String(prefilled).slice(0, 200)
      this.$router.replace({ path: this.$route.path, query: {} })
      this.$nextTick(() => {
        const box = this.$refs.input && this.$refs.input.$el
        if (box && box.focus) box.focus()
      })
    },
    // ---------- 会话管理 ----------
    restoreSessions() {
      this.sessions = loadSessions()
      const savedActive = loadActiveId()
      const active =
        this.sessions.find((s) => s.id === savedActive) || this.sessions[this.sessions.length - 1]
      if (active) {
        this.activate(active)
      } else {
        this.newSession()
      }
    },

    activate(session) {
      this.activeId = session.id
      this.sessionId = session.sessionId || ''
      // 深拷贝进响应式消息，避免视图直接改动持久化数据
      this.messages = (session.messages || []).map((m) => ({ ...m }))
      saveActiveId(this.activeId)
    },

    findActive() {
      return this.sessions.find((s) => s.id === this.activeId)
    },

    newSession() {
      if (this.loading && this.abortController) this.abortController.abort()
      const record = createSessionRecord()
      this.sessions.push(record)
      this.activate(record)
      this.errorText = ''
      this.persistAll()
    },

    switchSession(id) {
      if (id === this.activeId) return
      if (this.loading && this.abortController) this.abortController.abort()
      this.persistActiveSession()
      const target = this.sessions.find((s) => s.id === id)
      if (target) this.activate(target)
      this.errorText = ''
      this.scrollToBottom(false)
    },

    removeSession(id) {
      const idx = this.sessions.findIndex((s) => s.id === id)
      if (idx === -1) return
      if (!window.confirm('确定删除该会话？删除后不可恢复。')) return
      this.sessions.splice(idx, 1)
      if (id === this.activeId) {
        if (this.loading && this.abortController) this.abortController.abort()
        const next = this.sessions[this.sessions.length - 1]
        if (next) this.activate(next)
        else this.newSession()
      }
      this.persistAll()
    },

    /** 把当前视图中的消息写回会话记录并落盘 */
    persistActiveSession() {
      const record = this.findActive()
      if (!record) return
      record.messages = this.messages
        .filter((m) => m.role === 'user' || m.content)
        .map((m) => ({
          role: m.role,
          content: m.content,
          traceId: m.traceId || '',
          intent: m.intent || '',
          intentSource: m.intentSource || '',
          model: m.model || '',
          ragDecision: m.ragDecision || '',
          ragJudged: m.ragJudged || '',
          citations: m.citations || [],
          toolTrace: m.toolTrace || [],
          mode: m.mode || '',
          plan: m.plan || [],
          ts: m.ts || Date.now(),
        }))
      record.sessionId = this.sessionId
      record.updatedAt = Date.now()
      this.persistAll()
    },

    persistAll() {
      saveSessions(this.sessions)
      saveActiveId(this.activeId)
    },

    // ---------- 对话 ----------
    async checkHealth() {
      try {
        const data = await fetchAgentHealth()
        this.healthState = data && data.status === 'up' ? 'up' : 'down'
      } catch (e) {
        this.healthState = 'down'
      }
    },

    /** 刷新本页登录态（登录页跳回后走 mounted 触发）。
     *  退出登录的动作已上移到 AdminLayout，本页只读登录态用于请求参数。 */
    refreshAuth() {
      this.loggedIn = isLoggedIn()
      this.userLabel = displayName()
    },

    intentText(intent) {
      if (intent === 'business') return '业务'
      if (intent === 'rag') return '知识问答'
      return '闲聊'
    },

    /** rerank 分数转百分制展示（0.998 -> 99.8） */
    citeScore(score) {
      const n = Number(score)
      if (!Number.isFinite(n)) return ''
      return (n * 100).toFixed(1)
    },

    /** 工具轨迹条目转一行文本：步骤 · 工具 · 状态 · 耗时 · 错误 */
    traceText(t) {
      const tool = t.tool || '-'
      const status = t.ok ? '成功' : t.blocked ? '待确认' : '失败'
      const dur = t.durationMs != null ? ` · ${t.durationMs}ms` : ''
      const err = t.error ? ` · ${t.error}` : ''
      return `step ${t.step ?? '-'} · ${tool} · ${status}${dur}${err}`
    },

    /** 写操作待确认判定：后端复述话术的固定文案（guard.build_confirm_reply） */
    isPendingConfirm(content) {
      return typeof content === 'string' && content.includes('需要你确认后才会执行')
    },

    /** 从话术里摘出有效期提示（"10 分钟内有效…"），没有就返回空串 */
    confirmTtl(content) {
      const m = /（(\d+\s*分钟内有效[^）]*)）/.exec(content || '')
      return m ? m[1] : '超时未确认将自动作废'
    },

    /** 确认卡片按钮：只发送字面文本消息，不绕过"只认用户字面消息"原则 */
    replyLiteral(text) {
      this.draft = text
      this.send()
    },

    /** 子智能体徽标：从成功的 delegate_subagent 轨迹里取专家名（去重） */
    helperAgents(item) {
      const names = (item.toolTrace || [])
        .filter((t) => t.tool === 'delegate_subagent' && t.ok && t.arguments && t.arguments.name)
        .map((t) => t.arguments.name)
      return [...new Set(names)]
    },

    renderMd(text) {
      return renderMarkdown(text)
    },

    async send() {
      const text = this.draft.trim()
      if (!text || this.loading) return

      this.errorText = ''
      const record = this.findActive()
      if (!record) return

      this.messages.push({ role: 'user', content: text, ts: Date.now() })
      // 首条消息生成会话标题
      if (!record.messages.some((m) => m.role === 'user')) {
        record.title = buildTitle(text)
      }
      this.draft = ''
      this.loading = true
      this.scrollToBottom()

      // 助手消息占位：meta 到达后挂 traceId，rag 到达后挂检索结果，首个 delta 后逐字追加
      const reply = {
        role: 'assistant',
        content: '',
        streaming: true,
        model: '',
        traceId: '',
        intent: '',
        ragDecision: '',
        ragJudged: '',
        citations: [],
        citeOpen: false,
        ragNote: '',
        toolTrace: [],
        mode: '',
        plan: [],
        traceOpen: false,
        quotaNotice: '',
        ts: Date.now(),
      }
      let started = false
      /** rag 事件先于正文到达时提前上屏，让用户立刻看到检索状态 */
      const ensureVisible = () => {
        if (!started) {
          started = true
          this.messages.push(reply)
          this.scrollToBottom()
        }
      }

      this.abortController = new AbortController()
      try {
        await chatStream(
          {
            message: text,
            sessionId: this.sessionId || undefined,
            // 登录态带上真实 userId（服务端仍以令牌解析为准，这里只是展示一致性）
            ...(this.loggedIn ? { userId: this.currentUserId } : {}),
          },
          {
            onMeta: (e) => {
              this.sessionId = e.sessionId || this.sessionId
              reply.traceId = e.traceId || ''
              reply.intent = e.intent || ''
              reply.intentSource = e.intentSource || ''
            },
            onRag: (e) => {
              reply.ragDecision = e.decision || ''
              reply.ragJudged = e.judged || ''
              reply.citations = e.citations || []
              if (reply.citations.length && reply.ragJudged !== 'insufficient') {
                reply.ragNote = `已在知识库检索到 ${reply.citations.length} 条相关资料，正在组织回答…`
              } else if (reply.ragJudged === 'insufficient') {
                reply.ragNote = '知识库中未找到能直接回答该问题的资料'
              }
              ensureVisible()
            },
            onBiz: (e) => {
              // 业务执行过程（规约 §9）：工具轨迹 + 执行模式 + 计划清单
              reply.toolTrace = e.toolTrace || []
              reply.mode = e.mode || 'REACT'
              reply.plan = e.plan || []
              if (reply.toolTrace.length || reply.plan.length) ensureVisible()
            },
            onDelta: (e) => {
              ensureVisible()
              reply.ragNote = ''
              reply.content += e.content
              this.scrollToBottom()
            },
            onQuota: (e) => {
              // E-13：配额超限提示。超限**不拒绝服务**（规约原文：降级为提示）——
              // 回答照常给出，黄条只是告知用量事实；没有该事件 = 一切正常。
              reply.quotaNotice = e.message || ''
              ensureVisible()
            },
            onDone: (e) => {
              reply.model = e.model || ''
              this.healthState = 'up'
            },
            onError: (e) => {
              // 会话过期：清登录态并引导重新登录
              if (e.code === 4010) {
                clearAuth()
                this.refreshAuth()
                this.errorText = e.message || '登录已过期，请重新登录'
                if (!started) this.messages.push(reply)
                return
              }
              this.errorText = e.message || '智能体调用失败'
              if (!started) this.messages.push(reply)
            },
          },
          { signal: this.abortController.signal }
        )
      } catch (e) {
        if (e.name !== 'AbortError') {
          this.errorText = e.message
          this.healthState = 'down'
        }
      } finally {
        reply.streaming = false
        reply.ragNote = ''
        if (!started && !this.messages.includes(reply)) this.messages.push(reply)
        this.loading = false
        this.abortController = null
        this.scrollToBottom()
        // 对话完成（含出错）后落盘
        this.persistActiveSession()
      }
    },

    clear() {
      this.messages = []
      this.sessionId = ''
      this.errorText = ''
      this.persistActiveSession()
    },

    scrollToBottom(smooth = true) {
      this.$nextTick(() => {
        const el = this.$refs.bodyRef
        if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
      })
    },
  },
}
</script>

<!-- 页面自身样式（scoped） -->
<style scoped>
.chat-page {
  display: flex;
  /* 高度改为撑满 AdminLayout 的内容区，而不是按视口硬减固定值：
     硬减在旧的全宽布局里凑合能用，套进「顶栏 + 内边距」的壳之后会算错，
     露出多余的滚动条或压扁输入框。100% 由父级的 flex 链保证。 */
  height: 100%;
  min-height: 0;
  text-align: left;
  border: 1px solid #eef0f2;
  border-radius: 12px;
  overflow: hidden;
  background: #ffffff;
}

.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 14px 20px 16px;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 12px;
  border-bottom: 1px solid #e5e7eb;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.auth-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.auth-name {
  font-size: 13px;
  color: #4c5b6b;
  font-weight: 600;
}

.auth-btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #4c5b6b;
  cursor: pointer;
}

.auth-btn.primary {
  border-color: #2f6fed;
  color: #2f6fed;
}

.auth-btn:hover {
  opacity: 0.85;
}

.title-block h1 {
  margin: 0;
  font-size: 17px;
  font-weight: 500;
  color: #1f2328;
}

.subtitle {
  margin: 3px 0 0;
  font-size: 12px;
  color: #8b929c;
}

.health {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid transparent;
}

.health .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

.health-up {
  color: #0f6e56;
  background: #e1f5ee;
  border-color: #9fe1cb;
}

.health-down {
  color: #a32d2d;
  background: #fcebeb;
  border-color: #f7c1c1;
}

.health-unknown {
  color: #5f5e5a;
  background: #f1efe8;
  border-color: #d3d1c7;
}

.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 4px;
}

.empty {
  margin-top: 80px;
  text-align: center;
  color: #8b929c;
}

.empty-title {
  font-size: 14px;
  margin: 0 0 6px;
}

.empty-desc {
  font-size: 12px;
  margin: 0;
}

.row {
  display: flex;
  margin-bottom: 14px;
}

.row.user {
  justify-content: flex-end;
}

.row.assistant {
  justify-content: flex-start;
}

.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}

.row.user .bubble {
  background: #e6f1fb;
  color: #0c447c;
  border: 1px solid #b5d4f4;
  white-space: pre-wrap;
}

.row.assistant .bubble {
  background: #f6f7f9;
  color: #1f2328;
  border: 1px solid #e5e7eb;
}

.bubble.loading {
  color: #8b929c;
}

.content {
  margin: 0;
}

.meta {
  margin: 6px 0 0;
  font-size: 11px;
  color: #9aa1ab;
}

.cursor {
  color: #185fa5;
  animation: blink 1s step-start infinite;
}

@keyframes blink {
  50% { opacity: 0; }
}

.intent-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 10px;
  line-height: 16px;
}

.intent-tag.chitchat {
  color: #0f6e56;
  background: #e1f5ee;
}

.intent-tag.business {
  color: #8a5a00;
  background: #fdf2d4;
}

.intent-tag.rag {
  color: #5b21b6;
  background: #ede9fe;
}

.intent-tag.rag-refused {
  color: #a3541c;
  background: #fdebd9;
}

/* RAG 检索状态提示（正文开始前） */
.rag-note {
  margin: 0 0 4px;
  font-size: 12px;
  color: #8b929c;
}

/* E-13 配额超限提示（超限不拒绝服务，黄条只告知事实） */
.quota-note {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 8px;
  background: #fdf6e3;
  border: 1px solid #f0e0b0;
  color: #8a6d1a;
  font-size: 12px;
  line-height: 1.5;
}

/* 来源卡片 */
.citations {
  margin-top: 8px;
  border-top: 1px dashed #e0e3e8;
  padding-top: 6px;
}

.cite-toggle {
  padding: 0;
  font-size: 11px;
  color: #5f6b7a;
  background: none;
  border: none;
  cursor: pointer;
}

.cite-toggle .arrow {
  font-size: 9px;
  margin-left: 2px;
}

.cite-list {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

.cite-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 11px;
  min-width: 0;
}

.cite-score {
  flex-shrink: 0;
  min-width: 30px;
  text-align: center;
  color: #0f6e56;
  background: #e1f5ee;
  border-radius: 4px;
  font-size: 10px;
  line-height: 16px;
}

.cite-title {
  color: #3b4351;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cite-cat {
  flex-shrink: 0;
  color: #8b929c;
  background: #f1f3f5;
  border-radius: 4px;
  font-size: 10px;
  line-height: 16px;
  padding: 0 5px;
}

.error {
  margin: 8px 0;
  padding: 8px 12px;
  font-size: 12px;
  color: #a32d2d;
  background: #fcebeb;
  border: 1px solid #f7c1c1;
  border-radius: 8px;
}

/* 执行过程（业务工具轨迹，规约 §9 P0） */
.tooltrace {
  margin-top: 8px;
  border-top: 1px dashed #e0e3e8;
  padding-top: 6px;
}

.trace-list {
  margin-top: 6px;
}

.plan-steps {
  margin: 0 0 6px;
  padding-left: 18px;
  font-size: 11px;
  color: #4c5b6b;
}

.plan-step {
  margin: 2px 0;
}

.trace-item {
  padding: 2px 0 2px 10px;
  font-size: 11px;
  border-left: 2px solid #d8dbe0;
  margin: 3px 0;
  color: #4c5b6b;
  font-family: Consolas, Monaco, 'Courier New', monospace;
}

.trace-item.ok {
  border-left-color: #97c459;
}

.trace-item.fail {
  border-left-color: #f09595;
  color: #a32d2d;
}

/* 计划步骤打勾（规约 §9 P1） */
.plan-step .tick {
  margin-right: 4px;
  color: #97c459;
  font-weight: 600;
}

/* 确认卡片（规约 §9 P1）：按钮只发字面"确认/取消" */
.confirm-card {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
  padding: 8px 12px;
  background: #fdf6e7;
  border: 1px solid #f0d9a8;
  border-radius: 8px;
}

.confirm-tip {
  font-size: 12px;
  color: #8a5a00;
  flex: 1;
  min-width: 140px;
}

.confirm-btns {
  display: flex;
  gap: 8px;
}

.btn-confirm,
.btn-cancel {
  height: 26px;
  padding: 0 14px;
  font-size: 12px;
  border-radius: 6px;
  cursor: pointer;
}

.btn-confirm {
  color: #fff;
  background: #b7791f;
  border: 1px solid #b7791f;
}

.btn-cancel {
  color: #5f5e5a;
  background: #fff;
  border: 1px solid #d8dbe0;
}

/* 子智能体徽标（规约 §9 P1） */
.subagent-badge {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 10px;
  line-height: 16px;
  color: #1f5e8a;
  background: #e0f0fa;
  border: 1px solid #b8dcf2;
}

.chat-footer {
  border-top: 1px solid #e5e7eb;
  padding-top: 12px;
}

.input {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  padding: 10px 12px;
  font-size: 13px;
  font-family: inherit;
  line-height: 1.6;
  color: #1f2328;
  border: 1px solid #d8dbe0;
  border-radius: 10px;
  outline: none;
}

.input:focus {
  border-color: #85b7eb;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 10px;
}

button {
  font-size: 13px;
  padding: 7px 18px;
  border-radius: 8px;
  cursor: pointer;
  font-family: inherit;
}

button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.primary {
  color: #ffffff;
  background: #185fa5;
  border: 1px solid #185fa5;
}

.ghost {
  color: #5f5e5a;
  background: #ffffff;
  border: 1px solid #d8dbe0;
}
</style>

<!-- markdown 内容样式：v-html 注入的内容吃不到 scoped 样式，必须用全局样式 -->
<style>
.chat-page .md-body {
  line-height: 1.65;
}

.chat-page .md-body > :first-child {
  margin-top: 0;
}

.chat-page .md-body > :last-child {
  margin-bottom: 0;
}

.chat-page .md-body p {
  margin: 6px 0;
  white-space: pre-wrap;
}

.chat-page .md-body h1,
.chat-page .md-body h2,
.chat-page .md-body h3,
.chat-page .md-body h4 {
  margin: 12px 0 6px;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}

.chat-page .md-body ul,
.chat-page .md-body ol {
  margin: 6px 0;
  padding-left: 20px;
}

.chat-page .md-body li {
  margin: 2px 0;
}

.chat-page .md-body blockquote {
  margin: 6px 0;
  padding: 2px 10px;
  border-left: 3px solid #b5d4f4;
  color: #4b5563;
  background: #f0f6fd;
}

.chat-page .md-body code:not(.hljs) {
  padding: 1px 5px;
  font-size: 12px;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  color: #a32d5e;
  background: #f3e8ee;
  border-radius: 4px;
}

.chat-page .md-body pre {
  margin: 8px 0;
  padding: 10px 12px;
  background: #f6f8fa;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow-x: auto;
}

.chat-page .md-body pre code {
  font-size: 12px;
  line-height: 1.55;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  background: transparent;
  padding: 0;
  color: #24292f;
}

.chat-page .md-body table {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 12px;
  width: 100%;
  display: block;
  overflow-x: auto;
}

.chat-page .md-body th,
.chat-page .md-body td {
  border: 1px solid #d8dbe0;
  padding: 5px 10px;
  text-align: left;
}

.chat-page .md-body th {
  background: #f1f3f5;
  font-weight: 600;
}

.chat-page .md-body a {
  color: #185fa5;
}

.chat-page .md-body hr {
  border: none;
  border-top: 1px solid #e5e7eb;
  margin: 10px 0;
}
</style>

<template>
  <Teleport to="body">
    <div v-if="open" class="pm-mask" @click.self="close">
      <div class="pm-dialog" role="dialog" aria-modal="true">
        <!-- 头：问题 + 结论 -->
        <header class="pm-head">
          <div class="pm-head-main">
            <h3>
              执行流程
              <span class="pm-status" :class="'s-' + worstStatus">{{ worstLabel }}</span>
            </h3>
            <p class="pm-question">
              <span class="pm-q-tag">用户提问</span>
              <span v-if="question">{{ question }}</span>
              <span v-else class="pm-muted">原句不可得（埋点不落正文，仅点踩时捕获）· {{ traceId }}</span>
            </p>
          </div>
          <button class="pm-close" type="button" aria-label="关闭" @click="close">×</button>
        </header>

        <div v-if="loading" class="pm-body pm-muted">加载中…</div>
        <div v-else-if="!pipeline" class="pm-body pm-muted">未找到该会话的链路数据</div>

        <template v-else>
          <!-- 概览条 -->
          <div class="pm-meta">
            <span class="pm-chip">意图 <b>{{ intentText }}</b></span>
            <span v-if="chat && chat.mode" class="pm-chip">模式 <b>{{ chat.mode }}</b></span>
            <span class="pm-chip">总耗时 <b>{{ fmtMs(pipeline.totalMs) }}</b></span>
            <span v-if="chat" class="pm-chip">
              tokens <b>{{ chat.promptTokens || 0 }}+{{ chat.completionTokens || 0 }}</b>
            </span>
            <span v-if="chat && chat.ragDecision" class="pm-chip">
              RAG 判定 <b>{{ decisionText(chat.ragDecision) }}</b>
            </span>
            <span class="pm-chip pm-chip-mono">{{ traceId }}</span>
          </div>

          <!-- 进度条：仿申请进度样式，圆形状态点 + 连接线 + 阶段名 -->
          <div class="pm-track-wrap">
            <div class="pm-track">
              <div
                v-for="(s, i) in activeStages"
                :key="s.key"
                class="pm-node"
                :class="['st-' + s.status, { 'is-open': openKey === s.key }]"
                @click="toggle(s.key)"
              >
                <div class="pm-rail" :class="{ 'rail-first': i === 0, 'rail-last': i === activeStages.length - 1 }">
                  <span class="pm-line pm-line-left" :class="{ hide: i === 0 }"></span>
                  <span class="pm-dot" :class="'dot-' + s.status">
                    <span class="pm-dot-icon">{{ statusIcon(s.status) }}</span>
                  </span>
                  <span class="pm-line pm-line-right" :class="{ hide: i === activeStages.length - 1 }"></span>
                </div>
                <div class="pm-node-name">{{ s.name }}</div>
                <div class="pm-node-state">{{ statusText(s.status) }}</div>
              </div>
            </div>
          </div>

          <!-- 阶段详情：点节点展开 -->
          <div class="pm-detail">
            <div v-if="!openKey" class="pm-muted pm-detail-hint">
              点击上方任一阶段查看细节（耗时、工具、命中文档、错误原因）
            </div>
            <template v-else>
              <h4>
                {{ openStage.name }}
                <span class="pm-detail-status" :class="'s-' + openStage.status">
                  {{ statusText(openStage.status) }}
                </span>
              </h4>
              <p class="pm-detail-note">{{ openStage.note }}</p>
              <dl class="pm-kv">
                <template v-if="openStage.detail">
                  <dt>实际情况</dt><dd>{{ openStage.detail }}</dd>
                </template>
                <template v-if="openStage.error">
                  <dt>错误</dt><dd class="pm-err">{{ openStage.error }}</dd>
                </template>
              </dl>

              <!-- 召回/精选：命中文档 -->
              <div v-if="openStage.key === 'select' || openStage.key === 'recall' || openStage.key === 'rerank'">
                <p v-if="citations.length" class="pm-sub">命中资料（{{ citations.length }} 篇）</p>
                <ul v-else class="pm-muted pm-sub">无命中文档</ul>
                <ul class="pm-docs">
                  <li v-for="(c, i) in citations" :key="i">
                    <span class="pm-doc-title">{{ c.docTitle || c.docId }}</span>
                    <span v-if="c.score !== undefined" class="pm-doc-score">
                      相关度 {{ Number(c.score).toFixed(3) }}
                    </span>
                    <span v-if="c.category" class="pm-doc-cat">{{ c.category }}</span>
                  </li>
                </ul>
              </div>

              <!-- 工具调用：每步详情 -->
              <div v-if="openStage.key === 'tools'">
                <p v-if="tools.length" class="pm-sub">工具调用明细（{{ tools.length }} 步）</p>
                <table v-else class="pm-muted pm-sub">无工具调用</table>
                <table v-if="tools.length" class="pm-tools">
                  <thead>
                    <tr><th>步</th><th>工具</th><th>结果</th><th>耗时</th><th>参数摘要 / 错误</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(t, i) in tools" :key="i" :class="{ 'tool-bad': !t.ok }">
                      <td>{{ t.step }}</td>
                      <td class="pm-mono">{{ t.tool }}</td>
                      <td>{{ t.ok ? '成功' : (t.blocked ? '拦截' : '失败') }}</td>
                      <td>{{ fmtMs(t.durationMs) }}</td>
                      <td class="pm-digest">{{ t.error || t.argsDigest || '-' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <!-- 委派 -->
              <div v-if="openStage.key === 'delegate' && delegations.length">
                <p class="pm-sub">委派明细</p>
                <ul class="pm-docs">
                  <li v-for="(d, i) in delegations" :key="i">
                    <span class="pm-doc-title">{{ d.agent }}</span>
                    <span class="pm-doc-score">{{ d.status }}</span>
                    <span class="pm-doc-cat">{{ fmtMs(d.durationMs) }}</span>
                  </li>
                </ul>
              </div>

              <!-- 故障痕迹 -->
              <div v-if="openStage.key === 'evaluate' || openStage.key === 'answer' || openStage.key === 'memory'">
                <template v-if="stageFaults.length">
                  <p class="pm-sub">降级/熔断留痕</p>
                  <ul class="pm-docs">
                    <li v-for="(f, i) in stageFaults" :key="i">
                      <span class="pm-doc-title pm-mono">{{ f.path }}</span>
                      <span class="pm-doc-cat">{{ f.detail }}</span>
                    </li>
                  </ul>
                </template>
              </div>
            </template>
          </div>

          <!-- 图例 -->
          <footer class="pm-legend">
            <span><i class="lg lg-ok"></i>正常执行</span>
            <span><i class="lg lg-warn"></i>重试 / 降级 / 兜底</span>
            <span><i class="lg lg-gray"></i>如实拒答（设计内行为）</span>
            <span><i class="lg lg-error"></i>熔断 / 失败（真故障）</span>
          </footer>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script>
/**
 * 链路执行流程弹窗（v5）。
 *
 * 数据来自 GET /metrics/traces/{traceId} 的 pipeline 字段（后端按真实控制流还原），
 * 本组件只负责渲染，不做任何推断——避免"前端猜流程"和"后端真流程"两处口径漂移。
 *
 * 三色 + 一灰（用户 2026-09-19 定，见后端口径注释）：
 *   绿 ok    = 正常执行
 *   黄 warn  = 重试 / 降级 / 兜底
 *   灰 gray  = 如实拒答（知识库没收录 / 超出范围）——**设计内行为，不是故障**
 *   红 error = 熔断 / 失败——真故障，用户拿不到答案
 * 灰红拆开是因为**处置动作不同**：灰→补语料，红→查系统。
 * 若把 78 条拒答一律标红，看板会满屏红，把"如实说明"误读成"系统不可用"。
 *
 * idle 阶段（如 REACT 模式下的"任务规划"）不渲染进进度条——没发生的阶段画出来
 * 就是假节点，与本弹窗"还原真实执行"的目的相悖。
 */
import { fmtMs } from '../utils/metrics'

const STATUS_TEXT = { ok: '正常', warn: '降级', gray: '未作答', error: '失败', idle: '—' }
const STATUS_ICON = { ok: '✓', warn: '!', gray: '—', error: '×', idle: '·' }

export default {
  name: 'PipelineModal',
  props: {
    open: { type: Boolean, default: false },
    loading: { type: Boolean, default: false },
    traceId: { type: String, default: '' },
    /** 后端 pipeline：{ stages, worstStatus, totalMs } */
    pipeline: { type: Object, default: null },
    chat: { type: Object, default: null },
    tools: { type: Array, default: () => [] },
    delegations: { type: Array, default: () => [] },
    faults: { type: Array, default: () => [] },
    question: { type: String, default: '' },
  },
  emits: ['close'],
  data() {
    return { openKey: '' }
  },
  computed: {
    /** 只渲染真实参与本轮链路的阶段（idle 不画） */
    activeStages() {
      if (!this.pipeline || !this.pipeline.stages) return []
      return this.pipeline.stages.filter((s) => s.status !== 'idle')
    },
    worstStatus() {
      return (this.pipeline && this.pipeline.worstStatus) || 'ok'
    },
    worstLabel() {
      const map = {
        ok: '正常完成', warn: '降级完成', gray: '如实拒答', error: '执行失败',
      }
      return map[this.worstStatus] || '正常完成'
    },
    intentText() {
      const map = { rag: '知识问答', business: '业务操作', chitchat: '闲聊' }
      const k = (this.chat && this.chat.intent) || ''
      return map[k] || k || '-'
    },
    /**
     * 命中文档：trace 接口的 chat 只带 ragDocs（docId 字符串数组），
     * 没有标题与分数（那属于对话响应里的 citations）。
     * 这里如实只显示 docId，不编造标题——要看标题请去会话详情页的"来源资料"。
     */
    citations() {
      const docs = (this.chat && this.chat.ragDocs) || []
      return docs.map((d) => (typeof d === 'string' ? { docId: d } : d))
    },
    openStage() {
      return this.activeStages.find((s) => s.key === this.openKey) || {}
    },
    stageFaults() {
      // 只显示与该阶段相关的故障留痕（按后端的 path→stage 映射反查）
      if (!this.openKey) return []
      const KEY_BY_PATH = {
        rag_degraded: 'recall', rag_refused: 'evaluate',
        plan_degraded: 'route', plan_replan: 'plan',
        loop_fingerprint_warn: 'tools', loop_fingerprint_break: 'tools',
        tool_fail_break: 'tools', tool_timeout: 'tools',
        subagent_timeout: 'delegate', subagent_budget_exhausted: 'delegate',
        llm_fallback: 'answer', llm_failover: 'answer', reply_cleaned: 'answer',
        // M-4（会话记忆规约 §6）：记忆段留痕
        memory_sqlite_degraded: 'memory', memory_filter_degraded: 'memory',
        memory_trimmed: 'memory', memory_filtered: 'memory',
        memory_summary_failed: 'memory',
      }
      return (this.faults || []).filter((f) => KEY_BY_PATH[f.path] === this.openKey)
    },
  },
  watch: {
    open(v) {
      if (v) this.openKey = ''
      this._syncBodyLock()
    },
  },
  mounted() {
    this._onKey = (e) => { if (e.key === 'Escape' && this.open) this.close() }
    document.addEventListener('keydown', this._onKey)
  },
  beforeUnmount() {
    document.removeEventListener('keydown', this._onKey)
    this._syncBodyLock()
  },
  methods: {
    fmtMs,
    close() {
      this.$emit('close')
    },
    toggle(key) {
      this.openKey = this.openKey === key ? '' : key
    },
    statusText(s) {
      return STATUS_TEXT[s] || s
    },
    statusIcon(s) {
      return STATUS_ICON[s] || '·'
    },
    decisionText(d) {
      const map = {
        kept: 'kept（答上了）',
        cross_domain: 'cross_domain（问的不属知识域）',
        no_hit: 'no_hit（无召回）',
        insufficient_evidence: 'insufficient_evidence（证据不足）',
        degraded: 'degraded（检索降级）',
      }
      return map[d] || d
    },
    /** 弹窗打开时锁背景滚动，避免遮罩下层跟着滚 */
    _syncBodyLock() {
      document.body.style.overflow = this.open ? 'hidden' : ''
    },
  },
}
</script>

<style scoped>
/* ---- 遮罩与容器 ---- */
.pm-mask {
  position: fixed;
  inset: 0;
  background: rgba(17, 24, 39, 0.45);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.pm-dialog {
  width: min(980px, 100%);
  max-height: 88vh;
  overflow-y: auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 18px 48px rgba(16, 24, 40, 0.24);
  padding: 20px 24px 18px;
}

/* ---- 头 ---- */
.pm-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #eef0f2;
}
.pm-head h3 {
  margin: 0 0 6px;
  font-size: 16px;
  color: #1f2328;
  display: flex;
  align-items: center;
  gap: 10px;
}
.pm-question {
  margin: 0;
  font-size: 13px;
  color: #333;
  line-height: 1.6;
}
.pm-q-tag {
  display: inline-block;
  background: #eef4fb;
  color: #185fa5;
  border-radius: 4px;
  padding: 1px 7px;
  font-size: 11px;
  margin-right: 8px;
}
.pm-muted {
  color: #8a919c;
  font-size: 12px;
}
.pm-close {
  border: none;
  background: transparent;
  font-size: 22px;
  line-height: 1;
  color: #98a1ad;
  cursor: pointer;
  padding: 0 4px;
}
.pm-close:hover {
  color: #4c5b6b;
}
.pm-status {
  font-size: 12px;
  font-weight: 400;
  border-radius: 4px;
  padding: 2px 8px;
}
.s-ok { background: #e1f5ee; color: #0f6e56; }
.s-warn { background: #faeeda; color: #854f0b; }
.s-gray { background: #eceff2; color: #5b6673; }
.s-error { background: #fdeceb; color: #b42318; }

/* ---- 概览条 ---- */
.pm-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 0 4px;
}
.pm-chip {
  font-size: 12px;
  color: #6b7280;
  background: #f7f8fa;
  border-radius: 6px;
  padding: 3px 9px;
}
.pm-chip b {
  color: #1f2328;
  font-weight: 600;
}
.pm-chip-mono {
  font-family: Consolas, Menlo, monospace;
}

/* ---- 进度条 ---- */
.pm-track-wrap {
  overflow-x: auto;
  padding: 18px 4px 6px;
}
.pm-track {
  display: flex;
  align-items: flex-start;
  min-width: max-content;
}
.pm-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  min-width: 108px;
  cursor: pointer;
}
.pm-rail {
  display: flex;
  align-items: center;
  width: 100%;
}
.pm-line {
  flex: 1;
  height: 2px;
  background: #dfe4ea;
}
.pm-line.hide {
  visibility: hidden;
}
.pm-dot {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #fff;
  font-size: 14px;
  font-weight: 700;
}
.dot-ok { background: #12a06a; }
.dot-warn { background: #d99a2b; }
.dot-gray { background: #9aa4b0; }
.dot-error { background: #cf3b30; }

.pm-node-name {
  font-size: 12.5px;
  color: #333;
  margin-top: 8px;
  text-align: center;
  white-space: nowrap;
}
.pm-node-state {
  font-size: 11px;
  margin-top: 2px;
  color: #98a1ad;
}
.st-ok .pm-node-state { color: #0f6e56; }
.st-warn .pm-node-state { color: #854f0b; }
.st-gray .pm-node-state { color: #5b6673; }
.st-error .pm-node-state { color: #b42318; }
.pm-node.is-open .pm-node-name {
  font-weight: 600;
}
.pm-node.is-open .pm-dot {
  box-shadow: 0 0 0 4px rgba(47, 111, 237, 0.16);
}

/* ---- 详情 ---- */
.pm-detail {
  margin-top: 14px;
  border-top: 1px solid #eef0f2;
  padding-top: 12px;
  min-height: 90px;
}
.pm-detail h4 {
  margin: 0 0 4px;
  font-size: 13.5px;
  color: #1f2328;
  display: flex;
  align-items: center;
  gap: 8px;
}
.pm-detail-status {
  font-size: 11px;
  border-radius: 4px;
  padding: 1px 7px;
  font-weight: 400;
}
.pm-detail-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: #8a919c;
}
.pm-detail-hint {
  padding: 28px 0;
  text-align: center;
}
.pm-kv {
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 4px 10px;
  margin: 0 0 10px;
  font-size: 12.5px;
}
.pm-kv dt {
  color: #8a919c;
}
.pm-kv dd {
  margin: 0;
  color: #333;
}
.pm-err {
  color: #b42318;
}
.pm-sub {
  font-size: 12px;
  color: #6b7280;
  margin: 10px 0 6px;
}
.pm-docs {
  list-style: none;
  margin: 0;
  padding: 0;
}
.pm-docs li {
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 5px 0;
  border-bottom: 1px solid #f5f6f8;
  font-size: 12.5px;
}
.pm-doc-title {
  color: #1f2328;
}
.pm-doc-score {
  color: #0f6e56;
  font-size: 11.5px;
}
.pm-doc-cat {
  color: #8a919c;
  font-size: 11.5px;
  margin-left: auto;
}
.pm-mono {
  font-family: Consolas, Menlo, monospace;
}
.pm-tools {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.pm-tools th {
  text-align: left;
  color: #6b7280;
  font-weight: 500;
  padding: 5px 8px;
  border-bottom: 1px solid #eef0f2;
}
.pm-tools td {
  padding: 5px 8px;
  border-bottom: 1px solid #f5f6f8;
  color: #333;
}
.pm-tools tr.tool-bad td {
  background: #fffafa;
}
.pm-digest {
  color: #8a919c;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 图例 ---- */
.pm-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  padding-top: 12px;
  margin-top: 6px;
  border-top: 1px solid #eef0f2;
  font-size: 11.5px;
  color: #6b7280;
}
.pm-legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.lg {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.lg-ok { background: #12a06a; }
.lg-warn { background: #d99a2b; }
.lg-gray { background: #9aa4b0; }
.lg-error { background: #cf3b30; }
</style>

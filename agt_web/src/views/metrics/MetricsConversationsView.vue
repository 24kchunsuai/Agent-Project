

<template>
  <DataPage title="会话明细" subtitle="逐轮会话记录；点击任意行展开该轮的完整轨迹（chat + 工具 + 委派 + 故障）">
    <template #actions>
      <select v-model.number="days" class="sel" @change="reload">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <button class="btn" :disabled="loading" @click="exportCsv">导出 CSV</button>
      <button class="btn" :disabled="loading" @click="reload">{{ loading ? '加载中…' : '刷新' }}</button>
    </template>

    <EmptyState
      v-if="loaded && enabled === false"
      kind="off"
      title="可观测埋点未开启"
      :lines="['请在 agt_python/.env 中设置 telemetry_enabled=true 并重启 Python 服务。']"
    />
    <EmptyState
      v-else-if="error"
      kind="error"
      title="会话数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <div class="panel">
        <div class="filters">
          <select v-model="intentFilter" class="sel">
            <option value="">全部意图</option>
            <option v-for="row in intents" :key="row.intent" :value="row.intent">
              {{ row.intent || '(未知)' }}
            </option>
          </select>
          <label class="muted">
            条数
            <select v-model.number="limit" class="sel">
              <option :value="20">20</option>
              <option :value="50">50</option>
              <option :value="100">100</option>
            </select>
          </label>
          <span v-if="sessionFilter" class="muted">
            已按 session 过滤
            <a class="link" @click="clearSessionFilter">清除</a>
          </span>
        </div>

        <div class="tbl-wrap">
          <table class="tbl clickable">
            <thead>
              <tr>
                <th>时间</th><th>意图</th><th>模式</th><th>结果</th>
                <th>耗时</th><th>tokens</th><th>工具步数</th><th>traceId</th><th>流程</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!pagedChats.items.length"><td colspan="9" class="muted pad">暂无会话记录</td></tr>
              <tr
                v-for="c in pagedChats.items"
                :key="c.traceId"
                :class="{ active: c.traceId === activeTraceId }"
                @click="openTrace(c.traceId)"
              >
                <td class="nowrap">{{ shortTime(c.createdAt) }}</td>
                <td>
                  <span class="tag">{{ c.intent || '-' }}</span>
                  <span class="muted"> ({{ c.intentSource }})</span>
                </td>
                <td>{{ c.mode || '-' }}</td>
                <td>
                  <span class="tag" :class="c.ok ? 'ok-tag' : 'err-tag'">{{ c.ok ? 'OK' : '失败' }}</span>
                  <span v-if="c.degraded" class="tag warn-tag">降级</span>
                </td>
                <td>{{ fmtMs(c.durationMs) }}</td>
                <td class="muted">{{ tokenText(c) }}</td>
                <td>{{ c.toolCount ?? '-' }}</td>
                <td class="mono">{{ c.traceId }}</td>
                <td>
                  <button
                    class="flow-btn"
                    type="button"
                    title="查看该轮对话的完整执行流程"
                    @click.stop="openPipeline(c.traceId)"
                  >
                    查看详情
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <PaginationBar
          :page="pagedChats.page"
          :total-pages="pagedChats.totalPages"
          :page-size="pagedChats.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
      </div>

      <!-- trace 追溯：拆页后仍留在本页（它本来就是这个列表的展开态），
           但要显式可关，避免像原来那样长在下半页形成"页面里的页面" -->
      <div v-if="trace" class="panel trace-panel">
        <h3>
          轨迹追溯
          <span class="mono muted">{{ trace.traceId }}</span>
          <button class="btn small" @click="closeTrace">收起</button>
        </h3>
        <div v-if="!trace.found" class="muted pad">未找到该 trace 的记录</div>
        <template v-else>
          <p class="chat-summary">
            <span class="tag">{{ trace.chat.intent }}</span>
            <span v-if="trace.chat.mode" class="tag">{{ trace.chat.mode }}</span>
            模型 {{ trace.chat.model || '-' }} ·
            tokens {{ trace.chat.promptTokens }}+{{ trace.chat.completionTokens }} ·
            总耗时 {{ fmtMs(trace.chat.durationMs) }} · 回复 {{ trace.chat.contentChars }} 字
            <span v-if="trace.chat.error" class="err-text">· 错误：{{ trace.chat.error }}</span>
          </p>

          <!-- 反馈入口：原在看板 trace 面板里，现移到「反馈与金标集」页做专门闭环 -->
          <p class="muted pad">
            想给这次回答评分？
            <router-link class="link" :to="{ path: '/dashboard/feedback', query: { trace: trace.traceId } }">
              去「反馈与金标集」页打分 →
            </router-link>
          </p>

          <ol class="timeline">
            <li v-for="(ev, i) in timeline" :key="i" :class="ev.cls">
              <span class="ev-type">{{ ev.type }}</span>
              <span class="ev-main">{{ ev.main }}</span>
              <span class="ev-sub muted">{{ ev.sub }}</span>
            </li>
            <li v-if="!timeline.length" class="muted">该会话无工具/委派/故障事件</li>
          </ol>
        </template>
      </div>
    </template>

    <!-- 执行流程弹窗（v5）：与「点击行展开轨迹面板」是两个不同视角——
         面板是四表原始时间线（排查用），弹窗是按真实链路还原的阶段视图（讲解用）。
         数据同源（都来自 trace 接口），进弹窗时若该 trace 已拉过就直接复用，避免重复请求。 -->
    <PipelineModal
      :open="pipeOpen"
      :loading="pipeLoading"
      :trace-id="pipeTraceId"
      :pipeline="pipeData ? pipeData.pipeline : null"
      :chat="pipeData ? pipeData.chat : null"
      :tools="pipeData ? pipeData.tools : []"
      :delegations="pipeData ? pipeData.delegations : []"
      :faults="pipeData ? pipeData.faults : []"
      :question="pipeData ? pipeData.question : ''"
      @close="closePipeline"
    />
  </DataPage>
</template>

<script>
/**
 * 会话明细页（原看板「视图 2」+ trace 追溯面板）。
 *
 * 为什么 trace 留在本页而不是单独一页：trace 是"某一行会话"的展开态，
 * 脱离列表就没有入口对象（原实现靠点击行触发，本质是主从结构）。
 * 但反馈打分挪去了「反馈与金标集」页——它是另一个运维动作，
 * 挂在追溯面板上会让"看轨迹"和"做评价"两件事混在一起。
 */
import DataPage from '../../components/DataPage.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import PipelineModal from '../../components/PipelineModal.vue'
import {
  fetchMetricsOverview, fetchMetricsChats, fetchMetricsTrace, fetchMetricsExport, downloadTextFile,
} from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, fmtMs, shortTime, pct,
  paginate, PAGE_SIZE_LIST_ONLY, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsConversationsView',
  components: { DataPage, EmptyState, PaginationBar, PipelineModal },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      chats: [],
      intents: [],
      intentFilter: '',
      sessionFilter: '',
      limit: 50,
      trace: null,
      activeTraceId: '',
      // v5 执行流程弹窗
      pipeOpen: false,
      pipeLoading: false,
      pipeTraceId: '',
      pipeData: null,
      // 本页主体就是这张列表 → 纯列表默认每页 20 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_LIST_ONLY,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    /** 前端切片视图（后端只认 limit，分页在内存里做） */
    pagedChats() {
      return paginate(this.chats, this.page, this.pageSize)
    },
    timeline() {
      if (!this.trace || !this.trace.found) return []
      const evs = []
      for (const t of this.trace.tools || []) {
        evs.push({
          time: t.createdAt,
          type: `工具#${t.step}`,
          main: `${t.tool} ${t.ok ? '成功' : '失败'}${t.blocked ? `（拦截：${t.blocked}）` : ''}`,
          sub: `${fmtMs(t.durationMs)} · ${shortTime(t.createdAt)}`,
          cls: t.ok ? '' : 'ev-fail',
        })
      }
      for (const d of this.trace.delegations || []) {
        evs.push({
          time: d.createdAt,
          type: '委派',
          main: `${d.agent} → ${d.status}`,
          sub: `${fmtMs(d.durationMs)} · ${shortTime(d.createdAt)}`,
          cls: d.status === 'DONE' ? '' : 'ev-fail',
        })
      }
      for (const f of this.trace.faults || []) {
        evs.push({
          time: f.createdAt,
          type: '故障',
          main: `${f.path}：${f.detail}`,
          sub: shortTime(f.createdAt),
          cls: 'ev-warn',
        })
      }
      return evs.sort((a, b) => String(a.time).localeCompare(String(b.time)))
    },
  },
  watch: {
    intentFilter() {
      this.page = 1
      this.loadChats()
    },
    limit() {
      this.page = 1
      this.loadChats()
    },
  },
  mounted() {
    // 支持从「故障与降级」页带 sessionId 跳进来（原来靠 jumpSession 就地改列表，
    // 拆页后跨页跳转必须走 query，否则状态传不过来）
    const q = this.$route.query
    if (q.sessionId) this.sessionFilter = String(q.sessionId)
    if (q.hours) this.days = Number(q.hours) / 24 === 1 ? 1 : Number(q.hours)
    if (q.trace) this.$nextTick(() => this.openTrace(String(q.trace)))
    this.load()
  },
  methods: {
    fmtMs,
    shortTime,
    tokenText(c) {
      const p = c.promptTokens
      const t = c.completionTokens
      if (p === null && t === null) return '-'
      return `${p || 0}+${t || 0}`
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        const ov = await fetchMetricsOverview()
        this.enabled = ov.enabled !== false
        this.intents = ov.chatByIntent || []
        if (this.enabled) await this.loadChats()
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    reload() {
      this.trace = null
      this.activeTraceId = ''
      this.load()
    },
    async loadChats() {
      const params = { limit: this.limit, hours: this.days * 24 }
      if (this.intentFilter) params.intent = this.intentFilter
      if (this.sessionFilter) params.sessionId = this.sessionFilter
      this.chats = (await fetchMetricsChats(params)).chats || []
    },
    clearSessionFilter() {
      this.sessionFilter = ''
      this.page = 1
      this.loadChats()
    },
    /** 翻页只切内存切片，不重新请求（数据已整批在手） */
    goPage(p) {
      this.page = p
    },
    /** 换每页条数后回到第 1 页，避免落在越界页看到空表 */
    goPageSize(size) {
      this.pageSize = size
      this.page = 1
    },
    async openTrace(traceId) {
      this.activeTraceId = traceId
      this.trace = await fetchMetricsTrace(traceId)
      this.trace.traceId = traceId
      // 列表不带工具步数（避免 N+1 请求），追溯一次顺手回填该行
      const row = this.chats.find((c) => c.traceId === traceId)
      if (row && this.trace.found) row.toolCount = (this.trace.tools || []).length
      this.$nextTick(() => {
        const el = document.querySelector('.trace-panel')
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      })
    },
    closeTrace() {
      this.trace = null
      this.activeTraceId = ''
    },
    /**
     * 打开执行流程弹窗。
     * 若该 trace 已经在本页拉过（trace 面板展开中），直接复用那份数据；
     * 否则单独请求一次——弹窗与面板的生命周期相互独立，不能共享 activeTraceId 状态。
     */
    async openPipeline(traceId) {
      this.pipeTraceId = traceId
      this.pipeOpen = true
      if (this.trace && this.trace.traceId === traceId && this.trace.found) {
        this.pipeData = this.trace
        return
      }
      this.pipeLoading = true
      try {
        const d = await fetchMetricsTrace(traceId)
        d.traceId = traceId
        this.pipeData = d
      } catch (e) {
        this.pipeData = null
      } finally {
        this.pipeLoading = false
      }
    },
    closePipeline() {
      this.pipeOpen = false
    },
    async exportCsv() {
      const res = await fetchMetricsExport({ view: 'chats', hours: this.days * 24 })
      if (res.content) downloadTextFile(res.filename || 'metrics_chats.csv', res.content)
    },
  },
}
</script>

<style scoped>
.panel {
  border: 1px solid #eef0f2;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
  background: #fff;
}
.panel h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: #1f2328;
  display: flex;
  align-items: center;
  gap: 8px;
}
.tbl-wrap {
  overflow-x: auto;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.tbl th {
  text-align: left;
  color: #6b7280;
  font-weight: 500;
  padding: 6px 8px;
  border-bottom: 1px solid #eef0f2;
  white-space: nowrap;
}
.tbl td {
  padding: 6px 8px;
  border-bottom: 1px solid #f5f6f8;
}
.tbl.clickable tbody tr {
  cursor: pointer;
}
.tbl.clickable tbody tr:hover {
  background: #f6f9fd;
}
.tbl.clickable tbody tr.active {
  background: #eaf2fb;
}
.nowrap {
  white-space: nowrap;
}
.mono {
  font-family: Consolas, Menlo, monospace;
  font-size: 12px;
}
.muted {
  color: #8a919c;
  font-size: 12px;
}
.pad {
  padding: 8px 0;
  margin: 0;
  line-height: 1.7;
}
.sel {
  border: 1px solid #d8dce2;
  border-radius: 6px;
  padding: 5px 8px;
  font-size: 12px;
  color: #2c3e50;
  background: #fff;
}
.btn {
  border: 1px solid #d8dce2;
  background: #fff;
  color: #2c3e50;
  border-radius: 6px;
  padding: 6px 14px;
  cursor: pointer;
  font-size: 13px;
}
.btn:hover:not(:disabled) {
  border-color: #185fa5;
  color: #185fa5;
}
.btn.small {
  padding: 2px 10px;
  font-size: 12px;
  margin-left: 10px;
}
.filters {
  display: flex;
  gap: 14px;
  align-items: center;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.tag {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
  background: #f1f3f5;
  color: #444;
}
.ok-tag {
  background: #e1f5ee;
  color: #0f6e56;
}
.err-tag {
  background: #fdeceb;
  color: #b42318;
}
.warn-tag {
  background: #faeeda;
  color: #854f0b;
  margin-left: 4px;
}
.err-text {
  color: #b42318;
}
.link {
  color: #185fa5;
  cursor: pointer;
  text-decoration: none;
}
/* 流程详情按钮：独立于行点击（.stop 阻止冒泡），避免"想看流程却展开了轨迹面板" */
.flow-btn {
  border: 1px solid #d8dce2;
  background: #fff;
  color: #185fa5;
  border-radius: 5px;
  padding: 3px 10px;
  font-size: 11.5px;
  cursor: pointer;
  white-space: nowrap;
}
.flow-btn:hover {
  border-color: #185fa5;
  background: #f4f8fd;
}
.trace-panel {
  border-color: #cfe0f2;
  background: #fbfdff;
}
.chat-summary {
  font-size: 13px;
  color: #444;
  margin: 0 0 6px;
}
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
}
.timeline li {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 6px 0 6px 14px;
  border-left: 2px solid #d8e4f0;
  font-size: 13px;
}
.timeline li.ev-fail {
  border-left-color: #b42318;
}
.timeline li.ev-warn {
  border-left-color: #ba7517;
}
.ev-type {
  flex-shrink: 0;
  font-size: 11px;
  color: #185fa5;
  background: #eaf2fb;
  border-radius: 4px;
  padding: 1px 6px;
}
.ev-fail .ev-type {
  color: #b42318;
  background: #fdeceb;
}
.ev-warn .ev-type {
  color: #854f0b;
  background: #faeeda;
}
.ev-main {
  color: #333;
}
.ev-sub {
  flex-shrink: 0;
  margin-left: auto;
}
</style>

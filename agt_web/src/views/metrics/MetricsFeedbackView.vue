<template>
  <DataPage title="反馈与金标集" subtitle="对某次回答打分 → 反馈挂 trace → 高分/低分会话进评测候选池（需人工补 expected_* 才能入集）">
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <button class="btn" :disabled="loading" @click="exportCsv">导出 CSV</button>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
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
      title="反馈数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <!-- 打分区：原挂在会话明细的 trace 面板里，拆页后挪到这里，
           并支持从会话明细页带 ?trace=xxx 直接跳过来给某一条打分 -->
      <div class="panel">
        <h3>
          给某次回答打分
          <span class="hint">反馈按 trace 挂载，重复提交只记最新</span>
        </h3>
        <div class="rate-row">
          <label class="rate-field">
            <span class="rate-label">traceId</span>
            <input
              v-model.trim="rateTraceId"
              class="rate-input mono"
              placeholder="32 位十六进制，或直接从下方列表点「打分」"
            />
          </label>
          <button class="btn" :disabled="rateLoading || !rateTraceId" @click="loadTrace">
            {{ rateLoading ? '查询中…' : '载入该 trace' }}
          </button>
        </div>

        <div v-if="rateTrace" class="rate-body">
          <p v-if="!rateTrace.found" class="muted pad">未找到该 trace 的记录，请确认 traceId 是否正确。</p>
          <template v-else>
            <p class="chat-summary">
              <span class="tag">{{ rateTrace.chat.intent }}</span>
              <span v-if="rateTrace.chat.mode" class="tag">{{ rateTrace.chat.mode }}</span>
              模型 {{ rateTrace.chat.model || '-' }} · 耗时 {{ fmtMs(rateTrace.chat.durationMs) }} ·
              回复 {{ rateTrace.chat.contentChars }} 字 ·
              工具 {{ (rateTrace.tools || []).length }} 步 · 委派 {{ (rateTrace.delegations || []).length }} 次
              <span v-if="(rateTrace.faults || []).length" class="err-text">
                · 含 {{ rateTrace.faults.length }} 条故障
              </span>
            </p>

            <div class="fb-row">
              <span class="muted">这次回答有用吗？</span>
              <button
                class="btn small"
                :class="{ 'fb-active': fbRating === 'up' }"
                :disabled="fbSubmitting"
                @click="submitFeedback('up')"
              >👍 有用</button>
              <button
                class="btn small"
                :class="{ 'fb-active fb-down': fbRating === 'down' }"
                :disabled="fbSubmitting"
                @click="submitFeedback('down')"
              >👎 没用</button>
              <input
                v-model="fbComment"
                class="fb-input"
                maxlength="200"
                placeholder="备注（可选，≤200 字）"
              />
              <span class="muted">{{ fbMsg }}</span>
            </div>

            <p class="muted pad">
              想先看完整轨迹再决定？
              <router-link class="link" :to="{ path: '/dashboard/conversations', query: { trace: rateTrace.traceId } }">
                去「会话明细」看轨迹 →
              </router-link>
            </p>
          </template>
        </div>
      </div>

      <div class="panel">
        <h3>
          反馈记录
          <span class="hint">{{ winText }} · {{ feedback.up }} 👍 / {{ feedback.down }} 👎</span>
          <button class="btn small" @click="exportCandidates">导出候选 JSONL</button>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>时间</th><th>评分</th><th>意图</th><th>模式</th><th>备注</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedRows.items.length">
                <td colspan="6" class="muted pad">暂无反馈。在上方载入一个 trace 即可打分。</td>
              </tr>
              <tr v-for="row in pagedRows.items" :key="row.traceId + row.createdAt">
                <td class="nowrap">{{ shortTime(row.createdAt) }}</td>
                <td>
                  <span class="tag" :class="row.rating === 'up' ? 'ok-tag' : 'err-tag'">
                    {{ row.rating === 'up' ? '👍 有用' : '👎 没用' }}
                  </span>
                </td>
                <td>{{ row.intent || '-' }}</td>
                <td>{{ row.mode || '-' }}</td>
                <td class="fb-comment" :title="row.comment">{{ row.comment || '-' }}</td>
                <td class="fb-actions">
                  <a class="link" @click="goTrace(row.traceId)">看轨迹</a>
                  <a class="link" @click="rateAgain(row.traceId)">重新打分</a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedRows.page"
          :total-pages="pagedRows.totalPages"
          :page-size="pagedRows.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
        <p class="muted pad">
          候选池是**起点不是自动金标**：导出的 JSONL 里 expected_* 字段留空，需人工审核补齐后才能进评测集
          （qid 形如 <span class="mono">fb-&lt;traceId&gt;</span>）。
        </p>
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 反馈与金标集页（原看板「P2 反馈闭环 + 金标集候选」面板 + trace 面板里的打分控件）。
 *
 * 为什么把打分从 trace 面板挪走：打分的入口对象是 trace，但打分的**意图**
 * 是运营（积累评测候选），和"看轨迹排障"不是一回事。原来两件事挤在一个面板里，
 * 导致用户想看一眼轨迹就被问"这次回答有用吗"。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import {
  fetchMetricsOverview, fetchMetricsTrace, submitMetricsFeedback, fetchMetricsFeedback,
  fetchMetricsFeedbackExport, fetchMetricsExport, downloadTextFile,
} from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, fmtMs, shortTime, pct,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsFeedbackView',
  components: { DataPage, StatCards, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      feedback: { up: 0, down: 0, rows: [] },
      // 打分区
      rateTraceId: '',
      rateTrace: null,
      rateLoading: false,
      fbRating: '',
      fbComment: '',
      fbMsg: '',
      fbSubmitting: false,
      // 本页还有 KPI 卡片与打分交互区 → 有可视化内容，每页 10 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    /** 前端切片视图（feedback 接口只认 limit，分页在内存里做） */
    pagedRows() {
      return paginate(this.feedback.rows, this.page, this.pageSize)
    },
    winText() {
      return windowText(this.days)
    },
    total() {
      return (this.feedback.up || 0) + (this.feedback.down || 0)
    },
    satisfyRate() {
      return this.total ? Math.round((this.feedback.up / this.total) * 100) : null
    },
    cards() {
      return [
        { label: `反馈总数（${this.winText}）`, value: this.total, sub: `👍 ${this.feedback.up} · 👎 ${this.feedback.down}` },
        {
          label: '满意率',
          value: this.satisfyRate === null ? '-' : `${this.satisfyRate}%`,
          sub: 'up / (up + down) · < 80% 需关注',
          tone: this.satisfyRate === null ? '' : this.satisfyRate >= 80 ? 'ok' : 'err',
        },
        {
          label: '待审核候选',
          value: this.feedback.rows.filter((r) => r.rating === 'up').length,
          sub: '👍 反馈默认进候选池',
        },
        {
          label: '负反馈',
          value: this.feedback.down,
          sub: '👎 是知识库/提示词改进的主要输入',
          tone: this.feedback.down ? 'warn' : 'ok',
        },
      ]
    },
  },
  mounted() {
    const q = this.$route.query
    if (q.trace) {
      this.rateTraceId = String(q.trace)
      this.$nextTick(() => this.loadTrace())
    }
    this.load()
  },
  methods: {
    fmtMs,
    shortTime,
    pct,
    async load() {
      this.loading = true
      this.error = ''
      try {
        const ov = await fetchMetricsOverview()
        this.enabled = ov.enabled !== false
        if (this.enabled) await this.loadFeedback()
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    async loadFeedback() {
      const res = await fetchMetricsFeedback({ hours: this.days * 24, limit: 100 })
      this.feedback = { up: res.up || 0, down: res.down || 0, rows: res.rows || [] }
      this.page = 1
    },
    goPage(p) {
      this.page = p
    },
    goPageSize(size) {
      this.pageSize = size
      this.page = 1
    },
    async loadTrace() {
      if (!this.rateTraceId) return
      this.rateLoading = true
      this.fbMsg = ''
      try {
        const t = await fetchMetricsTrace(this.rateTraceId)
        t.traceId = this.rateTraceId
        this.rateTrace = t
        this.fbRating = ''
        this.fbComment = ''
      } catch (e) {
        this.fbMsg = e.message || '查询失败'
      } finally {
        this.rateLoading = false
      }
    },
    rateAgain(traceId) {
      this.rateTraceId = traceId
      this.loadTrace()
      window.scrollTo({ top: 0, behavior: 'smooth' })
    },
    async submitFeedback(rating) {
      if (!this.rateTrace || !this.rateTrace.found) return
      this.fbSubmitting = true
      this.fbMsg = ''
      try {
        const res = await submitMetricsFeedback({
          traceId: this.rateTrace.traceId,
          rating,
          sessionId: this.rateTrace.chat?.sessionId || '',
          comment: this.fbComment,
        })
        if (res.recorded) {
          this.fbRating = rating
          this.fbMsg = '已记录，感谢反馈'
          this.loadFeedback()
        } else {
          this.fbMsg = res.reason || '提交失败'
        }
      } catch (e) {
        this.fbMsg = e.message || '提交失败'
      } finally {
        this.fbSubmitting = false
      }
    },
    goTrace(traceId) {
      this.$router.push({ path: '/dashboard/conversations', query: { trace: traceId } })
    },
    async exportCandidates() {
      const res = await fetchMetricsFeedbackExport({})
      if (!res.count) {
        this.fbMsg = '暂无候选数据'
        return
      }
      const day = new Date().toISOString().slice(0, 10).replaceAll('-', '')
      downloadTextFile(`feedback_candidates_${day}.jsonl`, res.jsonl + '\n')
    },
    async exportCsv() {
      const res = await fetchMetricsExport({ view: 'feedback', hours: this.days * 24 })
      if (res.content) downloadTextFile(res.filename || 'metrics_feedback.csv', res.content)
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
.hint {
  font-size: 11px;
  font-weight: 400;
  color: #8a919c;
}
.rate-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.rate-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.rate-label {
  font-size: 11px;
  color: #6b7280;
}
.rate-input {
  border: 1px solid #d8dce2;
  border-radius: 6px;
  padding: 6px 9px;
  font-size: 12px;
  color: #2c3e50;
}
.rate-body {
  margin-top: 12px;
  border-top: 1px dashed #eef0f2;
  padding-top: 10px;
}
.chat-summary {
  font-size: 13px;
  color: #444;
  margin: 0 0 10px;
}
.fb-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.fb-input {
  border: 1px solid #d8dce2;
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
  width: 210px;
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
}
.tbl td {
  padding: 6px 8px;
  border-bottom: 1px solid #f5f6f8;
}
.nowrap {
  white-space: nowrap;
}
.fb-comment {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fb-actions {
  display: flex;
  gap: 10px;
  white-space: nowrap;
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
.err-text {
  color: #b42318;
}
.mono {
  font-family: Consolas, Menlo, monospace;
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
.link {
  color: #185fa5;
  cursor: pointer;
  text-decoration: none;
  font-size: 12px;
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
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn.small {
  padding: 3px 10px;
  font-size: 12px;
  margin-left: 6px;
}
.fb-active {
  border-color: #185fa5;
  color: #185fa5;
  background: #eaf2fb;
}
.fb-active.fb-down {
  border-color: #b42318;
  color: #b42318;
  background: #fdeceb;
}
</style>

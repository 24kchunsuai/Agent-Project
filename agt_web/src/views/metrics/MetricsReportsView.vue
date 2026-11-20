<template>
  <DataPage title="报表导出" :subtitle="`把观测数据导出成表格，供离线分析与归档 · 数据窗口：${winText}`">
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
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
      title="报表数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>CSV 报表<span class="hint">{{ winText }} · utf-8 带 BOM，Excel 可直接打开</span></h3>
        <div class="report-grid">
          <div v-for="r in csvReports" :key="r.view" class="report-card">
            <div class="report-head">
              <span class="report-icon">{{ r.icon }}</span>
              <div>
                <div class="report-title">{{ r.title }}</div>
                <div class="report-desc muted">{{ r.desc }}</div>
              </div>
            </div>
            <div class="report-foot">
              <span class="muted">列：{{ r.cols.join('、') }}</span>
              <button class="btn small" :disabled="busy === r.view" @click="download(r)">
                {{ busy === r.view ? '导出中…' : '导出 CSV' }}
              </button>
            </div>
          </div>
        </div>
        <p class="muted pad">
          导出复用各页已有的查询口径（不新写 SQL 规则），属 ADS 层消费；
          前端只负责把 Java 透传回来的文本落盘，不直连明细库（规约 §6）。
        </p>
      </div>

      <div class="panel">
        <h3>评测金标集候选<span class="hint">JSONL</span></h3>
        <p class="muted pad">
          把 👍/👎 反馈过的会话导出为评测候选（qid 形如 <span class="mono">fb-&lt;traceId&gt;</span>）。
          候选池只是起点——<strong>expected_* 字段留空，必须人工审核补齐</strong>后才会进评测集，
          不会自动写入 eval 目录。
        </p>
        <button class="btn" :disabled="busy === 'candidates'" @click="exportCandidates">
          {{ busy === 'candidates' ? '导出中…' : '导出候选 JSONL' }}
        </button>
        <span class="muted msg">{{ msg }}</span>
      </div>

      <div class="panel">
        <h3>指标口径速查<span class="hint">改口径必须同步改规约 §5 指标字典</span></h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>指标</th><th>口径</th><th>数据源</th></tr></thead>
            <tbody>
              <tr v-for="m in pagedDict.items" :key="m.name">
                <td>{{ m.name }}</td>
                <td class="muted">{{ m.def }}</td>
                <td class="mono muted">{{ m.src }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedDict.page"
          :total-pages="pagedDict.totalPages"
          :page-size="pagedDict.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="false"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 报表导出页（新增）。
 *
 * 为什么需要：导接口（/metrics/export 五个 view + 反馈候选 JSONL）原本只是
 * 看板头部一个「导出 CSV」按钮，点下去导的永远是会话明细——其余四个视图
 * 有接口但前端没有入口（"后端有能力、前端没展示"的典型）。单独成页后
 * 五个视图 + JSONL 各给一个明确入口，并附口径速查表。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import {
  fetchMetricsOverview, fetchMetricsExport, fetchMetricsFeedbackExport, downloadTextFile,
} from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsReportsView',
  components: { DataPage, StatCards, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      busy: '',
      msg: '',
      chatTotal: 0,
      toolCount: 0,
      faultCount: 0,
      feedbackTotal: 0,
      csvReports: [
        {
          view: 'chats', icon: '🧭', title: '会话明细', desc: '逐轮会话：意图/模式/结果/耗时/tokens',
          cols: ['traceId', 'sessionId', 'intent', 'mode', 'ok', 'degraded', 'durationMs', 'promptTokens', 'completionTokens', 'createdAt'],
        },
        {
          view: 'tools', icon: '🔧', title: '工具调用', desc: '按工具的调用次数/失败次数/失败率/平均耗时',
          cols: ['tool', '总调用', '失败次数', '失败率', '平均耗时ms'],
        },
        {
          view: 'delegations', icon: '🤝', title: '子智能体委派', desc: '子智能体 × 状态 × 次数',
          cols: ['agent', 'status', '次数'],
        },
        {
          view: 'faults', icon: '⚠️', title: '故障明细', desc: '故障路径/会话/详情/时间',
          cols: ['path', 'sessionId', 'detail', 'createdAt'],
        },
        {
          view: 'feedback', icon: '⭐', title: '反馈明细', desc: '反馈挂载的 trace/评分/备注',
          cols: ['traceId', 'rating', 'intent', 'mode', 'comment', 'createdAt'],
        },
      ],
      metricsDict: [
        { name: '会话成功率', def: '(会话量 − 失败数) / 会话量', src: 'agent_chat_log' },
        { name: '降级率', def: '降级数 / 会话量', src: 'agent_chat_log.degraded' },
        { name: 'p95 时延', def: 'nearest-rank 近似值（非插值）', src: 'agent_chat_log.duration_ms' },
        { name: '工具失败率', def: '失败次数 / 调用次数（跨天按 total 加权）', src: 'agent_tool_call' },
        { name: '委派完成率', def: 'DONE / 全部委派', src: 'agent_delegation' },
        { name: '反馈满意率', def: 'up / (up + down)，二值评分', src: 'agent_feedback' },
        { name: '告警关联', def: '委派 ↔ jsdstu 告警按 triggeredAt ±30 分钟近似', src: 'agent_delegation × jsdstu' },
      ],
      // 本页还有 KPI 卡片与导出卡片区 → 有可视化内容，每页 10 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    /** 口径速查是静态字典，同样走前端切片，翻页体验与其它页一致 */
    pagedDict() {
      return paginate(this.metricsDict, this.page, this.pageSize)
    },
    cards() {
      return [
        { label: `会话量（${this.winText}）`, value: this.chatTotal, sub: '可导出「会话明细」CSV' },
        { label: '工具种类', value: this.toolCount, sub: '可导出「工具调用」CSV' },
        { label: '故障记录', value: this.faultCount, sub: '可导出「故障明细」CSV', tone: this.faultCount ? 'warn' : 'ok' },
        { label: '反馈记录', value: this.feedbackTotal, sub: '可导出「反馈明细」CSV / JSONL' },
      ]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    goPage(p) {
      this.page = p
    },
    goPageSize(size) {
      this.pageSize = size
      this.page = 1
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        const ov = await fetchMetricsOverview()
        this.enabled = ov.enabled !== false
        if (this.enabled) {
          const chat = ov.chatLog24h || {}
          this.chatTotal = chat.total || 0
          this.toolCount = (ov.toolCalls24h || []).length
          this.faultCount = (ov.recentFaults || []).length
          this.feedbackTotal = ((ov.feedback24h || {}).up || 0) + ((ov.feedback24h || {}).down || 0)
        }
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    async download(r) {
      this.busy = r.view
      this.msg = ''
      try {
        const res = await fetchMetricsExport({ view: r.view, hours: this.days * 24 })
        if (res.content) {
          downloadTextFile(res.filename || `metrics_${r.view}.csv`, res.content)
          this.msg = `已导出「${r.title}」`
        } else {
          this.msg = res.reason || '导出失败'
        }
      } catch (e) {
        this.msg = e.message || '导出失败'
      } finally {
        this.busy = ''
      }
    },
    async exportCandidates() {
      this.busy = 'candidates'
      this.msg = ''
      try {
        const res = await fetchMetricsFeedbackExport({})
        if (!res.count) {
          this.msg = '暂无候选数据（先去「反馈与金标集」页给几次回答打分）'
          return
        }
        const day = new Date().toISOString().slice(0, 10).replaceAll('-', '')
        downloadTextFile(`feedback_candidates_${day}.jsonl`, res.jsonl + '\n')
        this.msg = `已导出 ${res.count} 条候选`
      } catch (e) {
        this.msg = e.message || '导出失败'
      } finally {
        this.busy = ''
      }
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
  flex-wrap: wrap;
}
.hint {
  font-size: 11px;
  font-weight: 400;
  color: #8a919c;
}
.report-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.report-card {
  border: 1px solid #eef0f2;
  border-radius: 9px;
  padding: 12px 14px;
  background: #fbfcfd;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.report-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.report-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.report-title {
  font-size: 13px;
  font-weight: 500;
  color: #1f2328;
}
.report-desc {
  margin-top: 3px;
  line-height: 1.5;
}
.report-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-top: 1px dashed #eef0f2;
  padding-top: 9px;
}
.report-foot .muted {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
.msg {
  margin-left: 10px;
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
  flex-shrink: 0;
}
</style>

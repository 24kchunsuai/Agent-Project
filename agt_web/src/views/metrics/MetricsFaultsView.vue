<template>
  <DataPage title="故障与降级" :subtitle="`故障明细 + 降级计数器（进程累计） · 数据窗口：${winText}`">
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
      title="故障数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>故障趋势<span class="hint">{{ winText }}</span></h3>
        <BarList
          :rows="faultTrend"
          label-key="day"
          value-key="value"
          empty-text="单日窗口暂不画趋势，切到 7 天或 30 天"
          :is-warn="() => true"
        />
      </div>

      <div class="panel">
        <h3>
          降级计数器
          <span class="hint">进程累计，不随窗口变化</span>
        </h3>
        <div v-if="!counters.length" class="muted pad">无降级发生</div>
        <div v-else class="chips">
          <span v-for="c in counters" :key="c.name" class="chip" :class="{ hot: c.value >= 10 }">
            {{ c.name }} × {{ c.value }}
          </span>
        </div>
        <p class="muted pad">
          降级 = 主链路某个依赖不可用时，系统主动退化成可用形态（如 LLM 超时走兜底话术、
          向量库不可用退化为关键词检索）而不是直接报错。计数是进程级的，重启归零；
          超过 10 次标红提示需要排查。
        </p>
      </div>

      <div class="panel">
        <h3>
          故障明细
          <span class="hint">最近 20 条 · 按路径过滤</span>
          <select v-if="pathFilter" v-model="pathFilter" class="sel small" @change="goPage(1)">
            <option :value="pathFilter">{{ pathFilter }}</option>
            <option value="">全部路径</option>
          </select>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>故障路径</th><th>详情</th><th>时间</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedFaults.items.length"><td colspan="4" class="muted pad">暂无故障记录</td></tr>
              <tr v-for="(f, i) in pagedFaults.items" :key="i">
                <td><span class="tag warn-tag">{{ f.path }}</span></td>
                <td class="detail" :title="f.detail">{{ f.detail }}</td>
                <td class="nowrap muted">{{ shortTime(f.createdAt) }}</td>
                <td>
                  <a v-if="f.sessionId" class="link" @click="goSession(f.sessionId)">查会话 →</a>
                  <span v-else class="muted">-</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedFaults.page"
          :total-pages="pagedFaults.totalPages"
          :page-size="pagedFaults.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 故障与降级页（原看板「视图 4：故障视图」+ 故障趋势）。
 *
 * 拆出来单独成页的理由：这是排障第一现场。原实现把降级计数器和最近故障
 * 塞在一个两列 grid 的右半边，左边是委派表——排障时要在一屏里绕开委派数据
 * 才能读到故障，且"最近 20 条"没有过滤，路径一多就找不着。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import {
  fetchMetricsOverview, fetchMetricsTimeseries, fetchMetricsExport, downloadTextFile,
} from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, shortTime, fmtDay,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsFaultsView',
  components: { DataPage, StatCards, BarList, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      faultCounters: {},
      recentFaults: [],
      faultTrend: [],
      chatTotal: 0,
      pathFilter: '',
      // 本页还有 KPI 卡片、故障趋势条形图与降级计数器 → 有可视化内容，每页 10 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    counters() {
      return Object.entries(this.faultCounters || {})
        .map(([name, value]) => ({ name, value }))
        .sort((a, b) => b.value - a.value)
    },
    counterTotal() {
      return this.counters.reduce((s, c) => s + c.value, 0)
    },
    faults() {
      if (!this.pathFilter) return this.recentFaults
      return this.recentFaults.filter((f) => f.path === this.pathFilter)
    },
    /** 前端切片视图（recentFaults 来自 overview，固定 LIMIT 20、无分页参数） */
    pagedFaults() {
      return paginate(this.faults, this.page, this.pageSize)
    },
    windowFaultTotal() {
      return this.faultTrend.reduce((s, p) => s + (p.value || 0), 0)
    },
    cards() {
      return [
        {
          label: '故障记录（列表内）',
          value: this.recentFaults.length,
          sub: `展示最近 20 条${this.pathFilter ? ` · 已过滤 ${this.pathFilter}` : ''}`,
          tone: this.recentFaults.length ? 'warn' : 'ok',
        },
        {
          label: `窗口内故障数（${this.winText}）`,
          value: this.days <= 1 ? this.recentFaults.length : this.windowFaultTotal,
          sub: this.days <= 1 ? '单日窗口按明细条数计' : '读 DWS 每日聚合',
          tone: 'warn',
        },
        {
          label: '降级累计（进程）',
          value: this.counterTotal,
          sub: `${this.counters.length} 类降级点`,
          tone: this.counterTotal ? 'warn' : 'ok',
        },
        {
          label: '会话量（24h）',
          value: this.chatTotal,
          sub: '作为故障率的分母参考',
        },
      ]
    },
  },
  mounted() {
    const q = this.$route.query
    if (q.path) this.pathFilter = String(q.path)
    this.load()
  },
  methods: {
    shortTime,
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
        this.faultCounters = ov.faultCounters || {}
        this.recentFaults = ov.recentFaults || []
        this.chatTotal = (ov.chatLog24h || {}).total || 0
        this.page = 1
        if (this.enabled) await this.loadTrend()
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    async loadTrend() {
      if (this.days <= 1) {
        this.faultTrend = []
        return
      }
      const to = fmtDay(new Date())
      const from = fmtDay(new Date(Date.now() - (this.days - 1) * 86400000))
      const res = await fetchMetricsTimeseries({ metric: 'fault_total', from, to })
      this.faultTrend = (res.series || []).filter((p) => p.dim === '')
    },
    goSession(sessionId) {
      this.$router.push({ path: '/dashboard/conversations', query: { sessionId } })
    },
    async exportCsv() {
      const res = await fetchMetricsExport({ view: 'faults', hours: this.days * 24 })
      if (res.content) downloadTextFile(res.filename || 'metrics_faults.csv', res.content)
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
.detail {
  max-width: 520px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #444;
}
.nowrap {
  white-space: nowrap;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  border: 1px solid #eef0f2;
  border-radius: 12px;
  padding: 3px 11px;
  font-size: 12px;
  background: #fbfcfd;
}
.chip.hot {
  background: #faeeda;
  color: #854f0b;
  border-color: #f0dcbc;
}
.tag {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
  background: #f1f3f5;
  color: #444;
}
.warn-tag {
  background: #faeeda;
  color: #854f0b;
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
.sel.small {
  padding: 2px 6px;
  font-size: 11px;
  font-weight: 400;
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
</style>

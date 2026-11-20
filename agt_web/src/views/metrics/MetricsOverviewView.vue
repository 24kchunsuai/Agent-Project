<template>
  <DataPage title="数据总览" :subtitle="`数据窗口：${winText} · 埋点明细不落内容原文（规约红线）`">
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
    </template>

    <EmptyState
      v-if="loaded && enabled === false"
      kind="off"
      title="可观测埋点未开启（telemetry_enabled=false）"
      :lines="['如需查看总览数据，请在 agt_python/.env 中设置 telemetry_enabled=true 并重启 Python 服务。']"
    />
    <EmptyState
      v-else-if="error"
      kind="error"
      title="总览数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线后点「刷新」。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="grid-2">
        <div class="panel">
          <h3>会话量趋势<span class="hint">{{ winText }}</span></h3>
          <BarList
            :rows="chatTrend"
            label-key="day"
            value-key="value"
            empty-text="单日窗口暂不画趋势，切到 7 天或 30 天"
          />
        </div>
        <div class="panel">
          <h3>故障趋势<span class="hint">{{ winText }}</span></h3>
          <BarList
            :rows="faultTrend"
            label-key="day"
            value-key="value"
            empty-text="单日窗口暂不画趋势，切到 7 天或 30 天"
          />
        </div>
      </div>

      <div class="panel">
        <h3>意图分布<span class="hint">近 24 小时</span></h3>
        <BarList
          :rows="intentRows"
          label-key="intent"
          value-key="count"
          empty-text="暂无会话记录"
          :formatter="(r) => `${r.count} 次 · ${pct(r.count, chatTotal)}`"
        />
      </div>

      <div class="panel">
        <h3>
          前端错误
          <span class="hint">{{ winText }} · 浏览器侧运行时报错（E-12）</span>
          <router-link class="more" :to="{ path: '/dashboard/client-errors' }">查看全部 →</router-link>
        </h3>
        <!-- 独立请求、独立失败：前端错误这一块拿不到数据**不能连累总览**，
             否则"浏览器真炸了"的时候恰好看不到总览（最需要看的时候看不到）。 -->
        <p v-if="clientErrorError" class="muted pad">
          前端错误数据暂不可用（{{ clientErrorError }}）——不影响上方总览数据。
        </p>
        <template v-else>
          <div class="grid-2 inner">
            <div>
              <p class="sub-title">按页面<span class="hint">上报次数降序</span></p>
              <BarList
                :rows="clientError.byPage || []"
                label-key="name"
                value-key="value"
                empty-text="窗口内没有前端报错"
                :is-warn="() => true"
              />
            </div>
            <div>
              <p class="sub-title">按前端版本<span class="hint">是不是这一版刚引入的</span></p>
              <BarList
                :rows="clientError.byVersion || []"
                label-key="name"
                value-key="value"
                empty-text="窗口内没有前端报错"
              />
            </div>
          </div>
          <p class="muted pad">
            共 {{ clientError.total || 0 }} 次上报 · {{ clientError.distinct || 0 }} 类错误。
            「次数」是<strong>上报次数</strong>（前端同一页面加载内同一错误只报一次、服务端再按 1 分钟合并），
            是真实次数的<strong>下界</strong>。
          </p>
        </template>
      </div>

      <div class="panel">
        <h3>关键指标口径</h3>
        <p class="muted pad">
          会话成功率 = (会话量 − 失败数) / 会话量；降级率 = 降级数 / 会话量；
          委派完成率 = DONE / 全部委派。p95 时延为 nearest-rank 近似值（指标字典已注明）。
          口径唯一来源：数据中台开发规约 §5 指标字典。
        </p>
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 数据总览页。
 *
 * 定位：一屏回答「系统现在健不健康」——四个 KPI + 两条趋势 + 意图构成。
 * 原先这些和会话明细、trace、告警全挤在一个页面里，导致首屏被无关内容
 * 淹没、且越加越长。现在细节各自成页，这里只留该看的汇总。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import { fetchMetricsOverview, fetchMetricsTimeseries, fetchMetricsCosts } from '../../api/metrics'
import { WINDOW_OPTIONS, windowText, fmtMs, fmtDay, pct } from '../../utils/metrics'

export default {
  name: 'MetricsOverviewView',
  components: { DataPage, StatCards, BarList, EmptyState },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      overview: {},
      chatTrend: [],
      faultTrend: [],
      // E-12 前端错误：单独一份数据 + 单独一个错误字段（见模板里的注释：不连累总览）
      clientError: { total: 0, distinct: 0, byPage: [], byVersion: [] },
      clientErrorError: '',
      // E-13 成本：同样独立取数、独立失败（成本查不到不能把总览打成加载失败）
      costs: { totals: {}, quota: {} },
      costsError: '',
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    chat() {
      return this.overview.chatLog24h || { total: 0, failed: 0, degraded: 0, avgDurationMs: null, p95DurationMs: null }
    },
    chatTotal() {
      return this.chat.total || 0
    },
    intentRows() {
      return this.overview.chatByIntent || []
    },
    toolsTotal() {
      return (this.overview.toolCalls24h || []).reduce((s, t) => s + (t.total || 0), 0)
    },
    toolsFailed() {
      return (this.overview.toolCalls24h || []).reduce((s, t) => s + (t.failed || 0), 0)
    },
    delegationStat() {
      const rows = this.overview.delegations24h || []
      const total = rows.reduce((s, d) => s + (d.count || 0), 0)
      const done = rows.filter((d) => d.status === 'DONE').reduce((s, d) => s + (d.count || 0), 0)
      return { total, done }
    },
    cards() {
      const c = this.chat
      const rate = c.total ? Math.round(((c.total - c.failed) / c.total) * 100) : null
      return [
        {
          label: '会话量（24h）',
          value: c.total || 0,
          sub: `失败 ${c.failed || 0} · 降级 ${c.degraded || 0}`,
          tone: c.failed ? 'warn' : '',
        },
        {
          label: '成功率（24h）',
          value: rate === null ? '-' : `${rate}%`,
          sub: `端到端均耗 ${fmtMs(c.avgDurationMs)} · p95 ${fmtMs(c.p95DurationMs)}（近似）`,
          tone: rate !== null && rate < 90 ? 'err' : rate !== null ? 'ok' : '',
        },
        {
          label: '工具调用（24h）',
          value: this.toolsTotal,
          sub: `失败 ${this.toolsFailed} · 涉及 ${(this.overview.toolCalls24h || []).length} 个工具`,
          tone: this.toolsFailed ? 'warn' : '',
        },
        {
          label: '委派完成率（24h）',
          value: pct(this.delegationStat.done, this.delegationStat.total),
          sub: `DONE ${this.delegationStat.done} / 共 ${this.delegationStat.total}`,
        },
        {
          label: '反馈（24h）',
          value: `${(this.overview.feedback24h || {}).up || 0} 👍 / ${(this.overview.feedback24h || {}).down || 0} 👎`,
          sub: '满意率详见「反馈与金标集」页',
        },
        {
          // E-12：前端错误也进 KPI 行——它是"用户实际感受"的那一体：
          // 后端全绿而前端崩了的时候，只有这一个数字会动（顶栏状态灯看的是服务端，照旧是绿的）
          label: '前端错误（24h）',
          value: this.clientError.total || 0,
          sub: `${this.clientError.distinct || 0} 类 · 涉及 ${(this.clientError.byPage || []).length} 个页面`,
          tone: this.clientError.total ? 'warn' : 'ok',
        },
        {
          // E-13：成本进 KPI 行——C7 判据"这个月花了多少钱"的最短路径。
          // 成本按当前价目折算；未配价时恒为 ¥0，sub 会提示去配价目表
          label: `LLM 成本（${this.winText}）`,
          value: this.costsError
            ? '-'
            : `¥${Number((this.costs.totals || {}).costCny || 0).toFixed(2)}`,
          sub: this.costsError
            ? '成本数据暂不可用（不影响其他总览数据）'
            : ((this.costs.totals || {}).unpricedTokens
              ? `tokens ${this.fmtNum((this.costs.totals || {}).tokens)} · 有未配价模型`
              : `tokens ${this.fmtNum((this.costs.totals || {}).tokens)} · 详情见「成本与配额」页`),
          tone: this.costsError ? '' : ((this.costs.totals || {}).unpricedTokens ? 'warn' : ''),
        },
      ]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    pct,
    fmtNum(n) {
      return Number(n || 0).toLocaleString('zh-CN')
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        this.overview = await fetchMetricsOverview()
        this.enabled = this.overview.enabled !== false
        if (this.enabled) await this.loadTrend()
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
      // 前端错误独立取：**放在主 try 之外**，它失败了只让那一块显示提示，
      // 不把整页打成"总览数据加载失败"（理由见模板注释）
      if (this.enabled) await this.loadClientErrors()
      // E-13 成本同样独立取：成本查询挂了只让 KPI 卡显示 "-"，总览照常
      if (this.enabled) await this.loadCosts()
    },
    async loadCosts() {
      this.costsError = ''
      try {
        const res = await fetchMetricsCosts({ days: this.days })
        if (res.enabled !== false) {
          this.costs = { totals: res.totals || {}, quota: res.quota || {} }
        }
      } catch (e) {
        this.costsError = e.message || '加载失败'
      }
    },
    async loadClientErrors() {
      this.clientErrorError = ''
      try {
        const res = await fetchClientErrors({ hours: this.days * 24, limit: 20 })
        this.clientError = {
          total: res.total || 0,
          distinct: res.distinct || 0,
          byPage: res.byPage || [],
          byVersion: res.byVersion || [],
        }
      } catch (e) {
        this.clientErrorError = e.message || '加载失败'
      }
    },
    /** 趋势读 DWS 预聚合：单天窗口不拉（没意义且多两次请求） */
    async loadTrend() {
      if (this.days <= 1) {
        this.chatTrend = []
        this.faultTrend = []
        return
      }
      const to = fmtDay(new Date())
      const from = fmtDay(new Date(Date.now() - (this.days - 1) * 86400000))
      const [chat, fault] = await Promise.all([
        fetchMetricsTimeseries({ metric: 'chat_total', from, to }),
        fetchMetricsTimeseries({ metric: 'fault_total', from, to }),
      ])
      this.chatTrend = (chat.series || []).filter((p) => p.dim === '')
      this.faultTrend = (fault.series || []).filter((p) => p.dim === '')
    },
  },
}
</script>

<style scoped>
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 12px;
}
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
.more {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: #185fa5;
  text-decoration: none;
}
.more:hover {
  text-decoration: underline;
}
.grid-2.inner {
  margin-bottom: 4px;
}
.sub-title {
  margin: 0 0 8px;
  font-size: 12px;
  color: #6b7280;
  display: flex;
  align-items: center;
  gap: 6px;
}
.muted {
  color: #8a919c;
  font-size: 12px;
}
.pad {
  padding: 4px 0;
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
</style>

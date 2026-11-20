<template>
  <DataPage title="智能体委派" :subtitle="`主智能体 → 子智能体（alarm-analyst / metric-analyst 等）的委派执行情况 · ${winText}`">
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
      title="委派数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>各子智能体执行分布<span class="hint">近 24 小时</span></h3>
        <BarList
          :rows="agentTotal"
          label-key="agent"
          value-key="count"
          empty-text="窗口内无委派记录"
          :formatter="(r) => `${r.count} 次 · 完成 ${r.done} / 失败 ${r.failed} / 超时 ${r.timeout}`"
          :is-warn="(r) => r.failed > 0 || r.timeout > 0"
        />
      </div>

      <div class="panel">
        <h3>委派明细<span class="hint">按子智能体 × 状态</span></h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>子智能体</th><th>状态</th><th>次数</th><th>占该智能体</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedRows.items.length"><td colspan="5" class="muted pad">暂无数据</td></tr>
              <tr v-for="(r, i) in pagedRows.items" :key="r.agent + r.status + i">
                <td>{{ r.agent }}</td>
                <td><span class="tag" :class="statusClass(r.status)">{{ r.status }}</span></td>
                <td>{{ r.count }}</td>
                <td class="muted">{{ pct(r.count, agentCount(r.agent), 1) }}</td>
                <td>
                  <a
                    v-if="r.status !== 'DONE'"
                    class="link"
                    @click="goConversations(r.agent)"
                  >查失败会话 →</a>
                  <span v-else class="muted">-</span>
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
      </div>

      <div class="panel">
        <h3>什么是委派</h3>
        <p class="muted pad">
          主智能体判断问题需要专门能力时，把子任务交给子智能体执行（如告警处置交给 alarm-analyst）。
          委派状态 DONE=正常完成、FAILED=子智能体报错、TIMEOUT=超时。
          委派成功率反映的是「多智能体编排」这条链路本身的健康度，与主链路会话成功率是两回事。
          委派↔告警的关联分析见「告警处置」页。
        </p>
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 智能体委派页（原看板「子智能体委派」面板，独立成一页并补了汇总口径）。
 *
 * 为什么值得单独一页：委派是多智能体架构特有的观测对象，原先它挤在一个
 * 两列 grid 的左边、右边是故障面板，两者毫无关系；且只有一张三列的裸表，
 * 看不出「哪个子智能体在拖后腿」。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import { fetchMetricsOverview, fetchMetricsExport, downloadTextFile } from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, pct, statusClass,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsDelegationsView',
  components: { DataPage, StatCards, BarList, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      rows: [],
      alarmLink: null,
      // 本页还有 KPI 卡片与子智能体分布条形图 → 有可视化内容，每页 10 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    /** 前端切片视图（delegations24h 来自 overview 聚合，无分页参数） */
    pagedRows() {
      return paginate(this.rows, this.page, this.pageSize)
    },
    winText() {
      return windowText(this.days)
    },
    /** 按子智能体聚合：一个智能体可能有 DONE/FAILED/TIMEOUT 多行 */
    agentTotal() {
      const map = new Map()
      for (const r of this.rows) {
        const k = r.agent || '(未知)'
        if (!map.has(k)) map.set(k, { agent: k, count: 0, done: 0, failed: 0, timeout: 0 })
        const item = map.get(k)
        item.count += r.count || 0
        if (r.status === 'DONE') item.done += r.count || 0
        else if (r.status === 'FAILED') item.failed += r.count || 0
        else if (r.status === 'TIMEOUT') item.timeout += r.count || 0
      }
      return [...map.values()].sort((a, b) => b.count - a.count)
    },
    total() {
      return this.rows.reduce((s, r) => s + (r.count || 0), 0)
    },
    done() {
      return this.rows.filter((r) => r.status === 'DONE').reduce((s, r) => s + (r.count || 0), 0)
    },
    failed() {
      return this.total - this.done
    },
    cards() {
      const rate = this.total ? Math.round((this.done / this.total) * 100) : null
      return [
        { label: '委派总数（24h）', value: this.total, sub: `涉及 ${this.agentTotal.length} 个子智能体` },
        {
          label: '完成率',
          value: rate === null ? '-' : `${rate}%`,
          sub: `DONE ${this.done} / 共 ${this.total}`,
          tone: rate === null ? '' : rate >= 95 ? 'ok' : rate >= 80 ? 'warn' : 'err',
        },
        { label: '未完成', value: this.failed, sub: 'FAILED + TIMEOUT', tone: this.failed ? 'warn' : 'ok' },
        {
          label: '执行最频繁',
          value: this.agentTotal[0] ? this.agentTotal[0].agent : '-',
          sub: this.agentTotal[0] ? `${this.agentTotal[0].count} 次` : '-',
        },
      ]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    pct,
    statusClass,
    agentCount(agent) {
      const hit = this.agentTotal.find((a) => a.agent === agent)
      return hit ? hit.count : 0
    },
    /** 委派失败时，跳到会话明细页看具体是哪几轮出的问题 */
    goConversations(agent) {
      this.$router.push({ path: '/dashboard/conversations', query: { agent } })
    },
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
        this.rows = ov.delegations24h || []
        this.page = 1
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    async exportCsv() {
      const res = await fetchMetricsExport({ view: 'delegations', hours: this.days * 24 })
      if (res.content) downloadTextFile(res.filename || 'metrics_delegations.csv', res.content)
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
</style>

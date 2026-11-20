<template>
  <DataPage title="工具调用" :subtitle="`按窗口统计工具调用量/失败率/耗时 · 数据窗口：${winText}`">
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <select v-model="orderBy" class="sel" @change="load">
        <option value="calls">按调用量</option>
        <option value="fail_rate">按失败率</option>
        <option value="duration">按耗时</option>
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
      title="工具调用数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>调用量排行<span class="hint">Top {{ rows.length }} · {{ winText }}</span></h3>
        <BarList
          :rows="rows"
          label-key="tool"
          value-key="total"
          empty-text="窗口内无工具调用记录"
          :is-warn="(r) => r.failed > 0"
          :formatter="(r) => `${r.failed > 0 ? r.failed + '✗ / ' : ''}${r.total} 次 · ${fmtMs(r.avgDurationMs)}`"
        />
      </div>

      <div class="panel">
        <h3>明细</h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>工具</th><th>调用次数</th><th>失败次数</th>
                <th>失败率</th><th>平均耗时</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!pagedRows.items.length"><td colspan="6" class="muted pad">暂无数据</td></tr>
              <tr v-for="r in pagedRows.items" :key="r.tool">
                <td :class="{ 'fail-text': r.failed > 0 }">{{ r.tool }}</td>
                <td>{{ r.total }}</td>
                <td :class="{ 'fail-text': r.failed > 0 }">{{ r.failed || 0 }}</td>
                <td>
                  <span class="tag" :class="failTagClass(r)">{{ pct(r.failed, r.total, 1) }}</span>
                </td>
                <td>{{ fmtMs(r.avgDurationMs) }}</td>
                <td>
                  <a class="link" @click="goFaults(r.tool)">查相关故障 →</a>
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
          失败率 = 失败次数 / 调用次数。耗时取平均；窗口跨天时按 total 加权（tools/top 读 ODS
          而非 DWS，原因见数据中台开发规约 §8 P1 完成记录）。
        </p>
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 工具调用页（原看板「视图 3：工具视图」）。
 *
 * 原来它和一个意图分布条形图挤在同一个 grid 里，两件事其实没关系；
 * 拆出来后本页只讲工具：排行条形 + 明细表 + 失败率口径说明。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import { fetchMetricsOverview, fetchMetricsToolsTop, fetchMetricsExport, downloadTextFile } from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, fmtMs, pct,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsToolsView',
  components: { DataPage, StatCards, BarList, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      orderBy: 'calls',
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      rows: [],
      feedback24h: { up: 0, down: 0 },
      // 本页还有 KPI 卡片与调用量排行条形图占位 → 有可视化内容，每页 10 条（规约 §7.2）
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    /** 前端切片视图（tools/top 只认 limit，分页在内存里做） */
    pagedRows() {
      return paginate(this.rows, this.page, this.pageSize)
    },
    winText() {
      return windowText(this.days)
    },
    total() {
      return this.rows.reduce((s, r) => s + (r.total || 0), 0)
    },
    failed() {
      return this.rows.reduce((s, r) => s + (r.failed || 0), 0)
    },
    worst() {
      return this.rows
        .filter((r) => r.failed > 0)
        .sort((a, b) => (b.failed / (b.total || 1)) - (a.failed / (a.total || 1)))[0]
    },
    cards() {
      return [
        { label: `调用总数（${windowText(this.days)}）`, value: this.total, sub: `涉及 ${this.rows.length} 个工具` },
        {
          label: '失败总数',
          value: this.failed,
          sub: `整体失败率 ${pct(this.failed, this.total, 1)}`,
          tone: this.failed ? 'warn' : 'ok',
        },
        {
          label: '失败率最高',
          value: this.worst ? this.worst.tool : '-',
          sub: this.worst ? `${pct(this.worst.failed, this.worst.total, 1)}（${this.worst.failed}/${this.worst.total}）` : '窗口内无失败',
          tone: this.worst ? 'err' : '',
        },
        {
          label: '平均耗时最高',
          value: this.slowest ? this.slowest.tool : '-',
          sub: this.slowest ? fmtMs(this.slowest.avgDurationMs) : '-',
        },
      ]
    },
    slowest() {
      return [...this.rows].sort((a, b) => (b.avgDurationMs || 0) - (a.avgDurationMs || 0))[0]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    fmtMs,
    pct,
    failTagClass(r) {
      const rate = r.total ? r.failed / r.total : 0
      if (rate >= 0.1) return 'err-tag'
      if (rate > 0) return 'warn-tag'
      return 'ok-tag'
    },
    /** 跳到故障页并按工具路径过滤——两个页面之间的自然动线 */
    goFaults(tool) {
      this.$router.push({ path: '/dashboard/faults', query: { path: tool } })
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
        this.feedback24h = ov.feedback24h || { up: 0, down: 0 }
        if (this.enabled) {
          const res = await fetchMetricsToolsTop({ orderBy: this.orderBy, limit: 50, hours: this.days * 24 })
          this.rows = res.rows || []
        }
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
      const res = await fetchMetricsExport({ view: 'tools', hours: this.days * 24 })
      if (res.content) downloadTextFile(res.filename || 'metrics_tools.csv', res.content)
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
.fail-text {
  color: #b42318;
  font-weight: 500;
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

<template>
  <DataPage title="告警处置" :subtitle="`告警清单 + 处置入口 + 委派关联分析 · ${winText} · 关联口径：时间 ±30 分钟近似`">
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
      title="告警数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />
    <EmptyState
      v-else-if="loaded && !link.linked"
      kind="empty"
      title="暂时拿不到告警列表"
      :lines="[link.reason || '未知原因', '告警数据来自无人机管控平台（jsdstu-3），需要登录态才能拉取。']"
    />

    <template v-else-if="loaded && link.linked">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>
          待处置告警
          <span class="hint">点「让 Agent 处置」送往对话页，由你在确认门后执行</span>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>告警</th><th>级别</th><th>状态</th><th>设备</th><th>触发时间</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedPending.items.length">
                <td colspan="6" class="muted pad">窗口内无待处置告警</td>
              </tr>
              <tr v-for="a in pagedPending.items" :key="a.id">
                <td class="title-cell" :title="a.title">{{ a.title || ('#' + a.id) }}</td>
                <td><span class="tag" :class="severityClass(a.severity)">{{ a.severity || '-' }}</span></td>
                <td>{{ a.status || '-' }}</td>
                <td>{{ a.droneCode || '-' }}</td>
                <td class="nowrap">{{ shortTime(a.triggeredAt) }}</td>
                <td class="actions">
                  <a class="link" @click="sendToAgent(a, 'acknowledge')">确认</a>
                  <a class="link" @click="sendToAgent(a, 'resolve')">解决</a>
                  <a class="link" @click="sendToAgent(a, 'ignore')">忽略</a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedPending.page"
          :total-pages="pagedPending.totalPages"
          :page-size="pagedPending.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPendingPage"
          @change-page-size="goPendingPageSize"
        />
        <p class="muted pad">
          <strong>本页不直接写库。</strong>点击后跳转到对话页并预填一句自然语言，由 Agent 复述处置对象与影响，
          你在确认门（guard.py）之后才会真正执行——写操作需 admin 权限。看板再实现一套确认等于
          在确认门之外开第二条写路径，安全边界会分叉（数据中台开发规约 §9 反模式 7）。
        </p>
      </div>

      <div class="panel">
        <h3>
          告警与委派关联
          <span class="hint">时间窗内的告警会标出可能对应的委派</span>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>告警</th><th>级别</th><th>设备</th><th>触发时间</th><th>关联来源</th><th>匹配委派</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedAlarms.items.length"><td colspan="6" class="muted pad">窗口内无告警记录</td></tr>
              <tr v-for="a in pagedAlarms.items" :key="'link-' + a.id">
                <td class="title-cell" :title="a.title">{{ a.title || ('#' + a.id) }}</td>
                <td><span class="tag" :class="severityClass(a.severity)">{{ a.severity || '-' }}</span></td>
                <td>{{ a.droneCode || '-' }}</td>
                <td class="nowrap">{{ shortTime(a.triggeredAt) }}</td>
                <td><span class="tag" :class="sourceClass(a.source)">{{ sourceText(a.source) }}</span></td>
                <td>{{ (a.matchedTraceIds || []).length || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedAlarms.page"
          :total-pages="pagedAlarms.totalPages"
          :page-size="pagedAlarms.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goAlarmsPage"
          @change-page-size="goAlarmsPageSize"
        />
        <p class="muted pad">
          <code>source</code> 不是展示字段而是<strong>语义字段</strong>：
          <span class="tag" :class="sourceClass('linked')">联动</span> 与某次 alarm-analyst 委派时间相近（±30 分钟，近似）；
          <span class="tag" :class="sourceClass('window')">窗口</span> 落在委派时间窗内但无精确匹配；
          <span class="tag" :class="sourceClass('pending')">待处置</span> 独立拉取、与委派无关。
          委派表没有 alarm_id，所以"疑似被处置过"与"还没处置"必须分开讲，不能混成一行。
        </p>
      </div>

      <div class="panel">
        <h3>alarm-analyst 委派明细<span class="hint">窗口内共 {{ link.delegations.length }} 次</span></h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>委派时间</th><th>状态</th><th>耗时</th><th>关联故障</th><th>匹配告警</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-if="!pagedDelegations.items.length">
                <td colspan="6" class="muted pad">窗口内无 alarm-analyst 委派记录</td>
              </tr>
              <tr v-for="(d, i) in pagedDelegations.items" :key="d.traceId + i">
                <td class="nowrap">{{ shortTime(d.createdAt) }}</td>
                <td><span class="tag" :class="statusClass(d.status)">{{ d.status }}</span></td>
                <td>{{ fmtMs(d.durationMs) }}</td>
                <td>{{ (d.faults || []).length || '-' }}</td>
                <td>{{ matchedAlarmCount(d.traceId) || '-' }}</td>
                <td>
                  <router-link class="link" :to="{ path: '/dashboard/conversations', query: { trace: d.traceId } }">
                    看轨迹 →
                  </router-link>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedDelegations.page"
          :total-pages="pagedDelegations.totalPages"
          :page-size="pagedDelegations.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goDelegationsPage"
          @change-page-size="goDelegationsPageSize"
        />
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 告警处置页（原看板「告警处置联动」+「告警清单」两个面板）。
 *
 * 原来这两个面板被塞进同一个两列 grid：左边反馈、右边告警联动，下面再挂一个
 * 告警清单——三件事挤成"半屏一块"。拆页后本页专讲告警：清单在前（要动手的）、
 * 关联分析在后（要看趋势的）、委派明细收尾（要追溯的）。
 *
 * B1 决策不变：本页不直接处置，只把人送到对话页走确认门。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import { fetchMetricsOverview, fetchMetricsAlarmLink } from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText, fmtMs, shortTime, statusClass, severityClass,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsAlarmsView',
  components: { DataPage, StatCards, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 1,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      link: { linked: false, delegations: [], alarms: [], reason: '' },
      // 本页有 KPI 卡片 + 三张表 → 有可视化内容，每页 10 条（规约 §7.2）
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
      // 三张表各自独立翻页：它们条数不同、看的顺序也不同，
      // 共用一个 page 会出现「翻告警列表把委派明细也翻走了」的错觉
      pendingPage: 1,
      alarmsPage: 1,
      delegationsPage: 1,
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    pendingAlarms() {
      return (this.link.alarms || []).filter((a) => a.source === 'pending')
    },
    linkedAlarms() {
      return (this.link.alarms || []).filter((a) => a.source !== 'pending')
    },
    /** 三张表的前端切片视图（alarms/link 固定返回，无分页参数） */
    pagedPending() {
      return paginate(this.pendingAlarms, this.pendingPage, this.pageSize)
    },
    pagedAlarms() {
      return paginate(this.link.alarms || [], this.alarmsPage, this.pageSize)
    },
    pagedDelegations() {
      return paginate(this.link.delegations || [], this.delegationsPage, this.pageSize)
    },
    highSeverity() {
      return (this.link.alarms || []).filter((a) => ['P0', 'P1'].includes(String(a.severity || '').toUpperCase()))
    },
    cards() {
      return [
        {
          label: '待处置告警',
          value: this.pendingAlarms.length,
          sub: 'status=pending，与委派无关',
          tone: this.pendingAlarms.length ? 'warn' : 'ok',
        },
        {
          label: '高危告警（P0/P1）',
          value: this.highSeverity.length,
          sub: `窗口内共 ${(this.link.alarms || []).length} 条告警`,
          tone: this.highSeverity.length ? 'err' : 'ok',
        },
        {
          label: '关联到委派的告警',
          value: this.linkedAlarms.length,
          sub: '时间 ±30 分钟近似匹配',
        },
        {
          label: 'alarm-analyst 委派',
          value: this.link.delegations.length,
          sub: '窗口内次数',
        },
      ]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    fmtMs,
    shortTime,
    statusClass,
    severityClass,
    sourceText(source) {
      if (source === 'linked') return '联动'
      if (source === 'window') return '窗口'
      return '待处置'
    },
    sourceClass(source) {
      if (source === 'linked') return 'src-linked'
      if (source === 'window') return 'src-window'
      return 'src-pending'
    },
    matchedAlarmCount(traceId) {
      return (this.link.alarms || []).filter((a) => (a.matchedTraceIds || []).includes(traceId)).length
    },
    goPendingPage(p) {
      this.pendingPage = p
    },
    goPendingPageSize(size) {
      this.pageSize = size
      this.pendingPage = 1
      this.alarmsPage = 1
      this.delegationsPage = 1
    },
    goAlarmsPage(p) {
      this.alarmsPage = p
    },
    goAlarmsPageSize(size) {
      this.pageSize = size
      this.pendingPage = 1
      this.alarmsPage = 1
      this.delegationsPage = 1
    },
    goDelegationsPage(p) {
      this.delegationsPage = p
    },
    goDelegationsPageSize(size) {
      this.pageSize = size
      this.pendingPage = 1
      this.alarmsPage = 1
      this.delegationsPage = 1
    },
    /**
     * 把处置请求「转交」给对话页。
     *
     * 预填自然语言而非工具调用参数：Agent 会用自己的工具链解析告警号，
     * 看板不需要知道工具叫什么、参数怎么传，避免前端与工具契约耦合。
     */
    sendToAgent(alarm, action) {
      const verb = { acknowledge: '确认', resolve: '解决', ignore: '忽略' }[action] || '处置'
      const label = alarm.title || `告警 #${alarm.id}`
      this.$router.push({ path: '/', query: { prefill: `帮我${verb}告警 ${alarm.id}（${label}）` } })
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        const ov = await fetchMetricsOverview()
        this.enabled = ov.enabled !== false
        if (this.enabled) {
          try {
            this.link = await fetchMetricsAlarmLink({ hours: this.days * 24 })
          } catch (e) {
            this.link = { linked: false, delegations: [], alarms: [], reason: e.message || '加载失败' }
          }
        }
        this.pendingPage = 1
        this.alarmsPage = 1
        this.delegationsPage = 1
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
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
.title-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nowrap {
  white-space: nowrap;
}
.actions {
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
.warn-tag {
  background: #faeeda;
  color: #854f0b;
}
.sev-high {
  color: #b42318;
  background: #fdeceb;
}
.sev-mid {
  color: #ba7517;
  background: #fdf4e3;
}
.sev-low {
  color: #5f5e5a;
  background: #f1efe8;
}
.src-linked {
  color: #0f6e56;
  background: #e1f5ee;
}
.src-window {
  color: #854f0b;
  background: #faeeda;
}
.src-pending {
  color: #185fa5;
  background: #eaf2fb;
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
code {
  background: #f1f3f5;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 11.5px;
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

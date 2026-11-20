<template>
  <DataPage title="知识缺口" :subtitle="`用户问了、知识库答不上来的问题聚类 · 数据窗口：${winText}`">
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
      <button class="btn" :disabled="busy === 'export' || !gapTotal" @click="exportCsv">
        {{ busy === 'export' ? '导出中…' : '导出补库清单' }}
      </button>
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
      title="知识缺口数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <!-- 口径说明前置：这一页最容易被误读成「答不上来的都该补库」，
           所以先把「什么不算缺口」写清楚，再看下面的清单 -->
      <div class="panel note">
        <h3>这一页在算什么<span class="hint">口径变更必须同步改规约 §5 指标字典</span></h3>
        <div class="decision-grid">
          <div v-for="d in decisionCards" :key="d.key" class="decision-card" :class="d.tone">
            <div class="dc-head">
              <span class="dc-name">{{ d.label }}</span>
              <span class="dc-count">{{ byDecision[d.key] || 0 }}</span>
            </div>
            <div class="dc-desc muted">{{ d.desc }}</div>
          </div>
        </div>
        <p class="muted pad">
          <strong>缺口只算后两类</strong>（无召回 / 证据不足）。<strong>跨域问题不算</strong>——
          用户问「今天天气」不是知识库的锅，算进来会把清单淹掉；
          <strong>答上了也不算</strong>——已命中的问题不构成缺口。
        </p>
        <p v-if="questionAvailable === false" class="muted pad warn-line">
          ⚠️ {{ reason || '问题原文暂不可得' }} ——
          埋点按规约 §10 不落对话正文，原句来自会话记忆（进程重启或超窗口即淘汰）与点踩捕获；
          <strong>这里只展示主题词与计数，不还原也不猜测原句</strong>。
          想让原句可追溯：由使用者对答不上的回答点👎，原文会随反馈落库。
        </p>
      </div>

      <!-- 补库建议：本页的主产出，按优先级排序 -->
      <div class="panel">
        <h3>补库建议<span class="hint">按优先级排序：高（证据不足）&gt; 中（无召回）&gt; 低（观察）</span></h3>
        <div v-if="!suggestions.length" class="empty-inline muted">
          {{ gapTotal ? '暂无达标的补充建议（样本量不足，继续观察）' : '窗口内没有知识缺口，知识库覆盖良好 👍' }}
        </div>
        <div v-for="(s, i) in pagedSuggestions.items" :key="i" class="sug-card" :class="prioClass(s.priority)">
          <div class="sug-head">
            <span class="prio" :class="prioClass(s.priority)">{{ s.priority }}</span>
            <span class="sug-topic">{{ s.topic }}</span>
            <span class="sug-count muted">出现 {{ s.count }} 次</span>
          </div>
          <div class="sug-text">{{ s.suggestion }}</div>
          <ul v-if="s.sampleQuestions.length" class="sug-samples">
            <li v-for="(q, qi) in s.sampleQuestions" :key="qi" class="muted">「{{ q }}」</li>
          </ul>
        </div>
        <PaginationBar
          :page="pagedSuggestions.page"
          :total-pages="pagedSuggestions.totalPages"
          :page-size="pagedSuggestions.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
      </div>

      <!-- 主题分布：条形给"哪个主题最扎堆"的直观感 -->
      <div class="panel">
        <h3>缺口主题分布<span class="hint">前 {{ topTopics.length }} 个主题</span></h3>
        <BarList
          :rows="topTopics"
          label-key="label"
          value-key="count"
          empty-text="窗口内没有缺口主题"
        />
      </div>

      <!-- 主题明细表：与建议卡互补——这里看"由哪些判定构成" -->
      <div class="panel">
        <h3>主题明细<span class="hint">含判定构成与可追溯 trace</span></h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>主题关键词</th><th>出现次数</th><th>判定构成</th>
                <th>优先级</th><th>样例 trace</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(t, i) in pagedTopics.items" :key="i">
                <td>{{ topicLabel(t) }}</td>
                <td>{{ t.count }}</td>
                <td class="muted">{{ decisionText(t.decisions) }}</td>
                <td><span class="prio" :class="prioClass(t.priority)">{{ t.priority }}</span></td>
                <td class="mono muted">{{ t.traceIds.join(', ') || '—' }}</td>
              </tr>
              <tr v-if="!topics.length">
                <td colspan="5" class="muted center">窗口内没有知识缺口</td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedTopics.page"
          :total-pages="pagedTopics.totalPages"
          :page-size="pagedTopics.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goTopicPage"
          @change-page-size="goTopicPageSize"
        />
      </div>

      <!-- 文档使用度：B 类巡检的已访问侧，为"无人访问文档"铺路（P3 才做差集） -->
      <div class="panel">
        <h3>命中文档使用度<span class="hint">只统计答上了的会话（kept）</span></h3>
        <p class="muted pad">
          这是「哪些文档从没被用过」的<strong>已访问侧</strong>：与知识库文档清单做差集，
          就是从未被命中的文档。本批只做数据积累（P3 再做差集视图，需 ≥7 天样本）。
        </p>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>docId</th><th>命中次数</th><th>最近命中</th></tr></thead>
            <tbody>
              <tr v-for="d in pagedDocs.items" :key="d.docId">
                <td class="mono">{{ d.docId }}</td>
                <td>{{ d.hits }}</td>
                <td class="muted">{{ d.lastHitAt || '—' }}</td>
              </tr>
              <tr v-if="!docUsage.length">
                <td colspan="3" class="muted center">窗口内没有命中文档记录</td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="pagedDocs.page"
          :total-pages="pagedDocs.totalPages"
          :page-size="pagedDocs.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goDocPage"
          @change-page-size="goDocPageSize"
        />
      </div>

      <div class="panel">
        <h3>指标口径速查<span class="hint">改口径必须同步改规约 §5 指标字典</span></h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>指标</th><th>口径</th><th>数据源</th></tr></thead>
            <tbody>
              <tr v-for="m in metricsDict" :key="m.name">
                <td>{{ m.name }}</td>
                <td class="muted">{{ m.def }}</td>
                <td class="mono muted">{{ m.src }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </DataPage>
</template>

<script>
/**
 * 知识缺口页（v4 新增，对标 AgentCraft admin 的「未命中问题分析」）。
 *
 * 为什么需要：RAG 早就算出了 decision（kept/cross_domain/no_hit/
 * insufficient_evidence），但之前**没落库**——数据中台闭环在"模型判了什么"
 * 这一步断掉，运营只能看到"答了几次"，看不到"哪类问题答不上"。
 * 本页把这个维度补上，并把结论直接变成可执行的补库清单（可导出 CSV）。
 *
 * 三个容易踩的坑，都在页面上明写了：
 * 1. **cross_domain 不算缺口**——问"今天天气"不是知识库的锅，混进来会淹掉清单；
 * 2. **原句可能不可得**——埋点不落正文（规约 §10），记忆易失；此时只出主题词，
 *    绝不还原/猜测原句（reason 字段会如实标注）；
 * 3. **聚类不许调 LLM**——缺口分析要可复现、可批跑，用规则（实词切段 + 重叠
 *    系数）而非模型，零 token 成本。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import EmptyState from '../../components/EmptyState.vue'
import BarList from '../../components/BarList.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import { fetchMetricsUnanswered, fetchMetricsDocUsage, fetchMetricsExport, downloadTextFile } from '../../api/metrics'
import {
  WINDOW_OPTIONS, windowText,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

export default {
  name: 'MetricsUnansweredView',
  components: { DataPage, StatCards, EmptyState, BarList, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      days: 7,                       // 缺口要看趋势，默认 7 天（一天样本太少）
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      busy: '',
      byDecision: {},
      total: 0,
      gapTotal: 0,
      distinctTopics: 0,
      questionAvailable: null,
      reason: '',
      suggestions: [],
      topics: [],
      docUsage: [],
      // 每个列表各有独立页码：三处列表在同一页，共用一个 page 会互相打架
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      topicPage: 1, topicPageSize: PAGE_SIZE_WITH_CONTENT,
      docPage: 1, docPageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
      decisionCards: [
        { key: 'kept', label: '正常作答', tone: 'ok', desc: '证据充足，答上了 —— 不算缺口' },
        { key: 'cross_domain', label: '跨域问题', tone: 'neutral', desc: '不属知识域（闲聊等）—— 不算缺口' },
        { key: 'no_hit', label: '完全无召回', tone: 'warn', desc: '库里没有相关内容 —— 是缺口，需新增文档' },
        { key: 'insufficient_evidence', label: '证据不足', tone: 'err', desc: '库里有但不够用 —— 是缺口，补一篇即可救活' },
      ],
      metricsDict: [
        { name: '知识缺口量', def: 'no_hit + insufficient_evidence 的会话数（排除 kept / cross_domain）', src: 'agent_chat_log.rag_decision' },
        { name: '缺口主题数', def: '关键词聚类后的簇数（实词切段 + 重叠系数，非 LLM）', src: '聚类计算' },
        { name: '优先级·高', def: '证据不足且同主题 ≥3 次 —— 补一篇就能救活，性价比最高', src: '聚类 + 阈值' },
        { name: '优先级·中', def: '完全无召回且同主题 ≥3 次 —— 需新增文档', src: '聚类 + 阈值' },
        { name: '优先级·低', def: '同主题出现 1~2 次 —— 先观察，攒样本再说', src: '聚类 + 阈值' },
        { name: '文档命中次数', def: '只统计 kept 会话的引用；拒答会话的召回不计入', src: 'agent_chat_log.rag_docs' },
      ],
    }
  },
  computed: {
    winText() {
      return windowText(this.days)
    },
    cards() {
      return [
        { label: `知识缺口（${this.winText}）`, value: this.gapTotal, sub: '无召回 + 证据不足的会话数', tone: this.gapTotal ? 'warn' : 'ok' },
        { label: '缺口主题数', value: this.distinctTopics, sub: '聚类去重后的主题数' },
        { label: '高优先级主题', value: this.highCount, sub: '建议立即补库', tone: this.highCount ? 'err' : 'ok' },
        { label: 'RAG 会话总量', value: this.total, sub: '含正常作答与跨域（对照用）' },
      ]
    },
    highCount() {
      return this.suggestions.filter((s) => s.priority === '高').length
    },
    topTopics() {
      return this.topics.slice(0, 8).map((t) => ({
        label: this.topicLabel(t) || '(无原句)',
        count: t.count,
      }))
    },
    pagedSuggestions() {
      return paginate(this.suggestions, this.page, this.pageSize)
    },
    pagedTopics() {
      return paginate(this.topics, this.topicPage, this.topicPageSize)
    },
    pagedDocs() {
      return paginate(this.docUsage, this.docPage, this.docPageSize)
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    // 三处列表的翻页各自独立（goPage/goTopicPage/goDocPage），避免互相重置页码
    goPage(p) { this.page = p },
    goPageSize(s) { this.pageSize = s; this.page = 1 },
    goTopicPage(p) { this.topicPage = p },
    goTopicPageSize(s) { this.topicPageSize = s; this.topicPage = 1 },
    goDocPage(p) { this.docPage = p },
    goDocPageSize(s) { this.docPageSize = s; this.docPage = 1 },
    prioClass(p) {
      return p === '高' ? 'p-high' : p === '中' ? 'p-mid' : 'p-low'
    },
    /**
     * 主题词 → 展示文案。
     * 后端在「原句不可得」时用 `sess-<sessionId>` 作占位词（同一会话的无原句行聚成一簇），
     * 这里翻译成人话——否则用户看到 `sess-v4e2e-a` 会以为自己看的是内部 ID。
     */
    topicLabel(t) {
      const kws = (t && t.keywords) || []
      const placeholder = kws.find((k) => k.startsWith('sess-'))
      if (placeholder) {
        return `某会话连续 ${t.count} 次未命中（原句不可得）`
      }
      return kws.slice(0, 3).join(' · ')
    },
    decisionText(mix) {
      const label = { no_hit: '无召回', insufficient_evidence: '证据不足' }
      const parts = Object.entries(mix || {}).map(([k, v]) => `${label[k] || k} ${v}`)
      return parts.join(' · ') || '—'
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        const hours = this.days * 24
        // 两个查询并发：页面上半部分与下半部分是独立数据源，串行会白等一倍
        const [r, u] = await Promise.all([
          fetchMetricsUnanswered({ hours, limit: 200 }),
          fetchMetricsDocUsage({ hours, limit: 200 }),
        ])
        this.enabled = r.enabled !== false
        if (this.enabled) {
          this.byDecision = r.byDecision || {}
          this.total = r.total || 0
          this.gapTotal = r.gapTotal || 0
          this.distinctTopics = r.distinctTopics || 0
          this.questionAvailable = r.questionAvailable
          this.reason = r.reason || ''
          this.suggestions = r.suggestions || []
          this.topics = r.topics || []
          this.docUsage = (u && u.rows) || []
        }
        this.loaded = true
      } catch (e) {
        this.error = e.message || '加载失败'
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    async exportCsv() {
      this.busy = 'export'
      try {
        const res = await fetchMetricsExport({ view: 'unanswered', hours: this.days * 24 })
        if (res.content) {
          downloadTextFile(res.filename || 'metrics_unanswered.csv', res.content)
        } else {
          this.error = res.reason || '导出失败'
        }
      } catch (e) {
        this.error = e.message || '导出失败'
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
.pad {
  padding: 8px 0;
  margin: 0;
  line-height: 1.75;
}
.warn-line {
  border-top: 1px dashed #f0e2c8;
  padding-top: 10px;
  color: #8a6d3b;
}
.decision-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}
.decision-card {
  border: 1px solid #eef0f2;
  border-left-width: 3px;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fbfcfd;
}
.decision-card.ok { border-left-color: #52c41a; }
.decision-card.warn { border-left-color: #faad14; }
.decision-card.err { border-left-color: #ff4d4f; }
.decision-card.neutral { border-left-color: #d0d5dc; }
.dc-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.dc-name {
  font-size: 13px;
  font-weight: 500;
  color: #1f2328;
}
.dc-count {
  font-size: 18px;
  font-weight: 600;
  color: #1f2328;
}
.dc-desc {
  margin-top: 4px;
  line-height: 1.5;
}
.sug-card {
  border: 1px solid #eef0f2;
  border-left-width: 3px;
  border-radius: 8px;
  padding: 11px 13px;
  margin-bottom: 9px;
  background: #fbfcfd;
}
.sug-card.p-high { border-left-color: #ff4d4f; }
.sug-card.p-mid { border-left-color: #faad14; }
.sug-card.p-low { border-left-color: #52c41a; }
.sug-head {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}
.sug-topic {
  font-size: 13px;
  font-weight: 500;
  color: #1f2328;
}
.sug-count {
  margin-left: auto;
}
.sug-text {
  margin-top: 6px;
  font-size: 12.5px;
  color: #2c3e50;
  line-height: 1.6;
}
.sug-samples {
  margin: 6px 0 0;
  padding-left: 18px;
}
.sug-samples li {
  line-height: 1.7;
}
.prio {
  display: inline-block;
  min-width: 30px;
  text-align: center;
  border-radius: 4px;
  padding: 1px 7px;
  font-size: 11px;
  color: #fff;
  background: #8a919c;
}
.prio.p-high { background: #ff4d4f; }
.prio.p-mid { background: #faad14; }
.prio.p-low { background: #52c41a; }
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
  vertical-align: top;
}
.center { text-align: center; }
.empty-inline {
  padding: 14px 0;
  text-align: center;
}
.mono { font-family: Consolas, Menlo, monospace; }
.muted { color: #8a919c; font-size: 12px; }
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
</style>

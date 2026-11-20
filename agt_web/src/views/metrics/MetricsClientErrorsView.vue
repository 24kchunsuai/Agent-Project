<template>
  <DataPage
    title="前端错误"
    :subtitle="`浏览器侧运行时报错（三处收口汇总） · 数据窗口：${winText}`"
  >
    <template #actions>
      <select v-model.number="hours" class="sel" @change="load">
        <option v-for="o in windows" :key="o.value" :value="o.value * 24">{{ o.label }}</option>
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
      title="前端错误数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <StatCards :cards="cards" />

      <div class="panel">
        <h3>
          按页面
          <span class="hint">上报次数降序 · 页面只记 path（查询串与 hash 已在两端剥掉）</span>
        </h3>
        <BarList
          :rows="byPage"
          label-key="name"
          value-key="value"
          empty-text="窗口内没有前端报错"
          :is-warn="() => true"
        />
      </div>

      <div class="panel">
        <h3>
          按前端版本
          <span class="hint">用来回答"是不是这一版刚引入的"</span>
        </h3>
        <BarList
          :rows="byVersion"
          label-key="name"
          value-key="value"
          empty-text="窗口内没有前端报错"
        />
      </div>

      <div class="panel">
        <h3>
          最新报错
          <span class="hint">按最后发生时间倒序 · 最近 {{ limit }} 条</span>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>页面</th><th>来源</th><th>消息</th><th>次数</th>
                <th>版本</th><th>用户</th><th>首次发生</th><th>最后发生</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!paged.items.length">
                <td colspan="8" class="muted pad">暂无前端报错</td>
              </tr>
              <tr v-for="r in paged.items" :key="r.fingerprint + '|' + r.lastAt">
                <td class="nowrap">{{ r.page || '(未知页面)' }}</td>
                <td><span class="tag" :class="sourceClass(r.source)">{{ sourceText(r.source) }}</span></td>
                <td class="detail" :title="r.message">{{ r.message }}</td>
                <td class="nowrap num">{{ r.count }}</td>
                <td class="nowrap muted">{{ r.appVersion || '(未知版本)' }}</td>
                <td class="nowrap muted">{{ r.userId || '匿名' }}</td>
                <td class="nowrap muted">{{ shortTime(r.firstAt) }}</td>
                <td class="nowrap muted">{{ shortTime(r.lastAt) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar
          :page="paged.page"
          :total-pages="paged.totalPages"
          :page-size="paged.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goPage"
          @change-page-size="goPageSize"
        />
      </div>

      <p class="muted foot">
        <strong>口径</strong>：「次数」是<strong>上报次数</strong>，不是真实抛错次数——
        前端在同一页面加载内对同一错误只上报一次（防报错风暴打满网络），
        所以它是真实次数的<strong>下界</strong>；服务端再按「同一错误 + 同一 1 分钟窗口」
        合并成一行累加（防表被刷爆）。两处口径都不改，改了就与历史数据不可比。
        <br>
        <strong>为什么没有「查会话」入口</strong>：库里确实存了 traceId，但那是<strong>上报请求自身</strong>
        的链路 id（排查"这次上报被收下了吗"时 grep 日志有用），
        <strong>不是</strong>用户那次出错动作的链路 id——拿它跳「会话明细」只会跳到空结果，
        所以不放这个入口，避免给出一个必然失败的动作。
        <br>
        <strong>验收/排障</strong>：浏览器控制台执行 <code>__agtClientErrorSelfTest()</code>
        三条收口各发包一次；或在任意组件内主动抛错。采样率与合并窗口是服务端配置项
        （<code>client_error_sample_rate</code> / <code>client_error_dedup_seconds</code>）。
      </p>
    </template>
  </DataPage>
</template>

<script>
/**
 * 前端错误页（企业级落地规约 E-12，判据 C3「可观测」）。
 *
 * 这一页存在的理由：E-12 之前"前端炸了"在三端都没有记录——用户看到白屏或一句
 * "操作失败"，后端日志干干净净，排障靠让用户 F12 截图（而用户往往已经关掉页面）。
 * 本页把浏览器侧的报错收成三个可回答的问题：
 *   ① 哪一页最容易炸（按页面聚合）→ 定位到功能；
 *   ② 是不是这一版刚引入的（按版本聚合）→ 定位到发布；
 *   ③ 具体报的什么、什么时候、谁碰到的（最新报错）→ 定位到复现路径。
 *
 * 为什么单独成页而不是塞进「故障与降级」：那页讲的是**服务端**的故障与降级
 * （LLM 超时走兜底、向量库不可用退化为关键词检索），而这里讲的是**浏览器**。
 * 两者的排查动作完全不同：前者去看组件健康与降级计数，后者去看前端代码与版本。
 * 混在一页会让"到底哪一侧坏了"这个第一判断都变模糊。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import PaginationBar from '../../components/PaginationBar.vue'
import { fetchClientErrors } from '../../api/ops'
import {
  WINDOW_OPTIONS, windowText, shortTime,
  paginate, PAGE_SIZE_WITH_CONTENT, METRICS_PAGE_SIZE_OPTIONS,
} from '../../utils/metrics'

/** 来源中文名：与 Python 侧 client_error.SOURCES 白名单一一对应（多出来的是 unknown） */
const SOURCE_TEXT = {
  vue: 'Vue 组件',
  window: 'window.onerror',
  promise: '未处理 Promise',
  unknown: '未知来源',
}

export default {
  name: 'MetricsClientErrorsView',
  components: { DataPage, StatCards, BarList, EmptyState, PaginationBar },
  data() {
    return {
      windows: WINDOW_OPTIONS,
      hours: 24,
      limit: 200,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      total: 0,
      distinct: 0,
      byPage: [],
      byVersion: [],
      latest: [],
      page: 1,
      pageSize: PAGE_SIZE_WITH_CONTENT,
      pageSizeOptions: METRICS_PAGE_SIZE_OPTIONS,
    }
  },
  computed: {
    winText() {
      return windowText(this.hours / 24)
    },
    paged() {
      return paginate(this.latest, this.page, this.pageSize)
    },
    cards() {
      return [
        {
          label: `上报次数（${this.winText}）`,
          value: this.total,
          sub: '同一错误同一分钟合并计数 · 真实次数的下界',
          tone: this.total ? 'warn' : 'ok',
        },
        {
          label: '错误类数',
          value: this.distinct,
          sub: '按「页面 + 来源 + 消息」去重后的种类',
        },
        {
          label: '涉及页面',
          value: this.byPage.length,
          sub: this.byPage.length ? `最吵：${this.byPage[0].name}` : '窗口内无报错',
        },
        {
          label: '前端版本',
          value: this.byVersion.length,
          sub: this.byVersion.map((v) => v.name).join(' / ') || '-',
        },
      ]
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    shortTime,
    sourceText(s) {
      return SOURCE_TEXT[s] || s || '未知来源'
    },
    sourceClass(s) {
      if (s === 'promise') return 'warn-tag'
      if (s === 'unknown') return 'err-tag'
      return ''
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
        const res = await fetchClientErrors({ hours: this.hours, limit: this.limit })
        this.enabled = res.enabled !== false
        if (this.enabled) {
          this.total = res.total || 0
          this.distinct = res.distinct || 0
          this.byPage = res.byPage || []
          this.byVersion = res.byVersion || []
          this.latest = res.latest || []
          // 窗口变了回到第 1 页：否则停在旧页码上看到空表，像是"这一窗没数据"
          this.page = 1
        }
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
}
.tbl td {
  padding: 6px 8px;
  border-bottom: 1px solid #f5f6f8;
  vertical-align: top;
}
.detail {
  max-width: 460px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #444;
}
.nowrap {
  white-space: nowrap;
}
.num {
  font-variant-numeric: tabular-nums;
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
.err-tag {
  background: #fdecea;
  color: #b42318;
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
.foot {
  line-height: 1.9;
  padding: 0 4px;
}
.foot code {
  background: #f1f3f5;
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 11px;
}
</style>

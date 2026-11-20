<template>
  <DataPage
    title="成本与配额"
    :subtitle="`LLM token 用量与成本折算（E-13） · 统计窗口：近 ${days} 天`"
  >
    <template #actions>
      <select v-model.number="days" class="sel" @change="load">
        <option :value="1">近 24 小时</option>
        <option :value="7">近 7 天</option>
        <option :value="30">近 30 天</option>
        <option :value="90">近 90 天</option>
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
      title="成本数据加载失败"
      :lines="[error, '请确认已用 admin 账号登录、Java(8080) 与 Python(8000) 服务在线。']"
    />

    <template v-else-if="enabled">
      <p v-if="!pricesConfigured" class="no-price">
        ⚠️ 尚未配置价目表（<code>LLM_PRICE_JSON</code>）——下方成本恒为 ¥0，
        token 用量照常统计。配置后无需重启即可在下次查询生效。
      </p>

      <StatCards :cards="cards" />

      <div class="panel">
        <h3>
          按模型
          <span class="hint">成本降序 · 单价单位：元 / 百万 token（与供应商定价页同口径）</span>
        </h3>
        <BarList
          :rows="byModel"
          label-key="model"
          value-key="costCny"
          empty-text="窗口内没有 LLM 调用"
          :formatter="modelCostText"
        />
        <p class="muted small">
          「未配价」的模型成本记 ¥0 但 token 照记（缺价 ≠ 免费照标）——
          看到未配价 token 就去 <code>LLM_PRICE_JSON</code> 把这一行补上。
        </p>
      </div>

      <div class="panel">
        <h3>
          日趋势
          <span class="hint">按本地日折算 · 配额也是本地日口径（与 DWS 一致）</span>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>日期</th><th>prompt tokens</th><th>completion tokens</th><th>合计</th><th>成本</th></tr>
            </thead>
            <tbody>
              <tr v-if="!daily.length">
                <td colspan="5" class="muted pad">窗口内没有 LLM 调用</td>
              </tr>
              <tr v-for="d in daily" :key="d.day">
                <td class="nowrap">{{ d.day }}</td>
                <td class="num">{{ fmtNum(d.promptTokens) }}</td>
                <td class="num">{{ fmtNum(d.completionTokens) }}</td>
                <td class="num">{{ fmtNum(d.tokens) }}</td>
                <td class="num">{{ fmtCny(d.costCny) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3>
          按用户
          <span class="hint">token 降序 · 配额按「单用户 · 本地日」累计</span>
        </h3>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>用户</th><th>部门</th><th>会话数</th><th>tokens</th><th>成本</th></tr>
            </thead>
            <tbody>
              <tr v-if="!byUser.length">
                <td colspan="5" class="muted pad">窗口内没有已登录用户的 LLM 调用</td>
              </tr>
              <tr v-for="u in byUser" :key="u.userId">
                <td>用户 {{ u.userId }}</td>
                <td>{{ u.dept }}</td>
                <td class="num">{{ u.chats }}</td>
                <td class="num">{{ fmtNum(u.tokens) }}</td>
                <td class="num">{{ fmtCny(u.costCny) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3>
          按部门
          <span class="hint">由「按用户」派生（组员成本之和） · C7 判据："这个月哪个部门用的"</span>
        </h3>
        <BarList
          :rows="byDept"
          label-key="dept"
          value-key="costCny"
          empty-text="窗口内没有已登录用户的 LLM 调用"
          :formatter="deptCostText"
        />
      </div>

      <p class="muted foot">
        <strong>口径</strong>：成本 = token × <strong>当前价目表</strong>，在查询期折算、
        不在 DWS 预聚合里冻结——供应商改价后历史按新价重算，回答的是"按现在的价格值多少钱"；
        要对某张历史账单，需用当时的价目重算。窗口内凡有「估算行」
        （上游未返回真实 usage、按本地估算，{{ totals.estimatedRows || 0 }} 行），
        成本只可看趋势、不可拿去逐张对账。
        <br>
        <strong>部门维度</strong>：本系统没有组织架构，部门 = 用户画像里<strong>声明过的</strong>
        <code>department</code> 键（读取期派生、不回填），没声明的归「unknown」——
        它回答"哪些人在用"，不是"哪个组织在用"。
        <br>
        <strong>配额</strong>：单用户日 token 上限（<code>QUOTA_USER_DAILY_TOKENS</code>，
        当前 {{ quotaText }}）；超限<strong>不拒绝服务</strong>——用户收到黄条提示、
        系统落 <code>quota_exceeded</code> 故障事件（每用户每日至多一条）。
        今日触顶 {{ quota.exceededEventsToday || 0 }} 次。
      </p>
    </template>
  </DataPage>
</template>

<script>
/**
 * 成本与配额页（企业级落地规约 E-13，判据 C7「可计量」）。
 *
 * 这一页回答三个问题：
 *   ① 花了多少钱（窗口成本 + 日趋势 + 按模型占比）；
 *   ② 谁在花（按用户 / 按部门）；
 *   ③ 用量有没有失控（配额上限 + 今日触顶次数）。
 *
 * 为什么单独成页：成本是"运营/管理员"的视角，与会话质量、前端健康不同源——
 * 混进总览会把"系统健不健康"和"账单大不大"两件事搅在一起。
 */
import DataPage from '../../components/DataPage.vue'
import StatCards from '../../components/StatCards.vue'
import BarList from '../../components/BarList.vue'
import EmptyState from '../../components/EmptyState.vue'
import { fetchMetricsCosts } from '../../api/metrics'

export default {
  name: 'MetricsCostsView',
  components: { DataPage, StatCards, BarList, EmptyState },
  data() {
    return {
      days: 7,
      loading: false,
      loaded: false,
      error: '',
      enabled: null,
      pricesConfigured: false,
      quota: {},
      totals: {},
      byModel: [],
      byUser: [],
      byDept: [],
      daily: [],
    }
  },
  computed: {
    cards() {
      const t = this.totals
      return [
        {
          label: `成本（近 ${this.days} 天）`,
          value: this.fmtCny(t.costCny || 0),
          sub: `按当前价目折算 · 未配价 ${this.fmtNum(t.unpricedTokens || 0)} tok`,
          tone: t.unpricedTokens ? 'warn' : '',
        },
        {
          label: 'tokens',
          value: this.fmtNum(t.tokens || 0),
          sub: `prompt ${this.fmtNum(t.promptTokens || 0)} · completion ${this.fmtNum(t.completionTokens || 0)}`,
        },
        {
          label: 'LLM 调用',
          value: t.chats || 0,
          sub: (t.estimatedRows
            ? `其中 ${t.estimatedRows} 行为本地估算（看趋势可、对账不可）`
            : '全部为服务端真实 usage'),
        },
        {
          label: '单用户日配额',
          value: this.quotaText,
          sub: `今日触顶 ${this.quota.exceededEventsToday || 0} 次 · 超限只提示不拒绝`,
          tone: this.quota.exceededEventsToday ? 'warn' : '',
        },
      ]
    },
    quotaText() {
      const cap = this.quota.capTokens || 0
      return cap ? this.fmtNum(cap) : '不限'
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    fmtNum(n) {
      return Number(n || 0).toLocaleString('zh-CN')
    },
    fmtCny(n) {
      return `¥${Number(n || 0).toFixed(2)}`
    },
    modelCostText(r) {
      const bits = [this.fmtCny(r.costCny), `${this.fmtNum(r.tokens)} tok`]
      if (r.unpricedTokens) bits.push(`未配价 ${this.fmtNum(r.unpricedTokens)} tok`)
      return bits.join(' · ')
    },
    deptCostText(r) {
      return `${this.fmtCny(r.costCny)} · ${this.fmtNum(r.tokens)} tok · ${r.chats} 次`
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        const res = await fetchMetricsCosts({ days: this.days })
        this.enabled = res.enabled !== false
        if (this.enabled) {
          this.pricesConfigured = !!res.pricesConfigured
          this.quota = res.quota || {}
          this.totals = res.totals || {}
          this.byModel = res.byModel || []
          this.byUser = res.byUser || []
          this.byDept = res.byDept || []
          this.daily = res.daily || []
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
}
.hint {
  margin-left: 8px;
  font-size: 12px;
  font-weight: normal;
  color: #98a0aa;
}
.no-price {
  margin: 0 0 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: #fdf6e3;
  border: 1px solid #f0e0b0;
  color: #8a6d1a;
  font-size: 13px;
}
.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 7px 10px; border-bottom: 1px solid #f0f2f4; text-align: left; }
.tbl th { color: #6b7280; font-weight: 600; background: #fafbfc; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.nowrap { white-space: nowrap; }
.muted { color: #98a0aa; }
.pad { padding: 10px; }
.small { font-size: 12px; margin: 8px 0 0; }
.foot { font-size: 12.5px; line-height: 1.9; margin-top: 4px; }
.sel, .btn { font-size: 13px; }
</style>

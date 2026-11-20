<template>
  <div class="bars">
    <div v-if="!rows.length" class="empty muted">{{ emptyText }}</div>
    <div
      v-for="row in rows"
      :key="String(row[labelKey]) + '|' + String(row[valueKey])"
      class="bar-row"
      :title="tooltip(row)"
    >
      <span class="bar-name" :class="{ fail: warn(row) }">{{ row[labelKey] || '(未知)' }}</span>
      <div class="bar-track">
        <div class="bar-fill" :class="{ warn: warn(row), danger: dangerRow(row) }" :style="{ width: width(row) }"></div>
      </div>
      <span class="bar-num">{{ suffix(row) }}</span>
    </div>
  </div>
</template>

<script>
/**
 * 横向条形图（纯 CSS，零依赖）。
 *
 * 沿用规约 §7 的 P0 技术取舍：条形这一类形态用 div 宽度画，不引入 ECharts。
 * 拆页后三处都要画条形（意图分布 / 每日趋势 / 工具 Top），所以抽成组件。
 */
export default {
  name: 'BarList',
  props: {
    rows: { type: Array, default: () => [] },
    labelKey: { type: String, default: 'name' },
    valueKey: { type: String, default: 'value' },
    emptyText: { type: String, default: '暂无数据' },
    /** 数字后缀渲染：默认直接显示数值；传入函数可自定义（如「3✗/120 · 420ms」） */
    formatter: { type: Function, default: null },
    /** 返回 true 时标红（如失败次数 > 0） */
    isWarn: { type: Function, default: null },
    /** 返回 true 时整条标红加粗（如失败的委派） */
    isDanger: { type: Function, default: null },
  },
  computed: {
    max() {
      return Math.max(1, ...this.rows.map((r) => Number(r[this.valueKey]) || 0))
    },
  },
  methods: {
    width(row) {
      const v = Number(row[this.valueKey]) || 0
      return `${Math.max(2, Math.round((v / this.max) * 100))}%`
    },
    warn(row) {
      return this.isWarn ? !!this.isWarn(row) : false
    },
    dangerRow(row) {
      return this.isDanger ? !!this.isDanger(row) : false
    },
    suffix(row) {
      return this.formatter ? this.formatter(row) : row[this.valueKey]
    },
    tooltip(row) {
      return this.formatter ? `${row[this.labelKey]}：${this.suffix(row)}` : ''
    },
  },
}
</script>

<style scoped>
.bars {
  width: 100%;
}
.empty {
  font-size: 12px;
  color: #8a919c;
  padding: 10px 0;
}
.muted {
  color: #8a919c;
}
.bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0;
}
.bar-name {
  width: 150px;
  flex-shrink: 0;
  font-size: 12px;
  color: #444;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-name.fail {
  color: #b42318;
}
.bar-track {
  flex: 1;
  height: 12px;
  background: #f1f3f5;
  border-radius: 6px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: #185fa5;
  border-radius: 6px;
}
.bar-fill.warn {
  background: #ba7517;
}
.bar-fill.danger {
  background: #d9534f;
}
.bar-num {
  width: 130px;
  flex-shrink: 0;
  font-size: 11px;
  color: #6b7280;
  white-space: nowrap;
}
</style>

<template>
  <div class="stat-row">
    <div v-for="c in cards" :key="c.label" class="stat-card">
      <div class="stat-label">{{ c.label }}</div>
      <div class="stat-value" :class="c.tone || ''">{{ c.value }}</div>
      <div v-if="c.sub" class="stat-sub muted">{{ c.sub }}</div>
    </div>
  </div>
</template>

<script>
/**
 * KPI 卡片行（拆页后总览/故障/报表三处共用）。
 * tone: ''（默认深色）| 'ok' 绿 | 'warn' 橙 | 'err' 红
 */
export default {
  name: 'StatCards',
  props: {
    cards: { type: Array, default: () => [] },
  },
}
</script>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.stat-card {
  border: 1px solid #eef0f2;
  border-radius: 10px;
  padding: 14px 16px;
  background: #fbfcfd;
}
.stat-label {
  font-size: 12px;
  color: #6b7280;
}
.stat-value {
  font-size: 25px;
  font-weight: 600;
  color: #1f2328;
  margin: 4px 0 2px;
  line-height: 1.15;
}
.stat-value.ok {
  color: #0f6e56;
}
.stat-value.warn {
  color: #ba7517;
}
.stat-value.err {
  color: #b42318;
}
.stat-sub {
  font-size: 12px;
}
.muted {
  color: #8a919c;
}
</style>

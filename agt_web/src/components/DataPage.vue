<template>
  <div class="page">
    <header class="page-head">
      <div class="head-left">
        <h2>{{ title }}</h2>
        <p v-if="subtitle" class="muted">{{ subtitle }}</p>
      </div>
      <div class="head-right">
        <slot name="actions" />
      </div>
    </header>
    <slot />
  </div>
</template>

<script>
/**
 * 数据页统一头：标题 + 窗口说明 + 右侧动作区（时间窗、导出、刷新）。
 * 拆分后每个看板页共用一个头，避免各写一份样式。
 */
export default {
  name: 'DataPage',
  props: {
    title: { type: String, required: true },
    subtitle: { type: String, default: '' },
  },
}
</script>

<style scoped>
.page {
  max-width: 1160px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}
.head-left h2 {
  margin: 0 0 4px;
  font-size: 18px;
  color: #1f2328;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.muted {
  color: #8a919c;
  font-size: 12px;
  margin: 0;
}
</style>

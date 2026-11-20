<template>
  <!-- 埋点未开启 / 加载失败 / 无权限 的统一空态 -->
  <div class="state" :class="{ err: kind === 'error' }">
    <p class="state-title">{{ title }}</p>
    <p v-for="(line, i) in lines" :key="i" class="state-line muted">{{ line }}</p>
    <slot />
  </div>
</template>

<script>
/**
 * 状态占位：区分「埋点关闭」「加载失败」「无数据」三种语义。
 *
 * 为什么单独抽一个：拆分后八个页面各自都要处理这三种态，
 * 文案散落会导致同一件事在不同页说法不一（原来一个页面里就够乱了）。
 */
export default {
  name: 'EmptyState',
  props: {
    /** 'off' 埋点关闭 | 'error' 加载失败 | 'empty' 无数据 */
    kind: { type: String, default: 'empty' },
    title: { type: String, default: '' },
    lines: { type: Array, default: () => [] },
  },
}
</script>

<style scoped>
.state {
  border: 1px dashed #d8dce2;
  border-radius: 10px;
  padding: 34px 24px;
  text-align: center;
  background: #fff;
}
.state.err {
  border-color: #f0c8c5;
}
.state-title {
  margin: 0 0 6px;
  font-size: 14px;
  color: #1f2328;
}
.state-line {
  margin: 3px 0;
  font-size: 12px;
}
.muted {
  color: #8a919c;
}
</style>

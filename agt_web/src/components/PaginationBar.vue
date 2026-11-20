<template>
  <div class="pager">
    <span class="pager-info">
      {{ page }} / {{ totalPages }} 页
    </span>

    <div class="pager-btns">
      <button
        class="pager-btn"
        type="button"
        :disabled="busy || page <= 1"
        @click="$emit('change-page', page - 1)"
      >
        上一页
      </button>
      <button
        class="pager-btn"
        type="button"
        :disabled="busy || page >= totalPages"
        @click="$emit('change-page', page + 1)"
      >
        下一页
      </button>
    </div>

    <label class="pager-size">
      每页
      <select
        :value="pageSize"
        :disabled="busy"
        @change="$emit('change-page-size', Number($event.target.value))"
      >
        <option v-for="opt in pageSizeOptions" :key="opt" :value="opt">{{ opt }}</option>
      </select>
      条
    </label>
  </div>
</template>

<script>
/**
 * 通用分页条：页码信息 + 上一页/下一页 + 每页条数选择。
 *
 * 只依赖后端返回的 page / totalPages / pageSize，翻页动作全部交给父组件
 * 发请求，本组件不持有任何业务状态，文档列表与回收站复用同一份。
 */
export default {
  name: 'PaginationBar',

  props: {
    page: { type: Number, required: true },
    totalPages: { type: Number, required: true },
    pageSize: { type: Number, required: true },
    pageSizeOptions: {
      type: Array,
      default: () => [10, 20, 50],
    },
    busy: { type: Boolean, default: false },
  },

  emits: ['change-page', 'change-page-size'],
}
</script>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding-top: 12px;
  font-size: 12.5px;
  color: #4c5b6b;
}

.pager-info {
  font-variant-numeric: tabular-nums;
  color: #6b7280;
}

.pager-btns {
  display: flex;
  gap: 8px;
}

.pager-btn {
  height: 26px;
  padding: 0 12px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #4c5b6b;
  cursor: pointer;
}

.pager-btn:hover:not(:disabled) {
  border-color: #2f6fed;
  color: #2f6fed;
}

.pager-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.pager-size {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.pager-size select {
  height: 26px;
  padding: 0 6px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  font-size: 12px;
  color: #2c3e50;
  background: #fff;
  outline: none;
}

.pager-size select:focus {
  border-color: #2f6fed;
}
</style>

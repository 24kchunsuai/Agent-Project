<template>
  <aside class="sidebar">
    <button class="new-btn" @click="$emit('create')">＋ 新建会话</button>

    <div class="list">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="item"
        :class="{ active: s.id === activeId }"
        @click="$emit('select', s.id)"
      >
        <div class="item-main">
          <p class="item-title">{{ s.title }}</p>
          <p class="item-time">{{ fmtTime(s.updatedAt) }}</p>
        </div>
        <button
          class="del-btn"
          title="删除会话"
          @click.stop="$emit('remove', s.id)"
        >×</button>
      </div>
      <p v-if="!sessions.length" class="empty-hint">暂无会话</p>
    </div>
  </aside>
</template>

<script>
export default {
  name: 'SessionSidebar',
  props: {
    sessions: { type: Array, default: () => [] },
    activeId: { type: String, default: '' },
  },
  emits: ['select', 'create', 'remove'],
  methods: {
    fmtTime(ts) {
      if (!ts) return ''
      const d = new Date(ts)
      const pad = (n) => String(n).padStart(2, '0')
      const today = new Date()
      const sameDay =
        d.getFullYear() === today.getFullYear() &&
        d.getMonth() === today.getMonth() &&
        d.getDate() === today.getDate()
      const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
      if (sameDay) return hm
      return `${d.getMonth() + 1}/${d.getDate()} ${hm}`
    },
  },
}
</script>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid #eef0f2;
  padding: 14px 10px 10px;
  background: #fafbfc;
}

.new-btn {
  width: 100%;
  padding: 8px 0;
  font-size: 13px;
  color: #185fa5;
  background: #ffffff;
  border: 1px solid #b5d4f4;
  border-radius: 8px;
  cursor: pointer;
  font-family: inherit;
}

.new-btn:hover {
  background: #e6f1fb;
}

.list {
  flex: 1;
  overflow-y: auto;
  margin-top: 10px;
}

.item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 8px 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 2px;
}

.item:hover {
  background: #f1f3f5;
}

.item.active {
  background: #e6f1fb;
}

.item.active .item-title {
  color: #0c447c;
}

.item-main {
  min-width: 0;
  flex: 1;
}

.item-title {
  margin: 0;
  font-size: 13px;
  color: #1f2328;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-time {
  margin: 2px 0 0;
  font-size: 11px;
  color: #9aa1ab;
}

.del-btn {
  visibility: hidden;
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  line-height: 18px;
  padding: 0;
  font-size: 14px;
  color: #8b929c;
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.item:hover .del-btn {
  visibility: visible;
}

.del-btn:hover {
  color: #a32d2d;
  background: #fcebeb;
}

.empty-hint {
  font-size: 12px;
  color: #9aa1ab;
  text-align: center;
  margin-top: 24px;
}
</style>

<template>
  <div class="admin-layout">
    <!-- 左侧导航：模仿 AgentCraft 的管理后台形态，把原先堆在一个看板页里的
         十二个数据面板按功能域拆到独立页面，靠导航切换而不是一路往下滚。 -->
    <aside class="admin-sidebar" :class="{ collapsed }">
      <div class="sidebar-header">
        <span class="sidebar-logo">🛩️</span>
        <span v-if="!collapsed" class="sidebar-title">Drone Agent 控制台</span>
      </div>

      <nav class="sidebar-nav">
        <template v-for="group in menuGroups" :key="group.label">
          <div v-if="!collapsed" class="nav-group-label">{{ group.label }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.path"
            class="nav-item"
            :class="{ active: isActive(item) }"
            :to="item.path"
            :title="collapsed ? item.label : ''"
          >
            <span class="nav-icon">{{ item.icon }}</span>
            <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
            <span
              v-if="!collapsed && item.badge && item.badge()"
              class="nav-badge"
              :class="item.badgeClass || ''"
            >{{ item.badge() }}</span>
          </router-link>
        </template>
      </nav>

      <div class="sidebar-footer">
        <div v-if="!collapsed" class="user-box">
          <div class="user-name">{{ userLabel || '未登录' }}</div>
          <div class="user-role">{{ roleText }}</div>
        </div>
        <button class="side-btn" type="button" @click="toggleCollapse" :title="collapsed ? '展开导航' : '收起导航'">
          {{ collapsed ? '»' : '« 收起' }}
        </button>
        <button v-if="loggedIn" class="side-btn danger" type="button" @click="doLogout">退出登录</button>
        <router-link v-else class="side-btn primary" to="/login">登录</router-link>
      </div>
    </aside>

    <!-- 右侧内容区 -->
    <div class="admin-main">
      <header class="main-topbar">
        <div class="crumb">
          <span class="crumb-root">{{ crumbRoot }}</span>
          <span v-if="crumbTail" class="crumb-sep">/</span>
          <span v-if="crumbTail" class="crumb-tail">{{ crumbTail }}</span>
        </div>
        <div class="topbar-right">
          <div class="health" :class="healthClass" :title="healthDetail || healthText">
            <span class="dot"></span>
            <span class="health-text">{{ healthText }}</span>
          </div>
        </div>
      </header>

      <div class="main-scroll">
        <router-view />
      </div>
    </div>
  </div>
</template>

<script>
/**
 * 管理控制台外壳（左侧导航 + 右侧内容）。
 *
 * 为什么要有这一层：原先 /dashboard/metrics 一个页里堆了十二个数据面板——
 * 总览卡片、趋势、意图分布、工具 Top、委派、故障、会话明细、trace 追溯、
 * 反馈、告警联动、告警清单，全在一列往下滚，越加越长且彼此无关。
 * 现在按「看什么」拆成独立页面，导航承担组织职责（参考 AgentCraft 的
 * admin 侧栏形态：菜单分组 + 页面各管一域）。
 *
 * 菜单可见性沿用既有约定（数据中台开发规约 §10 开放问题 3）：观测类页面
 * admin-only，对话/知识库等通用页面人人可见。
 */
import { getUser, displayName, isLoggedIn, clearAuth } from '../utils/auth'
import { logout } from '../api/auth'
import { fetchOpsHealth } from '../api/ops'

export default {
  name: 'AdminLayout',
  data() {
    return {
      collapsed: false,
      loggedIn: false,
      userLabel: '',
      role: '',
      healthState: 'unknown',
      healthDetail: '',
      healthTimer: null,
      menuGroups: [
        {
          label: '智能体',
          items: [
            { path: '/', icon: '💬', label: '智能体对话', exact: true },
            { path: '/kb', icon: '📚', label: '知识库管理' },
            { path: '/profile', icon: '🪪', label: '我的画像' },
          ],
        },
        {
          label: '数据中台 · 观测',
          admin: true,
          items: [
            { path: '/dashboard/metrics', icon: '📊', label: '总览', exact: true },
            { path: '/dashboard/conversations', icon: '🧭', label: '会话明细' },
            { path: '/dashboard/tools', icon: '🔧', label: '工具调用' },
            { path: '/dashboard/delegations', icon: '🤝', label: '智能体委派' },
            { path: '/dashboard/faults', icon: '⚠️', label: '故障与降级' },
            // E-12：浏览器侧的报错。刻意与「故障与降级」并列而不是合并——那页讲服务端故障
            // 与降级（LLM 超时走兜底、向量库不可用退化），排查动作是看组件健康与降级计数；
            // 这页讲浏览器，排查动作是看前端代码与版本。混一页会让"哪一侧坏了"都判断不清。
            { path: '/dashboard/client-errors', icon: '🐞', label: '前端错误' },
            // E-13：成本与配额——运营/管理员视角（"这个月花了多少钱、谁在用"），
            // 与"系统健不健康"不同源，混进总览会把两件事搅在一起。
            { path: '/dashboard/costs', icon: '💰', label: '成本与配额' },
          ],
        },
        {
          label: '数据中台 · 运营',
          admin: true,
          items: [
            { path: '/dashboard/feedback', icon: '⭐', label: '反馈与金标集' },
            { path: '/dashboard/unanswered', icon: '🕳️', label: '知识缺口' },
            { path: '/dashboard/alarms', icon: '🚨', label: '告警处置' },
            { path: '/dashboard/reports', icon: '📈', label: '报表导出' },
          ],
        },
        {
          label: '系统',
          items: [{ path: '/about', icon: 'ℹ️', label: '关于本项目' }],
        },
      ],
    }
  },
  computed: {
    isAdmin() {
      const u = getUser()
      return !!u && (u.permission === 'admin' || u.role === 'admin')
    },
    roleText() {
      if (!this.loggedIn) return '未登录（只读）'
      return this.isAdmin ? '管理员 · 全部权限' : '普通用户 · 只读观测'
    },
    healthClass() {
      return `health-${this.healthState}`
    },
    healthText() {
      // 三态（E-3）：up=系统正常 / degraded=有组件降级但服务仍在 / down=核心链路不可用
      if (this.healthState === 'up') return '系统正常'
      if (this.healthState === 'degraded') return '系统降级'
      if (this.healthState === 'down') return '系统异常'
      return '探测中'
    },
    crumbRoot() {
      const p = this.$route.path
      if (p.startsWith('/dashboard')) return '数据中台'
      if (p === '/kb') return '知识库管理'
      if (p === '/about') return '关于本项目'
      return '智能体对话'
    },
    crumbTail() {
      const p = this.$route.path
      if (p === '/dashboard/metrics') return '总览'
      if (p === '/dashboard/conversations') return '会话明细'
      if (p === '/dashboard/tools') return '工具调用'
      if (p === '/dashboard/delegations') return '智能体委派'
      if (p === '/dashboard/faults') return '故障与降级'
      if (p === '/dashboard/client-errors') return '前端错误'
      if (p === '/dashboard/costs') return '成本与配额'
      if (p === '/dashboard/feedback') return '反馈与金标集'
      if (p === '/dashboard/unanswered') return '知识缺口'
      if (p === '/dashboard/alarms') return '告警处置'
      if (p === '/dashboard/reports') return '报表导出'
      return ''
    },
  },
  mounted() {
    this.refreshAuth()
    this.checkHealth()
    // 状态灯要"会变"才有意义。30s 轮一次：Python 侧聚合自带 3s 缓存 + 8060 探测
    // 有 2s 超时，所以轮询开销可控；页面切走后由 beforeUnmount 清掉，不留悬挂定时器。
    this.healthTimer = setInterval(this.checkHealth, 30000)
  },
  beforeUnmount() {
    if (this.healthTimer) {
      clearInterval(this.healthTimer)
      this.healthTimer = null
    }
  },
  methods: {
    isActive(item) {
      const p = this.$route.path
      if (item.exact) return p === item.path
      return p === item.path || p.startsWith(item.path + '/')
    },
    refreshAuth() {
      this.loggedIn = isLoggedIn()
      this.userLabel = this.loggedIn ? displayName() : ''
      const u = getUser()
      this.role = u ? u.role || u.permission || '' : ''
    },
    /**
     * 拉运维聚合（E-3）。顶栏只认顶层 status；明细收进 title，鼠标悬停即可看到
     * "到底哪一环坏了"——这正是 C3 判据要的东西（5 分钟内指出哪一环，而不是翻日志）。
     */
    async checkHealth() {
      try {
        const data = await fetchOpsHealth()
        // 防御：后端异常版本可能没带 status，此时不能把灯刷成"正常"
        this.healthState = (data && data.status) || 'unknown'
        const comps = (data && data.components) || []
        const bad = comps
          .filter((c) => c.status !== 'up')
          .map((c) => `${c.name}: ${c.status}${c.detail ? ' — ' + c.detail : ''}`)
        this.healthDetail = bad.length
          ? bad.join('\n')
          : `全部 ${comps.length} 个组件正常（${(data && data.checkedAt) || ''}）`
      } catch (e) {
        // 8080 都连不上 = 核心链路不可用，直接红灯（拿不到明细，只能给原因）
        this.healthState = 'down'
        this.healthDetail = `无法连接后端服务：${(e && e.message) || '未知错误'}`
      }
    },
    async doLogout() {
      try {
        await logout()
      } catch (e) {
        // 后端退出失败不阻塞本地清理（token 可能已过期）
      }
      clearAuth()
      this.refreshAuth()
      if (this.$route.path.startsWith('/dashboard')) this.$router.push('/')
    },
    toggleCollapse() {
      this.collapsed = !this.collapsed
      try {
        localStorage.setItem('agt_sidebar_collapsed', this.collapsed ? '1' : '0')
      } catch (e) {
        // 隐私模式下 localStorage 可能不可写，忽略即可
      }
    },
  },
}
</script>

<style scoped>
.admin-layout {
  display: flex;
  height: 100vh;
  background: #f6f7f9;
}

/* ============ 左侧导航 ============ */
.admin-sidebar {
  width: 232px;
  flex-shrink: 0;
  background: linear-gradient(180deg, #12253c 0%, #16324f 100%);
  display: flex;
  flex-direction: column;
  transition: width 0.18s ease;
}
.admin-sidebar.collapsed {
  width: 62px;
}

.sidebar-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  min-height: 62px;
}
.sidebar-logo {
  font-size: 20px;
  flex-shrink: 0;
}
.sidebar-title {
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.sidebar-nav {
  flex: 1;
  /* min-height:0 是必需品：flex 子项默认 min-height:auto，内容一多就把
     兄弟节点（footer）顶出容器，表现为"底部按钮跑到导航中间/被遮挡"。
     配合 overflow-y:auto 才能让长菜单在自己区域内滚动。 */
  min-height: 0;
  overflow-y: auto;
  padding: 10px 0 16px;
}
.sidebar-nav::-webkit-scrollbar {
  width: 6px;
}
.sidebar-nav::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.15);
  border-radius: 3px;
}

.nav-group-label {
  padding: 14px 18px 6px;
  font-size: 11px;
  letter-spacing: 0.6px;
  color: rgba(255, 255, 255, 0.38);
  text-transform: uppercase;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 2px 8px;
  padding: 9px 12px;
  border-radius: 7px;
  color: rgba(255, 255, 255, 0.72);
  text-decoration: none;
  font-size: 13px;
  white-space: nowrap;
  transition: background 0.16s, color 0.16s;
}
.nav-item:hover {
  background: rgba(255, 255, 255, 0.09);
  color: #fff;
}
.nav-item.active {
  background: #2f81f7;
  color: #fff;
  font-weight: 500;
}
.nav-icon {
  font-size: 15px;
  width: 18px;
  text-align: center;
  flex-shrink: 0;
}
.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.nav-badge {
  margin-left: auto;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.16);
}

.sidebar-footer {
  flex-shrink: 0;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.user-box {
  background: rgba(255, 255, 255, 0.06);
  border-radius: 7px;
  padding: 9px 11px;
}
.user-name {
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-role {
  color: rgba(255, 255, 255, 0.5);
  font-size: 11px;
  margin-top: 2px;
}
.side-btn {
  display: block;
  text-align: center;
  padding: 7px 10px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: transparent;
  color: rgba(255, 255, 255, 0.75);
  font-size: 12px;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.16s;
}
.side-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}
.side-btn.danger {
  border-color: rgba(255, 99, 99, 0.35);
  color: rgba(255, 160, 160, 0.9);
}
.side-btn.danger:hover {
  background: rgba(255, 77, 79, 0.85);
  color: #fff;
}
.side-btn.primary {
  background: #2f81f7;
  border-color: #2f81f7;
  color: #fff;
}

/* ============ 右侧内容 ============ */
.admin-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.main-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 50px;
  flex-shrink: 0;
  padding: 0 22px;
  background: #fff;
  border-bottom: 1px solid #e8ebef;
}
.crumb {
  font-size: 13px;
  color: #6b7280;
}
.crumb-root {
  color: #1f2328;
  font-weight: 500;
}
.crumb-sep {
  margin: 0 7px;
  color: #c4c9d1;
}
.crumb-tail {
  color: #1f2328;
}

.health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #6b7280;
}
.health .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #c4c9d1;
}
.health-up .dot {
  background: #12a76b;
}
/* degraded（E-3）：有组件降级但服务仍在——黄灯。与红灯严格区分：
   黄=能力受损（如 Milvus 挂了、8060 不可达），红=核心链路不可用。 */
.health-degraded .dot {
  background: #e6a23c;
}
.health-down .dot {
  background: #d9534f;
}

.main-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 22px 40px;
}
</style>

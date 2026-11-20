import { createRouter, createWebHistory } from 'vue-router'
import AdminLayout from '../components/AdminLayout.vue'

/**
 * 路由结构（重构后）：
 *
 *   AdminLayout（左侧导航 + 右侧内容）
 *   ├── /                       智能体对话
 *   ├── /kb                     知识库管理
 *   ├── /dashboard/metrics      数据总览
 *   ├── /dashboard/conversations 会话明细（含 trace 追溯）
 *   ├── /dashboard/tools        工具调用
 *   ├── /dashboard/delegations  智能体委派
 *   ├── /dashboard/faults       故障与降级
 *   ├── /dashboard/feedback     反馈与金标集
 *   ├── /dashboard/alarms       告警处置
 *   ├── /dashboard/reports      报表导出
 *   ├── /dashboard/unanswered   知识缺口（未命中聚类 + 补库建议）
 *   ├── /dashboard/client-errors 前端错误（浏览器侧运行时报错，E-12）
 *   ├── /dashboard/costs        成本与配额（LLM token 用量与成本折算，E-13）
 *   └── /about                  关于本项目
 *
 * /login 是独立全屏页（不进导航壳）——它是登录动作本身，套导航反而怪。
 *
 * 为什么从"一个看板页"拆成八页：原 MetricsDashboardView 一度堆到 670 行、
 * 十二个数据面板、一列到底，加一个面板就长一截（用户原话"越堆越长"）。
 * 拆分的判据是"看什么"，不是"数据来自哪张表"——同一张 agent_chat_log 的
 * 汇总与会话明细分属总览和会话明细页，因为看它们的心智完全不同。
 */
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import(/* webpackChunkName: "login" */ '../views/LoginView.vue')
  },
  {
    path: '/',
    component: AdminLayout,
    children: [
      {
        path: '',
        name: 'agent-chat',
        component: () => import(/* webpackChunkName: "chat" */ '../views/AgentChatView.vue')
      },
      {
        // 知识库管理：上传文档、查看文档列表、回收站、治理面板、检索预览
        path: 'kb',
        name: 'knowledge-base',
        component: () => import(/* webpackChunkName: "kb" */ '../views/KnowledgeBaseView.vue')
      },
      {
        // 我的画像（M-5）：查看/删除模型记住的跨会话偏好（知情权入口）
        path: 'profile',
        name: 'user-profile',
        component: () => import(/* webpackChunkName: "profile" */ '../views/ProfileView.vue')
      },

      // ---- 数据中台看板（仅 admin 可见，规约 §10 开放问题 3）----
      // 每个子路由一个独立页面，各管一域；共用 AdminLayout 的左侧导航。
      {
        path: 'dashboard/metrics',
        name: 'metrics-overview',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsOverviewView.vue')
      },
      {
        path: 'dashboard/conversations',
        name: 'metrics-conversations',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsConversationsView.vue')
      },
      {
        path: 'dashboard/tools',
        name: 'metrics-tools',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsToolsView.vue')
      },
      {
        path: 'dashboard/delegations',
        name: 'metrics-delegations',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsDelegationsView.vue')
      },
      {
        path: 'dashboard/faults',
        name: 'metrics-faults',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsFaultsView.vue')
      },
      {
        path: 'dashboard/feedback',
        name: 'metrics-feedback',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsFeedbackView.vue')
      },
      {
        path: 'dashboard/alarms',
        name: 'metrics-alarms',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsAlarmsView.vue')
      },
      {
        path: 'dashboard/reports',
        name: 'metrics-reports',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsReportsView.vue')
      },
      {
        path: 'dashboard/unanswered',
        name: 'metrics-unanswered',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsUnansweredView.vue')
      },
      {
        // 前端错误（E-12）：浏览器侧运行时报错（三处收口汇总）
        path: 'dashboard/client-errors',
        name: 'metrics-client-errors',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsClientErrorsView.vue')
      },
      {
        // 成本与配额（E-13）：LLM token 用量与成本折算（判据 C7「可计量」）
        path: 'dashboard/costs',
        name: 'metrics-costs',
        component: () => import(/* webpackChunkName: "metrics" */ '../views/metrics/MetricsCostsView.vue')
      },

      // 旧路径兜底：原来只有 /dashboard/metrics 一个页面，外部书签/文档里
      // 记的多是这个地址，保持指向「总览」不动。
      {
        path: 'dashboard',
        redirect: '/dashboard/metrics'
      },
      {
        path: 'about',
        name: 'about',
        component: () => import(/* webpackChunkName: "about" */ '../views/AboutView.vue')
      }
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/'
  }
]

const router = createRouter({
  history: createWebHistory(process.env.BASE_URL),
  routes,
  scrollBehavior() {
    // 左侧导航切页时回到顶部：各页面都是长内容，不重置会停在上一页的滚动位置
    return { top: 0 }
  }
})

export default router

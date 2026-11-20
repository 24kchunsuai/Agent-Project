<template>
  <div class="kb-page">
    <header class="kb-header">
      <div class="title-block">
        <h1>知识库管理</h1>
        <p class="subtitle">上传文档 → 切片入库 → 立即参与智能体检索</p>
      </div>
      <!-- 登录入口已收归左侧导航（AdminLayout），本页只在需要登录的操作旁
           提示「上传需要登录」，不再重复一个全局登录按钮。
           注意：loggedIn 本身仍在本页多处用于权限门控（上传/回收站/治理 tab），
           不能一并删掉。 -->
    </header>

    <!-- ============ 统计概览 ============ -->
    <section class="stats-grid">
      <div class="stat-card">
        <span class="stat-label">文档数</span>
        <strong class="stat-value">{{ stats.docCount }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">切片数</span>
        <strong class="stat-value">{{ stats.chunkCount }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">公开文档</span>
        <strong class="stat-value">{{ stats.publicDocCount }}</strong>
      </div>
      <div class="stat-card">
        <span class="stat-label">私有文档</span>
        <strong class="stat-value">{{ stats.privateDocCount }}</strong>
      </div>
      <div class="stat-card wide">
        <span class="stat-label">向量库集合</span>
        <strong class="stat-value small">{{ stats.collection || '—' }}</strong>
      </div>
    </section>

    <!-- ============ 上传 ============ -->
    <section class="panel">
      <div class="panel-head">
        <h2>上传文档</h2>
        <span class="panel-hint">支持 .md / .markdown / .txt</span>
      </div>

      <div class="upload-grid">
        <label class="field">
          <span class="field-label">选择文件（可多选）</span>
          <input
            ref="fileInput"
            type="file"
            accept=".md,.markdown,.txt"
            multiple
            :disabled="uploading"
            @change="onFileChange"
          />
        </label>

        <label class="field">
          <span class="field-label">分类（可选）</span>
          <input
            v-model.trim="form.category"
            type="text"
            maxlength="64"
            placeholder="如 regulation / concept"
            :disabled="uploading"
          />
        </label>

        <label class="field">
          <span class="field-label">可见性</span>
          <select v-model="form.visibility" :disabled="uploading">
            <option value="public">公开（所有人可检索）</option>
            <option value="private">私有（仅自己可检索）</option>
          </select>
        </label>

        <div class="field action-field">
          <button
            class="btn primary"
            type="button"
            :disabled="!canUpload"
            @click="doUpload"
          >
            {{ uploadButtonText }}
          </button>
        </div>
      </div>

      <p class="hint">
        支持<strong>一次选中多个文件</strong>批量入库；同名文件重复上传为<strong>幂等覆盖</strong>
        （先删旧切片再写新的），不会产生重复内容。单文件建议不超过 2M 字。
        <span v-if="!loggedIn" class="warn">上传需要登录。</span>
      </p>

      <div v-if="queue.length" class="queue">
        <div class="queue-head">
          <span>上传队列（{{ doneCount }} / {{ queue.length }}）</span>
          <button v-if="!uploading" class="btn tiny" type="button" @click="clearQueue">清空</button>
        </div>
        <ul>
          <li
            v-for="(item, idx) in queue"
            :key="item.name + idx"
            class="queue-item"
            :class="item.status"
          >
            <span class="q-idx">{{ idx + 1 }}</span>
            <span class="q-name" :title="item.name">{{ item.name }}</span>
            <span class="q-meta">{{ item.sizeText }} · {{ item.chars }} 字</span>
            <span class="q-status">{{ STATUS_TEXT[item.status] }}</span>
            <span v-if="item.message" class="q-msg">{{ item.message }}</span>
          </li>
        </ul>
      </div>

      <div v-if="uploadMsg" class="msg" :class="uploadMsg.type">{{ uploadMsg.text }}</div>
    </section>

    <!-- ============ 检索预览 ============ -->
    <section class="panel">
      <div class="panel-head">
        <h2>检索预览</h2>
        <span class="panel-hint">与智能体问答走同一个检索器，用来验证入库是否生效</span>
      </div>

      <div class="search-row">
        <input
          v-model.trim="searchQuery"
          type="text"
          placeholder="输入一个问题，看能否命中刚上传的内容"
          :disabled="searching"
          @keyup.enter="doSearch"
        />
        <button class="btn" type="button" :disabled="!searchQuery || searching" @click="doSearch">
          {{ searching ? '检索中…' : '检索' }}
        </button>
      </div>

      <div v-if="searchResult" class="search-result">
        <div class="search-meta">
          <span class="tag" :class="decisionClass">{{ decisionText }}</span>
          <span v-if="searchResult.top1_score !== null && searchResult.top1_score !== undefined">
            top1 分数：{{ Number(searchResult.top1_score).toFixed(4) }}
          </span>
          <span>耗时：{{ searchResult.trace && searchResult.trace.total_ms }} ms</span>
        </div>

        <ol v-if="searchResult.candidates && searchResult.candidates.length" class="hit-list">
          <li v-for="hit in searchResult.candidates" :key="hit.rank">
            <div class="hit-head">
              <span class="hit-rank">#{{ hit.rank }}</span>
              <span class="hit-score">{{ Number(hit.score).toFixed(4) }}</span>
              <span class="hit-doc">{{ hit.doc_title || hit.doc_id }}</span>
              <span v-if="hit.heading" class="hit-heading">{{ hit.heading }}</span>
            </div>
            <p class="hit-text">{{ hit.text }}</p>
          </li>
        </ol>
        <p v-else class="empty-line">没有召回任何片段，说明这份内容还没入库或与该问题无关。</p>
      </div>
    </section>

    <!-- ============ 文档列表 / 回收站 ============ -->
    <section class="panel">
      <div class="panel-head">
        <div class="tabs" role="tablist">
          <button
            class="tab"
            :class="{ active: activeTab === 'documents' }"
            type="button"
            role="tab"
            @click="switchTab('documents')"
          >
            文档列表
            <span v-if="docsPagination.total" class="tab-count">{{ docsPagination.total }}</span>
          </button>
          <button
            v-if="loggedIn"
            class="tab"
            :class="{ active: activeTab === 'recycle' }"
            type="button"
            role="tab"
            @click="switchTab('recycle')"
          >
            回收站
            <span v-if="recyclePagination.total" class="tab-count">{{ recyclePagination.total }}</span>
          </button>
          <button
            v-if="loggedIn"
            class="tab"
            :class="{ active: activeTab === 'governance' }"
            type="button"
            role="tab"
            @click="switchTab('governance')"
          >
            治理
            <span v-if="openTickets" class="tab-count warn">{{ openTickets }}</span>
          </button>
        </div>
        <div class="panel-actions">
          <span class="panel-hint">{{ currentHint }}</span>
          <button class="btn tiny" type="button" :disabled="listBusy" @click="refreshTab">
            {{ listBusy ? '刷新中…' : '刷新' }}
          </button>
        </div>
      </div>

      <!-- ============ 文档列表 tab ============ -->
      <template v-if="activeTab === 'documents'">
        <div v-if="listError" class="msg error">{{ listError }}</div>

        <table v-if="documents.length" class="doc-table">
          <thead>
            <tr>
              <th>文档</th>
              <th class="col-cat">分类</th>
              <th class="col-num">切片</th>
              <th class="col-num">字数</th>
              <th class="col-owner">归属</th>
              <th class="col-vis">可见性</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="doc in documents" :key="doc.docId">
              <td>
                <div class="doc-title">{{ doc.docTitle }}</div>
                <div class="doc-id" :title="doc.docId">{{ doc.docId }}</div>
              </td>
              <td class="col-cat">
                <span v-if="doc.category" class="chip">{{ doc.category }}</span>
                <span v-else class="muted">未分类</span>
              </td>
              <td class="col-num">{{ doc.chunkCount }}</td>
              <td class="col-num">{{ doc.charCount }}</td>
              <td class="col-owner">{{ doc.owner || '—' }}</td>
              <td class="col-vis">
                <span class="vis-dot" :class="doc.visibility"></span>
                {{ doc.visibility === 'private' ? '私有' : '公开' }}
              </td>
              <td class="col-op">
                <button class="btn danger tiny" type="button" @click="askDelete(doc)">移入回收站</button>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-else-if="!loading" class="empty-line">
          还没有文档。上传一份 .md 试试，上传完立刻就能被智能体检索到。
        </p>

        <PaginationBar
          v-if="docsPagination.totalPages > 1"
          :page="docsPagination.page"
          :total-pages="docsPagination.totalPages"
          :page-size="docsPagination.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="loading"
          @change-page="goDocsPage"
          @change-page-size="goDocsPageSize"
        />
      </template>

      <!-- ============ 回收站 tab ============ -->
      <template v-else-if="activeTab === 'recycle'">
        <div v-if="recycleError" class="msg error">{{ recycleError }}</div>

        <table v-if="recycledItems.length" class="doc-table">
          <thead>
            <tr>
              <th>文档</th>
              <th class="col-cat">分类</th>
              <th class="col-num">切片</th>
              <th class="col-owner">归属</th>
              <th class="col-owner">删除人</th>
              <th class="col-time">删除时间</th>
              <th class="col-time">剩余期限</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in recycledItems" :key="item.docId">
              <td>
                <div class="doc-title">{{ item.docTitle }}</div>
                <div class="doc-id" :title="item.docId">{{ item.docId }}</div>
              </td>
              <td class="col-cat">
                <span v-if="item.category" class="chip">{{ item.category }}</span>
                <span v-else class="muted">未分类</span>
              </td>
              <td class="col-num">{{ item.chunkCount }}</td>
              <td class="col-owner">{{ item.owner || '—' }}</td>
              <td class="col-owner">{{ item.deletedBy || '—' }}</td>
              <td class="col-time">{{ item.deletedAt || '—' }}</td>
              <td class="col-time">
                <!-- TTL 倒计时（规约 §6.3）：剩余 ≤3 天标黄，已超期标红——超期说明 TTL
                     任务还没跑到或上一轮失败，此刻恢复仍然有效 -->
                <span v-if="item.expired" class="ttl-expired">已超期</span>
                <span v-else-if="item.ttlRemainingDays == null" class="muted">—</span>
                <span v-else :class="{ 'ttl-soon': item.ttlRemainingDays <= 3 }">
                  剩余 {{ item.ttlRemainingDays }} 天
                </span>
              </td>
              <td class="col-op col-op-wide">
                <button
                  class="btn tiny"
                  type="button"
                  :disabled="restoringId === item.docId"
                  @click="doRestore(item)"
                >
                  {{ restoringId === item.docId ? '恢复中…' : '恢复' }}
                </button>
                <button class="btn danger tiny" type="button" @click="askPurge(item)">彻底删除</button>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-else-if="!recycleLoading" class="empty-line">
          回收站是空的。删除文档只是移到这里，随时可以恢复。
        </p>

        <PaginationBar
          v-if="recyclePagination.totalPages > 1"
          :page="recyclePagination.page"
          :total-pages="recyclePagination.totalPages"
          :page-size="recyclePagination.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="recycleLoading"
          @change-page="goRecyclePage"
          @change-page-size="goRecyclePageSize"
        />
      </template>

      <!-- ============ 治理 tab（G1-G4）============
           概览看板 / 工单待办 / 停用区 / 治理登记 / 审计流水，外加巡检、TTL、对账的手动触发。
           全部端点均为管理员专属，组件内自行处理越权提示 -->
      <GovernancePanel
        v-else-if="activeTab === 'governance'"
        ref="govPanel"
        :overview="govOverview"
        @overview="onGovOverview"
        @changed="onGovChanged"
      />
    </section>

    <!-- ============ 移入回收站确认 ============ -->
    <div v-if="deleteTarget" class="modal-mask" @click.self="cancelDelete">
      <div class="modal">
        <h3>移入回收站</h3>
        <p class="modal-text">
          将把文档 <strong>{{ deleteTarget.docTitle }}</strong> 移入回收站，
          其 <strong>{{ deleteTarget.chunkCount }}</strong> 个切片会立即从智能体检索范围中移除。
        </p>
        <p class="modal-text small">
          文档标识：{{ deleteTarget.docId }}<br />
          这是软删除：之后可在「回收站」标签页一键恢复。
        </p>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="deleting" @click="cancelDelete">取消</button>
          <button class="btn danger" type="button" :disabled="deleting" @click="confirmDelete">
            {{ deleting ? '移入中…' : '移入回收站' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 彻底删除确认（双确认：复述文档名）============
         后端要求 confirm=true **且** confirmDocName 与文档名一致，缺一即返 3001。
         复述名不一致时确认按钮保持不可点——避免"点了没反应"（P0-1，2026-09-18 修） -->
    <div v-if="purgeTarget" class="modal-mask" @click.self="cancelPurge">
      <div class="modal">
        <h3>彻底删除</h3>
        <p class="modal-text">
          将<strong>永久删除</strong>文档 <strong>{{ purgeTarget.docTitle }}</strong>：
          清理回收站记录与源文件，此操作<strong>不可恢复</strong>。
        </p>
        <p class="modal-text small">
          文档标识：{{ purgeTarget.docId }}<br />
          不可逆操作须由管理员复述文档名确认（可复述标题、文档标识或文件名主干）。
        </p>
        <label class="field">
          <span class="field-label">请复述文档名：{{ purgeTargetName }}</span>
          <input
            v-model.trim="purgeInput"
            type="text"
            :disabled="purging"
            placeholder="在此输入上面的文档名"
          />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="purging" @click="cancelPurge">取消</button>
          <button
            class="btn danger"
            type="button"
            :disabled="purging || !purgeMatched"
            @click="confirmPurge"
          >
            {{ purging ? '删除中…' : '确认彻底删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 恢复确认门：命中在库近重复语料 ============
         后端返 3001 + requireConfirm 时弹（**本次恢复并未执行**），
         用户同意后由 withConfirmGate 带 confirm=true 重发（P0-2，2026-09-18 修） -->
    <div v-if="restoreGate" class="modal-mask" @click.self="cancelRestoreGate">
      <div class="modal">
        <h3>恢复前确认</h3>
        <p class="modal-text">
          回收站中的 <strong>{{ restoreGate.docTitle }}</strong> 与在库语料语义高度相似
          （最高相似度 <strong>{{ restoreGate.maxSimilarity }}</strong>，阈值
          {{ restoreGate.threshold }}），恢复会多出一份近重复语料。
        </p>
        <ul v-if="(restoreGate.hits || []).length" class="hit-list">
          <li v-for="h in restoreGate.hits" :key="h.docId">
            {{ h.docTitle }}<span class="muted"> · 相似度 {{ h.score }}</span>
          </li>
        </ul>
        <p class="modal-text small">本次恢复尚未执行，取消不会改动任何数据。</p>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="!!restoringId" @click="cancelRestoreGate">
            取消
          </button>
          <button
            class="btn danger"
            type="button"
            :disabled="!!restoringId"
            @click="confirmRestoreGate"
          >
            {{ restoringId ? '恢复中…' : '确认恢复' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import {
  uploadDocument,
  fetchDocuments,
  deleteDocument,
  fetchRecycleBin,
  restoreDocument,
  purgeDocument,
  fetchKbStats,
  searchKb,
  fetchGovernanceOverview,
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
} from '../api/kb'
import { isLoggedIn, displayName } from '../utils/auth'
import { nameRepeated, withConfirmGate } from '../utils/confirmGate'
import PaginationBar from '../components/PaginationBar.vue'
import GovernancePanel from '../components/GovernancePanel.vue'

/** 后端 4010：会话过期/未登录，request.js 拦截器会清本地登录态并抛出该文案 */
const NEED_LOGIN_HINT = '请先登录后再上传文档'

const DECISION_TEXT = {
  kept: '命中证据',
  insufficient_evidence: '证据不足',
  cross_domain: '完全跨域',
  no_hit: '无召回',
}

export default {
  name: 'KnowledgeBaseView',

  components: { PaginationBar, GovernancePanel },

  data() {
    return {
      stats: { docCount: 0, chunkCount: 0, publicDocCount: 0, privateDocCount: 0, collection: '' },

      // 文档列表（分页）
      activeTab: 'documents',
      documents: [],
      loading: false,
      listError: '',
      docsPagination: {
        page: 1,
        pageSize: DEFAULT_PAGE_SIZE,
        total: 0,
        totalPages: 1,
      },

      // 回收站（分页，需登录可见）
      recycledItems: [],
      recycleLoading: false,
      recycleLoaded: false,
      recycleError: '',
      recyclePagination: {
        page: 1,
        pageSize: DEFAULT_PAGE_SIZE,
        total: 0,
        totalPages: 1,
      },
      restoringId: '',
      pageSizeOptions: PAGE_SIZE_OPTIONS,

      // 治理概览（G1-G4）：只为「治理」tab 的待办角标而拉，进页面即请求一次
      govOverview: {},

      form: { category: '', visibility: 'public' },
      queue: [],
      uploading: false,
      uploadMsg: null,
      STATUS_TEXT: {
        pending: '待上传',
        uploading: '入库中',
        success: '成功',
        error: '失败',
      },

      searchQuery: '',
      searching: false,
      searchResult: null,

      deleteTarget: null,
      deleting: false,
      purgeTarget: null,
      purging: false,
      // 彻底删除的复述文档名（后端双确认要求；输错则确认按钮不可点）
      purgeInput: '',
      // 恢复的 L3 确认门：命中在库近重复语料时弹框（存后端返回的确认依据）
      restoreGate: null,
      restoreGateResolve: null,
    }
  },

  computed: {
    loggedIn() {
      return isLoggedIn()
    },
    userLabel() {
      return displayName()
    },
    /** 彻底删除要向用户展示、并要求复述的文档名（文档标题优先，与后端 doc_name 同源） */
    purgeTargetName() {
      return (this.purgeTarget && (this.purgeTarget.docTitle || this.purgeTarget.docId)) || ''
    },
    /** 后端允许复述"标题 / docId / 文件名主干"三者之一，前端校验保持同一口径 */
    purgeCandidates() {
      const t = this.purgeTarget || {}
      const stem = String(t.fileName || '').replace(/\.[^.]+$/, '')
      return [t.docTitle, t.docId, stem].filter(Boolean)
    },
    /** 复述一致才允许点「确认彻底删除」（后端双确认要求，不一致必然被拒） */
    purgeMatched() {
      return nameRepeated(this.purgeInput, this.purgeCandidates)
    },
    canUpload() {
      return this.loggedIn && this.queue.some((it) => it.status === 'pending') && !this.uploading
    },
    doneCount() {
      return this.queue.filter(
        (it) => it.status === 'success' || it.status === 'error'
      ).length
    },
    uploadButtonText() {
      if (this.uploading) {
        return `入库中 ${this.doneCount}/${this.queue.length}…`
      }
      if (this.queue.length > 1) {
        return `批量上传并入库（${this.queue.length}）`
      }
      return '上传并入库'
    },
    decisionText() {
      if (!this.searchResult) return ''
      return DECISION_TEXT[this.searchResult.decision] || this.searchResult.decision
    },
    decisionClass() {
      if (!this.searchResult) return ''
      return `decision-${this.searchResult.decision}`
    },
    listBusy() {
      if (this.activeTab === 'documents') return this.loading
      if (this.activeTab === 'recycle') return this.recycleLoading
      // 治理 tab 的各列表有自己的忙碌态，这里不接管，避免两个"刷新中"打架
      return false
    },
    recycleHint() {
      return '内容已不再参与检索'
    },
    /** 治理 tab 的待办角标：直接吃概览里的 openTickets，不额外发请求 */
    openTickets() {
      return Number(this.govOverview.openTickets || 0)
    },
    currentHint() {
      if (this.activeTab === 'documents') return `共 ${this.docsPagination.total} 篇`
      if (this.activeTab === 'recycle') return `共 ${this.recyclePagination.total} 条，${this.recycleHint}`
      return this.openTickets ? `待办工单 ${this.openTickets} 条` : '治理视图仅管理员可见'
    },
  },

  mounted() {
    this.init()
  },

  methods: {
    /**
     * 首次进入：统计 + 文档列表一起加载；回收站与治理延迟到点开 tab 再拉。
     *
     * 治理概览是例外——它要提前拉一次，因为「治理」tab 上的待办角标
     * 必须在用户点进去之前就显示出来（否则"有没有待办"这件事就被藏起来了）。
     * 失败一律静默：非管理员会拿到 4011，那不是页面错误。
     */
    async init() {
      const tasks = [this.loadStats(), this.loadDocuments()]
      if (this.loggedIn) {
        tasks.push(this.loadGovOverview())
      }
      await Promise.all(tasks)
    },

    async loadGovOverview() {
      try {
        this.govOverview = (await fetchGovernanceOverview()) || {}
      } catch (e) {
        // 非管理员 / 治理库不可用：不打断页面，治理 tab 自己会给出说明
        console.warn('治理概览加载失败（可能非管理员）', e)
      }
    },

    onGovOverview(data) {
      this.govOverview = data || {}
    },

    /** 治理 tab 里改了在库内容（工单执行 / 停用 / 启用 / 回滚）→ 统计与文档列表都变了 */
    onGovChanged() {
      this.loadStats()
      this.loadDocuments()
      if (this.recycleLoaded) {
        this.loadRecycle()
      }
    },

    async loadStats() {
      try {
        this.stats = (await fetchKbStats()) || this.stats
      } catch (e) {
        // 统计失败不打断页面，列表仍可看
        console.warn('知识库统计加载失败', e)
      }
    },

    async loadDocuments() {
      this.loading = true
      this.listError = ''
      try {
        const docs = await fetchDocuments({
          page: this.docsPagination.page,
          pageSize: this.docsPagination.pageSize,
        })
        this.documents = (docs && docs.items) || []
        this.docsPagination.page = docs.page
        this.docsPagination.pageSize = docs.pageSize
        this.docsPagination.total = docs.total
        this.docsPagination.totalPages = docs.totalPages
      } catch (e) {
        this.listError = e.message || '文档列表加载失败'
        this.documents = []
      } finally {
        this.loading = false
      }
    },

    async loadRecycle() {
      if (!this.loggedIn) {
        this.recycleError = '回收站需要登录后查看'
        return
      }
      this.recycleLoading = true
      this.recycleError = ''
      try {
        const bin = await fetchRecycleBin({
          page: this.recyclePagination.page,
          pageSize: this.recyclePagination.pageSize,
        })
        this.recycledItems = (bin && bin.items) || []
        this.recyclePagination.page = bin.page
        this.recyclePagination.pageSize = bin.pageSize
        this.recyclePagination.total = bin.total
        this.recyclePagination.totalPages = bin.totalPages
        this.recycleLoaded = true
      } catch (e) {
        this.recycleError = e.message || '回收站加载失败'
        this.recycledItems = []
      } finally {
        this.recycleLoading = false
      }
    },

    switchTab(tab) {
      if (this.activeTab === tab) return
      this.activeTab = tab
      // 回收站首次点开才拉取，之后沿用已加载的分页状态
      if (tab === 'recycle' && !this.recycleLoaded) {
        this.loadRecycle()
      }
    },

    refreshTab() {
      if (this.activeTab === 'documents') {
        this.loadDocuments()
        this.loadStats()
      } else if (this.activeTab === 'recycle') {
        this.loadRecycle()
      } else if (this.$refs.govPanel) {
        // 治理 tab 的刷新交给它自己：它知道自己停在哪个子视图
        this.$refs.govPanel.refresh()
      }
    },

    goDocsPage(page) {
      if (page < 1 || page > this.docsPagination.totalPages) return
      this.docsPagination.page = page
      this.loadDocuments()
    },

    goDocsPageSize(size) {
      // 改每页条数后回到第 1 页，避免页码越界
      this.docsPagination.pageSize = size
      this.docsPagination.page = 1
      this.loadDocuments()
    },

    goRecyclePage(page) {
      if (page < 1 || page > this.recyclePagination.totalPages) return
      this.recyclePagination.page = page
      this.loadRecycle()
    },

    goRecyclePageSize(size) {
      this.recyclePagination.pageSize = size
      this.recyclePagination.page = 1
      this.loadRecycle()
    },

    /**
     * 读取所选文件为文本，逐个校验后放入待上传队列。
     * 两类文件会被拦在队列外并说明原因：
     *   1. 格式不支持
     *   2. 非 UTF-8（如 Windows 记事本另存的 GBK）—— 在 JS 侧会解出大量替换字符，
     *      若直接上传会变成一片乱码且难以定位原因，这里提前识别并提示转码。
     */
    async onFileChange(event) {
      const files = Array.from(event.target.files || [])
      this.uploadMsg = null
      event.target.value = ''
      if (!files.length) return

      const accepted = []
      const rejected = []

      for (const file of files) {
        if (!/\.(md|markdown|txt)$/i.test(file.name)) {
          rejected.push(`${file.name}（格式不支持）`)
          continue
        }
        // 逐个读取：await 必须串行，file.text() 本身不阻塞
        const text = await file.text()
        const garbled = (text.match(/\uFFFD/g) || []).length
        if (garbled > 0 && garbled / Math.max(text.length, 1) > 0.01) {
          rejected.push(`${file.name}（编码非 UTF-8，疑似 GBK）`)
          continue
        }
        if (!text.trim()) {
          rejected.push(`${file.name}（内容为空）`)
          continue
        }
        accepted.push({
          name: file.name,
          text,
          chars: text.length,
          sizeText: this.formatSize(file.size),
          status: 'pending',
          message: '',
        })
      }

      this.queue = accepted

      if (rejected.length) {
        this.uploadMsg = {
          type: 'error',
          text:
            `已跳过 ${rejected.length} 个文件：${rejected.join('；')}` +
            (accepted.length ? `。其余 ${accepted.length} 个已加入队列。` : ''),
        }
      } else {
        this.uploadMsg = {
          type: 'success',
          text: `已加入队列 ${accepted.length} 个文件，点击「批量上传并入库」开始`,
        }
      }
    },

    formatSize(bytes) {
      if (bytes < 1024) return `${bytes} B`
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`
    },

    async doUpload() {
      if (!this.loggedIn) {
        this.uploadMsg = { type: 'error', text: NEED_LOGIN_HINT }
        return
      }
      // 已成功的跳过，便于失败后直接重试（不会重复入库）
      const targets = this.queue.filter((it) => it.status !== 'success')
      if (!targets.length) return

      this.uploading = true
      this.uploadMsg = null

      let ok = 0
      let fail = 0

      // 串行上传：入库需调用 embedding 接口，并发易触发限流；
      // 串行也能让进度与每条结果稳定对应。
      for (const item of targets) {
        item.status = 'uploading'
        item.message = ''
        try {
          const res = await uploadDocument({
            fileName: item.name,
            content: item.text,
            category: this.form.category,
            visibility: this.form.visibility,
          })
          item.status = 'success'
          item.message =
            `切片 ${res.chunkCount} 个 · ${res.charCount} 字 · 耗时 ${res.elapsedSec}s` +
            (res.replacedChunks ? ` · 覆盖旧切片 ${res.replacedChunks} 个` : '')
          ok += 1
        } catch (e) {
          item.status = 'error'
          item.message = e.message || '上传失败'
          fail += 1
        }
      }

      this.uploading = false
      this.uploadMsg = fail
        ? {
            type: 'error',
            text: `完成：成功 ${ok} 个，失败 ${fail} 个（失败原因见上方队列，可再次点击重试）`,
          }
        : { type: 'success', text: `全部入库成功：共 ${ok} 个文件` }

      await Promise.all([this.loadStats(), this.loadDocuments()])
    },

    clearQueue() {
      this.queue = []
      this.uploadMsg = null
      if (this.$refs.fileInput) {
        this.$refs.fileInput.value = ''
      }
    },

    async doSearch() {
      if (!this.searchQuery) return
      this.searching = true
      this.searchResult = null
      try {
        this.searchResult = await searchKb(this.searchQuery)
      } catch (e) {
        this.uploadMsg = { type: 'error', text: `检索失败：${e.message}` }
      } finally {
        this.searching = false
      }
    },

    askDelete(doc) {
      this.deleteTarget = doc
    },

    cancelDelete() {
      this.deleteTarget = null
    },

    async confirmDelete() {
      if (!this.deleteTarget) return
      this.deleting = true
      try {
        const res = await deleteDocument(this.deleteTarget.docId)
        this.uploadMsg = {
          type: 'success',
          text: `已将「${res.docTitle}」移入回收站（${res.removedChunks} 个切片已停止检索），可在列表上方「回收站」标签页恢复`,
        }
        this.deleteTarget = null
        await Promise.all([this.loadStats(), this.loadDocuments()])
        // 回收站已加载过则同步刷新，让计数立即 +1
        if (this.recycleLoaded) {
          this.loadRecycle()
        }
      } catch (e) {
        this.uploadMsg = { type: 'error', text: e.message || '移入回收站失败' }
        this.deleteTarget = null
      } finally {
        this.deleting = false
      }
    },

    async doRestore(item) {
      if (this.restoringId) return
      this.restoringId = item.docId
      try {
        // 恢复要重新切片 + 向量化，耗时与上传同级，按钮文案已提示"恢复中…"。
        // 命中在库近重复语料时后端返 3001（**本次未执行恢复**）——弹框让用户看着
        // 相似度与命中文档决定，同意后由 withConfirmGate 带 confirm=true 重发。
        const res = await withConfirmGate(
          (extra) => restoreDocument(item.docId, extra),
          (payload) =>
            new Promise((resolve) => {
              this.restoreGate = { ...payload, docId: item.docId, docTitle: item.docTitle }
              this.restoreGateResolve = resolve
            })
        )
        this.uploadMsg = {
          type: 'success',
          text: `已恢复「${res.docTitle}」（${res.chunkCount} 个切片重新参与检索）`,
        }
        await this.loadRecycle()
      } catch (e) {
        // 用户在确认门里点取消也会落到这里（后端原文已说明"确认恢复请带 confirm 重发"）
        this.uploadMsg = { type: 'error', text: e.message || '恢复失败' }
      } finally {
        this.restoringId = ''
        this.restoreGate = null
        this.restoreGateResolve = null
      }
    },

    /** 确认门弹框：同意恢复（此时才带 confirm 重发；此前恢复并未发生） */
    confirmRestoreGate() {
      const resolve = this.restoreGateResolve
      this.restoreGate = null
      this.restoreGateResolve = null
      if (resolve) resolve({})
    },

    cancelRestoreGate() {
      const resolve = this.restoreGateResolve
      this.restoreGate = null
      this.restoreGateResolve = null
      if (resolve) resolve(null)
    },

    askPurge(item) {
      this.purgeTarget = item
      this.purgeInput = ''
    },

    cancelPurge() {
      this.purgeTarget = null
      this.purgeInput = ''
    },

    async confirmPurge() {
      // 复述名不一致时按钮本就不可点，这里再兜一道（防回车等方式绕过）
      if (!this.purgeTarget || !this.purgeMatched) return
      this.purging = true
      try {
        // 双确认参数必须真的发出去：早先只传 docId，后端判 confirm 未带 → 必然拒绝，
        // 管理员点确认永远是"删不掉"（P0-1，2026-09-18 修）
        const res = await purgeDocument(this.purgeTarget.docId, {
          confirm: true,
          confirmDocName: this.purgeInput,
        })
        this.uploadMsg = {
          type: 'success',
          text: `已彻底删除「${res.docTitle}」，此操作不可恢复`,
        }
        this.purgeTarget = null
        this.purgeInput = ''
        await this.loadRecycle()
      } catch (e) {
        this.uploadMsg = { type: 'error', text: e.message || '彻底删除失败' }
        this.purgeTarget = null
        this.purgeInput = ''
      } finally {
        this.purging = false
      }
    },
  },
}
</script>

<style scoped>
.kb-page {
  max-width: 1080px;
  margin: 0 auto;
  text-align: left;
}

/* ---------- 头部 ---------- */
.kb-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 14px;
  border-bottom: 1px solid #e5e7eb;
}

.title-block h1 {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: #1f2328;
}

.subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: #8a94a0;
}

.auth-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.auth-name {
  font-size: 13px;
  color: #4c5b6b;
  font-weight: 600;
}

.auth-btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #4c5b6b;
  cursor: pointer;
}

.auth-btn.primary {
  border-color: #2f6fed;
  color: #2f6fed;
}

/* ---------- 统计卡片 ---------- */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr) 1.4fr;
  gap: 12px;
  margin: 16px 0;
}

.stat-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border: 1px solid #eef0f2;
  border-radius: 10px;
  background: #fff;
}

.stat-label {
  font-size: 12px;
  color: #8a94a0;
}

.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: #1f2328;
  line-height: 1.1;
}

.stat-value.small {
  font-size: 14px;
  font-weight: 500;
}

/* ---------- 面板 ---------- */
.panel {
  padding: 16px 18px;
  margin-bottom: 16px;
  border: 1px solid #eef0f2;
  border-radius: 12px;
  background: #fff;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-head h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #1f2328;
}

.panel-hint {
  font-size: 12px;
  color: #8a94a0;
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ---------- 上传 ---------- */
.upload-grid {
  display: grid;
  grid-template-columns: 2fr 1.2fr 1.2fr auto;
  gap: 12px;
  align-items: end;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.field-label {
  font-size: 12px;
  color: #6b7280;
}

.field input[type='text'],
.field select {
  height: 34px;
  padding: 0 10px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  font-size: 13px;
  color: #2c3e50;
  background: #fff;
  outline: none;
}

.field input[type='text']:focus,
.field select:focus {
  border-color: #2f6fed;
}

.field input[type='file'] {
  font-size: 12px;
  color: #4c5b6b;
}

.btn {
  height: 34px;
  padding: 0 16px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  background: #fff;
  font-size: 13px;
  color: #4c5b6b;
  cursor: pointer;
  white-space: nowrap;
}

.btn:hover:not(:disabled) {
  border-color: #2f6fed;
  color: #2f6fed;
}

.btn.primary {
  border-color: #2f6fed;
  background: #2f6fed;
  color: #fff;
}

.btn.primary:hover:not(:disabled) {
  opacity: 0.9;
  color: #fff;
}

.btn.danger {
  border-color: #f0c2c2;
  color: #d64545;
}

.btn.danger:hover:not(:disabled) {
  border-color: #d64545;
  background: #fff5f5;
  color: #d64545;
}

.btn.tiny {
  height: 26px;
  padding: 0 10px;
  font-size: 12px;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.hint {
  margin: 10px 0 0;
  font-size: 12px;
  color: #8a94a0;
  line-height: 1.6;
}

.warn {
  color: #d64545;
}

/* ---------- 上传队列 ---------- */
.queue {
  margin-top: 12px;
  border: 1px solid #e6ebf2;
  border-radius: 8px;
  overflow: hidden;
}

.queue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  background: #f7f9fc;
  border-bottom: 1px solid #e6ebf2;
  font-size: 12px;
  color: #4c5b6b;
}

.queue ul {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 280px;
  overflow-y: auto;
}

.queue-item {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto auto;
  gap: 3px 10px;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid #f0f3f8;
  font-size: 12.5px;
  color: #33404f;
}

.queue-item:last-child {
  border-bottom: none;
}

.q-idx {
  color: #a3adba;
  font-variant-numeric: tabular-nums;
}

.q-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.q-meta {
  color: #8a94a0;
  font-size: 12px;
}

.q-status {
  min-width: 46px;
  text-align: right;
}

.q-msg {
  grid-column: 2 / -1;
  font-size: 12px;
  color: #8a94a0;
}

.queue-item.success .q-status {
  color: #2f7d4f;
}

.queue-item.error .q-status {
  color: #c0392b;
}

.queue-item.error .q-msg {
  color: #c0392b;
}

.queue-item.uploading .q-status {
  color: #2f6fed;
}

/* ---------- 提示消息 ---------- */
.msg {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 12.5px;
  line-height: 1.6;
  word-break: break-all;
}

.msg.success {
  background: #f0f8f2;
  border: 1px solid #cfe8d6;
  color: #2f7d4f;
}

.msg.error {
  background: #fff5f5;
  border: 1px solid #f3d2d2;
  color: #c0392b;
}

/* ---------- 检索预览 ---------- */
.search-row {
  display: flex;
  gap: 10px;
}

.search-row input {
  flex: 1;
  height: 34px;
  padding: 0 10px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  font-size: 13px;
  color: #2c3e50;
  outline: none;
}

.search-row input:focus {
  border-color: #2f6fed;
}

.search-result {
  margin-top: 14px;
}

.search-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: #8a94a0;
}

.tag {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: #eef2f7;
  color: #4c5b6b;
}

.tag.decision-kept {
  background: #f0f8f2;
  color: #2f7d4f;
}

.tag.decision-insufficient_evidence {
  background: #fff8ec;
  color: #a9701a;
}

.tag.decision-cross_domain,
.tag.decision-no_hit {
  background: #fff5f5;
  color: #c0392b;
}

.hit-list {
  margin: 10px 0 0;
  padding-left: 0;
  list-style: none;
}

.hit-list li {
  padding: 10px 12px;
  margin-bottom: 8px;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  background: #fafbfc;
}

/* 回收站 TTL 倒计时（规约 §6.3）：≤3 天提前提示，已超期标红 */
.ttl-soon {
  color: #c47f17;
}

.ttl-expired {
  color: #d64545;
  font-weight: 500;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 6px;
}

.hit-rank {
  font-weight: 600;
  color: #2f6fed;
}

.hit-score {
  color: #8a94a0;
}

.hit-doc {
  font-weight: 600;
  color: #1f2328;
}

.hit-heading {
  color: #6b7280;
}

.hit-text {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: #4c5b6b;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 118px;
  overflow: hidden;
}

/* ---------- 文档列表 / 回收站 ---------- */
.tabs {
  display: flex;
  gap: 4px;
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 14px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font-size: 14px;
  font-weight: 600;
  color: #8a94a0;
  cursor: pointer;
}

.tab:hover {
  color: #4c5b6b;
}

.tab.active {
  color: #2f6fed;
  border-bottom-color: #2f6fed;
}

.tab-count {
  padding: 0 7px;
  border-radius: 999px;
  background: #eef2f7;
  font-size: 11.5px;
  font-weight: 500;
  color: #6b7280;
  font-variant-numeric: tabular-nums;
}

.tab.active .tab-count {
  background: #e5edfd;
  color: #2f6fed;
}

/* 有待办工单时才上色：0 的时候不该有颜色去抢注意力 */
.tab-count.warn {
  background: #fff8ec;
  color: #a9701a;
}

.tab.active .tab-count.warn {
  background: #fdf0d9;
  color: #a9701a;
}

.doc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.doc-table th {
  padding: 8px 10px;
  text-align: left;
  font-weight: 500;
  color: #8a94a0;
  border-bottom: 1px solid #eef0f2;
  white-space: nowrap;
}

.doc-table td {
  padding: 10px;
  border-bottom: 1px solid #f5f7f9;
  color: #4c5b6b;
  vertical-align: top;
}

.doc-table tr:hover td {
  background: #fafbfc;
}

.doc-title {
  font-weight: 600;
  color: #1f2328;
  margin-bottom: 2px;
}

.doc-id {
  font-size: 11.5px;
  color: #a4adb8;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.col-cat {
  width: 130px;
}

.col-num {
  width: 68px;
  text-align: right;
}

.col-owner {
  width: 90px;
}

.col-vis {
  width: 80px;
  white-space: nowrap;
}

.col-op {
  width: 90px;
  text-align: right;
}

.col-op-wide {
  white-space: nowrap;
}

.col-op-wide .btn + .btn {
  margin-left: 6px;
}

.col-time {
  width: 150px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.chip {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 999px;
  background: #eef2f7;
  color: #4c5b6b;
  font-size: 11.5px;
}

.muted {
  color: #a4adb8;
}

.vis-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 5px;
  background: #3fae6d;
}

.vis-dot.private {
  background: #e0a03a;
}

.empty-line {
  margin: 6px 0;
  font-size: 12.5px;
  color: #a4adb8;
}

/* ---------- 删除确认 ---------- */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(24, 32, 42, 0.32);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}

.modal {
  width: 400px;
  padding: 20px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 12px 32px rgba(24, 32, 42, 0.18);
}

.modal h3 {
  margin: 0 0 10px;
  font-size: 15px;
  color: #1f2328;
}

.modal-text {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.7;
  color: #4c5b6b;
}

.modal-text.small {
  font-size: 12px;
  color: #8a94a0;
  word-break: break-all;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
</style>

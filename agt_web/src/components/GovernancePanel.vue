<template>
  <div class="gov">
    <!-- 非管理员：整块替换成说明，不显示半截页面 -->
    <div v-if="permDenied" class="empty-line">
      治理视图只对管理员开放。<br />
      它展示的是全库状态（谁上传的、在哪儿、冗余多少）与治理开关，属于运维视图而非用户功能。
    </div>

    <template v-else>
      <!-- ============ 概览看板 ============ -->
      <div class="gov-stats">
        <div v-for="c in statCards" :key="c.key" class="g-card" :class="c.tone">
          <span class="g-label">{{ c.label }}</span>
          <strong class="g-value">{{ c.loading ? '—' : c.value }}</strong>
          <span class="g-hint">{{ c.hint }}</span>
        </div>
      </div>

      <div v-if="err.overview" class="msg error">{{ err.overview }}</div>

      <!-- ============ 开关 + 常驻任务 ============ -->
      <div class="gov-meta">
        <div class="meta-block">
          <div class="meta-head">
            治理开关
            <span class="meta-note">只读：改 agt_python/.env 后重启生效</span>
          </div>
          <div class="chips">
            <span class="chip" :class="{ off: !cfg.l1Enabled }">L1 整篇去重 {{ cfg.l1Enabled ? '开' : '关' }}</span>
            <span class="chip" :class="{ off: !cfg.l2Enabled }">L2 切片去重 {{ cfg.l2Enabled ? '开' : '关' }}</span>
            <span class="chip">L3 相似阈值 {{ cfg.l3Threshold }}</span>
            <span class="chip">回收站 TTL {{ cfg.ttlDays }} 天</span>
            <span class="chip" :class="{ off: !cfg.inspectionEnabled }">巡检 {{ cfg.inspectionEnabled ? '开' : '关' }}</span>
            <span class="chip">陈旧阈值 {{ cfg.staleDays }} 天</span>
            <span class="chip" :class="{ warn: true }">劣质阈值 {{ cfg.lowQualityChars }} 字</span>
            <span class="chip" :class="{ off: !cfg.versionChainEnabled }">版本链 {{ cfg.versionChainEnabled ? '开' : '关' }}</span>
            <span class="chip">版本保留 {{ cfg.versionKeep }}</span>
          </div>
        </div>

        <div class="meta-block">
          <div class="meta-head">
            常驻任务
            <span class="meta-note">异常被就地吞掉，只有这里能看见</span>
          </div>
          <ul class="task-list">
            <li v-for="t in tasks" :key="t.name" :class="{ bad: !!t.lastError }">
              <span class="t-name">{{ t.name }}</span>
              <span class="t-meta">
                每 {{ t.intervalSec }}s · 已跑 {{ t.runs }} 轮
                <template v-if="t.lastStartedAt"> · 上轮 {{ t.lastDurationMs }}ms</template>
              </span>
              <span v-if="t.lastError" class="t-err" :title="t.lastError">上轮报错</span>
            </li>
            <li v-if="!tasks.length" class="t-empty">暂无任务信息</li>
          </ul>
        </div>
      </div>

      <!-- ============ 手动触发 ============ -->
      <div class="gov-ops">
        <div class="op-card">
          <div class="op-head">巡检扫描</div>
          <p class="op-desc">近重复 / 陈旧 / 劣质切片 → 开单。幂等，已忽略的不重复开</p>
          <div class="op-actions">
            <button class="btn tiny" type="button" :disabled="busy.scan" @click="runScan(true)">预览</button>
            <button class="btn tiny primary" type="button" :disabled="busy.scan" @click="runScan(false)">
              {{ busy.scan ? '巡检中…' : '执行一轮' }}
            </button>
          </div>
        </div>

        <div class="op-card">
          <div class="op-head">回收站 TTL 清理</div>
          <p class="op-desc">超期回收站文档走彻底删除。不碰停用区，孤儿备份只告警</p>
          <div class="op-actions">
            <button class="btn tiny" type="button" :disabled="busy.ttl" @click="runTtl(true)">预览</button>
            <button class="btn tiny danger" type="button" :disabled="busy.ttl" @click="runTtl(false)">
              {{ busy.ttl ? '清理中…' : '执行一轮' }}
            </button>
          </div>
        </div>

        <div class="op-card">
          <div class="op-head">向量对账</div>
          <p class="op-desc">清理残留向量；缺失只告警，未登记语料绝不自动删</p>
          <div class="op-actions">
            <button class="btn tiny" type="button" :disabled="busy.reconcile" @click="runReconcile(true)">预览</button>
            <button class="btn tiny" type="button" :disabled="busy.reconcile" @click="runReconcile(false)">
              {{ busy.reconcile ? '对账中…' : '执行一轮' }}
            </button>
          </div>
        </div>
      </div>

      <!-- ============ 操作报告 ============ -->
      <div v-if="report" class="gov-report">
        <div class="report-head">
          <strong>{{ report.title }}</strong>
          <span class="panel-hint">
            {{ report.dryRun ? '预览（未落库/未删除）' : '已执行' }} · 耗时 {{ report.durationMs }} ms
          </span>
          <button class="btn tiny" type="button" @click="report = null">收起</button>
        </div>
        <div class="report-grid">
          <div v-for="row in report.rows" :key="row.k" class="report-row">
            <span class="r-k">{{ row.k }}</span>
            <span class="r-v" :class="row.tone">{{ row.v }}</span>
          </div>
        </div>
        <ul v-if="report.items.length" class="report-items">
          <li v-for="(it, i) in report.items" :key="i" :class="it.tone">{{ it.text }}</li>
        </ul>
      </div>

      <div v-if="msg" class="msg" :class="msg.type">{{ msg.text }}</div>

      <!-- ============ 子视图 ============ -->
      <div class="gov-subtabs" role="tablist">
        <button
          v-for="s in SUBTABS"
          :key="s.key"
          class="subtab"
          :class="{ active: subTab === s.key }"
          type="button"
          role="tab"
          @click="switchSub(s.key)"
        >
          {{ s.label }}
          <span v-if="s.key === 'tickets' && openCount" class="tab-count">{{ openCount }}</span>
        </button>
        <span class="subtab-spacer"></span>
        <button class="btn tiny" type="button" :disabled="subBusy" @click="reloadSub">
          {{ subBusy ? '刷新中…' : '刷新' }}
        </button>
      </div>

      <!-- ---------- 工单待办 ---------- -->
      <template v-if="subTab === 'tickets'">
        <div class="filters">
          <select v-model="ticketFilter.status" @change="reloadTickets">
            <option value="">全部状态</option>
            <option value="OPEN">待处理</option>
            <option value="DISMISSED">已忽略</option>
            <option value="DONE">已完成</option>
          </select>
          <select v-model="ticketFilter.type" @change="reloadTickets">
            <option value="">全部类型</option>
            <option v-for="(label, key) in TICKET_TYPE_TEXT" :key="key" :value="key">{{ label }}</option>
          </select>
          <span class="panel-hint">
            待处理 {{ openCount }} 条 · 巡检只负责检出与开单，改不改由人拍板
          </span>
        </div>

        <div v-if="err.tickets" class="msg error">{{ err.tickets }}</div>

        <table v-if="tickets.length" class="doc-table">
          <thead>
            <tr>
              <th class="col-type">类型</th>
              <th>文档</th>
              <th>判定依据</th>
              <th class="col-st">状态</th>
              <th class="col-time">开单时间</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tickets" :key="t.id">
              <td class="col-type">
                <span class="chip type" :class="'type-' + String(t.ticket_type).toLowerCase()">
                  {{ TICKET_TYPE_TEXT[t.ticket_type] || t.ticket_type }}
                </span>
              </td>
              <td>
                <div class="doc-title">{{ t.doc_name || t.doc_id }}</div>
                <div class="doc-id" :title="t.doc_id">{{ t.doc_id }}</div>
                <div v-if="t.matched_doc_id" class="doc-id" :title="t.matched_doc_id">
                  与 {{ t.matched_doc_id }}
                </div>
              </td>
              <td class="note-cell">{{ t.note || '—' }}</td>
              <td class="col-st">
                <span class="chip" :class="'st-' + String(t.status).toLowerCase()">
                  {{ TICKET_STATUS_TEXT[t.status] || t.status }}
                </span>
                <div v-if="t.handled_at" class="doc-id">{{ t.handled_at }}</div>
              </td>
              <td class="col-time">{{ t.created_at || '—' }}</td>
              <td class="col-op col-op-wide">
                <template v-if="t.status === 'OPEN'">
                  <button class="btn tiny primary" type="button" @click="ticketTarget = t">执行</button>
                  <button class="btn tiny" type="button" @click="askDismiss(t)">忽略</button>
                </template>
                <span v-else class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-else-if="!busy.tickets" class="empty-line">
          没有工单。巡检每小时一轮，检出近重复 / 陈旧 / 劣质切片时会在这里生成待办。
        </p>

        <PaginationBar
          v-if="ticketPage.totalPages > 1"
          :page="ticketPage.page"
          :total-pages="ticketPage.totalPages"
          :page-size="ticketPage.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="busy.tickets"
          @change-page="goTicketPage"
          @change-page-size="goTicketPageSize"
        />
      </template>

      <!-- ---------- 停用区 ---------- -->
      <template v-else-if="subTab === 'disabled'">
        <p class="panel-hint block">
          停用 = 下架：向量已物理移除、立即不可检索，但备份（含向量）完整保留，随时可一键启用。
          停用区<strong>没有 TTL</strong>——那份备份就是"启用"的唯一数据来源。
        </p>

        <div v-if="err.disabled" class="msg error">{{ err.disabled }}</div>

        <table v-if="disabled.length" class="doc-table">
          <thead>
            <tr>
              <th>文档</th>
              <th class="col-cat">分类</th>
              <th class="col-num">切片</th>
              <th class="col-owner">归属</th>
              <th class="col-owner">停用人</th>
              <th class="col-time">停用时间</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in disabled" :key="item.docId">
              <td>
                <div class="doc-title">{{ item.docTitle || item.docId }}</div>
                <div class="doc-id" :title="item.docId">{{ item.docId }}</div>
              </td>
              <td class="col-cat">
                <span v-if="item.category" class="chip">{{ item.category }}</span>
                <span v-else class="muted">未分类</span>
              </td>
              <td class="col-num">{{ item.chunkCount || 0 }}</td>
              <td class="col-owner">{{ item.owner || '—' }}</td>
              <td class="col-owner">{{ item.deletedBy || '—' }}</td>
              <td class="col-time">{{ item.deletedAt || '—' }}</td>
              <td class="col-op col-op-wide">
                <button
                  class="btn tiny"
                  type="button"
                  :disabled="busy.enable === item.docId"
                  @click="doEnable(item)"
                >
                  {{ busy.enable === item.docId ? '启用中…' : '启用' }}
                </button>
                <button class="btn tiny" type="button" @click="openVersions(item.docId)">版本链</button>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-else-if="!busy.disabled" class="empty-line">停用区是空的。</p>

        <PaginationBar
          v-if="disabledPage.totalPages > 1"
          :page="disabledPage.page"
          :total-pages="disabledPage.totalPages"
          :page-size="disabledPage.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="busy.disabled"
          @change-page="goDisabledPage"
          @change-page-size="goDisabledPageSize"
        />
      </template>

      <!-- ---------- 治理登记 ---------- -->
      <template v-else-if="subTab === 'registry'">
        <div class="filters">
          <select v-model="registryFilter.state" @change="reloadRegistry">
            <option value="">全部状态</option>
            <option value="ACTIVE">在库</option>
            <option value="DISABLED">停用</option>
            <option value="RECYCLED">回收站</option>
          </select>
          <span class="panel-hint">
            治理账本视角，<strong>回收站中的文档也在内</strong>；与「文档列表」tab 的差别就在这
          </span>
        </div>

        <div v-if="err.registry" class="msg error">{{ err.registry }}</div>

        <table v-if="registry.length" class="doc-table">
          <thead>
            <tr>
              <th>文档</th>
              <th class="col-st">状态</th>
              <th class="col-num">版本</th>
              <th class="col-num">切片</th>
              <th class="col-owner">归属</th>
              <th class="col-time">更新时间</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in registry" :key="row.doc_id">
              <td>
                <div class="doc-title">{{ row.doc_name || row.doc_id }}</div>
                <div class="doc-id" :title="row.doc_id">{{ row.doc_id }}</div>
              </td>
              <td class="col-st">
                <span class="chip" :class="'st-' + String(row.state).toLowerCase()">
                  {{ GOV_STATE_TEXT[row.state] || row.state }}
                </span>
              </td>
              <td class="col-num">v{{ row.version_no || 1 }}</td>
              <td class="col-num">{{ row.chunk_count || 0 }}</td>
              <td class="col-owner">{{ row.owner || '—' }}</td>
              <td class="col-time">{{ row.updated_at || '—' }}</td>
              <td class="col-op col-op-wide">
                <button class="btn tiny" type="button" @click="openVersions(row.doc_id)">版本链</button>
                <button
                  v-if="row.state === 'ACTIVE'"
                  class="btn tiny"
                  type="button"
                  :disabled="busy.disable === row.doc_id"
                  @click="askDisable(row)"
                >
                  {{ busy.disable === row.doc_id ? '停用中…' : '停用' }}
                </button>
                <button
                  v-else-if="row.state === 'DISABLED'"
                  class="btn tiny"
                  type="button"
                  :disabled="busy.enable === row.doc_id"
                  @click="doEnable(row)"
                >
                  {{ busy.enable === row.doc_id ? '启用中…' : '启用' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-else-if="!busy.registry" class="empty-line">治理账本里还没有记录。</p>

        <PaginationBar
          v-if="registryPage.totalPages > 1"
          :page="registryPage.page"
          :total-pages="registryPage.totalPages"
          :page-size="registryPage.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="busy.registry"
          @change-page="goRegistryPage"
          @change-page-size="goRegistryPageSize"
        />
      </template>

      <!-- ---------- 审计流水 ---------- -->
      <template v-else>
        <div class="filters">
          <select v-model="auditFilter.action" @change="reloadAudit">
            <option value="">全部动作</option>
            <option v-for="(label, key) in AUDIT_ACTION_TEXT" :key="key" :value="key">{{ label }}</option>
          </select>
          <select v-model="auditFilter.target" @change="reloadAudit">
            <option v-for="t in AUDIT_TARGET_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
          <input
            v-model.trim="auditFilter.operator"
            type="text"
            placeholder="按操作人过滤"
            @keyup.enter="reloadAudit"
          />
          <input
            v-model.trim="auditFilter.docId"
            type="text"
            placeholder="按文档标识过滤"
            @keyup.enter="reloadAudit"
          />
          <input
            v-model.trim="auditFilter.from"
            type="text"
            placeholder="起 YYYY-MM-DD HH:MM:SS"
            class="dt"
            @keyup.enter="reloadAudit"
          />
          <input
            v-model.trim="auditFilter.to"
            type="text"
            placeholder="止 YYYY-MM-DD HH:MM:SS"
            class="dt"
            @keyup.enter="reloadAudit"
          />
          <button class="btn tiny" type="button" @click="reloadAudit">查询</button>
          <button class="btn tiny" type="button" :disabled="exporting" @click="exportAudit">
            {{ exporting ? '导出中…' : '导出 CSV' }}
          </button>
          <button class="btn tiny" type="button" @click="resetAuditFilter">重置</button>
          <span class="panel-hint">审计行永不删除——彻底删除文档后，它是唯一留痕</span>
        </div>

        <p v-if="auditExportMsg" class="panel-hint export-msg">{{ auditExportMsg }}</p>

        <div v-if="err.audit" class="msg error">{{ err.audit }}</div>

        <table v-if="audit.length" class="doc-table">
          <thead>
            <tr>
              <th class="col-time">时间</th>
              <th class="col-owner">操作人</th>
              <th class="col-type">动作</th>
              <th>对象</th>
              <th class="col-st">结果</th>
              <th class="col-op">详情</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in audit" :key="row.id">
              <tr>
                <td class="col-time">{{ row.ts }}</td>
                <td class="col-owner">{{ row.operator || '—' }}</td>
                <td class="col-type">
                  <span class="chip">{{ AUDIT_ACTION_TEXT[row.action] || row.action }}</span>
                </td>
                <td>
                  <!-- 对象列优先显示 target（统一指向），文档名作为补充说明 -->
                  <div class="doc-title">{{ row.target || row.doc_name || row.doc_id || '—' }}</div>
                  <div v-if="row.doc_name && row.target" class="doc-id">{{ row.doc_name }}</div>
                  <div v-else-if="row.doc_id && row.target !== 'doc:' + row.doc_id" class="doc-id" :title="row.doc_id">
                    {{ row.doc_id }}
                  </div>
                </td>
                <td class="col-st">
                  <span class="chip" :class="row.result === 'SUCCESS' ? 'st-done' : 'st-open'">
                    {{ AUDIT_RESULT_TEXT[row.result] || row.result || '—' }}
                  </span>
                </td>
                <td class="col-op">
                  <button class="btn tiny" type="button" @click="toggleAudit(row.id)">
                    {{ expandedAudit === row.id ? '收起' : '展开' }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedAudit === row.id" class="detail-row">
                <td colspan="6">
                  <div class="detail-grid">
                    <div v-if="row.ip" class="detail-item">
                      <span class="d-k">来源 IP</span><span class="d-v">{{ row.ip }}</span>
                    </div>
                    <div v-if="row.trace_id" class="detail-item">
                      <span class="d-k">traceId</span><span class="d-v">{{ row.trace_id }}</span>
                    </div>
                    <div v-for="d in detailRows(row)" :key="d.k" class="detail-item">
                      <span class="d-k">{{ d.k }}</span>
                      <span class="d-v">{{ d.v }}</span>
                    </div>
                  </div>
                  <div v-if="!detailRows(row).length && !row.ip && !row.trace_id" class="muted">无附加信息</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>

        <p v-else-if="!busy.audit" class="empty-line">没有符合条件的审计记录。</p>

        <PaginationBar
          v-if="auditPage.totalPages > 1"
          :page="auditPage.page"
          :total-pages="auditPage.totalPages"
          :page-size="auditPage.pageSize"
          :page-size-options="pageSizeOptions"
          :busy="busy.audit"
          @change-page="goAuditPage"
          @change-page-size="goAuditPageSize"
        />
      </template>
    </template>

    <!-- ============ 执行工单确认 ============ -->
    <div v-if="ticketTarget" class="modal-mask" @click.self="ticketTarget = null">
      <div class="modal">
        <h3>执行工单</h3>
        <p class="modal-text">
          将对文档 <strong>{{ ticketTarget.doc_name || ticketTarget.doc_id }}</strong>
          执行 <strong>{{ TICKET_TYPE_TEXT[ticketTarget.ticket_type] || ticketTarget.ticket_type }}</strong>
          的处理动作：<strong>{{ ticketActionDesc(ticketTarget.ticket_type) }}</strong>。
        </p>
        <p class="modal-text small">判定依据：{{ ticketTarget.note || '—' }}</p>
        <p class="modal-text small">
          执行的是<strong>可回退动作</strong>——巡检判定带主观性，误判不会造成不可逆后果，之后可一键还原。
        </p>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="acting" @click="ticketTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="acting" @click="doConfirmTicket">
            {{ acting ? '执行中…' : '确认执行' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 忽略工单 ============ -->
    <div v-if="dismissTarget" class="modal-mask" @click.self="dismissTarget = null">
      <div class="modal">
        <h3>忽略工单</h3>
        <p class="modal-text">
          不会对 <strong>{{ dismissTarget.doc_name || dismissTarget.doc_id }}</strong> 做任何改动。
        </p>
        <p class="modal-text small">
          忽略是<strong>有记忆</strong>的：只要该文档此后没有变化，下一轮巡检不会再为同一问题开单。
        </p>
        <label class="field">
          <span class="field-label">忽略原因（可选）</span>
          <input v-model.trim="dismissNote" type="text" maxlength="200" placeholder="如：保留不同批次，内容差异有意义" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="acting" @click="dismissTarget = null">取消</button>
          <button class="btn" type="button" :disabled="acting" @click="doDismissTicket">
            {{ acting ? '处理中…' : '确认忽略' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 停用确认 ============ -->
    <div v-if="disableTarget" class="modal-mask" @click.self="disableTarget = null">
      <div class="modal">
        <h3>停用文档</h3>
        <p class="modal-text">
          将停用 <strong>{{ disableTarget.doc_name || disableTarget.doc_id }}</strong>：
          其 <strong>{{ disableTarget.chunk_count || 0 }}</strong> 个切片会立即从智能体检索范围中移除。
        </p>
        <p class="modal-text small">
          停用不是删除：备份（含向量）会完整保留，随时可在停用区一键启用，无需重新向量化。
        </p>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="acting" @click="disableTarget = null">取消</button>
          <button class="btn danger" type="button" :disabled="acting" @click="doDisable">
            {{ acting ? '停用中…' : '确认停用' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ============ 版本链 ============ -->
    <div v-if="versions" class="modal-mask" @click.self="closeVersions">
      <div class="modal wide">
        <h3>版本链</h3>
        <p class="modal-text">
          {{ versions.docTitle || versions.docId }}
          <span class="muted">
            · 当前 v{{ versions.currentVersion || '—' }} · 共 {{ versions.total }} 个版本
          </span>
        </p>
        <div v-if="err.versions" class="msg error">{{ err.versions }}</div>
        <table v-if="versions.items && versions.items.length" class="doc-table">
          <thead>
            <tr>
              <th class="col-num">版本</th>
              <th class="col-st">角色</th>
              <th class="col-num">切片</th>
              <th class="col-num">字数</th>
              <th class="col-time">快照时间</th>
              <th class="col-op">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="v in versions.items" :key="v.versionNo">
              <td class="col-num">v{{ v.versionNo }}</td>
              <td class="col-st">
                <span class="chip" :class="v.isCurrent ? 'st-done' : ''">
                  {{ v.isCurrent ? '当前版本' : '历史快照' }}
                </span>
                <div v-if="v.live" class="doc-id">在库可检索</div>
              </td>
              <td class="col-num">{{ v.chunkCount }}</td>
              <td class="col-num">{{ v.charCount }}</td>
              <td class="col-time">{{ v.snapshotAt || '—' }}</td>
              <td class="col-op">
                <button
                  v-if="!v.isCurrent"
                  class="btn tiny"
                  type="button"
                  @click="askRollback(v)"
                >
                  {{ v.hasVectors ? '瞬时回滚' : '回滚' }}
                </button>
                <span v-else class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-line">该文档没有历史版本。</p>
        <p class="modal-text small">
          带向量的快照回滚是<strong>零 embedding 回灌</strong>；无向量的旧快照会回落成重新切片 + 向量化，
          耗时与入库同级。
        </p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeVersions">关闭</button>
        </div>
      </div>
    </div>

    <!-- ============ 回滚确认门 ============ -->
    <div v-if="rollbackTarget" class="modal-mask" @click.self="rollbackTarget = null">
      <div class="modal">
        <h3>回滚到 v{{ rollbackTarget.versionNo }}</h3>
        <p class="modal-text">
          将把 <strong>{{ versions && versions.docTitle }}</strong> 的内容整体换回 v{{
            rollbackTarget.versionNo
          }}，当前版本会自动留档为历史快照。
        </p>
        <p class="modal-text small">
          回滚影响的是<strong>智能体正在使用的语料</strong>，操作人须自己复述文档名以确认。
        </p>
        <label class="field">
          <span class="field-label">请复述文档名：{{ rollbackDocName }}</span>
          <input
            v-model.trim="rollbackInput"
            type="text"
            :disabled="acting"
            placeholder="在此输入上面的文档名"
          />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" :disabled="acting" @click="rollbackTarget = null">取消</button>
          <button
            class="btn danger"
            type="button"
            :disabled="acting || !rollbackMatched"
            @click="doRollback"
          >
            {{ acting ? '回滚中…' : '确认回滚' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import {
  fetchGovernanceOverview,
  fetchGovernanceTasks,
  fetchGovernanceAudit,
  fetchGovernanceAuditExport,
  fetchGovernanceDocs,
  fetchDisabledDocs,
  disableDocument,
  enableDocument,
  // 与下面的方法同名，起别名避免方法体里到底调用谁的歧义
  runReconcile as requestReconcile,
  fetchVersions,
  rollbackDocument,
  fetchTickets,
  confirmTicket,
  dismissTicket,
  runInspectionScan,
  purgeExpired,
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  TICKET_TYPE_TEXT,
  TICKET_STATUS_TEXT,
  AUDIT_ACTION_TEXT,
  AUDIT_RESULT_TEXT,
  AUDIT_TARGET_OPTIONS,
  GOV_STATE_TEXT,
} from '../api/kb'
// 通用下载工具（CSV/JSONL 通用）：与看板报表页共用同一个实现，
// 不在这里再写一份 Blob + a[download]（重复实现迟早分叉）
import { downloadTextFile } from '../api/metrics'
import { nameRepeated } from '../utils/confirmGate'
import PaginationBar from './PaginationBar.vue'

/** 子视图定义（顺序即展示顺序） */
const SUBTABS = [
  { key: 'tickets', label: '工单待办' },
  { key: 'disabled', label: '停用区' },
  { key: 'registry', label: '治理登记' },
  { key: 'audit', label: '审计流水' },
]

export default {
  name: 'GovernancePanel',

  components: { PaginationBar },

  props: {
    /** 父级已拉到的概览（用于 tab 角标）；本组件会自行刷新并通过事件回传 */
    overview: {
      type: Object,
      default: () => ({}),
    },
  },

  emits: ['overview', 'changed'],

  data() {
    return {
      SUBTABS,
      TICKET_TYPE_TEXT,
      TICKET_STATUS_TEXT,
      AUDIT_ACTION_TEXT,
      AUDIT_RESULT_TEXT,
      AUDIT_TARGET_OPTIONS,
      GOV_STATE_TEXT,

      subTab: 'tickets',
      overviewData: {},
      tasks: [],
      permDenied: false,

      busy: {
        overview: false,
        tickets: false,
        disabled: false,
        registry: false,
        audit: false,
        scan: false,
        ttl: false,
        reconcile: false,
        enable: '',
        disable: '',
      },
      // 弹窗里的动作忙碌态（与列表刷新分开：弹窗按钮的文案不同）
      acting: false,

      tickets: [],
      ticketPage: { page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, totalPages: 1 },
      ticketFilter: { status: 'OPEN', type: '' },

      disabled: [],
      disabledPage: { page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, totalPages: 1 },

      registry: [],
      registryPage: { page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, totalPages: 1 },
      registryFilter: { state: '' },

      audit: [],
      auditPage: { page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, totalPages: 1 },
      auditFilter: { action: '', docId: '', operator: '', target: '', from: '', to: '' },
      expandedAudit: null,
      exporting: false,
      auditExportMsg: '',

      err: { overview: '', tickets: '', disabled: '', registry: '', audit: '', versions: '' },
      msg: null,
      report: null,

      ticketTarget: null,
      dismissTarget: null,
      dismissNote: '',
      disableTarget: null,

      versions: null,
      rollbackTarget: null,
      rollbackInput: '',

      pageSizeOptions: PAGE_SIZE_OPTIONS,

      loadedOnce: {},
    }
  },

  computed: {
    cfg() {
      return this.overviewData.config || {}
    },
    openCount() {
      return Number(this.overviewData.openTickets || 0)
    },
    statCards() {
      const byState = this.overviewData.docByState || {}
      const chunks = this.overviewData.chunks || {}
      const dupRate = Number(chunks.duplicateRate || 0)
      const loading = this.busy.overview && !this.overviewData.docTotal
      return [
        {
          key: 'active',
          label: '在库文档',
          value: byState.ACTIVE || 0,
          hint: '正在参与检索',
          tone: '',
          loading,
        },
        {
          key: 'disabled',
          label: '停用',
          value: byState.DISABLED || 0,
          hint: '备份保留，可随时启用',
          tone: '',
          loading,
        },
        {
          key: 'recycled',
          label: '回收站',
          value: byState.RECYCLED || 0,
          hint: `超 ${this.cfg.ttlDays || '—'} 天自动清理`,
          tone: '',
          loading,
        },
        {
          key: 'chunks',
          label: '切片行',
          value: chunks.chunkRows || 0,
          hint: `唯一指纹 ${chunks.uniqueHashes || 0}`,
          tone: '',
          loading,
        },
        {
          key: 'dup',
          label: '指纹冗余率',
          value: this.pct(dupRate),
          hint: `重复 ${chunks.duplicateRows || 0} 行 · 跨文档 ${chunks.crossDocHashes || 0}`,
          // 冗余率没有"正确值"，这里只在自己偏高时提示，不做红绿判断
          tone: dupRate >= 0.2 ? 'warn' : '',
          loading,
        },
        {
          key: 'tickets',
          label: '待办工单',
          value: this.openCount,
          hint: this.openCount ? '需人工拍板' : '暂无待办',
          tone: this.openCount ? 'warn' : '',
          loading,
        },
      ]
    },
    subBusy() {
      return !!this.busy[this.subTab]
    },
    rollbackDocName() {
      return (this.versions && (this.versions.docTitle || this.versions.docId)) || ''
    },
    /** 后端允许复述"标题 / docId / 文件名主干"三者之一（`_confirm_name_matches`），前端同口径 */
    rollbackCandidates() {
      const v = this.versions || {}
      const stem = String(v.fileName || '').replace(/\.[^.]+$/, '')
      return [v.docTitle, v.docId, stem].filter(Boolean)
    },
    rollbackMatched() {
      return nameRepeated(this.rollbackInput, this.rollbackCandidates)
    },
  },

  mounted() {
    this.init()
  },

  methods: {
    async init() {
      await this.loadOverview()
      if (this.permDenied) return
      this.loadTasks()
      this.loadTickets()
      this.loadedOnce.tickets = true
    },

    // ------------------------------------------------------------ 概览

    async loadOverview() {
      this.busy.overview = true
      this.err.overview = ''
      try {
        const data = (await fetchGovernanceOverview()) || {}
        this.overviewData = data
        this.permDenied = false
        this.$emit('overview', data)
      } catch (e) {
        const text = e.message || '治理概览加载失败'
        // 权限拦截不是"加载失败"：整块替换成说明，避免用户对着半截页面找原因
        if (text.indexOf('管理员') >= 0 || text.indexOf('登录') >= 0) {
          this.permDenied = true
        } else {
          this.err.overview = text
        }
      } finally {
        this.busy.overview = false
      }
    },

    async loadTasks() {
      try {
        const data = await fetchGovernanceTasks()
        this.tasks = (data && data.tasks) || []
      } catch (e) {
        // 任务状态是附加信息，失败不打断主视图
        console.warn('常驻任务状态加载失败', e)
      }
    },

    // ------------------------------------------------------------ 子视图切换

    switchSub(tab) {
      if (this.subTab === tab) return
      this.subTab = tab
      this.msg = null
      if (!this.loadedOnce[tab]) {
        this.loadedOnce[tab] = true
        this.reloadSub()
      }
    },

    reloadSub() {
      const map = {
        tickets: this.loadTickets,
        disabled: this.loadDisabled,
        registry: this.loadRegistry,
        audit: this.loadAudit,
      }
      const fn = map[this.subTab]
      if (fn) fn()
    },

    /**
     * 筛选变化 → 回到第 1 页再查。
     *
     * ⚠️ 这三个方法曾经**只存在于模板、脚本里没有实现**（2026-09-24 E-8 排查时发现）：
     * 点"查询"或切换筛选下拉会抛 `reloadTickets is not a function`，而界面只是
     * "点了没反应"，所以长期没人报。之所以一直没暴露：这几处默认值都是"全部"，
     * 不主动切换就不会触发；而工单/审计页面的"刷新"走的是 `reloadSub`，那条路径是好的。
     */
    reloadTickets() {
      this.ticketPage.page = 1
      this.loadTickets()
    },

    reloadRegistry() {
      this.registryPage.page = 1
      this.loadRegistry()
    },

    reloadAudit() {
      this.auditPage.page = 1
      this.loadAudit()
    },

    /** 给父级的「刷新」按钮用：概览 + 任务 + 当前子视图一起刷 */
    async refresh() {
      this.msg = null
      await Promise.all([this.loadOverview(), this.loadTasks()])
      if (this.permDenied) return
      this.loadedOnce[this.subTab] = true
      this.reloadSub()
    },

    // ------------------------------------------------------------ 工单

    async loadTickets() {
      this.busy.tickets = true
      this.err.tickets = ''
      try {
        const data = await fetchTickets({
          status: this.ticketFilter.status,
          type: this.ticketFilter.type,
          page: this.ticketPage.page,
          pageSize: this.ticketPage.pageSize,
        })
        this.tickets = (data && data.items) || []
        this.applyPage(this.ticketPage, data)
        // openCount 由列表自带：不用为"待办几个"再发一次概览请求
        if (data && typeof data.openCount !== 'undefined') {
          this.overviewData = { ...this.overviewData, openTickets: data.openCount }
        }
      } catch (e) {
        this.err.tickets = e.message || '工单列表加载失败'
        this.tickets = []
      } finally {
        this.busy.tickets = false
      }
    },

    ticketActionDesc(type) {
      if (type === 'DUPLICATE') return '移入回收站（软删，之后可在回收站恢复）'
      if (type === 'STALE' || type === 'LOW_QUALITY') return '停用（之后可在停用区启用）'
      return '未知动作'
    },

    async doConfirmTicket() {
      if (!this.ticketTarget) return
      this.acting = true
      try {
        const res = await confirmTicket(this.ticketTarget.id)
        this.ticketTarget = null
        if (res && res.executed) {
          const verb = res.action === 'SOFT_DELETE' ? '已移入回收站' : '已停用'
          this.msg = {
            type: 'success',
            text: `工单 #${res.ticketId} 已执行：『${verb}』，移除 ${res.removedChunks} 个切片。可一键还原`,
          }
        } else {
          // executed=false 不是失败：文档已被别的途径处理，工单本身过期了
          this.msg = { type: 'success', text: `工单 #${res && res.ticketId} 已关闭：${(res && res.reason) || '文档已不在库'}` }
        }
        await this.afterMutation()
      } catch (e) {
        this.msg = { type: 'error', text: e.message || '工单执行失败' }
        this.ticketTarget = null
      } finally {
        this.acting = false
      }
    },

    askDismiss(ticket) {
      this.dismissTarget = ticket
      this.dismissNote = ''
    },

    async doDismissTicket() {
      if (!this.dismissTarget) return
      this.acting = true
      try {
        await dismissTicket(this.dismissTarget.id, this.dismissNote)
        this.msg = {
          type: 'success',
          text: `工单 #${this.dismissTarget.id} 已忽略。该文档此后未变化时，下一轮巡检不会再开单`,
        }
        this.dismissTarget = null
        await this.afterMutation()
      } catch (e) {
        this.msg = { type: 'error', text: e.message || '工单忽略失败' }
        this.dismissTarget = null
      } finally {
        this.acting = false
      }
    },

    // ------------------------------------------------------------ 停用区

    async loadDisabled() {
      this.busy.disabled = true
      this.err.disabled = ''
      try {
        const data = await fetchDisabledDocs({
          page: this.disabledPage.page,
          pageSize: this.disabledPage.pageSize,
        })
        this.disabled = (data && data.items) || []
        this.applyPage(this.disabledPage, data)
      } catch (e) {
        this.err.disabled = e.message || '停用区加载失败'
        this.disabled = []
      } finally {
        this.busy.disabled = false
      }
    },

    // ------------------------------------------------------------ 治理登记

    async loadRegistry() {
      this.busy.registry = true
      this.err.registry = ''
      try {
        const data = await fetchGovernanceDocs({
          state: this.registryFilter.state,
          page: this.registryPage.page,
          pageSize: this.registryPage.pageSize,
        })
        this.registry = (data && data.items) || []
        this.applyPage(this.registryPage, data)
      } catch (e) {
        this.err.registry = e.message || '治理登记列表加载失败'
        this.registry = []
      } finally {
        this.busy.registry = false
      }
    },

    askDisable(row) {
      this.disableTarget = row
    },

    async doDisable() {
      if (!this.disableTarget) return
      const row = this.disableTarget
      this.busy.disable = row.doc_id
      this.acting = true
      try {
        const res = await disableDocument(row.doc_id)
        this.msg = {
          type: 'success',
          text: `已停用「${(res && res.docTitle) || row.doc_name || row.doc_id}」，移除 ${
            (res && res.removedChunks) || 0
          } 个切片（备份含向量，可在停用区启用回来）`,
        }
        this.disableTarget = null
        await this.afterMutation()
      } catch (e) {
        this.msg = { type: 'error', text: e.message || '停用失败' }
        this.disableTarget = null
      } finally {
        this.busy.disable = ''
        this.acting = false
      }
    },

    async doEnable(row) {
      const docId = row.docId || row.doc_id
      if (!docId) return
      this.busy.enable = docId
      try {
        const res = await enableDocument(docId)
        this.msg = {
          type: 'success',
          text: `已启用「${(res && res.docTitle) || docId}」，${
            (res && res.chunkCount) || 0
          } 个切片重新参与检索（从备份回灌，未重新向量化）`,
        }
        await this.afterMutation()
      } catch (e) {
        this.msg = { type: 'error', text: e.message || '启用失败' }
      } finally {
        this.busy.enable = ''
      }
    },

    // ------------------------------------------------------------ 审计

    async loadAudit() {
      this.busy.audit = true
      this.err.audit = ''
      this.expandedAudit = null
      try {
        const data = await fetchGovernanceAudit({
          action: this.auditFilter.action,
          docId: this.auditFilter.docId,
          operator: this.auditFilter.operator,
          target: this.auditFilter.target,
          from: this.auditFilter.from,
          to: this.auditFilter.to,
          page: this.auditPage.page,
          pageSize: this.auditPage.pageSize,
        })
        this.audit = (data && data.items) || []
        this.applyPage(this.auditPage, data)
      } catch (e) {
        this.err.audit = e.message || '审计流水加载失败'
        this.audit = []
      } finally {
        this.busy.audit = false
      }
    },

    /**
     * 导出当前筛选条件下的**全部**审计行（不是当前页）。
     *
     * 为什么不在前端循环翻页拼 CSV：中途一次请求失败就会生成一份
     * "看起来完整、其实缺一段"的证据文件——审计导出恰恰最不能接受这个。
     * 服务端一次取齐并回报 `truncated`，这里只负责如实提示用户。
     */
    async exportAudit() {
      this.exporting = true
      this.auditExportMsg = ''
      try {
        const res = await fetchGovernanceAuditExport({
          action: this.auditFilter.action,
          docId: this.auditFilter.docId,
          operator: this.auditFilter.operator,
          target: this.auditFilter.target,
          from: this.auditFilter.from,
          to: this.auditFilter.to,
        })
        if (!res || !res.content) {
          this.auditExportMsg = (res && res.reason) || '导出失败'
          return
        }
        downloadTextFile(res.filename || 'audit.csv', res.content)
        this.auditExportMsg = res.truncated
          ? `已导出前 ${res.rows} 行（共 ${res.total} 行，超出单次上限被截断——请缩小时间范围）`
          : `已导出 ${res.rows} 行`
      } catch (e) {
        this.auditExportMsg = e.message || '导出失败'
      } finally {
        this.exporting = false
      }
    },

    resetAuditFilter() {
      this.auditFilter = { action: '', docId: '', operator: '', target: '', from: '', to: '' }
      this.auditPage.page = 1
      this.loadAudit()
    },

    toggleAudit(id) {
      this.expandedAudit = this.expandedAudit === id ? null : id
    },

    /** 审计 detail 是 Python 写下的 JSON 字符串；解析失败就原样显示，不丢信息 */
    detailRows(row) {
      const raw = row && row.detail
      if (!raw) return []
      try {
        const obj = JSON.parse(raw)
        if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
          return Object.keys(obj).slice(0, 12).map((k) => ({ k, v: this.readable(obj[k]) }))
        }
      } catch (e) {
        /* 非 JSON：落到下面的原样展示 */
      }
      return [{ k: 'detail', v: String(raw) }]
    },

    readable(val) {
      if (val === null || typeof val === 'undefined') return '—'
      if (typeof val === 'boolean') return val ? '是' : '否'
      if (typeof val === 'object') return JSON.stringify(val)
      return String(val)
    },

    // ------------------------------------------------------------ 手动触发

    async runScan(dryRun) {
      this.busy.scan = true
      this.msg = null
      try {
        const r = await runInspectionScan({ dryRun })
        this.report = this.buildScanReport(r)
        if (!dryRun) await this.afterMutation()
        else await this.loadOverview()
      } catch (e) {
        this.report = null
        this.msg = { type: 'error', text: e.message || '巡检执行失败' }
      } finally {
        this.busy.scan = false
      }
    },

    async runTtl(dryRun) {
      this.busy.ttl = true
      this.msg = null
      try {
        const r = await purgeExpired({ dryRun })
        this.report = this.buildTtlReport(r)
        if (!dryRun) await this.afterMutation()
      } catch (e) {
        this.report = null
        this.msg = { type: 'error', text: e.message || 'TTL 清理失败' }
      } finally {
        this.busy.ttl = false
      }
    },

    async runReconcile(dryRun) {
      this.busy.reconcile = true
      this.msg = null
      try {
        const r = await requestReconcile({ dryRun })
        this.report = this.buildReconcileReport(r)
      } catch (e) {
        this.report = null
        this.msg = { type: 'error', text: e.message || '向量对账失败' }
      } finally {
        this.busy.reconcile = false
      }
    },

    // ---- 报告构造：把各接口的不同字段收成同一种渲染结构 ----

    buildScanReport(r) {
      const d = (r && r.detected) || {}
      const items = []
      ;((r && r.opened) || []).forEach((o) => {
        items.push({
          tone: 'ok',
          text: `开单 #${o.ticketId} · ${TICKET_TYPE_TEXT[o.type] || o.type} · ${o.docId}`,
        })
      })
      ;((r && r.errors) || []).forEach((e) => {
        items.push({ tone: 'bad', text: `该类未查成，本轮跳过（≠ 没问题）：${e}` })
      })
      ;((r && r.skipped) || []).forEach((s) => {
        items.push({
          tone: '',
          text: `跳过 ${s.docId}（${TICKET_TYPE_TEXT[s.type] || s.type}）：${s.reason}`,
        })
      })
      return {
        title: '巡检扫描报告',
        dryRun: !!(r && r.dryRun),
        durationMs: (r && r.durationMs) || 0,
        rows: [
          { k: '扫描在库文档', v: (r && r.scannedActiveDocs) || 0, tone: '' },
          { k: '检出近重复', v: ((d.duplicate || []).length), tone: '' },
          { k: '检出陈旧', v: ((d.stale || []).length), tone: '' },
          { k: '检出劣质切片', v: ((d.lowQuality || []).length), tone: '' },
          { k: '新开工单', v: ((r && r.opened) || []).length, tone: 'ok' },
          {
            k: '检测异常',
            v: ((r && r.errors) || []).length,
            tone: ((r && r.errors) || []).length ? 'bad' : '',
          },
        ],
        items,
      }
    },

    buildTtlReport(r) {
      const items = []
      ;((r && r.purged) || []).forEach((p) => {
        items.push({
          tone: 'ok',
          text: `已彻底删除 ${p.docId}（回收于 ${p.recycledAt || '—'}，清备份 ${p.backupsRemoved} 份）`,
        })
      })
      ;((r && r.failed) || []).forEach((f) => {
        items.push({ tone: 'bad', text: `清理失败 ${f.docId}（下轮重试）：${f.error}` })
      })
      if (r && r.orphanBackups && r.orphanBackups.length) {
        items.push({
          tone: 'bad',
          text:
            `${r.orphanBackups.length} 份回收站备份在账本里查不到记录，` +
            `已跳过不删（需人工核对）：${r.orphanBackups.slice(0, 10).join('、')}`,
        })
      }
      return {
        title: 'TTL 清理报告',
        dryRun: !!(r && r.dryRun),
        durationMs: (r && r.durationMs) || 0,
        rows: [
          { k: 'TTL 天数', v: (r && r.ttlDays) || 0, tone: '' },
          { k: '本轮批次上限', v: (r && r.batchLimit) || 0, tone: '' },
          { k: '超期候选', v: ((r && r.candidates) || []).length, tone: '' },
          { k: '已彻底删除', v: ((r && r.purged) || []).length, tone: 'ok' },
          { k: '失败', v: ((r && r.failed) || []).length, tone: ((r && r.failed) || []).length ? 'bad' : '' },
          {
            k: '孤儿备份（不自动删）',
            v: ((r && r.orphanBackups) || []).length,
            tone: ((r && r.orphanBackups) || []).length ? 'bad' : '',
          },
        ],
        items,
      }
    },

    buildReconcileReport(r) {
      const items = []
      ;((r && r.leakedRemoved) || []).forEach((x) => {
        items.push({ tone: 'ok', text: `已清理残留向量：${x.docId}（${x.removedChunks || 0} 片）` })
      })
      ;((r && r.missingDocs) || []).forEach((x) => {
        items.push({ tone: 'bad', text: `账本说有、向量库没有（只告警，不自愈）：${x}` })
      })
      ;((r && r.unknownDocs) || []).forEach((x) => {
        items.push({ tone: 'bad', text: `向量库有、账本无登记（绝不自动删）：${x}` })
      })
      ;((r && r.leakedPending) || []).forEach((x) => {
        items.push({ tone: '', text: `残留未清（下轮重试）：${x}` })
      })
      return {
        title: '向量对账报告',
        dryRun: !!(r && r.dryRun),
        durationMs: (r && r.durationMs) || 0,
        rows: [
          { k: '向量库文档数', v: (r && r.milvusDocs) || 0, tone: '' },
          { k: '账本登记数', v: (r && r.registeredDocs) || 0, tone: '' },
          { k: '残留已清', v: ((r && r.leakedRemoved) || []).length, tone: 'ok' },
          { k: '残留未清', v: ((r && r.leakedPending) || []).length, tone: '' },
          { k: '缺失（只告警）', v: ((r && r.missingDocs) || []).length, tone: 'bad' },
          { k: '未登记（不自动删）', v: ((r && r.unknownDocs) || []).length, tone: 'bad' },
        ],
        items,
      }
    },

    // ------------------------------------------------------------ 版本链与回滚

    async openVersions(docId) {
      if (!docId) return
      this.rollbackTarget = null
      this.rollbackInput = ''
      this.err.versions = ''
      this.versions = { docId, docTitle: '', items: [] }
      try {
        this.versions = (await fetchVersions(docId)) || this.versions
      } catch (e) {
        this.err.versions = e.message || '版本链加载失败'
      }
    },

    closeVersions() {
      this.versions = null
      this.rollbackTarget = null
      this.rollbackInput = ''
    },

    askRollback(v) {
      this.rollbackTarget = v
      this.rollbackInput = ''
    },

    async doRollback() {
      if (!this.rollbackTarget || !this.versions) return
      this.acting = true
      const docId = this.versions.docId
      try {
        const res = await rollbackDocument(docId, {
          versionNo: this.rollbackTarget.versionNo,
          confirm: true,
          confirmDocName: this.rollbackInput,
        })
        this.msg = {
          type: 'success',
          text: `已回滚到 v${this.rollbackTarget.versionNo}，现为 v${(res && res.versionNo) || '新'}；${
            (res && res.chunkCount) || 0
          } 个切片已重新参与检索`,
        }
        this.rollbackTarget = null
        await this.afterMutation()
        await this.openVersions(docId)
      } catch (e) {
        // 确认门未通过（名字不符）也会走到这里，原文提示已足够明确
        this.msg = { type: 'error', text: e.message || '回滚失败' }
      } finally {
        this.acting = false
      }
    },

    // ------------------------------------------------------------ 公共

    /** 任何改变在库内容的动作之后：刷新概览 + 相关列表，并通知父级（统计/文档列表会变） */
    async afterMutation() {
      await this.loadOverview()
      this.$emit('changed')
      if (this.loadedOnce.tickets) await this.loadTickets()
      if (this.subTab === 'disabled' && this.loadedOnce.disabled) await this.loadDisabled()
      if (this.subTab === 'registry' && this.loadedOnce.registry) await this.loadRegistry()
    },

    applyPage(target, data) {
      if (!data) return
      target.page = data.page || target.page
      target.pageSize = data.pageSize || target.pageSize
      target.total = data.total || 0
      target.totalPages = data.totalPages || 1
    },

    goTicketPage(p) {
      if (p < 1 || p > this.ticketPage.totalPages) return
      this.ticketPage.page = p
      this.loadTickets()
    },
    goTicketPageSize(s) {
      this.ticketPage.pageSize = s
      this.ticketPage.page = 1
      this.loadTickets()
    },
    goDisabledPage(p) {
      if (p < 1 || p > this.disabledPage.totalPages) return
      this.disabledPage.page = p
      this.loadDisabled()
    },
    goDisabledPageSize(s) {
      this.disabledPage.pageSize = s
      this.disabledPage.page = 1
      this.loadDisabled()
    },
    goRegistryPage(p) {
      if (p < 1 || p > this.registryPage.totalPages) return
      this.registryPage.page = p
      this.loadRegistry()
    },
    goRegistryPageSize(s) {
      this.registryPage.pageSize = s
      this.registryPage.page = 1
      this.loadRegistry()
    },
    goAuditPage(p) {
      if (p < 1 || p > this.auditPage.totalPages) return
      this.auditPage.page = p
      this.loadAudit()
    },
    goAuditPageSize(s) {
      this.auditPage.pageSize = s
      this.auditPage.page = 1
      this.loadAudit()
    },

    pct(rate) {
      const n = Number(rate || 0)
      return `${(n * 100).toFixed(2)}%`
    },
  },
}
</script>

<style scoped>
.gov {
  text-align: left;
}

/* ------------------------------------------------------------------
   本地样式基座
   ------------------------------------------------------------------
   本项目的样式全部写在各自的 scoped 块里，没有全局样式表——scoped 会给元素打上
   本组件的 data-v 属性，父组件的 .btn / .doc-table / .modal 一律**作用不到**子组件
   渲染出的节点。所以这里自带一份与 KnowledgeBaseView 同款的原语，
   保证治理 tab 的按钮、表格、弹窗与其余 tab 观感完全一致。
   （与 KnowledgeBaseView 里那份是有意的重复；将来若抽全局样式表，两处一起收。）
   ------------------------------------------------------------------ */

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

.msg {
  margin: 0 0 12px;
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

.panel-hint {
  font-size: 12px;
  color: #8a94a0;
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

.empty-line {
  margin: 6px 0;
  font-size: 12.5px;
  color: #a4adb8;
  line-height: 1.7;
}

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

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.field-label {
  font-size: 12px;
  color: #6b7280;
  word-break: break-all;
}

/* ---------- 概览卡片 ---------- */
.gov-stats {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}

.g-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid #eef0f2;
  border-radius: 10px;
  background: #fafbfc;
}

.g-card.warn {
  border-color: #f3dfbd;
  background: #fffaf1;
}

.g-label {
  font-size: 12px;
  color: #8a94a0;
}

.g-value {
  font-size: 19px;
  font-weight: 600;
  color: #1f2328;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}

.g-card.warn .g-value {
  color: #a9701a;
}

.g-hint {
  font-size: 11.5px;
  color: #a4adb8;
  line-height: 1.4;
}

/* ---------- 开关 / 任务 ---------- */
.gov-meta {
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: 12px;
  margin-bottom: 14px;
}

.meta-block {
  padding: 12px 14px;
  border: 1px solid #eef0f2;
  border-radius: 10px;
  background: #fff;
}

.meta-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 9px;
  font-size: 12.5px;
  font-weight: 600;
  color: #4c5b6b;
}

.meta-note {
  font-size: 11.5px;
  font-weight: 400;
  color: #a4adb8;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chips .chip {
  background: #f0f8f2;
  color: #2f7d4f;
}

.chips .chip.off {
  background: #f1efec;
  color: #8a94a0;
}

.chips .chip.warn {
  background: #fff8ec;
  color: #a9701a;
}

.task-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.task-list li {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: baseline;
  padding: 5px 0;
  border-bottom: 1px solid #f5f7f9;
  font-size: 12px;
}

.task-list li:last-child {
  border-bottom: none;
}

.t-name {
  font-weight: 600;
  color: #33404f;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
}

.t-meta {
  color: #8a94a0;
}

.t-err {
  padding: 1px 7px;
  border-radius: 999px;
  background: #fff5f5;
  color: #c0392b;
  font-size: 11.5px;
  cursor: help;
}

.t-empty {
  display: block;
  color: #a4adb8;
}

/* ---------- 操作条 ---------- */
.gov-ops {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}

.op-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid #eef0f2;
  border-radius: 10px;
  background: #fff;
}

.op-head {
  font-size: 13px;
  font-weight: 600;
  color: #1f2328;
}

.op-desc {
  margin: 0;
  flex: 1;
  font-size: 11.5px;
  line-height: 1.55;
  color: #8a94a0;
}

.op-actions {
  display: flex;
  gap: 8px;
}

/* ---------- 报告 ---------- */
.gov-report {
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid #dbe6f6;
  border-radius: 10px;
  background: #f7faff;
}

.report-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.report-head strong {
  font-size: 13px;
  color: #1f2328;
}

.report-head .panel-hint {
  flex: 1;
}

.report-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px 14px;
}

.report-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  border-bottom: 1px dashed #e6ebf2;
  padding-bottom: 4px;
}

.r-k {
  color: #8a94a0;
}

.r-v {
  font-weight: 600;
  color: #33404f;
  font-variant-numeric: tabular-nums;
}

.r-v.ok {
  color: #2f7d4f;
}

.r-v.bad {
  color: #c0392b;
}

.report-items {
  margin: 10px 0 0;
  padding-left: 0;
  list-style: none;
  max-height: 220px;
  overflow-y: auto;
}

.report-items li {
  padding: 5px 8px;
  margin-bottom: 4px;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #4c5b6b;
  line-height: 1.55;
  word-break: break-all;
}

.report-items li.ok {
  color: #2f7d4f;
}

.report-items li.bad {
  color: #c0392b;
  background: #fff5f5;
}

/* ---------- 子视图 ---------- */
.gov-subtabs {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 10px;
  border-bottom: 1px solid #eef0f2;
}

.subtab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font-size: 13px;
  font-weight: 600;
  color: #8a94a0;
  cursor: pointer;
}

.subtab:hover {
  color: #4c5b6b;
}

.subtab.active {
  color: #2f6fed;
  border-bottom-color: #2f6fed;
}

.subtab .tab-count {
  padding: 0 7px;
  border-radius: 999px;
  background: #eef2f7;
  font-size: 11.5px;
  font-weight: 500;
  color: #6b7280;
  font-variant-numeric: tabular-nums;
}

.subtab.active .tab-count {
  background: #e5edfd;
  color: #2f6fed;
}

.subtab-spacer {
  flex: 1;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.filters select,
.filters input[type='text'] {
  height: 30px;
  padding: 0 9px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  background: #fff;
  font-size: 12.5px;
  color: #2c3e50;
  outline: none;
}

.filters input[type='text'] {
  min-width: 170px;
}

/* 时间范围两个输入框给窄一点：它们是"精确到秒的字符串"，宽了反而难看全 */
.filters input[type='text'].dt {
  min-width: 156px;
  font-variant-numeric: tabular-nums;
}

.filters select:focus,
.filters input[type='text']:focus {
  border-color: #2f6fed;
}

/* 导出结果提示：成功/截断都要说清楚，不静默 */
.export-msg {
  display: block;
  margin: 0 0 10px;
  color: #2f6fed;
}

.panel-hint.block {
  display: block;
  margin: 0 0 10px;
  line-height: 1.6;
}

.note-cell {
  max-width: 330px;
  font-size: 12px;
  line-height: 1.6;
  color: #6b7280;
  word-break: break-all;
}

/* ---------- 表格内的小件 ---------- */
.doc-table .chip.type-duplicate {
  background: #eef2f7;
  color: #4c5b6b;
}

.doc-table .chip.type-stale {
  background: #fff8ec;
  color: #a9701a;
}

.doc-table .chip.type-low_quality {
  background: #fbeaf0;
  color: #993556;
}

.doc-table .chip.st-open {
  background: #fff8ec;
  color: #a9701a;
}

.doc-table .chip.st-done {
  background: #f0f8f2;
  color: #2f7d4f;
}

.doc-table .chip.st-dismissed {
  background: #f1efec;
  color: #8a94a0;
}

.doc-table .chip.st-active {
  background: #f0f8f2;
  color: #2f7d4f;
}

.doc-table .chip.st-disabled {
  background: #f1efec;
  color: #8a94a0;
}

.doc-table .chip.st-recycled {
  background: #fff5f5;
  color: #c0392b;
}

.col-type {
  width: 96px;
}

.col-st {
  width: 96px;
}

.detail-row td {
  background: #fafbfc;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 16px;
  font-size: 11.5px;
}

.detail-item {
  display: flex;
  gap: 8px;
  min-width: 0;
}

.d-k {
  flex: none;
  color: #a4adb8;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
}

.d-v {
  color: #4c5b6b;
  word-break: break-all;
}

/* ---------- 弹窗补丁（沿用 KnowledgeBaseView 的弹窗观感） ---------- */
.modal.wide {
  width: 660px;
}

.modal .field {
  margin-top: 12px;
}

.modal .field input[type='text'] {
  height: 34px;
  padding: 0 10px;
  border: 1px solid #d7dee6;
  border-radius: 6px;
  font-size: 13px;
  color: #2c3e50;
  background: #fff;
  outline: none;
}

.modal .field input[type='text']:focus {
  border-color: #2f6fed;
}
</style>

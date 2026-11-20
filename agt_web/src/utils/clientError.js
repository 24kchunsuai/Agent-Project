/**
 * 前端运行时报错上报（企业级落地规约 E-12，判据 C3「可观测」）。
 *
 * 为什么需要它
 * ------------
 * E-12 之前，"前端炸了"在系统里**没有任何记录**：用户看到的是一片空白或一句
 * "操作失败"，而后端日志干干净净——因为错误发生在浏览器里，从没到过服务端。
 * 排障只能靠"用户描述 + 让他按 F12 截图"，而用户往往已经关掉页面了。
 * E-3 的顶栏状态灯也帮不上：它看的是服务端组件，前端自己崩了它依然是绿的。
 *
 * 三处收口（漏一处就有一整类错误收不到）
 * --------------------------------------
 * | 收口 | 收到什么 | 漏了会丢什么 |
 * |---|---|---|
 * | `app.config.errorHandler` | 组件渲染 / 生命周期 / 事件处理器 / setup 内的异常 | **Vue 默认会吞掉**这类异常（只打一行 warning），不配这个连"炸过"都不知道 |
 * | `window.onerror` | 组件之外的同步脚本错误（第三方库、路由钩子、`setTimeout` 里抛的） | 一次性初始化脚本的错误 |
 * | `unhandledrejection` | 没 catch 的 Promise（漏 `await` / 漏 `catch` 的 async） | **实际项目里最多的一类** |
 *
 * ⚠️ 规约原文写的是 `Vue.config.errorHandler`（Vue 2 的 API），本项目是 **Vue 3**，
 * 等价物是**实例**上的 `app.config.errorHandler`（Vue 3 移除了全局 `Vue.config`）。
 * 这是据实记档的偏离，不是漏做——两者收的是同一类异常。
 *
 * 三条铁律（与埋点同源）
 * ----------------------
 * 1. **上报失败绝不影响用户**。本文件里任何一行抛异常都会被就地吞掉：
 *    把"错误上报失败"变成用户可见的错误，等于用一个观测能力制造一个新的故障源。
 *    因此这里**刻意不复用 `src/api/request.js`**（理由见 `send()` 上方注释）。
 * 2. **绝不吞掉开发者最后一条线索**。`onerror` 里**不 return true**，
 *    也不覆盖浏览器的默认控制台输出——上报是"多记一份"，不是"把原件收走"。
 * 3. **风控在两端各有一层**：前端按"指纹 + 单页上限"限流（防网络风暴），
 *    服务端按"指纹 + 时间桶"合并（防表爆炸）。见 `shouldReport()` 的长注释。
 *
 * 身份从哪来
 * ----------
 * 上报体里**没有 userId 字段**（服务端模型的字段根本不存在），身份只能由 Java
 * 从登录态注入。这里带上 `Authorization` 头**只是为了**让 Java 能解析出登录态、
 * 把 `user_id` 注进转发请求——客户端自己声明的身份一律不被采信。
 */

import { getToken } from './auth'

// ============================================================ 配置

/** 上报端点（8080 转发；浏览器不可能持有服务间密钥 X-Agent-Key） */
export const REPORT_PATH = '/v1/ops/client-error'

/**
 * 浏览器端的已知噪音：**默认忽略且不报**。
 *
 * 为什么要有这个清单：`ResizeObserver loop ...` 是 Chrome 在"回调里改了布局"
 * 时丢出的警告级事件，**不是业务缺陷**，但一次拖拽/缩放能刷几十上百条，
 * 会把看板真实错误淹掉（前端监控里公认的第一大噪音源）。
 *
 * ⚠️ 忽略不是静默丢弃——启动时会把这些规则 `console.info` 一次，
 * 排障时能看到"有 N 条被规则挡掉了"，而不是"奇怪，怎么没上报"。
 * 新增规则请连理由一起写，别只加字符串。
 */
export const BUILTIN_IGNORE = [
  'ResizeObserver loop limit exceeded',
  'ResizeObserver loop completed with undelivered notifications',
]

/** 默认配置；可用 `VUE_APP_CLIENT_ERROR_*` 环境变量或运行时覆盖（见 `configure`） */
const DEFAULTS = {
  enabled: true,      // VUE_APP_CLIENT_ERROR_REPORT=false 可整体关停
  sampleRate: 1,      // VUE_APP_CLIENT_ERROR_SAMPLE_RATE=0~1
  maxPerLoad: 30,     // VUE_APP_CLIENT_ERROR_MAX_PER_LOAD：单次页面加载最多上报几条不同错误
  ignore: [],
}

const LIMITS = {
  message: 500,   // 与服务端 config 的 client_error_max_message 同值（服务端还会再截一次）
  stack: 2000,
  page: 200,
  version: 32,
  ua: 200,
}

// ============================================================ 纯函数（无副作用，便于复核）

/** 截断并标注（`…`）：看的人要知道"后面还有内容"，否则会把截断的怪结尾当成真实报错 */
export function truncate(text, limit) {
  const s = text === null || text === undefined ? '' : String(text)
  if (!limit || limit <= 0 || s.length <= limit) return s
  return s.slice(0, limit) + '…'
}

/** 页面标识只留 path：`?query` / `#hash` 里常有 id、筛选条件乃至回跳 token */
export function cleanPage(raw, fallback = '') {
  let s = String(raw === null || raw === undefined ? '' : raw).trim()
  for (const sep of ['?', '#']) {
    const i = s.indexOf(sep)
    if (i >= 0) s = s.slice(0, i)
  }
  return truncate(s || fallback, LIMITS.page)
}

/** 来源归一到白名单；不认识的一律 `unknown`（**保留而非丢弃**：出现 unknown 就说明收口漏了场景） */
export function normalizeSource(raw) {
  const s = String(raw === null || raw === undefined ? '' : raw).trim().toLowerCase()
  return ['vue', 'window', 'promise'].indexOf(s) >= 0 ? s : 'unknown'
}

/**
 * 错误指纹：与服务端 `client_error._fingerprint` **同口径**
 * （`page|source|message 前 200 字符` 的 sha256 前 16 位），但用途不同：
 * 服务端用它做**跨用户跨会话的合并键**，前端用它做**单页内去重**。
 *
 * 为什么必须与服务端同口径：两边口径不一致时，前端"以为是同一条"和
 * 服务端"以为是同一条"会给出不同答案，看板上的数字就没人能解释了。
 *
 * 这里不用 `crypto.subtle`（异步且非安全上下文不可用，http 的 8080 就是非安全上下文），
 * 改用一个 32 位 FNV-1a + 长度的简易摘要：前端只做**同页去重**，碰撞的代价是
 * "两条不同错误在同一个页面里被当成一条"（少报一条），不影响服务端统计口径。
 */
export function fingerprint(page, source, message) {
  const raw = `${page}|${source}|${truncate(String(message || ''), 200)}`
  let h = 0x811c9dc5
  for (let i = 0; i < raw.length; i += 1) {
    h ^= raw.charCodeAt(i)
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0
  }
  return `${h.toString(16).padStart(8, '0')}${raw.length.toString(16)}`
}

/** 命中忽略规则？（大小写不敏感的子串匹配——报错文本里常掺随机 id，正则反而更容易漏） */
export function isIgnored(message, patterns) {
  const text = String(message || '').toLowerCase()
  if (!text) return false
  return (patterns || []).some((p) => {
    const needle = String(p || '').trim().toLowerCase()
    return needle && text.indexOf(needle) >= 0
  })
}

/** 采样判定：rate>=1 全收；rate<=0 全丢；中间按随机数抽（`rand` 可注入以便复核） */
export function isSampled(rate, rand) {
  const r = Number(rate)
  if (!isFinite(r) || r >= 1) return true
  if (r <= 0) return false
  const roll = typeof rand === 'function' ? rand() : Math.random()
  return roll < r
}

/** 把任意 `reason`（Error / 字符串 / 对象 / null）压成 message+stack —— 上报体只要字符串 */
export function describeReason(reason) {
  if (reason instanceof Error) {
    return {
      message: reason.message || reason.name || String(reason),
      stack: reason.stack || '',
    }
  }
  if (reason === null || reason === undefined) {
    // Promise 拒绝一个裸 null/undefined：确实没有信息，但不能因此丢这一条
    return { message: '(Promise 被拒绝，原因为空)', stack: '' }
  }
  if (typeof reason === 'object') {
    for (const key of ['message', 'msg', 'error', 'reason']) {
      if (typeof reason[key] === 'string' && reason[key]) {
        return { message: reason[key], stack: safeJson(reason) }
      }
    }
    return { message: safeJson(reason), stack: '' }
  }
  return { message: String(reason), stack: '' }
}

/** JSON 化兜底：循环引用 / BigInt / 抛异常的 toJSON 都不能让上报链路挂掉 */
export function safeJson(value) {
  try {
    const json = JSON.stringify(value)
    return json === undefined ? String(value) : truncate(json, LIMITS.message)
  } catch (e) {
    return '(无法序列化的原因对象)'
  }
}

/** 环境变量 → 配置对象（纯函数：传 `process.env` 进来就能脱离浏览器复核） */
export function readConfig(env, overrides) {
  const e = env || {}
  const o = overrides || {}
  const pick = (key, fallback) => (e[key] === undefined || e[key] === '' ? fallback : e[key])

  const rawEnabled = String(pick('VUE_APP_CLIENT_ERROR_REPORT', String(DEFAULTS.enabled))).toLowerCase()
  const rawRate = Number(pick('VUE_APP_CLIENT_ERROR_SAMPLE_RATE', DEFAULTS.sampleRate))
  const rawMax = Number(pick('VUE_APP_CLIENT_ERROR_MAX_PER_LOAD', DEFAULTS.maxPerLoad))
  const extraIgnore = String(pick('VUE_APP_CLIENT_ERROR_IGNORE', ''))
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)

  // ⚠️ 逐项取覆盖值，**不要写 `...o`**：整体展开会用 `o.ignore` 顶掉下面拼好的
  // 内置噪音清单，于是 ResizeObserver 那类噪音又回来了，而且看不出是谁弄丢的。
  const envEnabled = ['false', '0', 'no', 'off'].indexOf(rawEnabled) < 0
  const pickOverride = (key, computed) => (o[key] === undefined || o[key] === null ? computed : o[key])

  return {
    // 只有明确的 "false"/"0"/"no"/"off" 才算关：拼错值（如 "flase"）时**保持开启**——
    // 观测能力的失败方向应该是"多记"，而不是"因为写错一个词就静默不记了"
    enabled: !!pickOverride('enabled', envEnabled),
    // 采样率非法（NaN / 负数 / >1）收敛而不是拒绝：与 Python 侧 `_clamp_rate` 同一取舍
    sampleRate: (() => {
      const r = Number(pickOverride('sampleRate', isFinite(rawRate) ? rawRate : 1))
      return isFinite(r) ? Math.min(1, Math.max(0, r)) : 1
    })(),
    maxPerLoad: (() => {
      const m = Number(pickOverride('maxPerLoad', isFinite(rawMax) && rawMax > 0 ? Math.floor(rawMax) : DEFAULTS.maxPerLoad))
      return isFinite(m) && m > 0 ? Math.floor(m) : DEFAULTS.maxPerLoad
    })(),
    ignore: BUILTIN_IGNORE.concat(extraIgnore).concat(o.ignore || []),
  }
}

/**
 * 单页去重 + 上限判定。**这是风控的第一层**。
 *
 * 为什么前端也要去重（服务端已经合并了）：服务端的合并省的是**数据库**，
 * 省不了**网络**。一个渲染循环里的报错能在 3 秒内触发几千次 handler，
 * 每次都发一个请求的话，用户最先感受到的是页面卡——用一个观测能力
 * 制造了一次真实的卡顿。
 *
 * ⚠️ 由此产生的一处**口径**必须说清：看板里的 `count` 计的是**上报次数**，
 * 而前端"同一页面加载内同一指纹只报一次"，所以它是真实抛错次数的**下界**
 * （同一错误在 N 次页面加载 / N 个用户那里各报一次，才累加成 N）。
 * 这个口径写在 `README.md` 的「E-12 前端运行时报错上报」小节与本注释里，别当成 bug 去"修"。
 */
export function createLimiter(maxPerLoad) {
  const sent = new Set()
  let count = 0
  return {
    /** @returns {boolean} true = 允许上报（并已在内部记账） */
    allow(fp) {
      if (sent.has(fp)) return false
      if (count >= maxPerLoad) return false
      sent.add(fp)
      count += 1
      return true
    },
    /** 验收/演示用：绕过指纹去重（但仍受上限保护） */
    force() {
      if (count >= maxPerLoad) return false
      count += 1
      return true
    },
    stats() {
      return { distinct: sent.size, count, max: maxPerLoad }
    },
  }
}

// ============================================================ 运行时状态

const state = {
  installed: false,
  config: null,
  limiter: null,
  appVersion: '',
  unloading: false,
  /** 重入保护：上报链路上的异常若再触发一次收口，直接短路，避免自我循环 */
  busy: false,
}

/** 允许在 `main.js` 之前用一个全局对象覆盖配置（部署时不重新构建也能调） */
function runtimeOverrides() {
  if (typeof window === 'undefined') return {}
  const raw = window.__CLIENT_ERROR_CONFIG__
  return raw && typeof raw === 'object' ? raw : {}
}

/** 应用配置（幂等；`install` 内部会调用一次） */
export function configure(env) {
  state.config = readConfig(env === undefined ? process.env : env, runtimeOverrides())
  state.limiter = createLimiter(state.config.maxPerLoad)
  return state.config
}

/**
 * 应用版本：`/version.json`（E-1 的运行时出口，由 `scripts/stamp_version.py` 生成）。
 *
 * 为什么是异步取而不是打包时注入：E-1 定的是"**版本唯一来源 = 根 VERSION，
 * 其余由脚本派生**"。打包时再读一次 VERSION 就等于在构建链里复制一份派生逻辑，
 * 将来两边口径一偏就出现"页面显示的版本 ≠ 上报的版本"。
 *
 * 代价：`version.json` 到达之前的报错，版本记空 → 看板归到「(未知版本)」。
 * 这是**可见且可解释**的，比伪造一个版本号好。
 */
function loadAppVersion() {
  if (typeof window !== 'undefined' && window.__APP_VERSION__) {
    state.appVersion = String(window.__APP_VERSION__)
    return
  }
  try {
    const base = (process.env && process.env.BASE_URL) || '/'
    fetch(`${base}version.json`, { cache: 'no-cache' })
      .then((r) => (r && r.ok ? r.json() : null))
      .then((j) => {
        if (j && j.version) state.appVersion = String(j.version)
      })
      .catch(() => {}) // 版本取不到不影响上报，服务端会归到未知版本
  } catch (e) {
    /* 静默：版本信息是锦上添花，不能让它影响上报 */
  }
}

// ============================================================ 上报传输

/**
 * 真正发包。**刻意不复用 `src/api/request.js` 的 axios 实例**，两个具体理由：
 *
 * 1. 那个实例的响应拦截器把 `code !== 2000` 一律 `Promise.reject`。
 *    上报端点在任何异常下都返回 2000，但**网络层失败**（8080 挂了、超时）会走到
 *    axios 的 error 分支再 reject 一次 → 产生一个新的 unhandledrejection →
 *    被本文件的 promise 收口捕获 → 再上报 → 再失败……**形成自我放大的循环**。
 * 2. 更严重：拦截器对 `4010` 会调 `clearAuth()`。也就是说**会话过期期间每次
 *    前端报错都会把用户登出**。用一个观测能力修改用户的登录态，是不可接受的副作用。
 *
 * 因此这里用裸 `fetch`：不解析响应、不重试、失败静默。
 */
function send(payload) {
  const base = (process.env && process.env.VUE_APP_BASE_API) || ''
  const url = `${base}${REPORT_PATH}`
  const body = JSON.stringify(payload)

  // 卸载期（用户正在离开页面）用 sendBeacon：普通 fetch 在导航开始后会被中断，
  // 而**崩溃/离开页面的那一刻正是错误最多的时候**。
  // 代价：sendBeacon 无法自定义请求头 → 这条没有 Authorization → Java 解析不到登录态
  // → userId 记 0（归到匿名）。取舍是"宁可不带身份，也要把错误送出去"。
  if (state.unloading && typeof navigator !== 'undefined' && navigator.sendBeacon) {
    try {
      const blob = new Blob([body], { type: 'application/json' })
      if (navigator.sendBeacon(url, blob)) return
    } catch (e) {
      /* 退回到 fetch */
    }
  }

  const headers = { 'Content-Type': 'application/json' }
  const token = safeToken()
  // 带上 token **只为让 Java 把 userId 注入查询参数**（身份归属），
  // 前端自报的身份（body 里的 userId）服务端根本不认。
  if (token) headers.Authorization = `Bearer ${token}`

  try {
    const p = fetch(url, {
      method: 'POST',
      headers,
      body,
      // keepalive：允许请求在页面卸载后继续完成（浏览器会限制并发与体量）
      keepalive: true,
      credentials: 'same-origin',
    })
    // 必须接住：不接就会变成 unhandledrejection，被我们自己的收口再报一次（自我循环）
    if (p && typeof p.catch === 'function') p.catch(() => {})
  } catch (e) {
    /* 静默：上报失败不重试、不上抛 */
  }
}

function safeToken() {
  try {
    return getToken() || ''
  } catch (e) {
    return ''
  }
}

/**
 * 统一入口：清洗 → 忽略规则 → 采样 → 去重/上限 → 发包。**永不抛异常、永不返回错误给调用方**。
 *
 * @param {{page?:string, source?:string, message?:string, stack?:string}} input
 * @param {{force?:boolean}} [opts] force 供验收/演示脚本绕过指纹去重
 * @returns {boolean} true = 已发包（仅供自检与排障阅读，调用方不需要处理）
 */
export function report(input) {
  if (state.busy) return false
  state.busy = true
  try {
    const cfg = state.config || configure()
    if (!cfg.enabled) return false

    const src = input || {}
    const page = cleanPage(src.page, typeof window !== 'undefined' ? cleanPage(window.location.pathname) : '')
    const source = normalizeSource(src.source)
    const message = truncate(String(src.message === undefined || src.message === null ? '' : src.message).trim(), LIMITS.message)
    const stack = truncate(String(src.stack || ''), LIMITS.stack)

    if (isIgnored(message, cfg.ignore)) return false
    if (!isSampled(cfg.sampleRate)) return false

    const fp = fingerprint(page, source, message)
    const limiter = state.limiter || (state.limiter = createLimiter(cfg.maxPerLoad))
    const allowed = src.force ? limiter.force() : limiter.allow(fp)
    if (!allowed) return false

    send({
      page,
      source,
      message: message || '(无消息)',
      stack,
      appVersion: truncate(state.appVersion, LIMITS.version),
      userAgent: truncate(typeof navigator !== 'undefined' ? navigator.userAgent : '', LIMITS.ua),
    })
    return true
  } catch (e) {
    return false
  } finally {
    state.busy = false
  }
}

// ============================================================ 三处收口

/**
 * 安装三处收口。在 `createApp(...)` 之后、`mount()` 之前调用
 * （放 mounted 之后会漏掉首屏渲染阶段的异常——那正是最该记的一段）。
 *
 * @param {import('vue').App} app Vue 3 应用实例（**必须传实例**，见文件头注释）
 * @returns {() => void} 卸载函数（自检/测试用；生产不需要调用）
 */
export function install(app) {
  if (state.installed) return () => {}
  if (typeof window === 'undefined') return () => {}
  configure()
  loadAppVersion()
  state.installed = true

  // ---- ① Vue 组件内异常 ----
  // Vue 3 的等价物是实例上的 config.errorHandler（规约原文写的是 Vue 2 的 Vue.config.errorHandler）
  if (app && app.config) {
    app.config.errorHandler = (err, instance, info) => {
      if (state.busy) return // 我们自己在上报途中抛的异常，别再兜一圈
      const d = describeReason(err)
      report({
        source: 'vue',
        // info 是 Vue 给的上下文（'render function' / 'setup' / 'native event handler'…），
        // 排障时"哪一类钩子炸的"比堆栈第一行更有用，所以塞进 stack 头部
        stack: [`[info=${info || 'unknown'}]`, d.stack].filter(Boolean).join('\n'),
        message: d.message,
      })
    }
  }

  // ---- ② 组件之外的同步错误 ----
  // ⚠️ 不 return true：返回真值会**吞掉浏览器默认的控制台输出**，
  // 等于把开发者最后一条线索也掐了。上报是"多记一份"，不是"把原件收走"。
  window.addEventListener('error', (event) => {
    if (state.busy) return
    // 资源加载失败（<img>/<script> 404）也走 'error' 事件，但那是 Event 不是 ErrorEvent，
    // 且 target 不是 window。这类"资源 404"归 E-3 的组件健康，不在这里当运行时报错。
    if (event && event.target && event.target !== window) return
    const err = event && event.error
    const d = err ? describeReason(err) : {
      message: (event && event.message) || 'Script error.',
      stack: event && event.filename
        ? `at ${event.filename}:${event.lineno || 0}:${event.colno || 0}`
        : '',
    }
    report({ source: 'window', message: d.message, stack: d.stack })
  }, true)

  // ---- ③ 未处理的 Promise ----
  window.addEventListener('unhandledrejection', (event) => {
    if (state.busy) return
    const d = describeReason(event ? event.reason : null)
    report({ source: 'promise', message: d.message, stack: d.stack })
  })

  // ---- 卸载标记：决定后续上报走 sendBeacon 还是 fetch ----
  window.addEventListener('pagehide', () => { state.unloading = true })
  window.addEventListener('beforeunload', () => { state.unloading = true })
  // bfcache 回退（history.back 到本页）时页面并未重载，需复位，否则之后都走 sendBeacon
  window.addEventListener('pageshow', () => { state.unloading = false })

  installSelfTest()
  announceIgnored()

  // 排障钩子：控制台 `__agtClientErrorStats()` 看"到底发出去了几条、被限流挡了多少"。
  // 与自检入口一起暴露在 window 上——**只读、无副作用**，不构成额外口子。
  window.__agtClientErrorStats = stats

  return () => { state.installed = false }
}

/** 启动时把忽略规则与限流上限说出来一次——否则"某类错误为什么不报"要靠读代码才能回答 */
function announceIgnored() {
  try {
    const cfg = state.config || {}
    if (typeof console === 'undefined' || !console.info) return
    console.info(
      `[client-error] E-12 前端报错上报已启用 | 端点=${REPORT_PATH} 采样率=${cfg.sampleRate}` +
      ` 单页上限=${cfg.maxPerLoad} 同页同错只报一次(看板 count 为下界)` +
      ` | 忽略规则 ${cfg.ignore.length} 条: ${cfg.ignore.join(' / ')}`
    )
  } catch (e) {
    /* 静默 */
  }
}

/**
 * 验收/演示辅助：`window.__agtClientErrorSelfTest()`
 *
 * 三条线各发包一次，用来回答"三处收口到底接上没有"：
 * * `window` 与 `promise` 两条是**真实触发**（dispatch 一个 ErrorEvent / 拒绝一个 Promise）；
 * * `vue` 那条是**直接调用已安装的 errorHandler**——浏览器不允许外部伪造一次组件渲染异常，
 *   所以它验证的是"handler 已装上且能发包"，而不是"Vue 会把组件异常交给它"。
 *   后者由真实抛错验收覆盖（在控制台 `setTimeout(() => { throw new Error('x') })`
 *   走的是 ②；要验 ① 就在任一组件方法里主动抛）。
 *
 * 返回三态明细，便于脚本断言。
 */
function installSelfTest() {
  if (typeof window === 'undefined') return
  window.__agtClientErrorSelfTest = function selfTest() {
    const result = { vue: false, window: false, promise: false }
    const tag = `E-12 selftest ${new Date().toISOString()}`
    try {
      if (state.installed && state.config) {
        // ① vue：直接调用已安装的 handler（浏览器不允许外部伪造一次组件渲染异常）。
        // 临时换上一个"全放行"的 limiter，让每次自检都真发包——否则第二次自检会因为
        // "同页同指纹只报一次"而静默跳过，自检结果就变得不可信了。
        const before = state.limiter
        state.limiter = { allow: () => true, force: () => true, stats: () => ({}) }
        try {
          const d = describeReason(new Error(`${tag} (vue handler)`))
          result.vue = report({ source: 'vue', message: d.message, stack: d.stack })
        } finally {
          state.limiter = before
        }
        // ② window：真实 dispatch（onerror 是 window 上 'error' 事件的处理器，dispatch 会调到它）
        window.dispatchEvent(new ErrorEvent('error', {
          message: `${tag} (window.onerror)`,
          error: new Error(`${tag} (window.onerror)`),
        }))
        result.window = true
        // ③ promise：真实拒绝，等一轮微任务让事件派发出去
        Promise.reject(new Error(`${tag} (unhandledrejection)`))
        result.promise = true
      }
    } catch (e) {
      /* 自检自身不能抛 */
    }
    return result
  }
}

/** 只读快照（排障用：控制台 `__agtClientErrorStats()` 看"到底发出去了几条"） */
export function stats() {
  const cfg = state.config || {}
  return {
    installed: state.installed,
    enabled: !!cfg.enabled,
    sampleRate: cfg.sampleRate,
    appVersion: state.appVersion,
    unloading: state.unloading,
    ...(state.limiter ? state.limiter.stats() : { distinct: 0, count: 0, max: 0 }),
  }
}

export default { install, report, configure, stats }

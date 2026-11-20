import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import 'highlight.js/styles/github.css'

/**
 * Markdown 渲染工具
 * - marked: md -> html（GFM 表格/任务列表 + breaks 换行）
 * - highlight.js(lib/common): 代码块高亮（仅显式标注语言的块，避免自动检测误判）
 * - DOMPurify: 白名单净化，防流式内容 XSS
 */

marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    // marked v13+ renderer 方法接收 token 对象
    code(token) {
      const raw = String(token.text ?? '')
      const lang = String(token.lang || '').trim().split(/\s+/)[0]
      let body = ''
      if (lang && hljs.getLanguage(lang)) {
        try {
          body = hljs.highlight(raw, { language: lang, ignoreIllegals: true }).value
        } catch {
          body = ''
        }
      }
      if (!body) {
        // 无语言或未知语言：纯文本转义，不做自动检测（快且可预期）
        body = raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      }
      const cls = lang ? `hljs language-${lang}` : 'hljs'
      return `<pre><code class="${cls}">${body}</code></pre>`
    },
  },
})

// 净化后给外链补 target=_blank / rel=noopener
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * 渲染 markdown 为可信 HTML
 * @param {string} text
 * @returns {string} 净化后的 html
 */
export function renderMarkdown(text) {
  if (!text) return ''
  const html = marked.parse(String(text))
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] })
}

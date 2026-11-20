<template>
  <div class="about">
    <h1>关于本项目</h1>
    <p class="desc">drone-agent 采用三层分离架构，各层独立演进，互不越权。</p>

    <table>
      <thead>
        <tr>
          <th>层</th>
          <th>技术栈</th>
          <th>端口</th>
          <th>职责</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>前台</td>
          <td>Vue 3</td>
          <td>9090</td>
          <td>界面与交互</td>
        </tr>
        <tr>
          <td>后端</td>
          <td>Spring Boot 3.3.3</td>
          <td>8080</td>
          <td>业务、鉴权、持久化</td>
        </tr>
        <tr>
          <td>智能体</td>
          <td>FastAPI</td>
          <td>8000</td>
          <td>LLM 编排与推理</td>
        </tr>
      </tbody>
    </table>

    <p class="note">智能体层不直接接触数据库与权限，只通过 HTTP 接受后端的调用。</p>

    <!-- 构建信息（企业级落地规约 E-1）：version.json 由 scripts/stamp_version.py
         从仓库根 VERSION 生成。取不到就整块不显示，不影响页面其它功能。 -->
    <div v-if="build" class="build">
      <span class="label">构建版本</span>
      <code>{{ build.version }}</code>
      <span class="sep">·</span>
      <span class="label">提交</span>
      <code>{{ build.gitCommit }}</code>
      <span class="sep">·</span>
      <span class="label">构建于</span>
      <code>{{ build.buildTime || '未打戳' }}</code>
      <span v-if="build.gitDirty" class="dirty">含未提交改动（不可复现）</span>
    </div>
  </div>
</template>

<script>
export default {
  name: 'AboutView',
  data() {
    return { build: null }
  },
  async mounted() {
    try {
      // 静态文件走 public/，开发与生产同路径；no-cache 避免拿到上一次打戳的旧值
      const resp = await fetch(`${process.env.BASE_URL}version.json`, { cache: 'no-cache' })
      if (resp.ok) this.build = await resp.json()
    } catch (e) {
      this.build = null
    }
  }
}
</script>

<style scoped>
.about {
  max-width: 860px;
  margin: 0 auto;
  padding-top: 24px;
  text-align: left;
}

h1 {
  font-size: 18px;
  font-weight: 500;
  margin: 0 0 6px;
}

.desc {
  font-size: 13px;
  color: #6b7280;
  margin: 0 0 20px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  text-align: left;
}

th {
  font-weight: 500;
  background: #f6f7f9;
  color: #1f2328;
}

td {
  color: #2c3e50;
}

.note {
  margin-top: 16px;
  font-size: 12px;
  color: #8b929c;
}

.build {
  margin-top: 8px;
  font-size: 12px;
  color: #8b929c;
  line-height: 1.9;
}

.build code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: #2c3e50;
  background: #f6f7f9;
  border: 1px solid #e5e7eb;
  border-radius: 3px;
  padding: 1px 5px;
}

.build .sep {
  margin: 0 6px;
  color: #cbd2d9;
}

.build .dirty {
  margin-left: 10px;
  color: #b45309;
}
</style>

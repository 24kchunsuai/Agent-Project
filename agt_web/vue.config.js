const { defineConfig } = require('@vue/cli-service')

module.exports = defineConfig({
  transpileDependencies: true,
  devServer: {
    // 路由用 createWebHistory（HTML5 history 模式），刷新或直接访问 /kb、/about
    // 这类非根路径时，必须由 devServer 回退到 index.html 交给前端路由接管，
    // 否则 webpack-dev-server 会按静态资源找不到而返回 404。
    // 生产环境由反向代理配置同等规则（try_files / error_page 404 → index.html）。
    historyApiFallback: true,
    proxy: {
      // 开发环境把 /v1 转发到 Java 后端：
      // 一是避免跨域，二是让前端代码不必区分环境（生产走同域反向代理）
      '/v1': {
        target: 'http://localhost:8080',
        changeOrigin: true
      }
    }
  }
})

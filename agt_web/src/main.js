import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import store from './store'
import { install as installClientErrorReporter } from './utils/clientError'

const app = createApp(App)

// E-12 前端运行时报错上报：三处收口（Vue errorHandler / window.onerror / unhandledrejection）。
//
// ⚠️ 必须在 `mount()` 之前装：装在 mounted 之后会漏掉**首屏渲染阶段**的异常，
// 而那恰好是最该记的一段——组件没渲染出来，用户看到的就是一片空白（连错误提示都没有）。
installClientErrorReporter(app)

app.use(store).use(router).mount('#app')

<template>
  <!-- 导航壳已下移到 AdminLayout（左侧导航 + 右侧内容），
       本组件只保留全局基础样式与路由出口。 -->
  <router-view />
</template>

<script>
export default {
  name: 'App',
}
</script>

<style>
* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: #f6f7f9;
}

#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #2c3e50;
}
</style>

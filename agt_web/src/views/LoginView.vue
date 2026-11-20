<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <h1>Drone Agent 登录</h1>
      <p class="tip">账号即无人机管控平台（jsdstu-3）账号，无需重新注册</p>

      <label>
        <span>用户名</span>
        <input
          v-model.trim="username"
          type="text"
          placeholder="如 admin"
          autocomplete="username"
          :disabled="loading"
        />
      </label>

      <label>
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          placeholder="请输入密码"
          autocomplete="current-password"
          :disabled="loading"
        />
      </label>

      <p v-if="errorText" class="error">{{ errorText }}</p>

      <button class="primary" type="submit" :disabled="loading || !username || !password">
        {{ loading ? '登录中…' : '登 录' }}
      </button>

      <p class="back"><router-link to="/">先不登录，直接去对话 →</router-link></p>
    </form>
  </div>
</template>

<script>
import { login } from '../api/auth'

export default {
  name: 'LoginView',
  data() {
    return {
      username: '',
      password: '',
      loading: false,
      errorText: '',
    }
  },
  methods: {
    async submit() {
      this.loading = true
      this.errorText = ''
      try {
        await login({ username: this.username, password: this.password })
        // 登录成功后回到来源页（默认对话页）
        this.$router.push(this.$route.query.redirect || '/')
      } catch (e) {
        this.errorText = e.message || '登录失败，请稍后重试'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #10182a 0%, #1c2b45 55%, #16324f 100%);
}

.login-card {
  width: 360px;
  padding: 36px 32px 28px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.35);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.login-card h1 {
  margin: 0;
  font-size: 20px;
  text-align: center;
  color: #1f2d3d;
}

.tip {
  margin: 0 0 4px;
  font-size: 12px;
  color: #8a97a6;
  text-align: center;
}

label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #4c5b6b;
}

label span {
  font-weight: 600;
}

input {
  height: 40px;
  padding: 0 12px;
  border: 1px solid #d7dee6;
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
}

input:focus {
  border-color: #2f6fed;
}

button.primary {
  height: 42px;
  border: none;
  border-radius: 8px;
  background: #2f6fed;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

button.primary:hover:not(:disabled) {
  background: #255cd2;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.error {
  margin: 0;
  font-size: 13px;
  color: #d64541;
  text-align: center;
}

.back {
  margin: 0;
  font-size: 12px;
  text-align: center;
}

.back a {
  color: #8a97a6;
  text-decoration: none;
}

.back a:hover {
  color: #2f6fed;
}
</style>

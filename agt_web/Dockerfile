# ============================================================
# agt_web（9090，前端静态资源 + nginx 反代）—— E-14 容器化交付
#
# ⚠️ 构建上下文是**仓库根**（与 agt_python/Dockerfile 同一口径）。
#
# 两阶段：node 构建段跑 vue-cli 生产构建 → nginx 运行段只带 dist。
# 反代配置在 deploy/nginx/nginx.conf（S-2 与 E-14 同一份，不写两份）：
#   /v1 → agt-backend:8080（SSE：proxy_buffering off + read_timeout 300s）
#   /   → SPA 回退 index.html（history 路由）
# ============================================================
FROM node:20-alpine AS build

# 国内网络适配：npmmirror（与 milvus compose 的本地化思路一致）
RUN npm config set registry https://registry.npmmirror.com

WORKDIR /build
COPY agt_web/package.json agt_web/package-lock.json ./
# 依赖不变时命中缓存
RUN npm ci --no-audit --no-fund

COPY agt_web/vue.config.js agt_web/babel.config.js ./
COPY agt_web/src ./src
COPY agt_web/public ./public
RUN npm run build

# ---------- 运行段 ----------
FROM nginx:1.27-alpine

COPY deploy/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO /dev/null http://127.0.0.1/version.json || exit 1

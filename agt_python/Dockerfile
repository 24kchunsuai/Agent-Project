# ============================================================
# agt_python（8000，FastAPI Agent 服务）—— E-14 容器化交付
#
# ⚠️ 构建上下文是**仓库根**（不是本目录）：
#      docker build -f agt_python/Dockerfile -t agt-python:<v> .
#   这样才能 COPY 仓库根的 VERSION（E-1：版本唯一来源），
#   让容器里的 /v1/health 也答得出版本号（build_info 优先级 2）。
#
# 密钥不进镜像：AGENT_SHARED_KEY / LLM_API_KEY 由 compose 的
#   env_file 注入（见 deploy/docker-compose.app.yml），镜像里没有 .env。
# 数据不进镜像：DATA_ROOT 指向 /app/data，由 compose 挂命名卷。
# 健康检查用 python 一行式（镜像里不需要 curl，少装一个包）。
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只拷依赖清单再装依赖：requirements 不变时这层走缓存，
# 改代码重建镜像不用重新下载包。
# ⚠️ 用阿里云源：清华 pypi 的 /packages/ CDN 实测对容器内请求回 403
# （index 能拿到、包下不动——2026-09-24 E-14 实测换源即解）
COPY agt_python/requirements.freeze.txt agt_python/requirements.txt ./
RUN pip install -r requirements.freeze.txt \
        -i https://mirrors.aliyun.com/pypi/simple/

# 源码 + 版本来源（VERSION 在仓库根；build_info.py 按
# BASE_DIR 的上一级找它，WORKDIR /app 时 REPO_ROOT=/）
COPY agt_python/app ./app
COPY agt_python/scripts ./scripts
COPY VERSION /VERSION

# 数据目录：compose 挂卷到 /app/data（DATA_ROOT 相对 BASE_DIR 解析）
ENV DATA_ROOT=/app/data

EXPOSE 8000

# 容器形态绑定 0.0.0.0（容器网络内可达）；宿主机**不映射 8000 端口**——
# S-1"8000 只服务本机 Java"的容器等价物：浏览器与外网都摸不到，
# 只有同网络的 agt-backend（带 X-Agent-Key）能访问。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 健康检查：探针要求带 X-Agent-Key 的接口才会 200，/v1/health 是匿名探针
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=4)"]

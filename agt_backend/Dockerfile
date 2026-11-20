# ============================================================
# agt_backend（8080，Spring Boot 网关）—— E-14 容器化交付
#
# ⚠️ 构建上下文是**仓库根**（与 agt_python/Dockerfile 同一口径）：
#      docker build -f agt_backend/Dockerfile -t agt-backend:<v> .
#
# 两阶段：maven 构建段产出 jar → jre 运行段只装 JRE+curl。
# Maven 走阿里云镜像（国内网络实测 Central 直连极慢；与
# deploy/milvus 的本地化适配同一思路）。产物名固定 target/agt_backend.jar
# （pom finalName，见 E-1），运行段 COPY 不用通配符猜。
# ============================================================
FROM maven:3.9-eclipse-temurin-17 AS build

# 阿里云镜像源（mirrorOf=*：全部仓库走镜像）
COPY deploy/docker/maven-settings.xml /tmp/maven-settings.xml

WORKDIR /build
COPY agt_backend/pom.xml ./
# 先解依赖（pom 不变时命中缓存，改代码重建不用重拉全量依赖）
RUN mvn -s /tmp/maven-settings.xml -B -q dependency:go-offline

COPY agt_backend/src ./src
COPY VERSION /VERSION
RUN mvn -s /tmp/maven-settings.xml -B -q -DskipTests package

# ---------- 运行段 ----------
FROM eclipse-temurin:17-jre

# 健康检查用 curl（temurin 基础镜像不带）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /build/target/agt_backend.jar ./agt_backend.jar

EXPOSE 8080

# agent.base-url / drone.base-url 用**命令行参数**覆盖而不靠 Spring 的
# 环境变量松散绑定（AGENT_BASE_URL 未必能绑到 agent.base-url，歧义坑）。
# drone 主控（8060）在无人机主控机（jsdstu-3）上，不在 compose 里——
# 经 host.docker.internal 回宿主机网络找它（Docker Desktop 可用）；
# 无人机不在场时 drone 类工具按既有降级路径走（fault_event 有账）。
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=70.0", "-jar", "agt_backend.jar", \
    "--agent.base-url=http://agt-python:8000", \
    "--drone.base-url=http://host.docker.internal:8060"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -sf http://127.0.0.1:8080/v1/version || exit 1

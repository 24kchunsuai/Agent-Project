#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署前自检（preflight）—— 企业级落地规约 E-2 / 判据 C1。

在动手启动之前，先把"起不来 / 起来是残废的"的原因说清楚。

设计约束
  * **只用标准库**：任何 python3 都能跑，不依赖项目 venv（pydantic_settings 等）。
  * **只读**：不启动、不停止、不修改任何服务，不写任何文件。
  * **可被机器判定**：退出码 0 = 通过；1 = 存在阻塞项；2 = 用法错误。
    `deploy/scripts/start-all.ps1` 先跑本脚本，非 0 即拒绝启动（fail-fast）。

检查分组
  [1] 工具链      python / java / node / docker 是否可用
  [2] 配置与密钥  .env 必填项；deploy/secrets/agent.key 与 .env **必须同值**（S-1）
  [3] 构建产物    jar / node_modules / 版本打戳产物；源码是否比 jar 新（E-1 / E-2）
  [4] 端口        8000 / 8080 / 8060 / 9090：空闲 | 本系统已在运行 | 被别的进程占用
  [5] 外部依赖    Milvus 19530 / MySQL 3306 / jsdstu-3 8060

状态语义
  OK   通过
  INFO 信息，不影响启动
  WARN 建议修，但不阻塞（本仓库不负责的外部系统、可降级项）
  FAIL **阻塞**：带着这个状态启动，要么起不来，要么服务是残废的

用法
    python deploy/scripts/preflight.py             # 人类可读报告
    python deploy/scripts/preflight.py --json      # 机器可读（给 CI / 看板）
    python deploy/scripts/preflight.py --quiet     # 只打印 WARN / FAIL
环境变量覆盖（换机器、换路径时用）
    AGT_PYTHON / AGT_JAVA / AGT_NODE / JSDSTU3_ROOT
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[2]          # deploy/scripts/preflight.py -> 仓库根
JSDSTU3_ROOT = Path(os.environ.get("JSDSTU3_ROOT") or (ROOT.parent / "jsdstu-3"))

DEFAULT_PYTHON = r"D:\install\python\anaconda\envs\python311\python.exe"
DEFAULT_JAVA = r"D:\install\java\jdk\bin\java.exe"
DEFAULT_NPM = r"C:\Program Files\nodejs\npm.cmd"

BACKEND_JAR = ROOT / "agt_backend" / "target" / "agt_backend.jar"       # E-2 统一后的固定产物名
DRONE_JAR = JSDSTU3_ROOT / "drone" / "target" / "drone-0.0.1-SNAPSHOT.jar"
ENV_FILE = ROOT / "agt_python" / ".env"
KEY_FILE = ROOT / "deploy" / "secrets" / "agent.key"

STAMP_ARTIFACTS = [
    (ROOT / "agt_python" / "app" / "build_info.json", "json"),
    (ROOT / "agt_backend" / "src" / "main" / "resources" / "build-info.properties", "props"),
    (ROOT / "agt_web" / "public" / "version.json", "json"),
]

# port, 服务名, 探针 URL, 期望特征（用于区分"本系统在跑"与"被别人占了"）
# ⚠️ 探针必须能**区分本系统与随便一个 HTTP 服务**，否则会误判。
#    实测教训（2026-09-24，故障注入验证时当场暴露）：9090 早期只探 "/"，
#    于是 `python -m http.server 9090` 被当成"agt_web 已在运行"，自检放行、
#    一键启动也照跑——一个假阳性就把整套 fail-fast 掏空了。
#    现在的特征：9090 用 E-1 的 /version.json（本系统独有）；
#    8060 用 drone 统一响应体里的 "code"（未登录时是 {"code":4010}，HTTP 仍是 200）。
PORTS = [
    (8000, "agt_python", "http://127.0.0.1:8000/v1/health", '"code":2000'),
    (8080, "agt_backend", "http://127.0.0.1:8080/v1/version", '"version"'),
    (8060, "jsdstu-3", "http://127.0.0.1:8060/v1/dashboard/summary", '"code"'),
    (9090, "agt_web", "http://127.0.0.1:9090/version.json", '"version"'),
]

REQUIRED_ENV = [
    ("AGENT_SHARED_KEY", "服务间共享密钥（S-1）——留空 Python 侧拒绝启动"),
    ("LLM_API_KEY", "对话模型密钥——留空业务链路不可用"),
    ("EMBEDDING_API_KEY", "向量模型密钥——留空 RAG 检索不可用"),
]

MANUAL = "docs/部署手册.md"

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
_RANK = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}


@dataclass
class Item:
    group: str
    status: str
    title: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)

    def add(self, group: str, status: str, title: str, detail: str = "", hint: str = "") -> None:
        self.items.append(Item(group, status, title, detail, hint))

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    @property
    def blocking(self) -> list[Item]:
        return [i for i in self.items if i.status == FAIL]

    def exit_code(self) -> int:
        return 1 if self.blocking else 0


# --------------------------------------------------------------------------- #
# 底层探测（全部只读、带超时）
# --------------------------------------------------------------------------- #
def tcp_probe(host: str, port: int, timeout: float = 1.5) -> tuple[bool, int, str]:
    """TCP 连通性。返回 (可达, 毫秒, 错误类名)。"""
    t0 = time.perf_counter()
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, int((time.perf_counter() - t0) * 1000), ""
    except OSError as exc:
        return False, int((time.perf_counter() - t0) * 1000), exc.__class__.__name__
    finally:
        sock.close()


def local_port_listening(port: int, timeout: float = 1.0) -> bool:
    """本机 127.0.0.1:port 是否有进程在监听。"""
    ok, _, _ = tcp_probe("127.0.0.1", port, timeout)
    return ok


def http_get(url: str, timeout: float = 3.0) -> tuple[int | None, str]:
    """GET 一次（**显式绕开系统代理**——本机 curl/python 默认走代理会 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "preflight/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(8192).decode("utf-8", "replace")
        except Exception:
            return exc.code, ""
    except Exception as exc:                                     # noqa: BLE001 - 探测失败即"不通"
        return None, exc.__class__.__name__


def run_cmd(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip() or (proc.stderr or "").strip()
    except Exception as exc:                                     # noqa: BLE001
        return 127, exc.__class__.__name__


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，跳过空行与 # 注释。不依赖 pydantic。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def parse_properties(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def read_version(path: Path, kind: str) -> str:
    """从打戳产物里取 version。

    注意 Java 那侧用的是 `build.version`（带前缀，避免与 Spring 自身属性撞名），
    前端/agent 侧是 JSON 的 `version`。两侧命名不同，这里统一兜住。
    """
    try:
        if kind == "json":
            return str(json.loads(path.read_text(encoding="utf-8")).get("version", "")).strip()
        props = parse_properties(path)
        return (props.get("version") or props.get("build.version") or "").strip()
    except Exception:                                            # noqa: BLE001
        return ""


def newest_mtime(folder: Path, suffixes: tuple[str, ...]) -> float:
    newest = 0.0
    if not folder.is_dir():
        return newest
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix in suffixes:
            newest = max(newest, p.stat().st_mtime)
    return newest


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# --------------------------------------------------------------------------- #
# 各分组检查
# --------------------------------------------------------------------------- #
def check_toolchain(rep: Report) -> dict[str, str | None]:
    """工具链。返回解析出的可执行文件路径，供后续检查复用。"""
    g = "工具链"
    found: dict[str, str | None] = {"python": None, "java": None, "npm": None}

    # --- python（必须能 import pydantic_settings，否则跑不了本项目）--- #
    py = os.environ.get("AGT_PYTHON") or DEFAULT_PYTHON
    if not Path(py).exists():
        alt = shutil.which("python") or shutil.which("python3")
        if alt:
            rep.add(g, WARN, "python", f"未找到 {py}，回退到 PATH 上的 {alt}",
                    f"本项目必须用 conda python311（隔离环境缺 pydantic_settings）。"
                    f"装好后设 AGT_PYTHON，或见 {MANUAL} §2")
            py = alt
        else:
            rep.add(g, FAIL, "python", "既没有 conda python311，PATH 上也没有 python",
                    f"见 {MANUAL} §2 环境要求")
            return found
    code, out = run_cmd([py, "-c", "import sys,importlib.util as u;"
                                   "print(sys.version.split()[0],"
                                   "'pydantic_settings=' + str(u.find_spec('pydantic_settings') is not None))"])
    if code != 0:
        rep.add(g, FAIL, "python", f"{py} 无法执行：{out[:120]}", "检查解释器路径是否有效")
        return found
    found["python"] = py
    if "pydantic_settings=True" in out:
        rep.add(g, OK, "python", f"{out.split()[0]}  {py}")
    else:
        rep.add(g, FAIL, "python", f"{py} 缺依赖 pydantic_settings（{out}）",
                f"装依赖：{py} -m pip install -r agt_python/requirements.txt；或改用 conda python311（{MANUAL} §2）")

    # --- java --- #
    jh = os.environ.get("JAVA_HOME")
    java = (str(Path(jh) / "bin" / "java.exe") if jh else "") or ""
    if not java or not Path(java).exists():
        java = DEFAULT_JAVA if Path(DEFAULT_JAVA).exists() else (shutil.which("java") or "")
    if java and Path(java).exists():
        found["java"] = java
        code, out = run_cmd([java, "-version"])
        ver = out.splitlines()[0] if out else "?"
        if "17" in ver:
            rep.add(g, OK, "java", f"{ver[:60]}  {java}")
        else:
            rep.add(g, WARN, "java", f"{ver[:60]}  {java}", "本项目按 JDK 17 构建（Spring Boot 3.3.3）")
    else:
        rep.add(g, FAIL, "java", "未找到 java.exe", "设 JAVA_HOME，或改 start-backend.cmd 里的 JAVA_EXE")

    # --- node / npm --- #
    npm = shutil.which("npm") or (DEFAULT_NPM if Path(DEFAULT_NPM).exists() else "")
    if npm:
        code, out = run_cmd([npm, "--version"], timeout=30)
        node_ver = ""
        node = shutil.which("node")
        if node:
            _, node_ver = run_cmd([node, "--version"])
        found["npm"] = npm
        rep.add(g, OK, "node/npm", f"node {node_ver or '?'}  npm {out or '?'}"[:70])
    else:
        rep.add(g, FAIL, "node/npm", "未找到 npm", "前端 9090 需要 Node.js（见手册 §2）")

    # --- docker（Milvus 载体）--- #
    docker = shutil.which("docker")
    if docker:
        code, out = run_cmd([docker, "version", "--format", "{{.Server.Version}}"], timeout=25)
        if code == 0 and out:
            rep.add(g, OK, "docker", f"server {out.splitlines()[0][:40]}（Milvus 依赖它）")
        else:
            rep.add(g, FAIL, "docker", f"docker 存在但守护进程无响应：{out[:80]}",
                    "启动 Docker Desktop（Milvus 跑在容器里）")
    else:
        rep.add(g, FAIL, "docker", "未找到 docker 命令", "Milvus 需要 Docker Desktop；见手册 §3.5")
    return found


def check_config(rep: Report) -> dict[str, str]:
    g = "配置与密钥"
    env = parse_env_file(ENV_FILE)
    if not env:
        rep.add(g, FAIL, f"{ENV_FILE.relative_to(ROOT)}", "文件不存在或为空",
                f"复制 .env.example 为 .env 并填必填项：{MANUAL} §3.3")
        return env

    rep.add(g, OK, "agt_python/.env", f"已解析 {len(env)} 项")

    for key, why in REQUIRED_ENV:
        val = env.get(key, "")
        if val:
            rep.add(g, OK, key, f"已配置（长度 {len(val)}，值不外显）")
        else:
            rep.add(g, FAIL, key, f"未配置——{why}", f"见 {MANUAL} §3.2 服务间共享密钥")

    host = env.get("HOST", "")
    if host and host not in ("127.0.0.1", "localhost"):
        rep.add(g, WARN, "HOST", f"HOST={host}：agent 端会监听在非回环地址上",
                "S-1 的设计是只服务本机 Java。确需对外时，X-Agent-Key 就是唯一防线")
    elif host:
        rep.add(g, OK, "HOST", f"{host}（只服务本机，符合 S-1）")

    # --- S-1 两侧密钥一致性（本检查最有价值：不一致的症状是全链路 4010）--- #
    # E-10 起还要**分辨轮换阶段**：轮换期 .env 里多一把 AGENT_SHARED_KEY_PREV，
    # "两侧不一致"可能是**故意的中间态**（消费者已换、生产者未换）。
    # 把它一律判 FAIL 会让人不敢轮换；一律放过又会掩盖真正的配置错误 —— 故分阶段判。
    key_py = env.get("AGENT_SHARED_KEY", "")
    key_prev = env.get("AGENT_SHARED_KEY_PREV", "")
    if key_prev.strip() and key_prev.strip() == key_py.strip():
        rep.add(g, FAIL, "AGENT_SHARED_KEY_PREV 与当前密钥同值",
                f"两者长度均为 {len(key_py)}",
                "Python 侧会 fail-closed 拒绝启动（E-10：两把相同不构成轮换）。"
                "PREV 应填**真正要停用的旧密钥**；轮换结束后是**清空**它，而不是填当前值")
    if KEY_FILE.exists():
        key_java = KEY_FILE.read_text(encoding="utf-8", errors="replace").strip()
        if not key_java:
            rep.add(g, FAIL, "deploy/secrets/agent.key", "文件为空",
                    "8080 启动脚本会 fail-closed 拒绝启动（S-1）")
        elif key_java == key_py:
            if key_prev.strip():
                rep.add(g, OK, "轮换：Java 已用新密钥",
                        f"agent.key == .env AGENT_SHARED_KEY（长度 {len(key_py)}）",
                        "下一步：确认指标 "
                        "agt_degrade_events_since_start_total{path=\"auth_key_legacy_used\"} "
                        "不再增长后，清空 AGENT_SHARED_KEY_PREV 并重启 8000（之后旧密钥失效）")
            else:
                rep.add(g, OK, "两侧密钥一致",
                        f"agent.key == .env AGENT_SHARED_KEY（长度 {len(key_py)}）")
        elif key_prev.strip() and key_java == key_prev.strip():
            rep.add(g, WARN, "轮换进行中：Java 仍在用旧密钥",
                    f"agent.key == AGENT_SHARED_KEY_PREV（长度 {len(key_java)}）",
                    "这是「消费者先行」的正常中间态——本服务已收两把，Java 还没换。"
                    "继续：把 Java 的 AGENT_SHARED_KEY 改为**新值**并重启 8080")
        else:
            how = ("Java 用的既不是当前密钥也不是上一把密钥 —— 会被 4010 拒绝"
                   if key_prev.strip() else
                   "Java(8080) 每个请求都会拿到 4010")
            rep.add(g, FAIL, "两侧密钥不一致",
                    f"agent.key 长度 {len(key_java)} vs .env 长度 {len(key_py)}", how)
    else:
        rep.add(g, FAIL, "deploy/secrets/agent.key", "文件不存在",
                f"8080 启动脚本 fail-closed 拒绝启动。生成方法见 {MANUAL} §3.2")
    return env


def check_artifacts(rep: Report) -> None:
    g = "构建产物"
    version_file = ROOT / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    if version:
        rep.add(g, OK, "VERSION", version)
    else:
        rep.add(g, FAIL, "VERSION", "仓库根 VERSION 缺失", "E-1 的版本唯一来源；从版本库恢复")

    # --- Java 后端 jar（E-2 起固定名 agt_backend.jar）--- #
    if BACKEND_JAR.exists():
        jar_m = BACKEND_JAR.stat().st_mtime
        rep.add(g, OK, "agt_backend/target/agt_backend.jar",
                f"{BACKEND_JAR.stat().st_size / 1048576:.1f} MB, {fmt_ts(jar_m)}")
        newest_src = newest_mtime(ROOT / "agt_backend" / "src" / "main", (".java", ".properties", ".yml"))
        if newest_src > jar_m:
            rep.add(g, WARN, "后端源码比 jar 新",
                    f"最新源码 {fmt_ts(newest_src)} > jar {fmt_ts(jar_m)}",
                    "重新打包：cd agt_backend && mvn clean package -DskipTests（先停 8080，Windows 锁 jar）")
    else:
        rep.add(g, FAIL, "agt_backend/target/agt_backend.jar", "缺失",
                "8080 起不来。构建：cd agt_backend && mvn clean package -DskipTests（手册 §3.4）")

    # --- jsdstu-3（外部系统，不由本仓库构建）--- #
    if DRONE_JAR.exists():
        rep.add(g, INFO, "jsdstu-3 drone jar", f"存在（{JSDSTU3_ROOT}）")
    else:
        rep.add(g, WARN, "jsdstu-3 drone jar", f"未找到 {DRONE_JAR}",
                "8060 是外部系统：在 IDEA 里起 DroneApplication 即可，本仓库不构建它")

    # --- 前端依赖 --- #
    nm = ROOT / "agt_web" / "node_modules"
    if nm.is_dir() and any(nm.iterdir()):
        rep.add(g, OK, "agt_web/node_modules", "已安装")
    else:
        rep.add(g, FAIL, "agt_web/node_modules", "缺失或为空",
                "9090 起不来。安装：cd agt_web && npm install（手册 §3.4）")

    # --- 版本打戳产物（E-1）与 VERSION 一致性 --- #
    mismatched = []
    for path, kind in STAMP_ARTIFACTS:
        rel = path.relative_to(ROOT)
        if not path.exists():
            mismatched.append(f"{rel} 缺失")
            continue
        got = read_version(path, kind)
        if version and got != version:
            mismatched.append(f"{rel}={got or '?'}（VERSION={version}）")
    if mismatched:
        rep.add(g, WARN, "版本打戳产物与 VERSION 不一致", "；".join(mismatched),
                "打戳：python scripts/stamp_version.py（改了 VERSION / 首次 clone 后必做）")
    else:
        rep.add(g, OK, "版本打戳产物", f"三端一致 = {version}")

    # 打戳产物比 jar 新 → 8080 里那份 build-info.properties 还没进 jar
    props = ROOT / "agt_backend" / "src" / "main" / "resources" / "build-info.properties"
    if props.exists() and BACKEND_JAR.exists() and props.stat().st_mtime > BACKEND_JAR.stat().st_mtime:
        rep.add(g, WARN, "打戳晚于打包", "build-info.properties 比 jar 新 → jar 内版本可能是旧的",
                "重新打包，或忽略（仅影响 /v1/version 显示）")


def check_ports(rep: Report) -> None:
    """端口：空闲 / 本系统已在运行 / 被别的进程占用。

    "本系统已在运行"刻意**不算阻塞**——否则 start-all 无法幂等重跑。
    真正阻塞的是"端口被别人的进程占了"。
    """
    g = "端口"
    for port, name, url, needle in PORTS:
        if not local_port_listening(port):
            rep.add(g, OK, f"{port} {name}", "空闲，可启动")
            continue
        if url is None:
            rep.add(g, INFO, f"{port} {name}", f"已有进程监听（{name} 不由本仓库启动，视为外部系统在跑）")
            continue
        status, body = http_get(url)
        if status and (needle is None or needle in body):
            rep.add(g, INFO, f"{port} {name}", "本系统已在运行（探针正常）")
        else:
            rep.add(g, FAIL, f"{port} {name}", f"端口被**别的进程**占用（探针 {url} 返回 {status}）",
                    f"先找出占用者再启动：netstat -ano | findstr :{port} → taskkill /PID <pid> /F；"
                    f"或改端口（见 {MANUAL} §7.1）")


def check_dependencies(rep: Report) -> None:
    g = "外部依赖"
    # Milvus：本仓库负责（deploy/milvus），挂了 RAG 直接不可用 → 阻塞
    ok, ms, err = tcp_probe("127.0.0.1", 19530)
    if ok:
        rep.add(g, OK, "Milvus 19530", f"可达（{ms}ms）")
    else:
        rep.add(g, FAIL, "Milvus 19530", f"不可达（{err}）",
                "cd deploy/milvus && docker compose -p milvus up -d；"
                "若 standalone 在无限重启，先 down 再 up（手册 §7.2）")

    # MySQL：只被 8060（外部系统）使用 → 警告不阻塞
    ok, ms, err = tcp_probe("127.0.0.1", 3306)
    if ok:
        rep.add(g, OK, "MySQL 3306", f"可达（{ms}ms）")
    else:
        rep.add(g, WARN, "MySQL 3306", f"不可达（{err}）",
                "8060 无人机主控依赖它；不起 8060 也可以只跑 8000/8080/9090")

    # jsdstu-3 8060（外部系统：未登录时返回 {"code":4010} 而 HTTP 仍是 200，属正常）
    status, body = http_get("http://127.0.0.1:8060/v1/dashboard/summary")
    if status and '"code"' in body:
        rep.add(g, OK, "jsdstu-3 8060", "可达（外部系统；未登录返回 code=4010 属正常）")
    elif status:
        rep.add(g, WARN, "jsdstu-3 8060", f"端口有响应但不像 drone 服务（HTTP {status}）",
                "确认 8060 上跑的是 cn.tedu.drone.DroneApplication")
    else:
        rep.add(g, WARN, "jsdstu-3 8060", "未启动",
                "业务链路（登录/无人机查询）会失败。在 IDEA 里起 DroneApplication（手册 §4.5）")


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def render(rep: Report, quiet: bool) -> None:
    bar = "=" * 78
    groups: list[str] = []
    for item in rep.items:
        if item.group not in groups:
            groups.append(item.group)

    print(bar)
    print("  drone-agent001  部署前自检 (preflight)")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S')}   python {sys.version.split()[0]}")
    print(f"  项目根 {ROOT}")
    print(bar)

    for idx, group in enumerate(groups, 1):
        items = [i for i in rep.items if i.group == group]
        shown = items if not quiet else [i for i in items if i.status in (WARN, FAIL)]
        if not shown:
            continue
        print(f"\n[{idx}] {group}")
        for item in shown:
            print(f"  {item.status:<4} {item.title:<34} {item.detail}")
            if item.hint and (not quiet or item.status in (WARN, FAIL)):
                print(f"       -> {item.hint}")

    print("\n" + "-" * 78)
    total = len(rep.items)
    print(f"  合计 {total} 项：OK {rep.count(OK)} / INFO {rep.count(INFO)} / "
          f"WARN {rep.count(WARN)} / FAIL {rep.count(FAIL)}")

    for item in rep.items:
        if item.status in (WARN, FAIL):
            print(f"  [{item.status}] {item.title} — {item.detail}")

    print()
    if rep.exit_code() == 0:
        warn = rep.count(WARN)
        suffix = f"（{warn} 项警告，见上）" if warn else ""
        print(f"  >>> 自检通过，可以启动 {suffix}")
    else:
        print(f"  >>> 自检未通过：{len(rep.blocking)} 项阻塞（标 FAIL）。")
        print("      修好再启动——否则起不来，或者起来是个残废的服务（skip 掉的部分不会自动恢复）。")
        print(f"      对照表见 {MANUAL} §7")
    print(bar)


def render_json(rep: Report) -> None:
    print(json.dumps({
        "ok": rep.exit_code() == 0,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(ROOT),
        "summary": {s: rep.count(s) for s in (OK, INFO, WARN, FAIL)},
        "items": [{"group": i.group, "status": i.status, "title": i.title,
                   "detail": i.detail, "hint": i.hint} for i in rep.items],
    }, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="drone-agent001 部署前自检（只读，不改动任何东西）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    parser.add_argument("--quiet", action="store_true", help="只打印 WARN / FAIL")
    args = parser.parse_args(argv)

    # 中文输出的编码策略（踩过坑，别改成 reconfigure(encoding="utf-8")）：
    #   * 控制台直连：Python 走 WriteConsoleW（PEP 528），中文正常；
    #   * 被 PowerShell 管道 / 重定向捕获：Python 按 locale（中文 Windows 上是 GBK）编码，
    #     正好对上 PowerShell 5.1 的 [Console]::OutputEncoding → 中文正常。
    #   一旦强行把 encoding 设成 utf-8，第二场景就会错位，实测管道里全是
    #   "绔彛琚?*鍒殑杩涚▼" 这种乱码。这里**只补 errors 兜底**，不动 encoding。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")                     # type: ignore[union-attr]
        except Exception:                                            # noqa: BLE001
            pass

    rep = Report()
    check_toolchain(rep)
    check_config(rep)
    check_artifacts(rep)
    check_ports(rep)
    check_dependencies(rep)

    if args.json:
        render_json(rep)
    else:
        render(rep, args.quiet)
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())

# Milvus 知识库部署（drone-agent001 RAG）

> 版本：Milvus Standalone **v2.6.24** · 部署日期：2026-09-16
> 用途：drone-agent001 三端项目的 RAG 向量库与 BM25 全文检索底座

---

## 一、概览

| 组件 | 镜像 | 内存占用 | 端口 |
|---|---|---|---|
| Milvus Standalone | `milvusdb/milvus:v2.6.24` | 128 MiB | **19530**(gRPC/REST) / **9091**(WebUI) |
| etcd（元数据） | `quay.io/coreos/etcd:v3.5.18` | 16 MiB | 仅内部 2379 |
| MinIO（对象存储） | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | 76 MiB | 仅内部 9000 |

**全套仅占约 220 MB 内存**，远低于 3 GB 上限（本机 Docker 可见内存 7.5 GB，
另有 mysql/nacos/redis/rabbitmq/minio/qdrant 六个容器在跑）。

WebUI：<http://localhost:9091/webui/>

---

## 二、常用命令

```bash
cd deploy/milvus

docker-compose up -d          # 启动
docker-compose ps             # 查看状态
docker-compose logs -f standalone   # 跟踪日志
docker-compose stop           # 停止（保留容器与数据）
docker-compose down           # 删除容器（保留 volumes 数据）

# ⚠️ 清库重来（数据全部丢失）
docker-compose down && rm -rf ./volumes
```

健康检查：`curl http://localhost:9091/healthz` → 返回 `OK`

---

## 三、镜像拉取：为什么用加速源，以及踩过的坑

### 问题现象

反复 `docker pull milvusdb/milvus` 中断，两小时拉不下来。

### 根因

`C:\Users\LBB\.docker\daemon.json` 当时**只配置了一个镜像源**：

```json
{ "registry-mirrors": ["https://docker.xuanyuan.me"] }
```

该源不稳定，导致拉取反复中断。**镜像本身与网络带宽都没有问题。**

### 解决

改成多个已验证可用的源（镜像源按顺序尝试，前面的失败会自动回退到后面的）：

```json
{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://docker.xuanyuan.me"
  ]
}
```

> 修改后需重启 Docker Desktop 生效（Docker Desktop 4.75）。
> 重启会中断所有运行中的容器，建议在不影响业务时操作。

### 本次实际拉取结果（加速源直连，无需离线导入）

| 镜像 | 大小 | 耗时 |
|---|---|---|
| `milvusdb/milvus:v2.6.24` | 1.36 GB | **54 秒** |
| `quay.io/coreos/etcd:v3.5.18` | 86.2 MB | 18 秒 |

命令（直接指定源，绕过 daemon 配置，适合临时拉取）：

```bash
docker pull docker.1panel.live/milvusdb/milvus:v2.6.24
docker tag  docker.1panel.live/milvusdb/milvus:v2.6.24 milvusdb/milvus:v2.6.24

docker pull quay.io/coreos/etcd:v3.5.18          # etcd 在 quay.io，直连通
docker tag  quay.m.daocloud.io/minio/minio:latest minio/minio:RELEASE.2024-12-18T13-15-44Z  # 复用已有 minio
```

> **结论：不需要离线导入 tar 包。** 换掉失效的镜像源后，54 秒即可拉完 1.36 GB。

### 各加速源实测（2026-09-16）

| 源 | 状态 |
|---|---|
| `docker.1panel.live` | ✅ 可用（本次采用） |
| `docker.1ms.run` | ✅ 可用 |
| `docker.m.daocloud.io` | ✅ 可用 |
| `quay.io`（直连） | ✅ 可用（etcd 来源） |
| `docker.xuanyuan.me` | ⚠️ 不稳定，本次故障主因 |
| `dockerproxy.cn` / `dockerpull.org` | ❌ 不可达 |

---

## 四、端口冲突处理（与官方 compose 的差异）

官方 compose 会把 MinIO 的 9000/9001 映射到宿主机，本机存在冲突风险：

| 端口 | 本机占用情况 |
|---|---|
| 9090 | ⚠️ Vue dev server（agt_web）占用 |
| 9000 / 9001 | ⚠️ 已有独立 MinIO 容器占用 |
| 19530 / 9091 | ✅ 空闲 |

**处理方式**：Milvus 的 MinIO 与 etcd **完全不映射宿主机端口**，
仅通过 Docker 内部网络（`milvus-net`）供 Milvus 访问；只暴露 19530 与 9091。
这样既不影响已有服务，也减少攻击面。

---

## 五、能力验证结果

验证脚本：`agt_python/scripts/verify_milvus.py`
完整日志：`deploy/milvus/verify-report.log`

| 验证项 | 结果 |
|---|---|
| Milvus 服务版本 | **2.6.24** ✅ |
| GLM embedding-3 维度 | **2048** ✅（API 连通，0.42s） |
| 中文分析器 | **`{"type": "chinese"}`** ✅（Milvus 内置 jieba，无需客户端分词） |
| BM25 全文检索 | ✅ 服务端 Function 自动生成稀疏向量 |
| 稠密向量检索 | ✅ COSINE |
| RRF 混合检索 | ✅ 可用（**但见下节问题**） |
| 加权混合检索 | ✅ 可用（**但见下节问题**） |

### 集合 Schema 要点

```python
text   : VARCHAR, enable_analyzer=True, analyzer_params={"type": "chinese"}
sparse : SPARSE_FLOAT_VECTOR     # 由 BM25 Function 自动填充，无需客户端计算
dense  : FLOAT_VECTOR, dim=2048  # GLM embedding-3
Function(name="text_bm25", function_type=BM25,
         input_field_names=["text"], output_field_names=["sparse"])
```

---

## 六、⚠️ 关键实测发现：混合检索在口语查询上会失效

这是本次验证最有价值的产出，**直接影响 M6 检索链路设计**。

### 测试用例

查询「**黑飞**会有什么后果」（口语术语，与法规原文「未经批准在管制空域内飞行」零字面重合）

正确答案：`reg-003`（管制空域违规处罚）

### 四种方式实测排名

| 方式 | 第 1 名 | 分数 | 是否正确 |
|---|---|---|---|
| [A] 纯 BM25 | `man-001` 起飞前检查清单 | 1.498 | ❌ **错** |
| [B] 纯稠密向量 | `reg-003` 管制空域处罚 | 0.536 | ✅ 正确 |
| [C] RRF 混合 | `man-001` 起飞前检查清单 | 0.0318 | ❌ **错** |
| [D] 加权混合 (BM25 0.3 + 稠密 0.7) | `man-001` 起飞前检查清单 | 0.6916 | ❌ **错** |

### 三个结论

**1. 稠密向量对了，BM25 错了 —— 而两种混合方式都把错误结果放到了第一位。**

「黑飞」被中文分词器切成 `黑` / `飞`，其中 `飞` 字与「**飞**行」「起**飞**前检查」
产生字面命中，BM25 据此把「起飞前检查清单」排到第一。语义检索则正确理解为
「违规飞行」，命中了管制空域处罚条款。

**2. RRF 的错误机制：只看排名，不看分数。**

RRF 的融合公式是 `Σ 1/(k + rank)` —— 只消费每路的**名次**。
BM25 把错误结果排第一（名次 1，贡献满分），RRF 就忠实地把这份「高置信错误」放大。

**3. 加权融合同样没用，原因更隐蔽：归一化让「独苗」拿满分。**

本次 BM25 只召回了 **1 条**结果，其余候选分数均为 0。归一化后这条独苗
直接获得 BM25 分量的满分 1.0，`0.3 × 1.0 = 0.3` 的贡献，
已超过稠密向量第 1 名与第 2 名的差距（`(0.768-0.559) × 0.7 ≈ 0.146`）。
**所以调低权重并不能解决——问题不在权重，在 BM25 召回结果本身的质量。**

### 可用的解法（按推荐度排序）

| 方案 | 说明 | 代价 |
|---|---|---|
| **Query 改写** | 口语/术语先改写为标准表述（「黑飞」→「未经批准飞行」）再检索 | 一次 LLM 调用 |
| **BM25 分数下限** | BM25 分数低于阈值时视为无效召回。实测正常查询得分 2.98~5.38，噪音 1.498，**区分度明显** | 需按语料标定 |
| **交叉一致性校验** | 两路结果无交集时降级为纯稠密检索 | 纯计算，几乎零成本 |
| RRF 调参（k 值） | 治标不治本，不推荐单独使用 | — |

> **注意**：BM25 是绝对分数（不随批量偏移），比向量相似度更适合设固定阈值；
> 但它受语料规模影响（IDF 变化），**扩库到 300+ chunk 后必须重新标定**。

---

## 七、体积与后续

- 当前 `kb_verify` 为验证用集合（6 条语料），M6 正式建库时可删除
- 数据卷位于 `deploy/milvus/volumes/`，已加入 `.gitignore`（禁止提交）
- 磁盘占用：镜像 1.36 GB + 运行数据（随语料增长）
- BM25 索引由 Milvus 服务端维护（tantivy 引擎），无需额外部署 ES

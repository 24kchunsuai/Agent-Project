# -*- coding: utf-8 -*-
"""
验证：reranker 能否压制 BM25 在口语查询上的错误召回

背景 —— 上一轮实测结论（无 rerank 环境）：
  「黑飞会有什么后果」→ BM25 把「起飞前检查清单」排第一（错），
  纯稠密正确命中「管制空域处罚」，RRF 与加权融合都把错误放大。

上一轮基于此提出「BM25 分数下限」过滤，但那是**没有 reranker 时的权宜之计**。
本脚本验证在**完整方案（混合召回 → rerank）**下，reranker 是否已足够兜住噪音。

运行：
  D:/install/python/anaconda/envs/python311/python.exe scripts/verify_rerank.py
"""
import os
import sys
import json
from pathlib import Path

import httpx

# ---------------------------------------------------------------- 配置
def load_env() -> dict:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    cfg: dict = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


ENV = load_env()
API_KEY = ENV.get("EMBEDDING_API_KEY", "")     # 与 embedding 同一 GLM key
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# ---------------------------------------------------------------- 候选语料
# 取真实知识库内容，覆盖"正确项"与"被 BM25 误召的噪音项"
DOCS = [
    ("reg-001", "无人驾驶航空器按照重量分为微型、轻型、小型、中型、大型五类。"
                "微型无人驾驶航空器是指空机重量小于0.25千克，最大飞行真高不超过50米，"
                "最大平飞速度不超过40千米每小时的无人驾驶航空器。"),
    ("reg-002", "民用无人驾驶航空器所有者应当依法进行实名登记。未经实名登记的，"
                "由公安机关责令改正，可以处200元以下的罚款。"),
    ("reg-003", "在管制空域内飞行未经批准的，由公安机关责令停止飞行，可以处500元以下的罚款；"
                "情节严重的，没收实施违规飞行的无人驾驶航空器，并处1000元以上1万元以下的罚款。"),
    ("man-001", "起飞前检查清单：一、检查螺旋桨是否安装牢固；二、检查电池电量；"
                "三、校准指南针与IMU；四、确认GPS卫星数量不少于12颗；五、确认返航点已刷新。"),
    ("man-002", "电池保养：锂电池应存放在阴凉干燥处，环境温度以20至25摄氏度为宜，"
                "长期不使用时电量保持在50%至60%。"),
    ("man-003", "返航高度设置：建议将返航高度设置为飞行区域内最高障碍物高度再增加30至50米。"),
    # ---- 以下为明显无关的干扰项，用于检验 rerank 分数的区分度 ----
    ("noise-1", "红烧肉的做法：五花肉切块，冷水下锅焯水去腥，加入冰糖小火炒出糖色，"
                "再放生抽老抽慢炖四十分钟。"),
    ("noise-2", "Python 列表推导式语法：[x * 2 for x in range(10) if x % 2 == 0]，"
                "等价于 for 循环加 append。"),
    ("noise-3", "北京地铁 1 号线首末班车时间：苹果园站首班车 5:10，末班车 22:55；"
                "四惠东站首班车 5:05。"),
    ("noise-4", "咖啡豆烘焙程度分为浅烘、中烘、深烘，浅烘酸度明亮，深烘苦味突出。"),
]

# (查询, 期望的正确答案 doc_id, 该查询的背景说明)
CASES = [
    ("黑飞会有什么后果", "reg-003", "口语词，与原文零字面重合，BM25 会误召 man-001"),
    ("起飞前要检查什么", "man-001", "正常查询，各路都应正确"),
    ("实名登记怎么弄", "reg-002", "正常查询"),
]


def glm_rerank(query: str, documents: list[str], top_n: int = 6):
    """调用 GLM rerank API，返回 [(index, score), ...] 按分数降序"""
    payload = {
        "model": "rerank",
        "query": query,
        "documents": documents,
        "top_n": top_n,
        "return_documents": False,
    }
    with httpx.Client(timeout=60) as c:
        r = c.post(
            f"{BASE_URL}/rerank",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
    return [(it["index"], it.get("relevance_score", it.get("score"))) for it in body["results"]]


def hr(title: str):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def main():
    hr("GLM Rerank 可用性确认")
    if not API_KEY:
        print("  [FATAL] .env 中缺少 EMBEDDING_API_KEY")
        sys.exit(1)
    print(f"  接口   : {BASE_URL}/rerank")
    print(f"  模型   : rerank")
    print(f"  候选数 : {len(DOCS)} 条")

    # 先探测 API 是否可用 + 分数制
    try:
        probe = glm_rerank("无人机怎么实名登记", [d[1] for d in DOCS], top_n=3)
        print(f"  探测成功，返回 {len(probe)} 条，分数样本：")
        for i, s in probe:
            print(f"      {s!r:>22}  {DOCS[i][0]}")
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {type(e).__name__}: {e}")
        print("\n  → rerank 接口不可用，后续实验无法进行")
        sys.exit(2)

    # 分数制判断
    sample_scores = [s for _, s in probe if isinstance(s, (int, float))]
    if sample_scores:
        mx = max(sample_scores)
        print(f"\n  ** 分数制判定：最大值 {mx} → "
              f"{'0~1 归一化' if mx <= 1.0 else '0~100 或原始分'} **")

    # ---------------------------------------------------- 核心实验
    hr("核心实验：reranker 能否把 BM25 的噪音压下去")
    for query, expect_id, note in CASES:
        print(f"\n  ┌─ 查询：{query}")
        print(f"  │  期望答案：{expect_id}    （{note}）")
        print("  │")
        print("  │  Rerank 后排序：")
        try:
            ranked = glm_rerank(query, [d[1] for d in DOCS], top_n=len(DOCS))
        except Exception as e:  # noqa: BLE001
            print(f"  │      → 报错 {type(e).__name__}: {e}")
            print("  └" + "─" * 62)
            continue

        for rank, (idx, score) in enumerate(ranked, 1):
            did = DOCS[idx][0]
            marker = "  ← 期望答案" if did == expect_id else ""
            star = " ✅" if (rank == 1 and did == expect_id) else ("  ❌ 首位非期望" if rank == 1 else "")
            print(f"  │      #{rank}  {score!s:>20}  {did}{marker}{star}")

        first_id = DOCS[ranked[0][0]][0]
        verdict = "✅ reranker 纠正成功" if first_id == expect_id else "❌ reranker 未能纠正"
        print(f"  │")
        print(f"  │  → 结论：{verdict}")
        print("  └" + "─" * 62)

    hr("实验完成")


if __name__ == "__main__":
    main()

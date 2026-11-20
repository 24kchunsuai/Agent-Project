"""M-3 相关性过滤单测（docs/specs/会话记忆规约.md §6 M-3 验收标准）。

打桩策略：monkeypatch `history_filter._embed_batch`（模块内唯一的 glm 出口），
用三维主题向量做**确定性**打分——炸机 [1,0,0] / 登记 [0,1,0] / 无关 [0,0,1]，
三个轴两两正交，cosine 要么 0 要么 1，断言不依赖浮点巧合。
"""

import pytest

from app.core import config as config_mod
from app.rag import history_filter as hf

BOMB = "炸机"
REG = "登记"


def _mk_turns(specs: list[str]) -> list[dict]:
    """按主题序列造轮次：第 i 轮 user/assistant 内容都带主题词与序号。"""
    msgs: list[dict] = []
    for i, topic in enumerate(specs):
        msgs.append({"role": "user", "content": f"第{i}轮{topic}问题内容"})
        msgs.append({"role": "assistant", "content": f"第{i}轮{topic}回答内容"})
    return msgs


def _fake_embed(topic_map: dict[str, list[float]], default: list[float] | None = None):
    """按「内容含主题词」返回固定向量；未命中主题用正交缺省向量。"""
    default = default or [0.0, 0.0, 1.0]

    def _impl(texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            for kw, vec in topic_map.items():
                if kw in t:
                    out.append(list(vec))
                    break
            else:
                out.append(list(default))
        return out

    return _impl


@pytest.fixture(autouse=True)
def _clear_vec_cache():
    """向量缓存是模块级全局，用例间必须清空（否则上一用例的向量会串味）。"""
    hf._vec_cache.clear()
    hf._cache_order.clear()
    yield
    hf._vec_cache.clear()
    hf._cache_order.clear()


def _no_embed(_texts):
    raise AssertionError("不应调用 embedding")


# ---------- 验收 1：跨话题淘汰 ----------

def test_filter_drops_off_topic_middle_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 轮混合话题 + 「实名登记」查询 → 炸机话题的中间轮被淘汰（规约验收 1）。"""
    monkeypatch.setattr(hf, "_embed_batch", _fake_embed({BOMB: [1, 0, 0], REG: [0, 1, 0]}))

    # 10 轮：头 2 轮（身份）+ 中间 2 轮（炸机）+ 尾 6 轮
    msgs = _mk_turns(["身份", "身份", BOMB, BOMB, "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    assert res.was_filtered and res.dropped_turns == 2, "炸机中间轮应被淘汰"
    contents = " ".join(m["content"] for m in res.messages)
    assert "炸机" not in contents, "结果里不应残留炸机话题"
    assert "第0轮身份问题内容" in contents, "头部锚点必须保留"
    assert "第9轮通用回答内容" in contents, "尾部锚点必须保留"
    assert res.degraded is False


# ---------- 验收 2：短会话不触发 ----------

def test_short_history_returns_all_without_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮数 ≤ memory_filter_min_turns → 返回全量且**不调用 embedding**（规约验收 2）。"""
    monkeypatch.setattr(hf, "_embed_batch", _no_embed)

    msgs = _mk_turns(["通用"] * 8)  # 8 轮 = min_turns，不触发
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    assert res.messages == msgs and not res.was_filtered


# ---------- 验收 3：时间序重建 ----------

def test_result_keeps_original_time_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """入选轮次按**原始时间升序**拼回，不是按分数排序（规约验收 3）。"""
    monkeypatch.setattr(hf, "_embed_batch", _fake_embed({BOMB: [1, 0, 0], REG: [0, 1, 0]}))

    # 12 轮：头 2 + 中间 4（炸机/登记/炸机/登记）+ 尾 6
    msgs = _mk_turns(["身份", "身份", BOMB, REG, BOMB, REG,
                      "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    # 幸存的中间轮是第 3、5 轮；结果 = 头2轮 + [第3轮, 第5轮] + 尾6轮，整体仍升序
    got = [m["content"] for m in res.messages]
    want = (
        ["第0轮身份问题内容", "第0轮身份回答内容",
         "第1轮身份问题内容", "第1轮身份回答内容"]
        + ["第3轮登记问题内容", "第3轮登记回答内容",
           "第5轮登记问题内容", "第5轮登记回答内容"]
        + [c for i in range(6, 12)
           for c in (f"第{i}轮通用问题内容", f"第{i}轮通用回答内容")]
    )
    assert got == want, "结果必须是「头锚点 + 幸存中间轮(按原序) + 尾锚点」"
    assert res.dropped_turns == 2


# ---------- 验收 4：轮次成对 ----------

def test_turn_pairs_preserved_no_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮内 user/assistant 成对保留，不出现「有问无答」断头上下文（规约验收 4）。"""
    monkeypatch.setattr(hf, "_embed_batch", _fake_embed({BOMB: [1, 0, 0], REG: [0, 1, 0]}))

    msgs = _mk_turns(["身份", "身份", BOMB, REG, BOMB, REG,
                      "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    roles = [m["role"] for m in res.messages]
    assert len(roles) % 2 == 0
    for i in range(0, len(roles), 2):
        assert roles[i] == "user" and roles[i + 1] == "assistant", f"第{i}条附近出现断头轮"


# ---------- 验收 5：embedding 失败降级 ----------

def test_embed_failure_degrades_to_full_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding 异常 → 返回全量 + degraded 标记 + 降级计数（规约验收 5 / 铁律 3）。"""
    from app.services.telemetry import PATH_MEMORY_FILTER_DEGRADED, telemetry

    def _boom(_texts):
        raise RuntimeError("embedding 服务不可用")

    monkeypatch.setattr(hf, "_embed_batch", _boom)
    recorded: list[str] = []
    monkeypatch.setattr(telemetry, "record_fault",
                        lambda path, detail="": recorded.append(path))

    msgs = _mk_turns(["身份", "身份"] + [BOMB] * 2 + ["通用"] * 6)
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    assert res.degraded is True and res.was_filtered is False
    assert res.messages == msgs, "降级必须返回全量，不能返回空/半截历史"
    assert PATH_MEMORY_FILTER_DEGRADED in recorded, "降级必须计数（铁律 3）"


# ---------- 归零线与阈值语义 ----------

def test_min_similarity_floor_zeroes_low_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """低于 min_similarity 直接归零：即使分数 > min_score 也不保留。"""
    monkeypatch.setattr(config_mod.settings, "memory_filter_min_similarity", 0.5)
    # 查询 [0,1,0]；轮 X 与查询 cos=0.6（过线），轮 Y cos=0.3（低于 0.5 归零线）
    monkeypatch.setattr(
        hf, "_embed_batch",
        _fake_embed({"X题": [0.8, 0.6, 0.0], "Y题": [0.95394, 0.3, 0.0], REG: [0, 1, 0]}),
    )

    msgs = _mk_turns(["身份", "身份", "X题", "Y题", "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, REG + "需要什么材料")

    contents = " ".join(m["content"] for m in res.messages)
    assert "X题" in contents, "cos=0.6 ≥ 0.5 应保留"
    assert "Y题" not in contents, "cos=0.3 < 归零线 0.5，即使 > min_score 也必须淘汰"


def test_lowered_floor_lets_borderline_score_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    """归零线降到 0.25 后，cos=0.3 的轮次存活（0.3 ≥ 0.25 且 ≥ min_score 0.2）。"""
    monkeypatch.setattr(config_mod.settings, "memory_filter_min_similarity", 0.25)
    monkeypatch.setattr(
        hf, "_embed_batch",
        _fake_embed({"Y题": [0.95394, 0.3, 0.0], REG: [0, 1, 0]}),
    )

    msgs = _mk_turns(["身份", "身份", "Y题", "通用", "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, REG + "需要什么材料")

    contents = " ".join(m["content"] for m in res.messages)
    assert "Y题" in contents, "归零线之下才淘汰；0.3 ≥ 0.25 应保留"


# ---------- topK 上限 ----------

def test_top_k_caps_kept_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """20 轮全相关：中间 12 轮也只能保留 top_k=6 轮（topK 是上限不是保底）。"""
    monkeypatch.setattr(hf, "_embed_batch", _fake_embed({REG: [0, 1, 0]}))

    msgs = _mk_turns(["身份", "身份"] + [REG] * 12 + ["通用"] * 6)
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    assert res.scored_turns == 12 and res.dropped_turns == 6
    kept = len(res.messages) - 2 * 2 - 6 * 2  # 去掉头尾锚点
    assert kept == 6 * 2, "中间候选最多保留 top_k=6 轮"


# ---------- 锚点保护 ----------

def test_anchor_turns_survive_even_if_irrelevant(monkeypatch: pytest.MonkeyPatch) -> None:
    """头部锚点（炸机话题）与当前查询（登记）无关也必须保留——锚点承载身份。"""
    monkeypatch.setattr(hf, "_embed_batch", _fake_embed({BOMB: [1, 0, 0], REG: [0, 1, 0]}))

    msgs = _mk_turns([BOMB, BOMB, BOMB, BOMB, "通用", "通用", "通用", "通用", "通用", "通用"])
    res = hf.filter_history(msgs, "实名登记需要什么材料")

    contents = " ".join(m["content"] for m in res.messages)
    assert "第0轮炸机问题内容" in contents and "第1轮炸机回答内容" in contents, \
        "头部锚点即使无关也保留（与 M-2 双锚点同一组配置）"
    assert "第2轮炸机问题内容" not in contents, "锚点保护不等于全量保留，中间炸机轮仍被淘汰"


# ---------- 指代消解（复用 P0 判据） ----------

def test_pronoun_query_augmented_with_prev_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """含指代词的查询：打分用查询 = 上一轮 user 消息 + 本轮查询（零 LLM 启发式）。"""
    captured: list[list[str]] = []

    def _capture(texts):
        captured.append(list(texts))
        return [[0.0, 0.0, 1.0] for _ in texts]

    monkeypatch.setattr(hf, "_embed_batch", _capture)

    msgs = _mk_turns(["身份", "身份", "通用", "通用", "通用", "通用", "通用",
                      "通用", "通用", "通用"])
    # 最后一轮（尾部）user 提到 3 号机；当前查询「它最高能飞多高」含指代词
    msgs[-2] = {"role": "user", "content": "3号无人机的参数有哪些"}
    res = hf.filter_history(msgs, "它最高能飞多高")

    assert not res.degraded
    assert captured, "10 轮历史应触发打分"
    scoring_query = captured[0][0]  # 批量里第一条是查询表示
    assert "它最高能飞多高" in scoring_query
    assert "3号无人机的参数" in scoring_query, "指代消解应拼上上一轮 user 消息"


def test_clear_query_not_augmented(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整独立的问题不拼上一轮（判据复用 _needs_llm，无指代不增强）。"""
    captured: list[list[str]] = []

    def _capture(texts):
        captured.append(list(texts))
        return [[0.0, 0.0, 1.0] for _ in texts]

    monkeypatch.setattr(hf, "_embed_batch", _capture)

    msgs = _mk_turns(["身份", "身份", "通用", "通用", "通用", "通用", "通用",
                      "通用", "通用", "通用"])
    hf.filter_history(msgs, "实名登记需要准备哪些材料手续")

    scoring_query = captured[0][0]
    assert scoring_query == "实名登记需要准备哪些材料手续", "独立问题不做指代增强"


# ---------- 向量缓存 ----------

def test_vector_cache_reuses_embeddings_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """同会话第二次过滤：历史轮向量全部命中缓存，只补算查询（省 embedding 成本）。"""
    calls: list[int] = []

    def _counter(texts):
        calls.append(len(texts))
        return [[0.0, 0.0, 1.0] for _ in texts]

    monkeypatch.setattr(hf, "_embed_batch", _counter)

    msgs = _mk_turns(["身份", "身份"] + [REG] * 2 + ["通用"] * 6)
    hf.filter_history(msgs, "实名登记需要什么材料")
    first_total = sum(calls)
    assert first_total == 1 + 2, "首次：查询 + 2 个中间轮"

    hf.filter_history(msgs, "实名登记需要什么材料")
    assert sum(calls) == first_total, "第二次同查询：全部命中缓存，不再调 embedding"


def test_different_query_only_reembeds_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """同历史换查询：只有查询文本未命中，历史轮向量走缓存。"""
    calls: list[int] = []

    def _counter(texts):
        calls.append(len(texts))
        return [[0.0, 0.0, 1.0] for _ in texts]

    monkeypatch.setattr(hf, "_embed_batch", _counter)

    msgs = _mk_turns(["身份", "身份"] + [REG] * 2 + ["通用"] * 6)
    hf.filter_history(msgs, "实名登记需要什么材料")
    hf.filter_history(msgs, "实名登记的办理地点在哪")
    assert calls == [3, 1], "第二次只补算 1 条查询向量"


# ---------- 开关与空查询 ----------

def test_disabled_switch_returns_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_filter_enabled=False → 退回 M-2 行为，不调 embedding。"""
    monkeypatch.setattr(config_mod.settings, "memory_filter_enabled", False)
    monkeypatch.setattr(hf, "_embed_batch", _no_embed)

    msgs = _mk_turns(["身份", "身份"] + [BOMB] * 2 + ["通用"] * 6)
    res = hf.filter_history(msgs, "实名登记需要什么材料")
    assert res.messages == msgs and not res.was_filtered


def test_empty_query_returns_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """空查询（无消息文本的极端请求）→ 原样返回、零 embedding。"""
    monkeypatch.setattr(hf, "_embed_batch", _no_embed)

    msgs = _mk_turns(["通用"] * 10)
    res = hf.filter_history(msgs, "  ")
    assert res.messages == msgs

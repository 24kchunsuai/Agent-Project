"""E-1 版本基线单测（判据 C2：可复现）。

要守住的三条不变式：

1. **唯一版本来源**：任何情况下 `version` 都等于仓库根 `VERSION` 文件的内容，
   不是写死在代码里的字符串（改 VERSION 而代码不动，读数必须跟着变）。
2. **永远答得出**：打戳产物缺失/损坏时回落运行时求值，不让 `/v1/health` 崩或返回空。
3. **如实报未知**：连 VERSION 文件都没有时返回 `unknown`，绝不猜一个像样的版本号。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import build_info
from app.core.build_info import get_build_info, read_version_file
from app.main import app

client = TestClient(app)

#: 与 tests/conftest.py 中注入仓库根的 VERSION 同源
EXPECTED_VERSION = (build_info.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

#: 允许出现的三种 git 语义（与 scripts/stamp_version.py 对齐）
_VALID_COMMIT_PREFIX = ("unborn@", "no-git")


@pytest.fixture(autouse=True)
def _clear_build_info_cache():
    """每个用例前后清缓存——否则 lru_cache 会把上一个用例的 monkeypatch 结果带过来。"""
    get_build_info.cache_clear()
    yield
    get_build_info.cache_clear()


def _missing(path: Path) -> Path:
    """一个确定不存在的路径（在临时目录下，不碰产品目录）。"""
    return path.parent / "__e1_no_such_file__.json"


# --------------------------------------------------------------------------- #
# 1. 唯一版本来源
# --------------------------------------------------------------------------- #
def test_version_comes_from_version_file():
    """无论是打戳产物还是运行时求值，version 都必须等于 VERSION 文件内容。"""
    info = get_build_info()
    assert info.version == EXPECTED_VERSION
    assert info.version != "unknown"
    assert info.source in {"stamp", "runtime"}


def test_read_version_file_reports_unknown_when_missing(monkeypatch):
    """VERSION 文件缺失时如实报 unknown，而不是回落到某个代码里的默认值。"""
    monkeypatch.setattr(build_info, "VERSION_FILE", _missing(build_info.VERSION_FILE))
    assert read_version_file() == "unknown"


# --------------------------------------------------------------------------- #
# 2. 打戳产物与运行时回落
# --------------------------------------------------------------------------- #
def test_stamp_artifact_wins_when_present():
    """有打戳产物时以其为准，且 source 标记为 stamp。"""
    if not build_info.STAMP_FILE.exists():
        pytest.skip("本机尚未打戳（正常：交付构建才会生成）")
    info = get_build_info()
    assert info.source == "stamp"
    assert info.version == EXPECTED_VERSION
    assert info.build_time, "打戳产物必须带 buildTime"


def test_falls_back_to_runtime_when_artifact_missing(monkeypatch):
    """产物缺失 → 运行时求值，version 仍来自 VERSION 文件（非硬编码）。"""
    monkeypatch.setattr(build_info, "STAMP_FILE", _missing(build_info.STAMP_FILE))
    info = get_build_info()
    assert info.source == "runtime"
    assert info.version == EXPECTED_VERSION
    assert info.build_time is None, "未打戳时不应凭空编造 buildTime"


def test_falls_back_when_artifact_corrupted(monkeypatch, tmp_path):
    """产物是坏 JSON / 缺 version 字段 → 同样回落，不让 health 崩。"""
    for payload in ("{ this is not json", json.dumps({"foo": "bar"})):
        bad = tmp_path / "build_info.json"
        bad.write_text(payload, encoding="utf-8")
        monkeypatch.setattr(build_info, "STAMP_FILE", bad)
        get_build_info.cache_clear()
        info = get_build_info()
        assert info.source == "runtime"
        assert info.version == EXPECTED_VERSION


def test_unknown_when_everything_missing(monkeypatch):
    """产物与 VERSION 双双缺失 → unknown（交付包残缺的形态）。"""
    monkeypatch.setattr(build_info, "STAMP_FILE", _missing(build_info.STAMP_FILE))
    monkeypatch.setattr(build_info, "VERSION_FILE", _missing(build_info.VERSION_FILE))
    info = get_build_info()
    assert info.version == "unknown"


# --------------------------------------------------------------------------- #
# 3. git 语义与对外暴露
# --------------------------------------------------------------------------- #
def test_git_commit_has_known_semantics():
    """gitCommit 必须是短 hash，或明确标记 unborn / no-git——不能是空串。"""
    commit = get_build_info().git_commit
    assert commit and commit != "unknown"
    assert len(commit) >= 7 or commit.startswith(_VALID_COMMIT_PREFIX)


def test_health_exposes_build_info():
    """验收标准：/v1/health 能看到 version 与 gitCommit。"""
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 2000

    data = body["data"]
    assert data["version"] == EXPECTED_VERSION
    assert data["gitCommit"]
    assert data["gitBranch"]
    assert "gitDirty" in data
    assert "buildTime" in data
    assert data["versionSource"] in {"stamp", "runtime"}
    # 存活探针的既有字段不能被这次改动挤掉
    assert data["status"] == "up"
    assert data["llmConfigured"] in (True, False)


def test_describe_is_readable():
    """启动日志用的一行摘要不应抛异常，且带上版本号。"""
    desc = get_build_info().describe()
    assert EXPECTED_VERSION in desc

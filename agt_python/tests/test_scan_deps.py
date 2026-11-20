"""E-11 依赖与漏洞扫描单测（企业级落地规约 §4 E-11 验收标准）。

**全部离线**：不联网、不读 `security/` 下的真实快照、不碰产品数据目录
（数据目录由 tests/conftest.py 隔离到 tmp）。

被测对象 `scripts/scan_deps.py` 是**零第三方依赖**的脚本，这里用 importlib 直接加载。

覆盖：
1. **CVSS v3.1 基础分**（用已发布的已知向量对照，含 Log4Shell 10.0）——
   这是"Critical 阻断"能不能成立的地基：分算错，高危就会变低危；
2. **严重度回退链四档**：`database_specific` → GHSA 别名 → CVSS 向量 → UNKNOWN；
   最后一档刻意不阻断，但必须**被标出来**（不能静默消失）；
3. 分级边界（9.0 / 7.0 / 4.0 / 0.0）逐点卡；
4. **豁免台账自身的门禁**：缺字段 / 日期格式错 / 已过期 → 一律判红
   （防"豁免"慢慢变成"永久遗忘"）；
5. `scan()` 的五种结论：Critical 阻断 / Critical 已豁免则放行 / High 只告警 /
   闭包未进快照 → 判"快照过期" / 某端一个包都没采到 → 判覆盖问题；
6. 需求行解析（extras 不能丢）与 PEP 503 包名归一化；
7. Java 构件名解析（含 classifier 的场景）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DEPS = REPO_ROOT / "scripts" / "scan_deps.py"


def _load_scan_deps():
    spec = importlib.util.spec_from_file_location("scan_deps_under_test", _SCAN_DEPS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load_scan_deps()


# ---------------------------------------------------------------- CVSS v3.1

@pytest.mark.parametrize(
    "vector,expected",
    [
        # Log4Shell（CVE-2021-44228）：满分向量，跨 Scope 的公式分支
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
        # 经典 9.8（Scope 不变分支）
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        # 本地利用 + 需要交互 → 7.1（真实取自 accelerate 的 PYSEC 条目）
        ("CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:H", 7.1),
        # 仅可用性受影响 + 高复杂度 → 5.9
        ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:H", 5.9),
    ],
)
def test_cvss31_known_vectors(vector, expected):
    assert sd.cvss31_base_score(vector) == expected


@pytest.mark.parametrize(
    "vector",
    [
        None,
        "",
        "garbage",
        # CVSS v4 向量不在本实现范围内 —— 必须老实返回 None，而不是猜一个分数
        "CVSS:4.0/AV:L/AC:L/AT:N/PR:N/UI:P/VC:H/VI:N/VA:H/SC:N/SI:N/SA:N",
        # 缺字段
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H",
    ],
)
def test_cvss31_unsupported_returns_none(vector):
    assert sd.cvss31_base_score(vector) is None


@pytest.mark.parametrize(
    "score,expected",
    [
        (10.0, "CRITICAL"),
        (9.0, "CRITICAL"),   # 含下界
        (8.9, "HIGH"),
        (7.0, "HIGH"),       # 含下界
        (6.9, "MEDIUM"),
        (4.0, "MEDIUM"),     # 含下界
        (3.9, "LOW"),
        (0.1, "LOW"),
        (0.0, "UNKNOWN"),    # 0 分不是"低危"，是"无影响/未定" —— 不能混进 LOW
    ],
)
def test_score_to_severity_boundaries(score, expected):
    assert sd._score_to_severity(score) == expected


# ------------------------------------------------------- 严重度回退链四档

def test_severity_from_database_specific_wins():
    """GHSA 直接给 severity 时用第一档，不问别名、也不算 CVSS。"""
    sev, src = sd.resolve_severity({"database_specific": {"severity": "MODERATE"}}, {}, lambda _v: {})
    assert sev == "MEDIUM"      # MODERATE 归一化成 MEDIUM
    assert src == "database_specific"


def test_severity_falls_back_to_ghsa_alias():
    """PyPI 的 PYSEC 条目自己没有 severity，但有 GHSA 别名 —— 顺别名取。"""
    detail = {
        "id": "PYSEC-2026-3804",
        "database_specific": {},
        "aliases": ["CVE-2026-69112", "GHSA-4j2p-28q2-5m79"],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:H"}],
    }
    cache: dict = {}
    sev, src = sd.resolve_severity(
        detail, cache, lambda vid: {"database_specific": {"severity": "HIGH"}} if vid == "GHSA-4j2p-28q2-5m79" else {}
    )
    assert sev == "HIGH"
    assert src == "alias:GHSA-4j2p-28q2-5m79"


def test_severity_falls_back_to_cvss_score():
    """没有任何 severity、也没有 GHSA 别名时，自己算 CVSS —— 否则真 Critical 会被降成 UNKNOWN。"""
    detail = {
        "id": "X-1",
        "database_specific": {},
        "aliases": [],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    }
    sev, src = sd.resolve_severity(detail, {}, lambda _v: {})
    assert sev == "CRITICAL"
    assert src == "cvss:9.8"


def test_severity_unknown_is_explicit_not_silent():
    """三档都取不到 → UNKNOWN，且**来源可解释**（这条不阻断，但必须看得见）。"""
    sev, src = sd.resolve_severity({"id": "X-2", "database_specific": {}, "aliases": []}, {}, lambda _v: {})
    assert sev == "UNKNOWN"
    assert src == "unresolved"


# --------------------------------------------------------- 需求行 / 包名解析

@pytest.mark.parametrize(
    "line,name,extras,marker",
    [
        ("uvicorn[standard]>=0.32.0,<1.0", "uvicorn", ["standard"], ""),
        ('tomli; python_version < "3.11"', "tomli", [], 'python_version < "3.11"'),
        ("pymilvus>=2.6.0,<4.0", "pymilvus", [], ""),
        ("# 注释行", "", [], ""),
    ],
)
def test_split_requirement(line, name, extras, marker):
    assert sd._split_requirement(line) == (name, extras, marker)


def test_extras_must_not_be_dropped():
    """`uvicorn[standard]` 的 extras 依赖是真装在运行期里的东西，丢了就是漏扫。"""
    name, extras, _ = sd._split_requirement("uvicorn[standard]>=0.32.0,<1.0")
    assert name == "uvicorn" and extras == ["standard"]


@pytest.mark.parametrize(
    "raw,expected",
    [("Foo_Bar.Baz", "foo-bar-baz"), ("PyYAML", "pyyaml"), ("charset_normalizer", "charset-normalizer")],
)
def test_norm_name_pep503(raw, expected):
    assert sd._norm_name(raw) == expected


@pytest.mark.parametrize(
    "filename,artifact,version",
    [
        ("jackson-databind-2.17.2", "jackson-databind", "2.17.2"),
        ("tomcat-embed-core-10.1.60", "tomcat-embed-core", "10.1.60"),
        # 带 classifier 的场景（fat jar 里真实存在）
        ("netty-transport-native-epoll-4.1.100.Final-linux-x86_64", "netty-transport-native-epoll", "4.1.100.Final"),
    ],
)
def test_java_jar_name_parse(filename, artifact, version):
    m = sd._JAVA_VERSION_RE.match(filename)
    assert m and m.group("artifact") == artifact and m.group("version") == version


# ------------------------------------------------------------ 豁免台账门禁

def _write_allowlist(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "deps-allowlist.json"
    p.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    return p


def _valid_entry(**over):
    e = {
        "id": "GHSA-xxxx-yyyy-zzzz",
        "package": "PyPI:foo@1.0",
        "reason": "本系统未启用相关特性，不可达",
        "disposition": "接受风险 + 90 天后复评",
        "owner": "owner@example.com",
        "expiresAt": "2099-01-01",
    }
    e.update(over)
    return e


def test_valid_allowlist_entry_has_no_problems(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry()]))
    entries, problems = sd.load_allowlist()
    assert len(entries) == 1 and problems == []


@pytest.mark.parametrize("drop", ["id", "package", "reason", "disposition", "owner", "expiresAt"])
def test_allowlist_missing_any_required_field_is_flagged(tmp_path, monkeypatch, drop):
    """缺任一必填字段 → 判红。写得残缺的豁免看上去像"已处置"，比没有豁免更危险。"""
    entry = _valid_entry()
    entry.pop(drop)
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [entry]))
    _entries, problems = sd.load_allowlist()
    assert problems and drop in problems[0]


def test_allowlist_expired_entry_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(expiresAt="2020-01-01")]))
    _entries, problems = sd.load_allowlist()
    assert problems and "过期" in problems[0]


def test_allowlist_bad_date_format_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(expiresAt="2099/01/01")]))
    _entries, problems = sd.load_allowlist()
    assert problems and "YYYY-MM-DD" in problems[0]


def test_allowlist_unknown_severity_value_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(severity="SCARY")]))
    _entries, problems = sd.load_allowlist()
    assert problems and "severity" in problems[0]


def test_missing_allowlist_file_is_not_an_error(tmp_path, monkeypatch):
    """没有台账文件 = 没有豁免，不是错误（否则新拉一份仓库就跑不过门禁）。"""
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", tmp_path / "nope.json")
    entries, problems = sd.load_allowlist()
    assert entries == [] and problems == []


# ------------------------------------------------------------- scan() 结论

def _end(eco, name, version):
    return {"source": "test", "packages": [{"ecosystem": eco, "name": name, "version": version}],
            "unresolved": [], "problems": []}


PY = ("PyPI", "pytest", "9.1.1")
JAVA = ("Maven", "org.example:demo", "1.0.0")
NPM = ("npm", "lodash", "4.17.21")


def _closure(py=PY, java=JAVA, npm=NPM, empty_end=None):
    ends = {"python": _end(*py), "java": _end(*java), "npm": _end(*npm)}
    if empty_end:
        ends[empty_end]["packages"] = []
    return {"generatedAt": "t", "version": "1.0.0", "expectedEnds": sd.EXPECTED_ENDS, "ends": ends}


def _snapshot(rows_by_pkg: dict[tuple, list], *, skip: tuple | None = None):
    queried, findings = [], {}
    for pkg in (PY, JAVA, NPM):
        if pkg == skip:
            continue
        key = sd._pkg_key(*pkg)
        queried.append(key)
        rows = rows_by_pkg.get(pkg) or []
        if rows:
            findings[key] = [
                {"id": i, "severity": s, "severitySource": "test", "summary": "sum", "aliases": []}
                for i, s in rows
            ]
    return {"generatedAt": "2026-01-01T00:00:00+08:00", "source": "osv", "queried": queried, "findings": findings}


def _no_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", tmp_path / "absent.json")


def test_critical_blocks(tmp_path, monkeypatch):
    _no_allowlist(tmp_path, monkeypatch)
    result = sd.scan(_closure(), _snapshot({JAVA: [("GHSA-crit-0001", "CRITICAL")]}))
    assert result["exitCode"] == sd.EXIT_BLOCK
    assert len(result["blocking"]) == 1


def test_high_is_advisory_only(tmp_path, monkeypatch):
    """用户 2026-09-24 决策：High 只告警，不阻断。"""
    _no_allowlist(tmp_path, monkeypatch)
    result = sd.scan(_closure(), _snapshot({PY: [("GHSA-high-0001", "HIGH")]}))
    assert result["exitCode"] == sd.EXIT_PASS
    assert len(result["advisory"]) == 1 and result["blocking"] == []


def test_unknown_severity_does_not_block_but_is_listed(tmp_path, monkeypatch):
    """定不了级不进阻断（否则门禁长期红着必被绕过），但必须出现在报告里。"""
    _no_allowlist(tmp_path, monkeypatch)
    result = sd.scan(_closure(), _snapshot({NPM: [("X-unknown", "UNKNOWN")]}))
    assert result["exitCode"] == sd.EXIT_PASS
    assert len(result["info"]) == 1


def test_waived_critical_passes_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(id="GHSA-crit-0001", package="Maven:org.example:demo@1.0.0")])
    )
    result = sd.scan(_closure(), _snapshot({JAVA: [("GHSA-crit-0001", "CRITICAL")]}))
    assert result["exitCode"] == sd.EXIT_PASS
    assert len(result["waived"]) == 1 and result["blocking"] == []
    assert result["waived"][0]["waiveOwner"] == "owner@example.com"


def test_ledger_violation_blocks_even_without_any_critical(tmp_path, monkeypatch):
    """台账违规**自己**就要判红 —— 与有没有 Critical 无关。"""
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(expiresAt="2020-01-01")]))
    result = sd.scan(_closure(), _snapshot({}))
    assert result["ledgerProblems"]
    assert result["exitCode"] == sd.EXIT_BLOCK


def test_package_missing_from_snapshot_is_a_snapshot_problem(tmp_path, monkeypatch):
    """新依赖没进快照 → 判"快照过期"（exit 2），而不是当成安全放过。"""
    _no_allowlist(tmp_path, monkeypatch)
    result = sd.scan(_closure(), _snapshot({}, skip=JAVA))
    assert result["exitCode"] == sd.EXIT_SNAPSHOT
    assert any("未在漏洞快照中" in d for d in result["drift"])


def test_end_with_zero_packages_is_a_coverage_problem(tmp_path, monkeypatch):
    """某端一个包都没采到 → 覆盖问题（多半是清单缺失/解析失败），不能静默当"这端没依赖"。"""
    _no_allowlist(tmp_path, monkeypatch)
    result = sd.scan(_closure(empty_end="npm"), _snapshot({}))
    assert result["exitCode"] == sd.EXIT_SNAPSHOT
    assert any("一个依赖都没采集到" in d for d in result["drift"])


def test_dangling_waiver_is_reported_but_not_blocking(tmp_path, monkeypatch):
    """漏洞已被修复后，台账里那条豁免就成了"悬空"——提示清理，但不阻断（这是好事）。"""
    monkeypatch.setattr(sd, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [_valid_entry(id="GHSA-gone-0001")]))
    result = sd.scan(_closure(), _snapshot({}))
    assert result["exitCode"] == sd.EXIT_PASS
    assert result["danglingWaivers"]


def test_unresolved_packages_are_surfaced_not_blocking(tmp_path, monkeypatch):
    _no_allowlist(tmp_path, monkeypatch)
    closure = _closure()
    closure["ends"]["python"]["unresolved"] = [{"name": "uvloop", "reason": "当前环境未安装"}]
    result = sd.scan(closure, _snapshot({}))
    assert result["exitCode"] == sd.EXIT_PASS
    assert any("uvloop" in u for u in result["unresolved"])


# ------------------------------------------------------------- 真实快照可读

def test_repo_snapshots_are_valid_json():
    """仓库里的快照必须能被解析 —— 它是 CI 离线跑得动的前提。"""
    for name in ("sbom.json", "vuln-snapshot.json", "deps-allowlist.json"):
        p = REPO_ROOT / "security" / name
        assert p.exists(), f"缺少 security/{name}（需先跑 scripts/scan_deps.py --refresh）"
        assert isinstance(json.loads(p.read_text(encoding="utf-8")), dict)

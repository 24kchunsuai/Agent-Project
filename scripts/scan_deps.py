#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖与漏洞扫描（企业级落地规约 E-11，判据 C1 可交付）。

设计取舍（每一条都是实测逼出来的，不是偏好）
================================================

1. **零第三方依赖（只用 stdlib）**
   门禁工具本身跑不起来，比没有门禁更糟。本机实测 `pip-audit` / `safety` /
   `cyclonedx` **一个都没装**；装它们就意味着 CI 多一个"装不上就红"的失败点。

2. **一套接口扫三端**
   OSV（`api.osv.dev`）实测覆盖 PyPI / npm / **Maven** 三生态。于是不必分别上
   pip-audit + npm audit + OWASP dependency-check（后者还要下载 NVD 库 + 申请 API key）。

3. **默认离线，`--refresh` 才联网**
   CI 断网也必须能跑、且不能因为断网就变红。漏洞库以**快照**形式随仓库走
   （`security/vuln-snapshot.json`），报告里始终标出快照时间戳——不静默用旧数据。

4. **扫"依赖闭包"，不扫"解释器环境"**
   实测：共享 conda 环境 77 个包里 15 条漏洞命中，**全部来自与本项目无关的
   jupyter 生态**（accelerate / Django / sqlparse / pip 本身）。扫环境＝大量误报。
   所以三端都只扫**运行期闭包**：
     Python：从 `requirements.txt` 的直接依赖出发，用 `importlib.metadata` 走传递闭包
     Java  ：Spring Boot fat jar 的 `BOOT-INF/lib/*.jar`（运行期真实闭包）
     前端  ：`package-lock.json` 里 **dev ≠ true** 的包（对齐规约的 `--omit=dev`）

5. **分级：Critical 阻断 / High 告警**（用户 2026-09-24 定的阈值）
   Critical 未处置 → 退出码 1；High 只告警不阻断；MEDIUM/LOW/UNKNOWN 进报告。

6. **豁免台账本身也有门禁**
   豁免条目必须写全 id / 理由 / 处置 / 责任人 / **到期日**，缺任一项或已过期
   **仍然判红**。否则"豁免"会慢慢变成"永久遗忘"，而台账会给人已处置的错觉。

退出码
------
  0  通过（可能带 High/Medium 告警，或 Critical 已有效豁免）
  1  阻断：存在未豁免的 Critical，或豁免台账自身违规
  2  快照/覆盖问题：闭包与快照不一致、端点未采集到依赖等（**需先跑 --refresh**）

用法
----
  python scripts/scan_deps.py                  # 离线扫描（CI 用）
  python scripts/scan_deps.py --refresh        # 联网刷新闭包 + 漏洞快照 + 许可证，再扫描
  python scripts/scan_deps.py --json           # 机器可读结果
  python scripts/scan_deps.py --report P.md    # 额外写一份 markdown 报告
  python scripts/scan_deps.py --check-only     # 只做覆盖/漂移一致性检查
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

# Windows 控制台默认可能是 GBK，中文报告会乱码（preflight.py 踩过同一个坑）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
SECURITY_DIR = ROOT / "security"

SBOM_PATH = SECURITY_DIR / "sbom.json"
VULN_PATH = SECURITY_DIR / "vuln-snapshot.json"
LICENSE_PATH = SECURITY_DIR / "licenses.json"
ALLOWLIST_PATH = SECURITY_DIR / "deps-allowlist.json"

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
NPM_JSON = "https://registry.npmjs.org/{name}/{version}"

#: 阻断阈值（用户 2026-09-24 决策：Critical 阻断、High 告警）
BLOCKING_SEVERITIES = {"CRITICAL"}
#: 只告警不阻断
ADVISORY_SEVERITIES = {"HIGH"}

HTTP_TIMEOUT = 20
EXIT_PASS, EXIT_BLOCK, EXIT_SNAPSHOT = 0, 1, 2

#: 三端各自的"依赖来源"，供覆盖自检使用。
#: 加新的一端时**必须**同时改这里和 collect_* 采集函数——否则覆盖自检会红，
#: 这正是它存在的意义（防"只扫了 Python，忘了 Java/前端"）。
EXPECTED_ENDS = {
    "python": "agt_python/requirements.txt",
    "java": "agt_backend/target/agt_backend.jar（Spring Boot fat jar 的 BOOT-INF/lib）",
    "npm": "agt_web/package-lock.json（dev != true 的运行期闭包）",
}


# ============================================================================
# 工具
# ============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg_key(ecosystem: str, name: str, version: str) -> str:
    """快照里的稳定键：`PyPI:fastapi@0.140.13` / `Maven:g:a@1.2.3` / `npm:lodash@4.17.20`。"""
    return f"{ecosystem}:{name}@{version}"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[scan_deps] {path.relative_to(ROOT)} 不是合法 JSON：{exc}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _http_json(url: str, payload: dict | None = None, timeout: int = HTTP_TIMEOUT, retries: int = 2) -> dict:
    """带重试的 JSON 请求。

    为什么必须重试：`--refresh` 要对 145 个包逐个取许可证，一个 20s 超时
    （实测 marked 就抖了一次）会让那条许可证永久变成 UNKNOWN，
    而 UNKNOWN 是**不会再自动修正**的——除非有人注意到并重跑。
    """
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "drone-agent001-scan-deps"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except OSError as exc:  # URLError / TimeoutError / socket 错都归这里
            last = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    assert last is not None
    raise last


# ============================================================================
# 采集一：Python 闭包（走传递闭包，滤掉 extras）
# ============================================================================

def _norm_name(name: str) -> str:
    """PEP 503 归一化：`Foo_Bar.Baz` → `foo-bar-baz`（否则同一个包会被数两次）。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _split_requirement(req: str) -> tuple[str, list[str], str]:
    """把 `uvicorn[standard]>=0.32.0,<1.0 ; python_version >= "3.8"` 拆成 (名字, extras, 标记原文)。"""
    body, _, marker = req.partition(";")
    m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:\[([^\]]*)\])?", body.strip())
    if not m:
        return "", [], marker.strip()
    extras = [e.strip() for e in (m.group(2) or "").split(",") if e.strip()]
    return m.group(1), extras, marker.strip()


def _requirement_roots() -> tuple[list[tuple[str, list[str]]], list[str]]:
    """从 requirements.txt 取直接依赖，**连 extras 一起取**。

    ⚠️ extras 不能丢：`uvicorn[standard]` 会真的把 uvloop / httptools / watchfiles
    装进运行期，忽略 extras 就是**漏扫**（这些包同样会出漏洞）。
    """
    req = ROOT / "agt_python" / "requirements.txt"
    roots: list[tuple[str, list[str]]] = []
    bad: list[str] = []
    if not req.exists():
        return roots, [f"{req.relative_to(ROOT)} 不存在"]
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, extras, _marker = _split_requirement(line)
        if name:
            roots.append((name, extras))
        else:
            bad.append(line)
    return roots, bad


def collect_python() -> dict:
    """Python 运行期闭包。

    三个容易踩的点：
    * `dist.requires` 会把 **extras**（`; extra == "model"`）一起列出来，
      不过滤就会把 sphinx / tox / jupyter 全拉进来（实测 36 → 248 个包）；
      但**声明里写了 extras 的就要按 extras 展开**（见 `_requirement_roots`）。
    * 环境标记不成立的包（如 `tomli; python_version < "3.11"`、Windows 上的 uvloop）
      在当前环境**没装**——记进 unresolved 并说明，**不算漏扫**。
    * 先发现、后补 extras 的情况要能二次展开：用「签名变化即重展开」的循环，
      而不是一次性遍历（否则 `A` 先被无 extras 展开，后面 `B[A]` 要求的 extras 就丢了）。
    """
    import importlib.metadata as md

    roots, bad_lines = _requirement_roots()
    seen: dict[str, str | None] = {}
    requested: dict[str, set[str]] = {}
    signature: dict[str, tuple[str, ...]] = {}

    queue: list[tuple[str, list[str]]] = list(roots)
    while queue:
        name, extras = queue.pop(0)
        key = _norm_name(name)
        bucket = requested.setdefault(key, set())
        bucket.update(extras)

        if key not in seen:
            try:
                dist = md.distribution(name)
            except md.PackageNotFoundError:
                seen[key] = None
                continue
            seen[key] = dist.version
        elif seen[key] is None:
            continue
        else:
            try:
                dist = md.distribution(name)
            except md.PackageNotFoundError:
                continue

        sig = tuple(sorted(bucket))
        if signature.get(key) == sig:
            continue
        signature[key] = sig

        for req in dist.requires or []:
            dep_name, dep_extras, marker = _split_requirement(req)
            if not dep_name:
                continue
            m = re.search(r"""extra\s*==\s*['"]([^'"]+)['"]""", marker)
            if m and m.group(1) not in bucket:
                continue
            queue.append((dep_name, dep_extras))

    packages = [
        {"ecosystem": "PyPI", "name": n, "version": v} for n, v in sorted(seen.items()) if v is not None
    ]
    unresolved = [
        {
            "name": n,
            "reason": "当前环境未安装（环境标记不成立：平台/解释器版本，或可选 extra 未启用）",
        }
        for n, v in sorted(seen.items())
        if v is None
    ]
    return {
        "source": EXPECTED_ENDS["python"],
        "direct": sorted({_norm_name(n) for n, _e in roots}),
        "packages": packages,
        "unresolved": unresolved,
        "problems": bad_lines,
    }


# ============================================================================
# 采集二：Java 闭包（Spring Boot fat jar + 本地 .m2 反查 groupId）
# ============================================================================

_JAVA_VERSION_RE = re.compile(r"^(?P<artifact>.+?)-(?P<version>\d[^-]*(?:[.\-][^-]*)*?)(?:-(?P<classifier>.+))?$")


def _m2_index() -> dict[str, tuple[str, str, str]]:
    """扫一次本地 .m2，建 `文件名 → (groupId, artifactId, version)` 索引。

    fat jar 的 `BOOT-INF/lib/*.jar` 里**只有 artifactId 和版本，没有 groupId**，
    而 OSV 的 Maven 生态要求 `groupId:artifactId`。`.m2` 的目录结构
    （`<group 路径>/<artifact>/<version>/<artifact>-<version>.jar`）恰好补上这一格，
    且完全离线。实测可行：knife4j-core-4.4.0 → com.github.xiaoymin。
    """
    index: dict[str, tuple[str, str, str]] = {}
    repo = Path.home() / ".m2" / "repository"
    if not repo.is_dir():
        return index
    for dirpath, _dirnames, filenames in os.walk(repo):
        version_dir = Path(dirpath)
        try:
            parts = version_dir.relative_to(repo).parts
        except ValueError:
            continue
        # 至少要 <group 路径…>/<artifact>/<version> 三段；浅于三段的目录不是构件目录
        # （`.m2/repository/` 根、`.../repository/org/` 这类越界访问会让 relative_to 抛错）
        if len(parts) < 3:
            continue
        group = ".".join(parts[:-2])
        artifact, version = parts[-2], parts[-1]
        if not group:
            continue
        for fn in filenames:
            if fn.endswith(".jar") and fn not in index:
                index[fn] = (group, artifact, version)
    return index


def _fat_jar() -> Path | None:
    candidates = sorted((ROOT / "agt_backend" / "target").glob("*.jar")) if (ROOT / "agt_backend" / "target").is_dir() else []
    # 排除 *-sources.jar / *-javadoc.jar / *.original（原始 jar 不含 BOOT-INF/lib）
    for p in candidates:
        if p.name.endswith((".original", "-sources.jar", "-javadoc.jar")):
            continue
        return p
    return None


def collect_java() -> dict:
    """Java 运行期闭包 = fat jar 的 `BOOT-INF/lib`。"""
    jar = _fat_jar()
    problems: list[str] = []
    if jar is None:
        return {
            "source": EXPECTED_ENDS["java"],
            "jar": None,
            "packages": [],
            "unresolved": [],
            "problems": ["未找到后端产物 jar —— 先构建：bash scripts/ci.sh java 只编译；需 mvn package 才产出 fat jar"],
        }

    names: list[str] = []
    with zipfile.ZipFile(jar) as zf:
        for entry in zf.namelist():
            if entry.startswith("BOOT-INF/lib/") and entry.endswith(".jar"):
                names.append(Path(entry).name)

    index = _m2_index()
    packages, unresolved = [], []
    for fn in sorted(set(names)):
        hit = index.get(fn)
        if hit:
            group, artifact, version = hit
            packages.append({"ecosystem": "Maven", "name": f"{group}:{artifact}", "version": version})
            continue
        m = _JAVA_VERSION_RE.match(fn[: -len(".jar")])
        unresolved.append(
            {
                "name": fn,
                "reason": "本地 .m2 中未找到该 jar，无法确定 groupId（OSV 的 Maven 生态要求 groupId:artifactId）",
                "guess": f"{m.group('artifact')}@{m.group('version')}" if m else None,
            }
        )
    return {
        "source": EXPECTED_ENDS["java"],
        "jar": str(jar.relative_to(ROOT)),
        "jarSha256": _sha256_file(jar),
        "pomSha256": _sha256_file(ROOT / "agt_backend" / "pom.xml")
        if (ROOT / "agt_backend" / "pom.xml").exists()
        else "",
        "packages": packages,
        "unresolved": unresolved,
        "problems": problems,
    }


# ============================================================================
# 采集三：前端闭包（package-lock.json，dev != true）
# ============================================================================

def collect_npm() -> dict:
    lock = ROOT / "agt_web" / "package-lock.json"
    if not lock.exists():
        return {
            "source": EXPECTED_ENDS["npm"],
            "packages": [],
            "unresolved": [],
            "problems": [f"{lock.relative_to(ROOT)} 不存在"],
        }
    data = _read_json(lock)
    nested = data.get("packages")
    problems: list[str] = []
    packages = []
    if isinstance(nested, dict):  # lockfileVersion 2 / 3
        for path, meta in nested.items():
            if not path.startswith("node_modules/") or not isinstance(meta, dict):
                continue
            if meta.get("dev") or meta.get("optional"):
                continue
            version = meta.get("version")
            if not version:
                continue
            name = path.split("node_modules/")[-1]
            packages.append({"ecosystem": "npm", "name": name, "version": version})
    else:  # lockfileVersion 1
        def walk(deps: dict) -> None:
            for name, meta in (deps or {}).items():
                if not isinstance(meta, dict):
                    continue
                if not meta.get("dev") and meta.get("version"):
                    packages.append({"ecosystem": "npm", "name": name, "version": meta["version"]})
                walk(meta.get("dependencies") or {})

        walk(data.get("dependencies") or {})
        problems.append("package-lock.json 是 v1 结构（无 packages 字段），已按 dependencies 树回退解析")
    return {
        "source": EXPECTED_ENDS["npm"],
        "packages": packages,
        "unresolved": [],
        "problems": problems,
    }


def collect_all() -> dict:
    ends = {"python": collect_python(), "java": collect_java(), "npm": collect_npm()}
    return {
        "generatedAt": _now(),
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "",
        "expectedEnds": EXPECTED_ENDS,
        "ends": ends,
    }


# ============================================================================
# CVSS v3.1 基础分（用于 severity 回退链的最后一环）
# ============================================================================

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
_AC = {"L": 0.77, "H": 0.44}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.50}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def cvss31_base_score(vector: str) -> float | None:
    """按 CVSS v3.1 规范算基础分。算不出（格式不认识）返回 None。

    为什么值得自己实现：PYSEC 条目**没有** `database_specific.severity`，
    只给 CVSS 向量。若直接标 UNKNOWN，一个真正的 Critical 就会被悄悄降级成
    "告警"——那样"Critical 阻断"这条约定就是纸面上的。
    已知校验点：`AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H` → 10.0（Log4Shell）。
    """
    if not vector or not vector.startswith(("CVSS:3.1/", "CVSS:3.0/")):
        return None
    parts = {}
    for seg in vector.split("/")[1:]:
        if ":" in seg:
            k, v = seg.split(":", 1)
            parts[k] = v
    try:
        scope_changed = parts["S"] == "C"
        isc = 1 - (1 - _CIA[parts["C"]]) * (1 - _CIA[parts["I"]]) * (1 - _CIA[parts["A"]])
        impact = (
            7.52 * (isc - 0.029) - 3.25 * (isc - 0.02) ** 15 if scope_changed else 6.42 * isc
        )
        if impact <= 0:
            return 0.0
        pr = (_PR_C if scope_changed else _PR_U)[parts["PR"]]
        expl = 8.22 * _AV[parts["AV"]] * _AC[parts["AC"]] * pr * _UI[parts["UI"]]
        raw = min((1.08 * (impact + expl)) if scope_changed else (impact + expl), 10.0)
        return math.ceil(round(raw, 6) * 10) / 10
    except KeyError:
        return None


def _score_to_severity(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "UNKNOWN"


#: OSV/GHSA 的措辞 → 本脚本的五档
_SEV_ALIAS = {"MODERATE": "MEDIUM", "MEDIUM": "MEDIUM", "IMPORTANT": "HIGH"}


def _canon_severity(text: str | None) -> str | None:
    if not text:
        return None
    up = str(text).strip().upper()
    up = _SEV_ALIAS.get(up, up)
    return up if up in {"CRITICAL", "HIGH", "MEDIUM", "LOW"} else None


def resolve_severity(detail: dict, cache: dict, fetch_detail) -> tuple[str, str]:
    """定级回退链，返回 `(severity, 来源说明)`。

    ① `database_specific.severity`（GHSA 直接给 CRITICAL/HIGH/MODERATE/LOW）
    ② 顺着 `aliases` 找 GHSA 条目，用它那一份定级（PyPI 的 PYSEC 条目走这条）
    ③ 用 CVSS v3.1 向量自己算基础分（前两条都拿不到时）
    ④ 仍拿不到 → UNKNOWN（**进报告并显式标注，不静默**）

    第 ④ 档刻意不阻断：把"定不了级"当成 Critical 会让门禁长期红着、然后被人
    绕过；当成"安全"又会在报告里消失。所以只标出来，并计入 UNKNOWN 数量。
    """
    sev = _canon_severity(detail.get("database_specific", {}).get("severity"))
    if sev:
        return sev, "database_specific"

    for alias in detail.get("aliases") or []:
        if not str(alias).startswith("GHSA-"):
            continue
        other = cache.get(alias) or fetch_detail(alias)
        cache[alias] = other
        sev = _canon_severity((other or {}).get("database_specific", {}).get("severity"))
        if sev:
            return sev, f"alias:{alias}"

    for entry in detail.get("severity") or []:
        score = cvss31_base_score(str(entry.get("score") or ""))
        if score is not None:
            return _score_to_severity(score), f"cvss:{score}"

    return "UNKNOWN", "unresolved"


# ============================================================================
# 联网刷新：漏洞快照 + 许可证
# ============================================================================

def refresh_vuln_snapshot(closure: dict) -> dict:
    pairs: list[tuple[str, str, str]] = []
    for end in closure["ends"].values():
        for pkg in end["packages"]:
            pairs.append((pkg["ecosystem"], pkg["name"], pkg["version"]))

    batch = [{"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v in pairs]
    results: list[dict] = []
    for i in range(0, len(batch), 200):
        resp = _http_json(OSV_BATCH, {"queries": batch[i : i + 200]})
        results.extend(resp.get("results") or [])

    detail_cache: dict[str, dict] = {}

    def fetch_detail(vid: str) -> dict:
        try:
            return _http_json(OSV_VULN + urllib.parse.quote(vid, safe=""))
        except urllib.error.HTTPError:
            return {}
        except urllib.error.URLError:
            return {}

    findings: dict[str, list[dict]] = {}
    for (eco, name, ver), res in zip(pairs, results):
        vulns = res.get("vulns") or []
        if not vulns:
            continue
        key = _pkg_key(eco, name, ver)
        rows = []
        for v in vulns:
            vid = v.get("id")
            if not vid:
                continue
            detail = detail_cache.get(vid) or fetch_detail(vid)
            detail_cache[vid] = detail
            sev, src = resolve_severity(detail, detail_cache, fetch_detail)
            rows.append(
                {
                    "id": vid,
                    "severity": sev,
                    "severitySource": src,
                    "summary": (detail.get("summary") or v.get("summary") or "")[:200],
                    "aliases": (detail.get("aliases") or [])[:5],
                }
            )
        findings[key] = sorted(rows, key=lambda r: (r["severity"], r["id"]))

    return {
        "generatedAt": _now(),
        "source": OSV_BATCH,
        "queried": [_pkg_key(e, n, v) for e, n, v in pairs],
        "findings": findings,
    }


def refresh_licenses(closure: dict) -> dict:
    """许可证清单（企业采购会问）。

    来源：PyPI JSON API / npm registry / **本地 .m2 的 POM**（Java 走离线，不联网）。
    """
    out: dict[str, dict] = {}
    for end in closure["ends"].values():
        for pkg in end["packages"]:
            eco, name, ver = pkg["ecosystem"], pkg["name"], pkg["version"]
            key = _pkg_key(eco, name, ver)
            try:
                if eco == "PyPI":
                    info = (_http_json(PYPI_JSON.format(name=name, version=ver)) or {}).get("info") or {}
                    # 回退链：license_expression（PEP 639，现代包只填这个）→ license（老包，
                    # 有时是整段法律文本）→ classifiers 里的 License 项。
                    # 实测 fastapi / pydantic / pytest 的 `license` 都是 None，
                    # 只有 license_expression 有值——不补这一档，"license 可查"就是空话。
                    lic = (info.get("license_expression") or "").strip()
                    if not lic:
                        raw = (info.get("license") or "").strip()
                        lic = raw if 0 < len(raw) <= 120 else ""
                    if not lic:
                        for c in info.get("classifiers") or []:
                            if c.startswith("License ::"):
                                lic = c.split("::")[-1].strip()
                                break
                    out[key] = {"license": lic or "UNKNOWN", "source": "pypi.org"}
                elif eco == "npm":
                    meta = _http_json(NPM_JSON.format(name=name, version=ver)) or {}
                    lic = meta.get("license") or meta.get("licenses") or "UNKNOWN"
                    if isinstance(lic, list):
                        lic = ", ".join(str(x.get("type", x)) for x in lic)
                    out[key] = {"license": str(lic), "source": "registry.npmjs.org"}
                else:
                    out[key] = {"license": _maven_license(name, ver), "source": "local .m2 POM"}
            except Exception as exc:  # 单个包拿不到不该让整轮刷新失败
                out[key] = {"license": "UNKNOWN", "source": f"取用失败：{type(exc).__name__}"}
    return {"generatedAt": _now(), "licenses": out}


def _maven_license(ga: str, version: str, _depth: int = 0) -> str:
    """从本地 .m2 的 POM 读许可证（离线）。

    ⚠️ 必须**沿父 POM 回退**：大量构件的 POM 里根本没有 `<licenses>`——
    许可证写在父 POM（实测 commons-lang3 → commons-parent、
    logback-classic → logback-parent、hibernate-validator → hibernate-validator-parent）。
    只要自己那一层就有一半构件查不到许可证。
    """
    group, _, artifact = ga.partition(":")
    pom = (
        Path.home() / ".m2" / "repository" / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.pom"
    )
    if not pom.exists() or _depth > 4:
        return "UNKNOWN"
    text = pom.read_text(encoding="utf-8", errors="replace")
    names = re.findall(r"<license>\s*.*?<name>\s*([^<]+?)\s*</name>", text, flags=re.S)
    if names:
        return " / ".join(dict.fromkeys(names))
    parent = re.search(
        r"<parent>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>\s*<version>([^<]+)</version>",
        text,
    )
    if parent:
        return _maven_license(f"{parent.group(1)}:{parent.group(2)}", parent.group(3), _depth + 1)
    return "UNKNOWN"


# ============================================================================
# 豁免台账
# ============================================================================

ALLOW_REQUIRED = ("id", "package", "reason", "disposition", "owner", "expiresAt")


def load_allowlist() -> tuple[list[dict], list[str]]:
    """读豁免台账，返回 (有效条目, 台账自身的问题)。

    ⚠️ 台账问题会让扫描**判红**（不是警告）：一条写得残缺的豁免，
    看上去像"已处置"，实则等于没有——这种错觉比没有豁免更危险。
    """
    if not ALLOWLIST_PATH.exists():
        return [], []
    data = _read_json(ALLOWLIST_PATH)
    entries = data.get("entries") or []
    problems: list[str] = []
    today = date.today()
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            problems.append(f"第 {i} 条不是对象")
            continue
        missing = [f for f in ALLOW_REQUIRED if not str(e.get(f) or "").strip()]
        if missing:
            problems.append(f"第 {i} 条（{e.get('id') or '无 id'}）缺字段：{', '.join(missing)}")
            continue
        try:
            exp = date.fromisoformat(str(e["expiresAt"]).strip())
        except ValueError:
            problems.append(f"第 {i} 条（{e['id']}）expiresAt 不是 YYYY-MM-DD：{e['expiresAt']!r}")
            continue
        if exp < today:
            problems.append(f"第 {i} 条（{e['id']}）豁免已于 {exp.isoformat()} 过期 —— 请处置后删除该条")
        if _canon_severity(e.get("severity")) is None and e.get("severity"):
            problems.append(f"第 {i} 条（{e['id']}）severity 取值不认识：{e['severity']!r}")
    return entries, problems


def apply_allowlist(findings: list[dict], entries: list[dict]) -> None:
    """给每条 finding 打上豁免标记（原地修改）。"""
    by_id = {str(e.get("id")): e for e in entries if isinstance(e, dict) and e.get("id")}
    for f in findings:
        e = by_id.get(f["id"])
        if not e:
            continue
        if e.get("package") and e["package"] not in (f["key"], f["name"]):
            continue
        f["waived"] = True
        f["waiveReason"] = e.get("reason")
        f["waiveOwner"] = e.get("owner")
        f["waiveExpiresAt"] = e.get("expiresAt")


# ============================================================================
# 扫描主体
# ============================================================================

def scan(closure: dict, snapshot: dict) -> dict:
    queried = set(snapshot.get("queried") or [])
    findings: list[dict] = []
    drift: list[str] = []
    unresolved: list[str] = []

    for end_name, end in closure["ends"].items():
        if not end["packages"]:
            drift.append(
                f"{end_name} 端一个依赖都没采集到（来源：{end['source']}）"
                " —— 多半是清单文件缺失或解析失败，而不是「这端没有依赖」"
            )
        for pkg in end["packages"]:
            key = _pkg_key(pkg["ecosystem"], pkg["name"], pkg["version"])
            if key not in queried:
                drift.append(f"{key} 未在漏洞快照中（新依赖或版本变了）")
                continue
            for row in (snapshot.get("findings") or {}).get(key, []):
                findings.append({**row, "key": key, "name": pkg["name"], "version": pkg["version"], "end": end_name})
        for item in end.get("unresolved") or []:
            unresolved.append(f"{end_name}: {item.get('name')} —— {item.get('reason')}")
        for p in end.get("problems") or []:
            drift.append(f"{end_name} 端采集告警：{p}")

    entries, ledger_problems = load_allowlist()
    apply_allowlist(findings, entries)

    known = {f["id"] for f in findings}
    dangling = [
        f"{e.get('id')}（{e.get('package')}）"
        for e in entries
        if isinstance(e, dict) and e.get("id") and e["id"] not in known
    ]

    blocking = [f for f in findings if f["severity"] in BLOCKING_SEVERITIES and not f.get("waived")]
    advisory = [f for f in findings if f["severity"] in ADVISORY_SEVERITIES and not f.get("waived")]
    info = [
        f
        for f in findings
        if f["severity"] not in BLOCKING_SEVERITIES | ADVISORY_SEVERITIES and not f.get("waived")
    ]
    waived = [f for f in findings if f.get("waived")]

    if ledger_problems or blocking:
        code = EXIT_BLOCK
    elif drift:
        code = EXIT_SNAPSHOT
    else:
        code = EXIT_PASS

    return {
        "exitCode": code,
        "snapshotGeneratedAt": snapshot.get("generatedAt"),
        "counts": {
            "packages": sum(len(e["packages"]) for e in closure["ends"].values()),
            "findings": len(findings),
            "blocking": len(blocking),
            "advisory": len(advisory),
            "info": len(info),
            "waived": len(waived),
        },
        "blocking": blocking,
        "advisory": advisory,
        "info": info,
        "waived": waived,
        "drift": drift,
        "unresolved": unresolved,
        "ledgerProblems": ledger_problems,
        "danglingWaivers": dangling,
    }


# ============================================================================
# 输出
# ============================================================================

def render_console(result: dict, closure: dict) -> str:
    c = result["counts"]
    lines = []
    per_end = " / ".join(f"{k} {len(v['packages'])}" for k, v in closure["ends"].items())
    lines.append(f"依赖闭包：{c['packages']} 个包（{per_end}）")
    lines.append(f"漏洞快照：{result['snapshotGeneratedAt'] or '缺失'}（离线读取）")
    lines.append(
        f"命中：{c['findings']} 条 —— Critical(阻断) {c['blocking']} / High(告警) {c['advisory']}"
        f" / 中低危 {c['info']} / 已豁免 {c['waived']}"
    )

    if result["blocking"]:
        lines.append("")
        lines.append("阻断项（Critical 且未豁免）：")
        for f in result["blocking"]:
            lines.append(f"  [CRITICAL] {f['key']}  {f['id']}  {f['summary'][:70]}")
    if result["advisory"]:
        lines.append("")
        lines.append("告警项（High，不阻断）：")
        for f in result["advisory"]:
            lines.append(f"  [HIGH]     {f['key']}  {f['id']}  {f['summary'][:70]}")
    if result["ledgerProblems"]:
        lines.append("")
        lines.append("豁免台账违规（判红）：")
        for p in result["ledgerProblems"]:
            lines.append(f"  - {p}")
    if result["drift"]:
        lines.append("")
        lines.append(f"快照/覆盖问题 {len(result['drift'])} 项（须跑 --refresh）：")
        for d in result["drift"][:12]:
            lines.append(f"  - {d}")
    if result["unresolved"]:
        lines.append("")
        lines.append(f"未能评估的依赖 {len(result['unresolved'])} 项（已计入报告，不阻断）：")
        for u in result["unresolved"][:6]:
            lines.append(f"  - {u}")
    if result["danglingWaivers"]:
        lines.append("")
        lines.append(f"悬空豁免（漏洞已消失，建议清理台账）：{', '.join(result['danglingWaivers'])}")

    lines.append("")
    lines.append(
        {EXIT_PASS: "结论：通过", EXIT_BLOCK: "结论：**阻断**（Critical 未处置或台账违规）", EXIT_SNAPSHOT: "结论：**快照过期/覆盖不全**，先跑 --refresh"}
        [result["exitCode"]]
    )
    return "\n".join(lines)


def render_markdown(result: dict, closure: dict, licenses: dict) -> str:
    c = result["counts"]
    out = [
        "# 依赖与漏洞扫描报告（E-11）",
        "",
        f"- 生成时间：{_now()}",
        f"- 漏洞快照时间：{result['snapshotGeneratedAt'] or '缺失'}",
        f"- 闭包规模：**{c['packages']}** 个包（" + " / ".join(f"{k} {len(v['packages'])}" for k, v in closure["ends"].items()) + "）",
        f"- 命中：Critical {c['blocking']} ｜ High {c['advisory']} ｜ 中低危 {c['info']} ｜ 已豁免 {c['waived']}",
        "- 阈值：**Critical 阻断、High 告警**（用户 2026-09-24 决策）；",
        f"- 扫描器：`scripts/scan_deps.py`（零第三方依赖，离线可跑）",
        "",
        "## 阻断项",
        "",
    ]
    if result["blocking"]:
        out += ["| 依赖 | 漏洞 | 定级来源 | 摘要 |", "|---|---|---|---|"]
        out += [
            f"| `{f['key']}` | {f['id']} | {f['severitySource']} | {f['summary'][:90]} |"
            for f in result["blocking"]
        ]
    else:
        out.append("无。")
    out += ["", "## 告警项（High）", ""]
    if result["advisory"]:
        out += ["| 依赖 | 漏洞 | 定级来源 | 摘要 |", "|---|---|---|---|"]
        out += [
            f"| `{f['key']}` | {f['id']} | {f['severitySource']} | {f['summary'][:90]} |"
            for f in result["advisory"]
        ]
    else:
        out.append("无。")

    out += ["", "## 中低危与未定级", ""]
    if result["info"]:
        out += ["| 依赖 | 漏洞 | 级别 | 摘要 |", "|---|---|---|---|"]
        out += [
            f"| `{f['key']}` | {f['id']} | {f['severity']} | {f['summary'][:90]} |" for f in result["info"]
        ]
    else:
        out.append("无。")

    if result["waived"]:
        out += ["", "## 已豁免（台账有效期内）", "", "| 依赖 | 漏洞 | 级别 | 理由 | 责任人 | 到期 |", "|---|---|---|---|---|---|"]
        out += [
            f"| `{f['key']}` | {f['id']} | {f['severity']} | {f.get('waiveReason', '')} | {f.get('waiveOwner', '')} | {f.get('waiveExpiresAt', '')} |"
            for f in result["waived"]
        ]

    out += ["", "## 未评估 / 覆盖问题", ""]
    for label, rows in (("未能评估的依赖", result["unresolved"]), ("快照与覆盖问题", result["drift"]), ("豁免台账违规", result["ledgerProblems"])):
        out.append(f"**{label}**：{len(rows)} 项")
        for r in rows[:20]:
            out.append(f"- {r}")
        out.append("")

    lic = licenses.get("licenses") or {}
    out += ["## 许可证（直接依赖）", "", "| 依赖 | 许可证 | 来源 |", "|---|---|---|"]
    for end in closure["ends"].values():
        for pkg in end["packages"][:200]:
            key = _pkg_key(pkg["ecosystem"], pkg["name"], pkg["version"])
            row = lic.get(key)
            if row:
                out.append(f"| `{key}` | {row.get('license', '')} | {row.get('source', '')} |")
    return "\n".join(out) + "\n"


# ============================================================================
# 入口
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="依赖与漏洞扫描（E-11）")
    ap.add_argument("--refresh", action="store_true", help="联网刷新闭包快照 / 漏洞快照 / 许可证，然后扫描")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出扫描结果")
    ap.add_argument("--report", metavar="PATH", help="额外写一份 markdown 报告")
    ap.add_argument("--check-only", action="store_true", help="只做覆盖与漂移一致性检查，不计漏洞分级")
    args = ap.parse_args(argv)

    closure = collect_all()

    if args.refresh:
        print("[scan_deps] --refresh：联网刷新闭包 / 漏洞快照 / 许可证 …")
        snap = refresh_vuln_snapshot(closure)
        _write_json(SBOM_PATH, closure)
        _write_json(VULN_PATH, snap)
        _write_json(LICENSE_PATH, refresh_licenses(closure))
        if not ALLOWLIST_PATH.exists():
            _write_json(
                ALLOWLIST_PATH,
                {
                    "note": "豁免台账：每条必须有 id/package/reason/disposition/owner/expiresAt，缺一项或已过期则扫描判红",
                    "entries": [],
                },
            )
        print(f"[scan_deps] 已更新 {SBOM_PATH.relative_to(ROOT)} / {VULN_PATH.relative_to(ROOT)} / {LICENSE_PATH.relative_to(ROOT)}")
        print(f"[scan_deps] 闭包 {sum(len(e['packages']) for e in closure['ends'].values())} 个包，"
              f"命中 {sum(len(v) for v in snap['findings'].values())} 条")

    snapshot = _read_json(VULN_PATH)
    if not snapshot:
        print("[scan_deps] 缺少 security/vuln-snapshot.json —— 先联网执行一次：python scripts/scan_deps.py --refresh")
        return EXIT_SNAPSHOT

    result = scan(closure, snapshot)

    if args.check_only:
        result["exitCode"] = EXIT_SNAPSHOT if result["drift"] else EXIT_PASS
        result["blocking"] = []

    if args.report:
        Path(args.report).write_text(
            render_markdown(result, closure, _read_json(LICENSE_PATH)), encoding="utf-8"
        )
        print(f"[scan_deps] 报告已写入 {args.report}")

    if args.json:
        print(json.dumps({"result": result, "closure": closure}, ensure_ascii=False, indent=2))
    else:
        print(render_console(result, closure))

    return result["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())

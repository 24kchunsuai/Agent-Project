#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 `docs/权限矩阵.md` 与代码是否一致（企业级落地规约 E-10 的验收手段）。

## 为什么需要这个脚本

E-10 的验收标准是"矩阵里每一行都能指向一处代码校验（grep 出证据），
不允许『文档写了代码没做』"。这句话如果靠人 review，那么：

* 加/删端点时**没人会记得**同步矩阵 → 文档悄悄变成错的；
* 而错的权限文档比没有文档更危险 —— 它会让人**相信**某个端点已经受保护了。

所以把"一致性"做成**可执行**的东西。四项检查（缺一项只防住一半）：

  A 端点覆盖   代码里所有 Controller 映射，都必须在矩阵里有一行
  B 证据真实   每行 `文件:行` 存在，且该行确实包含 `依据` 列写的代码片段
  C 约束语义   标"登录"的行证据须含 requireLogin/UNAUTHORIZED；
               标"管理员"的须含 requireAdmin/PERMISSION_DENIED/isAdmin
  D 反向完整   全部 `requireLogin(session` / `requireAdmin(session` 调用点
               必须逐条出现在矩阵里（方法内部的自调用除外）

## 用法

    python scripts/check_permission_matrix.py          # 安静模式，只报问题
    python scripts/check_permission_matrix.py -v       # 逐行打印核对结果

退出码：0 = 通过；1 = 有 FAIL（可挂到 CI / pre-push）。

## 设计取舍

* **零第三方依赖**：手写解析。与 `scripts/ci.sh` 同一取向 —— "门禁工具本身跑不起来"
  比没有门禁更糟。
* **行号漂移只 WARN 不算 FAIL**：代码上移几行是日常操作，若因此判红，
  人们会开始无视这个门禁。脚本会打印**实际行号**，方便手工修正（自愈设计）。
* **只比较"是否包含片段"**，不做 AST 匹配：片段是文档里人写的，长度可控；
  引入 AST 会把脚本本身变成需要维护的项目。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "权限矩阵.md"
BEGIN = "<!-- PERMISSION-MATRIX:BEGIN -->"
END = "<!-- PERMISSION-MATRIX:END -->"


def _matrix_path(argv: list[str]) -> Path:
    """`--matrix <path>` 可指向另一份文件。

    存在的理由：**给这个门禁自己做验红**（改坏一份副本，确认它真的会红）。
    一个从没红过的检查，和没有检查是一样的。
    """
    if "--matrix" in argv:
        i = argv.index("--matrix")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
    return MATRIX


JAVA_MAIN = ROOT / "agt_backend" / "src" / "main" / "java"
JAVA_TEST = ROOT / "agt_backend" / "src" / "test" / "java"
PY_APP = ROOT / "agt_python" / "app"
PY_TEST = ROOT / "agt_python" / "tests"

#: 判定"必须登录 / 必须管理员"时，证据行里必须出现的关键字（检查 C）
_NEED_LOGIN = ("requireLogin", "UNAUTHORIZED")
_NEED_ADMIN = ("requireAdmin", "PERMISSION_DENIED", "isAdmin")

#: 局限：只扫这几个 Service 实现里的统一判定入口（检查 D）。
#: 刻意**不**全仓扫 —— `requireLogin` 在别处是普通词，全仓扫会引入误报，
#: 而"误报的门禁"比没有门禁更容易被关掉。
_CHECK_D_FILES = (
    JAVA_MAIN / "cn/tedu/agt/governance/service/impl/GovernanceServiceImpl.java",
    JAVA_MAIN / "cn/tedu/agt/kb/service/impl/KbServiceImpl.java",
)

_MAPPING_RE = re.compile(r"@(Get|Post|Put|Delete|Patch)Mapping\s*(?:\(([^)]*)\))?")
#: 任意映射注解（检查 E 用：匿名行的证据行必须是一个映射注解）
_MAPPING_ANY_RE = re.compile(r"@(Get|Post|Put|Delete|Patch)Mapping")
_CLASS_PREFIX_RE = re.compile(r'@RequestMapping\s*\(\s*"([^"]*)"')
_FIRST_STRING_RE = re.compile(r'"([^"]*)"')
_DEF_RE = re.compile(r"private\s+LoginUser\s+require(Login|Admin)\s*\(")

fails: list[str] = []
warns: list[str] = []
verbose = "-v" in sys.argv or "--verbose" in sys.argv


def note(msg: str) -> None:
    if verbose:
        print(msg)


def _index_files() -> dict[str, list[Path]]:
    """basename → 候选绝对路径列表（矩阵里只写短文件名，便于阅读）。

    **同名不立刻报错**：本项目里有多个 `__init__.py` / `agent.py`，
    它们多数与权限矩阵无关。只有在矩阵真的引用了这个短名、且候选不唯一时，
    才算失败（见 `resolve`）——否则会出现"门禁自己乱报"，那种门禁会被关掉。
    """
    idx: dict[str, list[Path]] = {}
    for base in (JAVA_MAIN, JAVA_TEST, PY_APP, PY_TEST, ROOT / "deploy" / "scripts"):
        if not base.exists():
            continue
        for p in list(base.rglob("*.java")) + list(base.rglob("*.py")):
            idx.setdefault(p.name, []).append(p)
    return idx


def _resolve(idx: dict[str, list[Path]], name: str) -> Path | None:
    """把矩阵里的短文件名解析成唯一路径；歧义时报 FAIL 并列出候选。"""
    cands = idx.get(name) or []
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    fails.append(f"[B] 短文件名 {name} 有 {len(cands)} 个候选，矩阵里无法定位："
                 + "、".join(str(c.relative_to(ROOT)) for c in cands))
    return None


def _method_span(lines: list[str], line_no: int) -> tuple[int, int]:
    """给定 1-based 行号，返回其所属方法体的 (起, 止) 1-based 行区间。

    本仓 Java 风格固定：方法签名在 4 空格缩进、方法体以一行 `    }` 收尾。
    找不到就退化为"单行区间"（不炸，只是覆盖判定范围变小）。

    为什么需要它：**一个端点可以有两处判定**（如 `DELETE /v1/kb/recycle/{docId}`
    先 `requireLogin` 再 `if (!isAdmin)`）。矩阵每行只写一处证据（写最强的那处），
    若按"点"比对，另一处永远算未登记。按"方法"比对才符合真实语义。
    """
    i = line_no - 1
    start = None
    for j in range(i, max(-1, i - 80), -1):
        s = lines[j]
        if re.match(r"^    (public|private|protected)\s", s) and not s.rstrip().endswith(";"):
            start = j
            break
    if start is None:
        return line_no, line_no
    end = len(lines) - 1
    for j in range(start + 1, len(lines)):
        if lines[j].rstrip() == "    }":
            end = j
            break
    return start + 1, end + 1


def parse_matrix(text: str) -> list[dict]:
    """解析矩阵表：| 端点 | 约束 | 校验点 | 依据 |"""
    block = text.split(BEGIN, 1)[-1].split(END, 1)[0]
    rows: list[dict] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= set("-: "):
            continue
        if cells[0] in ("端点",):
            continue
        endpoint = cells[0].strip("`").strip()
        level = cells[1].strip()
        ref = cells[2].strip("`").strip()
        snippet = cells[3].strip("`").strip()
        parts = ref.rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            fails.append(f"[B] 校验点格式应为 `文件:行`，实际：{ref}")
            continue
        rows.append({"endpoint": endpoint, "level": level, "file": parts[0],
                     "line": int(parts[1]), "snippet": snippet})
    return rows


def scan_controller_endpoints() -> set[str]:
    """解析全部 Controller → {'GET /v1/xxx', ...}"""
    found: set[str] = set()
    for ctrl in JAVA_MAIN.rglob("*Controller.java"):
        text = ctrl.read_text(encoding="utf-8", errors="replace")
        m = _CLASS_PREFIX_RE.search(text)
        prefix = m.group(1) if m else ""
        for line in text.splitlines():
            mm = _MAPPING_RE.search(line)
            if not mm:
                continue
            verb = mm.group(1).upper()
            args = mm.group(2) or ""
            sm = _FIRST_STRING_RE.search(args)
            sub = sm.group(1) if sm else ""
            path = f"{prefix}/{sub.strip('/')}" if sub else prefix
            path = "/" + path.strip("/")
            found.add(f"{verb} {path}")
    return found


def scan_guard_call_sites() -> set[tuple[str, int]]:
    """扫统一判定入口的调用点；排除方法内部的自调用（如 requireAdmin 内调 requireLogin）。"""
    sites: set[tuple[str, int]] = set()
    for path in _CHECK_D_FILES:
        if not path.exists():
            fails.append(f"[D] 文件不存在：{path}")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        # 先找出 helper 定义的方法体范围（本仓 Java 风格：4 空格缩进，方法以 "    }" 收尾）
        helper_spans: list[tuple[int, int]] = []
        for i, line in enumerate(lines):
            if _DEF_RE.search(line):
                end = len(lines) - 1
                for j in range(i + 1, len(lines)):
                    if lines[j].rstrip() == "    }":
                        end = j
                        break
                helper_spans.append((i + 1, end + 1))
        for i, line in enumerate(lines, start=1):
            if not re.search(r"require(Login|Admin)\(session", line):
                continue
            if any(a <= i <= b for a, b in helper_spans):
                continue
            sites.add((path.name, i))
    return sites


def main() -> int:
    matrix = _matrix_path(sys.argv)
    if not matrix.exists():
        print(f"找不到 {matrix}")
        return 1
    text = matrix.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"矩阵缺少标记 {BEGIN} / {END}")
        return 1

    files = _index_files()
    rows = parse_matrix(text)
    note(f"矩阵解析：{len(rows)} 行")

    # ---- 检查 B：证据真实 + C：约束语义 ----
    cited_spans: list[tuple[str, int, int]] = []   # (文件名, 起, 止) —— 供检查 D 用
    for r in rows:
        f = _resolve(files, r["file"])
        if f is None:
            if not any(r["file"] in x for x in fails):
                fails.append(f"[B] {r['endpoint']}：找不到源文件 {r['file']}")
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if r["line"] > len(lines):
            fails.append(f"[B] {r['endpoint']}：{r['file']} 只有 {len(lines)} 行，"
                         f"却引用第 {r['line']} 行")
            continue
        actual = lines[r["line"] - 1]
        if r["snippet"] not in actual:
            where = [i + 1 for i, L in enumerate(lines) if r["snippet"] in L]
            if where:
                warns.append(f"[B] {r['endpoint']}：{r['file']}:{r['line']} 行号漂移，"
                             f"片段实际在第 {where[0]} 行")
                actual = lines[where[0] - 1]
                r["line"] = where[0]
            else:
                fails.append(f"[B] {r['endpoint']}：{r['file']} 中找不到片段 "
                             f"{r['snippet']!r}（文档写的代码不存在）")
                continue
        lo, hi = _method_span(lines, r["line"])
        cited_spans.append((r["file"], lo, hi))

        if r["level"] == "登录" and not any(k in actual for k in _NEED_LOGIN):
            fails.append(f"[C] {r['endpoint']} 标为「登录」，但证据行不含 "
                         f"{'/'.join(_NEED_LOGIN)}：{actual.strip()}")
        elif r["level"] == "管理员" and not any(k in actual for k in _NEED_ADMIN):
            fails.append(f"[C] {r['endpoint']} 标为「管理员」，但证据行不含 "
                         f"{'/'.join(_NEED_ADMIN)}：{actual.strip()}")
        elif r["level"] == "匿名":
            # 检查 E：匿名行的证据必须是 **Controller 的映射注解**。
            #
            # 为什么这么要求：本系统的拒绝只发生在 Service 层（见文档 §2），
            # 所以"Controller 层不判身份"就是"匿名可达"的结构性证明。
            # 若不强制这一点，把某个**实际受保护**的端点错标成"匿名"、
            # 却仍引用那行 requireAdmin，检查 C 会直接跳过（它只管登录/管理员），
            # 检查 D 也会因为"调用点仍被某个行的方法区间覆盖"而放过 —— 错标逃逸。
            if not r["file"].endswith("Controller.java") or not _MAPPING_ANY_RE.search(actual):
                fails.append(f"[E] {r['endpoint']} 标为「匿名」，但证据 "
                             f"{r['file']}:{r['line']} 不是 Controller 的映射注解 "
                             f"（匿名行的证据须指向 Controller 层不判身份这件事）")
            else:
                note(f"  ok  {r['endpoint']:<52} {r['level']:<4} "
                     f"{r['file']}:{r['line']}")
        elif r["level"] not in ("匿名", "登录", "管理员"):
            fails.append(f"[C] {r['endpoint']}：约束取值必须是 匿名/登录/管理员，"
                         f"实际 {r['level']!r}")

    # ---- 检查 A：端点覆盖（双向）----
    code_eps = scan_controller_endpoints()
    doc_eps = {r["endpoint"] for r in rows}
    for ep in sorted(code_eps - doc_eps):
        fails.append(f"[A] 代码里有端点但矩阵未登记：{ep}")
    for ep in sorted(doc_eps - code_eps):
        fails.append(f"[A] 矩阵登记了但代码里找不到：{ep}")
    note(f"端点覆盖：代码 {len(code_eps)} 个 / 矩阵 {len(doc_eps)} 行")

    # ---- 检查 D：反向完整（判定入口的调用点都被登记）----
    # 按**方法区间**覆盖而非按行点比对：一个端点可以有两处判定
    # （如彻底删除先 requireLogin 再 if(!isAdmin)），矩阵每行只写最强的那处证据。
    sites = scan_guard_call_sites()
    for name, line in sorted(sites):
        if not any(n == name and lo <= line <= hi for n, lo, hi in cited_spans):
            fails.append(f"[D] {name}:{line} 有统一判定调用，但它所在的方法未在矩阵登记")

    print(f"权限矩阵校验：{len(rows)} 行 / 端点 {len(code_eps)} 个 / 判定调用点 {len(sites)} 处")
    for w in warns:
        print(f"  WARN  {w}")
    for f_ in fails:
        print(f"  FAIL  {f_}")
    if fails:
        print(f"\n未通过：{len(fails)} 项。矩阵与代码不一致 —— 改一侧就要改另一侧。")
        return 1
    print("通过：矩阵每一行都能指向真实代码校验，且代码里没有未登记的端点/判定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""治理透传冒烟（打 8080）：验证「前端 → Java → Python」这条链在治理域真的通。

为什么需要这个脚本
------------------
G1–G4 四个验收脚本的 `--base` 默认值全是 `http://127.0.0.1:8000`（Python），
且 `agt_backend` 没有 `src/test` 目录——于是**治理域的 Java 透传层从未被端到端验证**。
2026-09-18 的三层审计正是因此才发现「前端彻底删除/恢复不传 confirm」这类断裂：
功能完全不可用，而所有验收都绿。本脚本把 Java 层纳入可重复验证。

它校验什么
----------
1. **链路通**：登录 8080 拿 token → 治理端点逐个可达（不是连接失败、不是 500）
2. **字段透传**：不只看 `code=2000`，还要看关键字段真的到了——`overview.milvusCollection`
   （新代码才有的自述字段）、`config.lowQualityChars == 50`（阈值改动透过 Java 可见）、
   `config.purgeRequireConfirm`（前端确认门的前提）等
3. **错误通道**：业务拒绝（不存在的工单/文档）要返回**业务码**而不是 2000，证明 Java
   没有把异常吞成成功
4. **确认门通道**（回收站非空时才测）：不带 confirm 调彻底删除，必须回
   `3001 + data.requireConfirm + data.expectedDocName`——**data 能不能穿过 Java 是
   前端能否弹框的关键**（`ConfirmRequiredException` 专门为它保留结构化 detail）

安全边界（重要）
----------------
- **只读为主**：GET 直接调；三个重操作（对账 / 巡检 / TTL）一律带 `dryRun=true`，
  只报告不落库
- **不调用任何真实写操作**：disable / enable / rollback / ticket confirm 一律用
  **不存在的 id** 触发，只验证"链路通 + 拒绝得对"，绝不触碰真实数据
- 确认门探测用的是**不带 confirm 的彻底删除**——后端语义就是"不执行任何删除"，
  所以它本身是安全调用；回收站为空时自动跳过

用法
----
    python scripts/gov_smoke_8080.py                      # 默认 8080 + admin/123456
    python scripts/gov_smoke_8080.py --base http://127.0.0.1:8080
    python scripts/gov_smoke_8080.py --username admin --password xxx

退出码：0 = 全部通过（SKIP 不计失败）；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.request import ProxyHandler, build_opener

# 系统代理会劫持 localhost（与 test_guard / 各验收脚本同一个坑），显式绕过
OPENER = build_opener(ProxyHandler({}))

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"  | {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print(f"  ~ {name}  | 跳过：{why}")


def call(
    method: str, url: str, token: str | None = None, timeout: float = 60.0,
) -> tuple[int | None, dict[str, Any] | None, str]:
    """发请求，返回 (HTTP 状态, 解出的响应体, 原始文本)。任何异常都收敛成 (None, None, 描述)。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                       # 非 2xx 也可能带统一响应体
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                     # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw), raw
    except json.JSONDecodeError:
        return status, None, raw[:300]


def dig(data: Any, path: str) -> Any:
    """按 `a.b.c` 取值，任一层缺失返回 None（不抛异常）。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def is_ok(body: dict[str, Any] | None) -> bool:
    return bool(body) and body.get("code") == 2000


def login(base: str, username: str, password: str) -> str | None:
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(f"{base}/v1/auth/login", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:                                     # noqa: BLE001
        print(f"✗ 登录失败：{type(e).__name__}: {e}")
        print("  8080 起了吗？（Java 侧：mvn spring-boot:run 或已打包的 jar）")
        return None
    token = dig(body, "data.token")
    if not token:
        print(f"✗ 登录未拿到 token：{json.dumps(body, ensure_ascii=False)[:200]}")
        return None
    return str(token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("治理透传冒烟（Java 8080）")
    print(f"  目标     : {base}")
    print(f"  账号     : {args.username}")
    print("  说明     : 只读 + dryRun；写操作一律用不存在的 id 探异常通道，不碰真实数据")
    print("=" * 78)

    token = login(base, args.username, args.password)
    if not token:
        return 1
    print(f"  ✓ 登录成功（token 长度 {len(token)}）\n")

    g = f"{base}/v1/kb/governance"

    # ---------------- 1. 只读端点 ----------------
    print("[1] 只读端点（GET）")

    st, body, raw = call("GET", f"{g}/overview", token)
    check("GET /governance/overview 可达", is_ok(body),
          f"http={st} code={dig(body, 'code')} {raw[:80] if not is_ok(body) else ''}")
    # milvusCollection 是 2026-09-18 新增的服务自述字段——它能穿过 Java，才证明
    # Java 不是"按旧 DTO 挑字段返回"，而是真的整包透传
    check("overview 透传新字段 milvusCollection",
          bool(dig(body, "data.milvusCollection")), str(dig(body, "data.milvusCollection")))
    doc_total = dig(body, "data.docTotal")
    check("overview 文档总数 > 0", isinstance(doc_total, int) and doc_total > 0, str(doc_total))

    st, body, raw = call("GET", f"{g}/config", token)
    check("GET /governance/config 可达", is_ok(body), f"http={st} code={dig(body, 'code')}")
    # 阈值 10 → 50（2026-09-18），透过 Java 看得到才算改动对前端生效
    check("config.lowQualityChars = 50（阈值改动已透传）",
          dig(body, "data.lowQualityChars") == 50, str(dig(body, "data.lowQualityChars")))
    # 前端确认门的前提：后端确实要求确认，才谈得上"必须把 confirm 发出去"
    check("config.purgeRequireConfirm = true（前端确认门的前提）",
          dig(body, "data.purgeRequireConfirm") is True,
          str(dig(body, "data.purgeRequireConfirm")))

    st, body, raw = call("GET", f"{g}/tasks", token)
    names = {t.get("name") for t in (dig(body, "data.tasks") or [])}
    check("GET /governance/tasks 含三个常驻任务",
          is_ok(body) and {"reconcile", "ttl_purge", "inspection_scan"} <= names,
          str(sorted(names)))

    st, body, raw = call("GET", f"{g}/docs?page=1&pageSize=5", token)
    check("GET /governance/docs 分页可用",
          is_ok(body) and isinstance(dig(body, "data.items"), list),
          f"items={len(dig(body, 'data.items') or [])}")

    st, body, raw = call("GET", f"{g}/audit?page=1&pageSize=5", token)
    check("GET /governance/audit 分页可用",
          is_ok(body) and isinstance(dig(body, "data.items"), list),
          f"items={len(dig(body, 'data.items') or [])}")

    st, body, raw = call("GET", f"{g}/tickets?page=1&pageSize=5", token)
    check("GET /governance/tickets 返回 openCount",
          is_ok(body) and dig(body, "data.openCount") is not None,
          f"openCount={dig(body, 'data.openCount')}")

    st, body, raw = call("GET", f"{g}/disabled?page=1&pageSize=5", token)
    check("GET /governance/disabled 分页可用",
          is_ok(body) and isinstance(dig(body, "data.items"), list),
          f"items={len(dig(body, 'data.items') or [])}")

    # ---------------- 2. dryRun 重操作（只报告不落库） ----------------
    print("\n[2] dryRun 重操作（不落库）")

    st, body, raw = call("POST", f"{g}/reconcile/run?dryRun=true", token, timeout=180)
    check("POST /reconcile/run?dryRun=true 返回对账报告",
          is_ok(body) and dig(body, "data.milvusDocs") is not None,
          f"milvusDocs={dig(body, 'data.milvusDocs')} registered={dig(body, 'data.registeredDocs')}")

    st, body, raw = call("POST", f"{g}/inspection/scan?dryRun=true", token, timeout=180)
    check("POST /inspection/scan?dryRun=true 返回巡检报告",
          is_ok(body) and dig(body, "data.dryRun") is True,
          f"dryRun={dig(body, 'data.dryRun')}")

    st, body, raw = call("POST", f"{g}/recycle-bin/purge-expired?dryRun=true", token, timeout=180)
    check("POST /recycle-bin/purge-expired?dryRun=true 返回清理预览",
          is_ok(body) and dig(body, "data.dryRun") is True,
          f"dryRun={dig(body, 'data.dryRun')}")

    # ---------------- 3. 错误通道（用不存在的 id，不碰真实数据） ----------------
    print("\n[3] 错误通道（不存在的 id：要业务码，不要 2000，也不要 500）")

    st, body, raw = call("POST", f"{g}/doc/__smoke_not_exist__/disable", token)
    code = dig(body, "code")
    check("停用不存在的文档 → 业务拒绝（非 2000 / 非 500）",
          body is not None and code != 2000 and (st == 200 or (st is not None and st < 500)),
          f"http={st} code={code} {str(dig(body, 'message'))[:60]}")

    st, body, raw = call("POST", f"{g}/doc/__smoke_not_exist__/rollback", token)
    code = dig(body, "code")
    check("回滚不存在的版本 → 业务拒绝（非 2000 / 非 500）",
          body is not None and code != 2000 and (st == 200 or (st is not None and st < 500)),
          f"http={st} code={code}")

    st, body, raw = call("POST", f"{g}/tickets/99999999/confirm", token)
    code = dig(body, "code")
    check("执行不存在的工单 → 业务拒绝（非 2000 / 非 500）",
          body is not None and code != 2000 and (st == 200 or (st is not None and st < 500)),
          f"http={st} code={code}")

    # ---------------- 4. 回收站 + 确认门通道 ----------------
    print("\n[4] 回收站与确认门通道")

    st, body, raw = call("GET", f"{base}/v1/kb/recycle?page=1&pageSize=5", token)
    items = dig(body, "data.items") or []
    check("GET /v1/kb/recycle 可达", is_ok(body), f"items={len(items)}")
    if items:
        first = items[0]
        check("回收站条目带 TTL 剩余期（规约 §6.3）",
              first.get("ttlRemainingDays") is not None and first.get("ttlDays") is not None,
              f"remaining={first.get('ttlRemainingDays')} ttlDays={first.get('ttlDays')}")
    else:
        skip("回收站条目带 TTL 剩余期（规约 §6.3）", "回收站当前为空，无条目可验")

    # 不带 confirm 的彻底删除：后端语义就是"不执行任何删除"，调用本身安全。
    # 它验证的是**Java 有没有把 data 吞掉**——前端能否弹框全看这个。
    if items:
        target = items[0]
        doc_id = target.get("docId")
        st, body, raw = call(
            "DELETE", f"{base}/v1/kb/recycle/{urllib.parse.quote(str(doc_id))}", token,
        )
        code = dig(body, "code")
        check("彻底删除缺确认 → 3001 + data.requireConfirm（Java 未吞 data）",
              code == 3001 and dig(body, "data.requireConfirm") is True,
              f"http={st} code={code} expectedDocName={dig(body, 'data.expectedDocName')}")
    else:
        skip("彻底删除缺确认 → 3001 + data.requireConfirm",
             "回收站为空；确认门需要真实回收站文档才能触发（可先在隔离服务上跑 "
             "scripts/gov_g4_e2e.py 造出回收站文档，再对产品库复测）")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项 / 跳过 {len(SKIP)} 项")
    if FAIL:
        print("\n失败项：")
        for name in FAIL:
            print(f"  ✗ {name}")
    if SKIP:
        print("\n跳过项（不计失败）：")
        for name in SKIP:
            print(f"  ~ {name}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

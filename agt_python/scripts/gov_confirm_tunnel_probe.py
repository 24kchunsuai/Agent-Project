#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""确认门隧道探针：验证「3001 + data」能否穿过 Java 透传层。

为什么需要它（补的是哪块盲区）
------------------------------
`scripts/gov_smoke_8080.py` 打的是**真实产品库**，而确认门只有在**回收站非空**时
才会触发——产品库回收站一旦清空，这项检查就永远 SKIP。于是最容易断的地方
（Java 全局异常处理器**只保留 message、丢掉 data**）反而长期没有可重复的验证手段。
2026-09-18 修复前端的 `confirm` 缺失，正是被这个盲区掩盖的。

本脚本用**内置 stub 下游**代替真实 Python：stub 对确认门端点固定返回
`3001 + data.requireConfirm`，于是**不需要回收站、不需要 Milvus、不碰任何产品数据**，
就能验证 Java 是否把 data 原样交给前端。

它验证什么
----------
1. **data 穿透**（核心）：`requireConfirm / expectedDocName` 与**嵌套结构**（`hits[]`）
   能否穿过 Java —— 不能穿过，前端就只能弹一个没有依据的确认框
2. **两条代码路径都覆盖**：`KbServiceImpl`（restore/purge）与 `GovernanceServiceImpl`
   （rollback）是两段独立实现，只有都验到才算通
3. **参数下传**：Java 是否真把 `confirm / confirmDocName / versionNo` 发给下游
   （看 stub 收到的原始 query）
4. **身份注入**：`owner / is_admin` 必须由 Java 侧注入，前端传值不采信
5. **匿名收口**：不带 token 必须 4010，且**拦在调用下游之前**（stub 不应收到请求）

怎么跑（需要两个终端）
----------------------
    # 终端 A：起一个指向 stub 的临时 Java 实例（端口 8082，不动产品 8080）
    cd agt_backend
    java -jar target/agt_backend.jar \
         --server.port=8082 --agent.base-url=http://127.0.0.1:8001

    # 终端 B：起 stub（脚本内置）并探测
    cd agt_python
    python scripts/gov_confirm_tunnel_probe.py

注意：Java 的治理接口要求管理员，`--username/--password` 需给管理员账号。
退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.request import ProxyHandler, build_opener

# 系统代理会劫持 localhost（与其余验收脚本同一个坑），显式绕过
OPENER = build_opener(ProxyHandler({}))

# 与真实 Python 返回逐字段对齐（kb_service / governance_service）
STUB_PURGE = {
    "code": 3001,
    "message": "彻底删除不可恢复，需要二次确认：请带 confirm=true 与 confirmDocName「隧道探针假文档.md」重发",
    "data": {
        "requireConfirm": True,
        "expectedDocName": "隧道探针假文档.md",
        "docId": "__tunnel_probe_doc__",
        "origin": "recycle",
    },
}
STUB_RESTORE = {
    "code": 3001,
    "message": "该文档与在库语料高度相似（0.957），确认仍要恢复？",
    "data": {
        "requireConfirm": True,
        "gate": "L3_SIMILARITY",
        "maxSimilarity": 0.957,
        "hits": [
            {"docId": "uav-battery-md", "docName": "无人机锂电池保养.md", "similarity": 0.957},
            {"docId": "uav-link-md", "docName": "无人机图传故障.md", "similarity": 0.931},
        ],
    },
}
STUB_ROLLBACK = {
    "code": 3001,
    "message": "回滚会替换在库内容，需要确认：请带 confirm=true 与 confirmDocName「隧道探针假文档.md」重发",
    "data": {
        "requireConfirm": True,
        "gate": "ROLLBACK",
        "expectedDocName": "隧道探针假文档.md",
        "docId": "__tunnel_probe_doc__",
        "sourceVersion": 1,
        "nextVersion": 4,
    },
}
STUB_OK = {"code": 2000, "message": "ok", "data": {"path": "stub"}}

PASS: list[str] = []
FAIL: list[str] = []
RECEIVED: list[str] = []          # stub 收到的原始请求行
RECEIVED_LOCK = threading.Lock()


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self) -> None:
        with RECEIVED_LOCK:
            RECEIVED.append(f"{self.command} {self.path}")

    def do_DELETE(self) -> None:  # noqa: N802
        self._record()
        self._reply(STUB_PURGE if "/recycle/" in self.path else STUB_OK)

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        if "/restore" in self.path:
            self._reply(STUB_RESTORE)
        elif "/rollback" in self.path:
            self._reply(STUB_ROLLBACK)
        else:
            self._reply(STUB_OK)

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._reply(STUB_OK)

    def log_message(self, fmt: str, *args: object) -> None:
        return                                    # 静默，避免刷屏


def start_stub(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  | {detail}" if detail else ""))


def call(
    method: str, url: str, token: str | None = None, timeout: float = 20.0,
    body: dict | None = None,
) -> tuple[int | None, dict[str, Any] | None, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
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
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def login(base: str, username: str, password: str) -> str | None:
    st, body, raw = call("POST", f"{base}/v1/auth/login",
                         body={"username": username, "password": password})
    token = dig(body, "data.token")
    if not token:
        print(f"✗ 登录失败：http={st} {raw[:200]}")
        return None
    return str(token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8082",
                    help="临时 Java 实例（其 agent.base-url 必须指向 --stub-port）")
    ap.add_argument("--stub-port", type=int, default=8001)
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("确认门隧道探针（Java 透传层 → data 是否保留）")
    print(f"  Java 实例 : {base}")
    print(f"  stub 下游 : http://127.0.0.1:{args.stub_port}（脚本内置，非真实 Python）")
    print("  安全性    : 不接触真实回收站 / Milvus / 产品库；下游全部为固定假响应")
    print("=" * 78)

    start_stub(args.stub_port)
    print(f"  ✓ stub 已监听 127.0.0.1:{args.stub_port}\n")

    token = login(base, args.username, args.password)
    if not token:
        print("\n  提示：请先起指向 stub 的临时 Java 实例（见脚本头部说明），"
              "并确认它是用 --agent.base-url 指过来的。")
        return 1
    print(f"  ✓ 登录成功（token 长度 {len(token)}）\n")

    # ---------------- 1. 匿名收口 ----------------
    print("[1] 匿名收口（必须 4010，且不应触达下游）")
    before = len(RECEIVED)
    st, body, raw = call("GET", f"{base}/v1/kb/governance/overview")
    check("治理端点匿名访问 → 4010", dig(body, "code") == 4010, f"code={dig(body, 'code')}")
    check("匿名请求未触达下游（拦在 Java）", len(RECEIVED) == before,
          f"stub 新增请求 {len(RECEIVED) - before} 条")

    # ---------------- 2. Kb 域确认门（KbServiceImpl） ----------------
    print("\n[2] Kb 域确认门（KbServiceImpl）")

    st, body, raw = call(
        "DELETE", f"{base}/v1/kb/recycle/__tunnel_probe_doc__", token)
    check("彻底删除缺确认 → 3001", dig(body, "code") == 3001, f"code={dig(body, 'code')}")
    check("data.requireConfirm 穿过 Java（前端能否弹框的关键）",
          dig(body, "data.requireConfirm") is True, str(dig(body, "data.requireConfirm")))
    check("data.expectedDocName 穿过 Java（复述用）",
          bool(dig(body, "data.expectedDocName")), str(dig(body, "data.expectedDocName")))

    st, body, raw = call(
        "POST", f"{base}/v1/kb/recycle/__tunnel_probe_doc__/restore", token)
    check("恢复缺确认 → 3001 + gate=L3_SIMILARITY",
          dig(body, "code") == 3001 and dig(body, "data.gate") == "L3_SIMILARITY",
          f"gate={dig(body, 'data.gate')}")
    hits = dig(body, "data.hits")
    check("嵌套数组 hits[] 穿过 Java（列表结构不被拍平/丢弃）",
          isinstance(hits, list) and len(hits) == 2 and bool(dig(hits[0], "docName")),
          f"hits={len(hits) if isinstance(hits, list) else hits}")
    check("maxSimilarity 数值类型保持", dig(body, "data.maxSimilarity") == 0.957,
          str(dig(body, "data.maxSimilarity")))

    # ---------------- 3. 治理域确认门（GovernanceServiceImpl，独立实现） ----------------
    print("\n[3] 治理域确认门（GovernanceServiceImpl，与上节是两段独立代码）")

    st, body, raw = call(
        "POST", f"{base}/v1/kb/governance/doc/__tunnel_probe_doc__/rollback", token)
    check("回滚缺确认 → 3001 + data.requireConfirm",
          dig(body, "code") == 3001 and dig(body, "data.requireConfirm") is True,
          f"code={dig(body, 'code')} gate={dig(body, 'data.gate')}")
    check("回滚 data.sourceVersion/nextVersion 穿过 Java",
          dig(body, "data.sourceVersion") == 1 and dig(body, "data.nextVersion") == 4,
          f"src={dig(body, 'data.sourceVersion')} next={dig(body, 'data.nextVersion')}")

    # ---------------- 4. 参数下传与身份注入 ----------------
    print("\n[4] 参数下传与身份注入（看 stub 收到的原始 query）")
    with RECEIVED_LOCK:
        RECEIVED.clear()
    call("DELETE",
         f"{base}/v1/kb/recycle/__tunnel_probe_doc__"
         "?confirm=true&confirmDocName=" + urllib.parse.quote("隧道探针假文档.md"),
         token)
    call("POST",
         f"{base}/v1/kb/governance/doc/__tunnel_probe_doc__/rollback"
         "?versionNo=1&confirm=true&confirmDocName=abc",
         token)
    with RECEIVED_LOCK:
        got = list(RECEIVED)

    joined = " || ".join(got)
    check("confirm / confirmDocName 下传下游", "confirm=true" in joined and "confirmDocName=" in joined,
          f"{len(got)} 条请求")
    check("versionNo 下传下游", "versionNo=1" in joined)
    check("owner / is_admin 由 Java 注入（前端不采信）",
          "owner=" in joined and "is_admin=" in joined,
          next((x for x in got if "owner=" in x), "")[:110])

    code = dig(body, "code")
    print(f"\n  （末条响应 code={code}，为 stub 固定返回，非真实回滚）")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败项：")
        for name in FAIL:
            print(f"  ✗ {name}")
    print("=" * 78)
    print("\n提醒：本脚本的 Java 实例是临时的，验证完请关掉（别占用 8082）。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

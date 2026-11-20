"""数据目录的唯一解析入口（支撑"测试不写产品目录"）。

为什么单独一个模块
------------------
落盘数据（治理账本 / 回收站与停用区备份 / 上传源文件）原来由三个模块各自
`ROOT / "data/..."` 拼路径。结果是"给测试换一套数据目录"必须改代码，
于是没人改，测试脚本就直接写产品目录（2026-09-18 一天留下三批残留，见 DEV_ISSUES §6.37）。

现在统一由环境变量 `DATA_ROOT` 决定：

- **不设置（默认）** → `agt_python/data`，即产品目录，行为与改造前完全一致
- **设置**（绝对路径，或相对 `agt_python/` 的路径）→ 全部落盘数据随之外迁

铁律
----
验收 / 探测脚本**必须**让 `DATA_ROOT` 指向临时目录。这件事由
`app.core.test_guard.isolate()` 自动完成，脚本无需手写环境变量。

实现约束
--------
本模块**只依赖标准库**，刻意不 import `app.core.config`：
① 避免与 config 形成循环依赖；
② 保证"先设 env、后 import"这一顺序约定能真正生效——路径在 import 时求值，
   所以 test_guard 必须在 import app.* 之前调用。
"""

from __future__ import annotations

import os
from pathlib import Path

# agt_python/ 目录（本文件位于 agt_python/app/core/paths.py）
BASE_DIR = Path(__file__).resolve().parents[2]

#: 产品数据目录——用于判定"当前是否已隔离"
PRODUCT_DATA_DIR = BASE_DIR / "data"

_raw = (os.environ.get("DATA_ROOT") or "").strip() or "data"
_candidate = Path(_raw)
#: 实际生效的数据根目录（绝对路径）
DATA_DIR = _candidate if _candidate.is_absolute() else (BASE_DIR / _candidate)


def is_isolated() -> bool:
    """当前数据目录是否已与产品目录隔离。"""
    try:
        return DATA_DIR.resolve() != PRODUCT_DATA_DIR.resolve()
    except OSError:                      # 路径异常（超长/权限）时按"已隔离"放行，由后续读写报错
        return True


def describe() -> str:
    """人类可读的一行描述，便于脚本启动时打印与排查。"""
    return f"{DATA_DIR}（{'隔离' if is_isolated() else '产品目录'}）"

"""pytest 全局配置：把数据目录隔离到临时路径（项目铁律，见 app/core/paths.py）。


为什么必须这么做
----------------
`app/core/paths.py` 的模块 docstring 写得很明确：验收/测试脚本**必须**让
`DATA_ROOT` 指向临时目录，否则会直接写产品数据目录——2026-09-18 一天就留下
三批残留（DEV_ISSUES §6.37）。

本文件在**任何 app.* 模块被导入之前**设置 `DATA_ROOT`（conftest 由 pytest 最先加载），
因此 `paths.DATA_DIR` 求值时就已指向 tmp，后续所有落盘（治理账本、回收站、
会话记忆库、埋点库）全部自动外迁。

⚠️ 会话记忆切到 SQLite 后，测试里的 `session_memory` 是模块级单例，
   会写真实库文件——没有本文件时，跑一次 `pytest` 就会把产品库写脏
   （实测污染了 `raw-e2e` / `trim-e2e` 两个会话共 80 条）。
"""

import os
import sys
import tempfile
from pathlib import Path

# ---- 必须在 import app.* 之前完成 ----
_TMP_ROOT = tempfile.mkdtemp(prefix="agt_pytest_")
os.environ["DATA_ROOT"] = _TMP_ROOT

# S-1 服务间信任边界：app.main 在**模块级**校验 AGENT_SHARED_KEY（空则拒绝启动），
# 且鉴权中间件要求除探针外的所有请求带 X-Agent-Key。
# 这里给测试进程一个固定密钥：让 `import app.main` 能过，测试再据此构造请求头。
AGENT_SHARED_KEY_TEST = "pytest-agent-shared-key"
os.environ["AGENT_SHARED_KEY"] = AGENT_SHARED_KEY_TEST

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ARG001
    """测试结束后清理临时数据目录。

    只删本会话自建的 tmp 目录（路径由 tempfile 生成，绝无产品数据风险）。
    """
    import shutil

    shutil.rmtree(_TMP_ROOT, ignore_errors=True)

"""验收 / 探测脚本的数据目录守卫——把"测试不写产品目录"从约定变成机制。

背景（真实事故）
----------------
2026-09-18 一天之内，产品数据目录被脚本写进三批调试残留：

===========  ======================  ==========================================
时间          残留                     来源
===========  ======================  ==========================================
12:21        ``watch-122151-doc.md``  "观察内容。"重复 30 遍的探测文件
12:26        ``dbg-122633-doc``       调试文档（2 切片 + 向量 + 账本行）
12:49~12:51  ``wsweep-*`` 三部         滑窗探测（9 行孤儿切片指纹）
===========  ======================  ==========================================

规矩早就立过（测试产物不得落产品目录），但**没有机制强制**，所以又发生了。
本模块把它变成两道门：

1. **进程侧** ``isolate()``——脚本自己的数据目录强制指向临时目录（退出自动清理）
2. **服务侧** ``assert_remote_isolated()``——脚本要打的**目标服务**也必须跑在隔离目录上，
   否则脚本一上传语料就写进了产品库（进程侧隔离挡不住这条路径）

调用时机（**必须写在 import app.\* 之前**）
------------------------------------------
数据目录在模块 import 时求值（见 ``app/core/paths.py``），所以：

.. code-block:: python

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

    from app.core.test_guard import assert_remote_isolated, isolate
    DATA_DIR = isolate()                    # ← 必须在 import app.* 之前

    from app.services.kb_service import kb_service      # 此后 import 才拿到临时目录

调用晚了不会静默出错——``isolate()`` 会检测到 ``app.core.paths`` 已被 import 并直接报错。

配套：``scripts/serve_isolated.py`` 可一行起一个隔离服务实例供脚本使用。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# 刻意不 import app.core.paths：本模块必须能在路径求值之前可用
_BASE_DIR = Path(__file__).resolve().parents[2]        # agt_python/
_PRODUCT_DATA_DIR = _BASE_DIR / "data"

_ISOLATED_DIR: Path | None = None                       # 幂等标记


def _under(path: Path, parent: Path) -> bool:
    """path 是否等于 parent 或位于其子树内（解析失败按"不在"处理，交由后续读写报错）。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _script_name() -> str:
    """当前脚本名（供提示信息用）；`python -c` 这类无脚本名场景给个占位。"""
    argv0 = (sys.argv[0] if sys.argv else "").strip()
    return Path(argv0).name if argv0 and argv0 != "-c" else "<脚本名>"


def isolate(
    prefix: str = "agt-test-", keep: bool = False, follow: str | None = None,
) -> Path:
    """把当前进程的数据目录定下来（幂等），必要时注册退出清理。

    :param prefix: 临时目录名前缀，便于在系统临时目录里认出来
    :param keep:   保留临时目录不删（排查脚本自身问题时用）
    :param follow: 目标服务地址——**验收脚本应当传它**。脚本除了走 HTTP，还会
                   in-process 直接调服务层（例如 G4 直改 ``chunks.char_len`` 造劣质切片）。
                   若脚本与服务各用一个临时目录，两边看到的不是同一份数据，断言必然对不上。
                   传了它就跟随目标服务的数据目录，保证"脚本改的"就是"服务读的"。
    :returns:      生效的数据根目录

    优先级：

    1. ``follow`` 能连上 → 用目标服务的数据目录。
       此模式下**不注册清理**（目录归服务管，脚本删了会影响服务运行）；
       服务若没隔离，稍后的 ``assert_remote_isolated()`` 会拒跑。
    2. ``DATA_ROOT`` 已设置 → 沿用它；指向产品数据目录（或其子树）则拒绝运行。
    3. 都没有 → 自建临时目录，退出时自动清理。
    """
    global _ISOLATED_DIR
    if _ISOLATED_DIR is not None:                       # 幂等：重复调用返回同一目录
        return _ISOLATED_DIR

    if "app.core.paths" in sys.modules:
        raise RuntimeError(
            "isolate() 调用太晚：app.core.paths 已被 import，数据目录已经求值。\n"
            "请把 isolate() 移到脚本 import 区最前面（import app.* 之前）。"
        )

    if follow:
        info: dict = {}
        try:
            info = remote_overview(follow)
        except (urllib.error.URLError, OSError, ValueError):
            print(f"⚠️  未连上目标服务 {follow}，本次进程侧数据目录改为自建；"
                  f"若后续 in-process 断言与服务不一致，先确认服务是否在跑。")
        db_path = str(info.get("dbPath") or "")
        if db_path:
            root = Path(db_path).resolve().parent.parent
            os.environ["DATA_ROOT"] = str(root)
            # 集合也必须跟随：只跟随数据目录的话，脚本 in-process 查的是产品集合，
            # 而语料灌在测试集合里 —— 表现为"向量查不到、相似度算出 0.0"（G4 实测）。
            collection = str(info.get("milvusCollection") or "")
            if collection:
                os.environ["MILVUS_COLLECTION"] = collection
            _ISOLATED_DIR = root
            return root

    raw = os.environ.get("DATA_ROOT", "").strip()
    if raw:
        cand = Path(raw)
        if not cand.is_absolute():
            cand = _BASE_DIR / cand
        if _under(cand, _PRODUCT_DATA_DIR):
            raise RuntimeError(
                f"拒绝运行：DATA_ROOT 指向产品数据目录或其子树（{cand}）。\n"
                "验收/探测脚本不允许写产品数据目录。二选一：\n"
                "  ① 不设 DATA_ROOT（推荐）——isolate() 会自动建临时目录\n"
                f"  ② DATA_ROOT=<项目外的路径> {_script_name()}"
            )
        _ISOLATED_DIR = cand
        return _ISOLATED_DIR

    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["DATA_ROOT"] = str(tmp)
    _ISOLATED_DIR = tmp
    if not keep:
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    return tmp


def remote_overview(base_url: str, timeout: float = 10.0) -> dict:
    """读目标服务的治理概览——服务自述"我在用哪个数据目录、哪个向量集合"。

    刻意用**空 ProxyHandler**：本机系统代理会劫持 localhost 请求（其他脚本用
    ``httpx(trust_env=False)`` 规避的是同一个坑），走代理时这里会拿不到 dbPath，
    守卫就会把"隔离服务"误判成"连不上"。
    """
    url = base_url.rstrip("/") + "/v1/kb/governance/overview"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url)
    with opener.open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return dict(payload.get("data") or {})


def remote_db_path(base_url: str, timeout: float = 10.0) -> str:
    """读目标服务的治理账本路径。"""
    return str(remote_overview(base_url, timeout).get("dbPath") or "")


def base_from_argv(default: str = "http://127.0.0.1:8000") -> str:
    """从 ``sys.argv`` 取 ``--base``（供 isolate 在 argparse 之前使用）。"""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--base" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--base="):
            return arg.split("=", 1)[1]
    return default


def data_dir_isolated() -> bool:
    """当前进程的数据目录是否已隔离（供脚本写"隔离模式下不适用"的断言分支）。

    必须在 ``isolate()`` 之后调用（内部延迟 import ``app.core.paths``，
    因为本模块要能在路径求值之前可用）。

    用途：个别断言验的是**生产环境的数据态**而不是功能，隔离集合从零开始必然不成立。
    典型例子——G1 的"存量语料已回填（切片指纹 > 2000 行）"：产品库跑过一次
    存量回填才有 2000+ 行，隔离库里只有本轮那几行。这类断言要按模式分成两支，
    否则隔离验收会长期挂一条无法修复的假失败（2026-09-18 实测 44/45）。
    """
    from app.core.paths import is_isolated                # 延迟 import：见上
    return is_isolated()


def warn_if_collection_dirty(base_url: str, *, limit: int = 10000) -> list[str]:
    """检查目标服务的向量集合是否"起点非空"，非空则大声告警并返回既有 doc_id。

    为什么需要
    ----------
    验收里有一类断言的前提是**集合从零点开始**：

    - G2「对账报告无未知文档（账本覆盖 100%）」——集合里有上一轮遗留的向量，
      必然被算成"未知文档"，于是把"起点脏"报成"功能坏"。

    2026-09-18 实测：隔离集合 ``kb_drone_test`` 里留着上一轮 G4 的 7 个
    ``g4e2e-153843-*`` 文档（44 条向量），让 G2 的未知文档断言渲染成一条
    看着像功能缺陷的失败项。根子在"起隔离服务时没重建集合"。

    现在起隔离服务默认重建集合（``serve_isolated.py``），本函数是兜底：
    起点确实非空时**立刻说清楚**，别让人去怀疑对账逻辑。

    :returns: 既有 doc_id 升序列表；空列表＝起点干净。
    """
    info = remote_overview(base_url)
    collection = str(info.get("milvusCollection") or "")
    if not collection:
        print("⚠️  目标服务未上报向量集合名（overview.milvusCollection），跳过起点清洁度检查")
        return []
    try:
        from app.core.config import settings
        from pymilvus import MilvusClient
    except Exception as e:                                # noqa: BLE001
        print(f"⚠️  起点清洁度检查跳过（依赖不可用：{type(e).__name__}: {e}）")
        return []
    try:
        client = MilvusClient(uri=settings.milvus_uri)
        # 注意：Milvus 3.0.1 上 `doc_id like "x%"` 会报
        # "like operation on non-string or no-json field is unsupported"，
        # 所以这里全量取回来、前缀过滤在 Python 侧做。
        rows = client.query(collection_name=collection, filter='doc_id != ""',
                            output_fields=["doc_id"], limit=limit)
    except Exception as e:                                # noqa: BLE001
        print(f"⚠️  起点清洁度检查跳过（查集合失败：{type(e).__name__}: {e}）")
        return []

    ids = sorted({str(r["doc_id"]) for r in rows})
    if ids:
        print("!" * 78)
        print(f"⚠️  向量集合 {collection} 起点非空：{len(ids)} 个既有文档（{len(rows)} 条向量）")
        for d in ids[:8]:
            print(f"      - {d}")
        if len(ids) > 8:
            print(f"      … 另有 {len(ids) - 8} 个")
        print("    这些文档不在本轮账本里，会让「未知文档 / 对账」类断言误报为功能失败。")
        print("    处理：起隔离服务时让它重建集合（serve_isolated.py 默认已重建），")
        print("          或确认这些是有意保留的起点数据。")
        print("!" * 78)
    return ids


def assert_remote_isolated(
    base_url: str, *, allow_product: bool = False, timeout: float = 10.0,
) -> str:
    """断言目标服务运行在隔离数据目录上，否则拒绝运行。

    :param allow_product: 显式放行"打产品服务"（会打印醒目警告）。仅在明确知道自己在做什么时使用。
    :returns: 目标服务的数据根目录
    """
    try:
        db_path = remote_db_path(base_url, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(
            f"连不上目标服务或读不到数据目录：{base_url}（{type(e).__name__}: {e}）\n"
            "验收脚本需要服务在跑。隔离用法：\n"
            "  python scripts/serve_isolated.py --port 8001\n"
            f"  {_script_name()} --base http://127.0.0.1:8001"
        ) from e

    if not db_path:
        raise RuntimeError(f"目标服务 {base_url} 未返回 dbPath，无法确认数据目录是否隔离")

    data_dir = Path(db_path).resolve().parent.parent      # <data>/governance/xxx.db
    if _under(data_dir, _PRODUCT_DATA_DIR):
        if allow_product:
            print("!" * 78)
            print(f"⚠️  已显式放行打产品服务：{base_url}")
            print(f"    目标服务数据目录＝产品数据目录：{data_dir}")
            print("    本次运行**会写入产品库**；跑完请确认没有残留（用 scripts/gov_e2e_cleanup.py）。")
            print("!" * 78)
            return str(data_dir)
        raise RuntimeError(
            f"拒绝运行：目标服务 {base_url} 用的是**产品数据目录**。\n"
            f"  服务账本：{db_path}\n\n"
            "验收/探测脚本不允许写产品数据目录——历史事故："
            "dbg-122633-doc（12:26）、watch-122151-doc.md（12:21）、wsweep-*（12:49）。\n\n"
            "正确做法（二选一）：\n"
            "  ① 起一个隔离服务，用它跑验收（推荐）：\n"
            "       python scripts/serve_isolated.py --port 8001\n"
            f"       {_script_name()} --base http://127.0.0.1:8001\n"
            "  ② 确实要打产品服务（会写产品库，风险自负）：\n"
            f"       {_script_name()} --base {base_url} --allow-product-db"
        )
    return str(data_dir)

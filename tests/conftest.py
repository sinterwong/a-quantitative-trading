"""
conftest.py — pytest 全局 fixtures，测试数据库隔离。

所有调用 portfolio.py / watchlist.py / alert_history.py 的测试
统一使用 tempfile 里的空数据库，真实 portfolio.db 完全不受影响。

覆盖范围（DB_PATH 统一劫持）：
  backend.services.portfolio       — DB_PATH, get_db(), init_db()
  backend.services.watchlist      — DB_PATH
  backend.services.alert_history  — DB_PATH

用法：
  import pytest
  class TestFoo:
      @pytest.fixture(autouse=True)
      def setup_portfolio_db(self, portfolio_db):
          ...  # 你的 setUp；tearDown 由 fixture 自动处理

  或无需 import，autouse 的 conftest 已在所有测试前生效。
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
import pytest
import importlib

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)


# ─────────────────────────────────────────────────────────────────────────────
# Session-level fixture — 整个 pytest 会话只创建一次 temp dir
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope='session', autouse=True)
def _isolate_db_session():
    """
    在任何测试运行前，把三个模块的 DB_PATH 全部指向 session-scoped temp dir。
    测试结束后 shutil.rmtree 自动清理。
    autouse=True 意味着无需在每个测试文件里 import。
    """
    # 所有后续 import 都基于当前 sys.path，PROJECT_ROOT 已包含
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, 'backend'))

    # 动态 import（避免顶层 import 提前绑定旧 DB_PATH）
    ps_mod   = importlib.import_module('backend.services.portfolio')
    wl_mod   = importlib.import_module('backend.services.watchlist')
    ah_mod   = importlib.import_module('backend.services.alert_history')

    # session-scoped temp dir，进程结束才清理
    tmp_root = tempfile.mkdtemp(prefix='quant_test_db_')

    # 强制重新加载，让模块内部的 import 链和 THIS_DIR 全部正确解析。
    # 注意：reload() 会重新执行顶层 DB_PATH = ...（还原为原始路径），
    # 所以必须在 reload 之后再次 patch。
    importlib.reload(ps_mod)
    importlib.reload(wl_mod)
    importlib.reload(ah_mod)

    # reload 之后重新 patch（reload 覆盖了上面的 patch）
    ps_new = os.path.join(tmp_root, 'portfolio.db')
    ps_mod.DB_PATH = ps_new
    wl_mod.DB_PATH = ps_new
    ah_mod.DB_PATH = ps_new

    # 对三个模块都初始化 schema，这样任一 import 路径拿到的模块都能正常工作
    ps_mod.init_db()        # positions, orders, trades, cash, ...
    wl_mod.init_watchlist() # watchlist 表
    ah_mod.init_alerts()    # alerts 表

    # 关键：test_api.py 用 "from services.portfolio import"（不带 backend. 前缀），
    # 这会走 sys.path[0]=ROOT/backend 找到 services/portfolio.py，
    # 作为 "services.portfolio" 注册到 sys.modules。
    # 必须同时 patch sys.modules['services.portfolio']，否则 test_api.py
    # 会拿到另一个 module 实例，patch 不生效。
    import sys as _sys
    _sys.modules['services.portfolio']      = ps_mod
    _sys.modules['services.watchlist']       = wl_mod
    _sys.modules['services.alert_history']   = ah_mod

    yield tmp_root  # 测试在这里运行

    # ── restore ──────────────────────────────────────────────────────────────
    import sys as _sys_restore
    _sys_restore.modules['services.portfolio']     = None
    _sys_restore.modules['services.watchlist']      = None
    _sys_restore.modules['services.alert_history']  = None
    # DB_PATH 还原由 portfolio.py 等模块的 reload 自行恢复
    shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Function-level fixture — 每个测试函数用独立的 temp DB
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def portfolio_db(tmp_path, _isolate_db_session):
    """
    为单个测试函数提供独立 temp DB 路径 + 已初始化数据库。

    三个模块的 DB_PATH 已由 session fixture 固定到 _isolate_db_session，
    这里再创建子 temp dir 让每次测试有独立文件（避免并发冲突）。

    等价于老测试中 setUp() 里的：
        ps.DB_PATH = self.db_path
        ps.init_db()
    """
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, 'backend'))

    ps_mod = importlib.import_module('backend.services.portfolio')
    wl_mod = importlib.import_module('backend.services.watchlist')
    ah_mod = importlib.import_module('backend.services.alert_history')

    # 每个测试函数独享一个 temp sub-dir
    test_tmp = tmp_path / 'test_run'
    test_tmp.mkdir()

    db_file = str(test_tmp / 'portfolio.db')

    # patch（函数级别，每次测试完自动还原）
    orig_ps = ps_mod.DB_PATH
    orig_wl = wl_mod.DB_PATH
    orig_ah = ah_mod.DB_PATH

    ps_mod.DB_PATH = db_file
    wl_mod.DB_PATH = db_file
    ah_mod.DB_PATH = db_file

    # 同步 sys.modules 里的短路径引用
    import sys as _sys_local
    _sys_local.modules['services.portfolio']     = ps_mod
    _sys_local.modules['services.watchlist']     = wl_mod
    _sys_local.modules['services.alert_history'] = ah_mod

    # 初始化 schema — 必须包含三个模块的所有表
    ps_mod.init_db()       # positions, cash, orders, trades, ...
    wl_mod.init_watchlist()  # watchlist 表
    ah_mod.init_alerts()     # alerts 表

    yield db_file

    # ── restore ──────────────────────────────────────────────────────────────
    ps_mod.DB_PATH = orig_ps
    wl_mod.DB_PATH = orig_wl
    ah_mod.DB_PATH = orig_ah


# ─────────────────────────────────────────────────────────────────────────────
# 可选：自动清理模块全局状态（避免测试间隐式污染）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_module_caches():
    """
    每个测试函数结束后清理模块顶层缓存。
    如有其他全局状态（_trade_calendar、_singleton 等），在此扩展。
    """
    yield
    # 测试后清理 — 追加需要清理的模块在这里
    try:
        import backend.main as bm
        bm._trade_calendar = set()
        bm._trade_calendar_date = ''
    except (ImportError, AttributeError):
        pass

"""
conftest.py — pytest 全局 fixtures，测试数据库隔离。

策略：patch sqlite3.connect，所有访问 portfolio.db 的连接重定向到 temp file。
不需要修改任何测试文件，不需要 importlib.reload，不需要 patch DB_PATH。

真实 portfolio.db 完全不受影响。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import shutil
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Session-level — patch sqlite3.connect，拦截所有 portfolio.db 访问
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope='session', autouse=True)
def _isolate_db_session():
    """
    在任何测试运行前，把 sqlite3.connect 劫持到 session-scoped temp file。
    所有测试进程共享这一个隔离 DB，测试结束后自动清理。
    """
    _original_connect = sqlite3.connect

    # session-scoped temp dir
    tmp_db = tempfile.mkdtemp(prefix='quant_test_db_')
    tmp_db_file = os.path.join(tmp_db, 'portfolio.db')

    def _patched_connect(path, *args, **kwargs):
        """
        所有 sqlite3.connect 调用都经过这里。
        包含 'portfolio.db'(legacy) 或 'state.db'(P3-4 后)就重定向到 temp file，
        其他 .db 文件正常连接。
        """
        path_str = str(path) if path is not None else ''
        if 'portfolio.db' in path_str or path_str.endswith('state.db'):
            kwargs.setdefault('check_same_thread', False)
            return _original_connect(tmp_db_file, *args, **kwargs)
        return _original_connect(path, *args, **kwargs)

    sqlite3.connect = _patched_connect

    yield tmp_db_file

    # ── restore ──────────────────────────────────────────────────────────────
    sqlite3.connect = _original_connect
    shutil.rmtree(tmp_db, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Function-level fixture — 每个测试函数用独立的 temp DB（按需）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def portfolio_db(_isolate_db_session, tmp_path):
    """
    为单个测试函数提供独立 temp DB。

    注意：由于 session fixture 已经 patch 了 sqlite3.connect，
    实际上这里不需要再 patch 任何东西——所有 connect 都已经被劫持。
    只要在 tmp_path 下创建空的 .db 文件路径，init_db() 会自动写入这里。

    真实 portfolio.db 永远不会被访问。
    """
    db_file = str(tmp_path / 'portfolio.db')
    yield db_file
    # 不需要手动清理，tmp_path 在每个测试函数结束后自动清理


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
    try:
        # P3-2: 交易日历缓存搬到 quant_app/run_worker.py
        import quant_app.run_worker as wm
        wm._trade_calendar = set()
        wm._trade_calendar_date = ''
    except (ImportError, AttributeError):
        pass

    # R0-2: 通过 SingletonRegistry 一次性清掉所有迁移到 LockedSingleton 的全局态，
    # 避免新增单例时再来这里手写 reset_*。
    try:
        from core.singleton import SingletonRegistry
        SingletonRegistry.reset_all()
    except ImportError:
        pass

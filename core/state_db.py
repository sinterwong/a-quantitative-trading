"""
core/state_db.py — 统一状态数据库路径 + schema 版本管理

设计:
  - 单一 SQLite 状态库 `data/state.db`,所有 service(portfolio /
    alert_history / watchlist / walkforward_persistence 等)共享
  - 历史的 `backend/services/portfolio.db` 在首次访问时一次性迁移到
    canonical 位置,并把 legacy 文件重命名为 `.migrated-<ts>.bak`
    保留以便回滚。之后再也不会 fallback 到 legacy 路径
  - 想关闭自动迁移可设 ``QUANT_STATE_DB_NO_MIGRATE=1``
  - 提供 ``init_schema_version(conn, "portfolio", 1)`` 用于增量迁移
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('core.state_db')

_PROJ_DIR = Path(__file__).parent.parent
_CANONICAL_DB = _PROJ_DIR / 'data' / 'state.db'
_LEGACY_DB = _PROJ_DIR / 'backend' / 'services' / 'portfolio.db'

_MIGRATION_LOCK = threading.Lock()
_MIGRATION_ATTEMPTED = False


def _migrate_legacy_if_needed() -> None:
    """若 canonical 不存在但 legacy 存在,把 legacy 复制到 canonical 并
    把 legacy 重命名成 .bak。幂等 + 线程安全。失败仅记录,不抛异常。"""
    global _MIGRATION_ATTEMPTED
    if os.environ.get('QUANT_STATE_DB_NO_MIGRATE', '').strip() == '1':
        return
    if _MIGRATION_ATTEMPTED:
        return
    with _MIGRATION_LOCK:
        if _MIGRATION_ATTEMPTED:
            return
        _MIGRATION_ATTEMPTED = True
        if _CANONICAL_DB.exists() or not _LEGACY_DB.exists():
            return
        try:
            _CANONICAL_DB.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_LEGACY_DB, _CANONICAL_DB)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            backup = _LEGACY_DB.with_name(f'portfolio.migrated-{ts}.bak')
            _LEGACY_DB.rename(backup)
            logger.warning(
                '[state_db] legacy %s 已迁移到 %s (旧文件保留为 %s)',
                _LEGACY_DB, _CANONICAL_DB, backup,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error('[state_db] legacy 迁移失败,继续用旧路径: %s', exc)


def state_db_path() -> str:
    """返回当前活动状态库路径(字符串)。

    优先级:
      1. ``QUANT_STATE_DB`` 环境变量(absolute path,绕过迁移和 fallback)
      2. ``data/state.db``(canonical,如有 legacy 会先一次性迁移过来)
      3. 全新部署 → 直接创建 canonical
    """
    env = os.environ.get('QUANT_STATE_DB', '').strip()
    if env:
        return env
    _migrate_legacy_if_needed()
    if _CANONICAL_DB.exists():
        return str(_CANONICAL_DB)
    # 全新部署 / 迁移失败但 legacy 已经被改名:用 canonical,首次 connect 创建
    _CANONICAL_DB.parent.mkdir(parents=True, exist_ok=True)
    return str(_CANONICAL_DB)


def reset_migration_flag_for_tests() -> None:
    """测试辅助:重置迁移状态,允许同一进程内多次触发迁移。"""
    global _MIGRATION_ATTEMPTED
    with _MIGRATION_LOCK:
        _MIGRATION_ATTEMPTED = False


def init_schema_version(
    conn: sqlite3.Connection,
    module: str,
    version: int,
) -> int:
    """登记/读取模块 schema 版本号。

    Parameters
    ----------
    conn :
        已打开的 SQLite 连接(行为符合 PEP-249)
    module :
        模块标识(如 'portfolio' / 'wf_results' / 'alert_history')
    version :
        当前代码期望的 schema 版本

    Returns
    -------
    int
        记录的 schema 版本(若首次注册则等于 ``version``,否则为已存在的版本号)
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            module      TEXT PRIMARY KEY,
            version     INTEGER NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    row = cur.execute(
        "SELECT version FROM schema_versions WHERE module = ?",
        (module,),
    ).fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO schema_versions (module, version) VALUES (?, ?)",
            (module, version),
        )
        conn.commit()
        return version
    return int(row[0])


def update_schema_version(
    conn: sqlite3.Connection,
    module: str,
    version: int,
) -> None:
    """迁移完成后调用,更新模块 schema 版本号。"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO schema_versions (module, version, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(module) DO UPDATE SET
            version = excluded.version,
            updated_at = CURRENT_TIMESTAMP
    """, (module, version))
    conn.commit()

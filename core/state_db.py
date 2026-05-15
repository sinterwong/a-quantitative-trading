"""
core/state_db.py — 统一状态数据库路径 + schema 版本管理 (P3-4 阶段一)

设计目标:
  - 单一 SQLite 状态库 `data/state.db`,所有 service(portfolio /
    alert_history / watchlist / walkforward_persistence 等)共享
  - 但本次 *不* 强制迁移现有 `backend/services/portfolio.db` 数据;
    通过 fallback 机制平滑过渡:
      1. 环境变量 `QUANT_STATE_DB` 设置则 absolute 优先
      2. `data/state.db` 存在则用新位置
      3. 否则回退到 `backend/services/portfolio.db`(legacy)
  - 提供 `init_schema_version(conn, "portfolio", 1)` 用于增量迁移

Future:
  - 等 walkforward_persistence.py / portfolio.py 的 schema 都注册版本号后,
    可以加 `migrate_legacy_db(src_path)` 一次性迁移工具
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


_PROJ_DIR = Path(__file__).parent.parent
_CANONICAL_DB = _PROJ_DIR / 'data' / 'state.db'
_LEGACY_DB = _PROJ_DIR / 'backend' / 'services' / 'portfolio.db'


def state_db_path() -> str:
    """返回当前活动状态库路径(字符串)。

    优先级:
      1. ``QUANT_STATE_DB`` 环境变量(absolute path,绕过 fallback)
      2. ``data/state.db`` 已存在 → 用新位置
      3. ``backend/services/portfolio.db``(legacy,本地开发回退)

    若都不存在(全新部署),返回 canonical 路径,首次 connect 时自动创建。
    """
    env = os.environ.get('QUANT_STATE_DB', '').strip()
    if env:
        return env
    if _CANONICAL_DB.exists():
        return str(_CANONICAL_DB)
    if _LEGACY_DB.exists():
        return str(_LEGACY_DB)
    # 全新部署:用 canonical 路径,首次 connect 会创建 data/ 目录
    _CANONICAL_DB.parent.mkdir(parents=True, exist_ok=True)
    return str(_CANONICAL_DB)


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

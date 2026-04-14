"""
watchlist.py — 自选股监控服务
===============================
管理用户的自选股列表，每只股票可设置不同的预警阈值。
盘中 IntradayMonitor 对 watchlist 中的股票独立检查信号，
发现异动时主动推送飞书。

数据库：watchlist (symbol TEXT PK, name TEXT, added_at, reason TEXT,
                   alert_pct REAL DEFAULT 5.0, enabled INTEGER DEFAULT 1)
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger('watchlist')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(THIS_DIR, 'portfolio.db')  # 共用 portfolio.db


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_watchlist():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol      TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                added_at    TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT '',
                alert_pct   REAL NOT NULL DEFAULT 5.0,
                enabled     INTEGER NOT NULL DEFAULT 1
            )
        """)


# ─── CRUD ──────────────────────────────────────────────────────────────

def get_watchlist() -> List[Dict]:
    """返回所有启用的自选股"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE enabled=1 ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_watchlist_all() -> List[Dict]:
    """返回所有自选股（含禁用的）"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY enabled DESC, added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_to_watchlist(symbol: str, name: str = '', reason: str = '',
                      alert_pct: float = 5.0) -> bool:
    """
    添加股票到自选股列表。
    若已存在则更新 name/reason/alert_pct。
    Returns True if added/updated, False on error.
    """
    if not symbol:
        return False
    symbol = symbol.upper().strip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with _conn() as conn:
            conn.execute("""
                INSERT INTO watchlist (symbol, name, added_at, reason, alert_pct, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(symbol) DO UPDATE SET
                    name       = excluded.name,
                    reason     = excluded.reason,
                    alert_pct  = excluded.alert_pct,
                    enabled    = 1
            """, (symbol, name or '', now, reason or '', alert_pct))
        logger.info('Watchlist: added %s (%s) alert_pct=%.1f%%', symbol, name, alert_pct)
        return True
    except Exception as e:
        logger.error('Watchlist add failed: %s', e)
        return False


def remove_from_watchlist(symbol: str) -> bool:
    """禁用（软删除）自选股"""
    symbol = symbol.upper().strip()
    try:
        with _conn() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET enabled=0 WHERE symbol=?", (symbol,)
            )
        logger.info('Watchlist: removed %s', symbol)
        return cur.rowcount > 0
    except Exception as e:
        logger.error('Watchlist remove failed: %s', e)
        return False


def set_alert_threshold(symbol: str, alert_pct: float) -> bool:
    """更新单只股票的预警阈值"""
    symbol = symbol.upper().strip()
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE watchlist SET alert_pct=? WHERE symbol=?",
                (alert_pct, symbol)
            )
        return True
    except Exception as e:
        logger.error('Watchlist threshold update failed: %s', e)
        return False


def get_stock_alert_pct(symbol: str) -> float:
    """获取单只股票的预警阈值（默认 5%）"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT alert_pct FROM watchlist WHERE symbol=? AND enabled=1",
            (symbol,)
        ).fetchone()
    return row['alert_pct'] if row else 5.0


# ─── 初始化 ─────────────────────────────────────────────────────────────
init_watchlist()

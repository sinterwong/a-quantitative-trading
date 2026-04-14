"""
alert_history.py — 盘中预警历史记录
=====================================
每次盘中预警（指数异动/持仓信号/自选股异动）触发时记录一条，
支持查询最近 N 条、按类型过滤、按时间范围过滤。

Schema:
    alerts (id INTEGER PK AUTOINCREMENT,
            type TEXT,        -- 'INDEX'/'POSITION'/'WATCHLIST'/'SECTOR_FLOW'
            symbol TEXT,      -- 相关标的代码，指数用 'SH000001' 等
            message TEXT,     -- 推送的完整消息文本
            price REAL,       -- 触发时价格
            pct_change REAL,  -- 触发时涨跌幅
            triggered_at TEXT,
            delivered INTEGER DEFAULT 1)
"""

import os
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger('alert_history')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(THIS_DIR, 'portfolio.db')


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


def init_alerts():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                type         TEXT NOT NULL,
                symbol       TEXT NOT NULL DEFAULT '',
                message      TEXT NOT NULL DEFAULT '',
                price        REAL,
                pct_change   REAL,
                triggered_at TEXT NOT NULL,
                delivered    INTEGER NOT NULL DEFAULT 1
            )
        """)
        # 索引加速查询
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_triggered_at
            ON alerts(triggered_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_type
            ON alerts(type)
        """)


def record_alert(
    alert_type: str,
    message: str,
    symbol: str = '',
    price: float = None,
    pct_change: float = None,
) -> Optional[int]:
    """
    记录一条预警。
    Returns: alert id on success, None on error.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with _conn() as conn:
            cur = conn.execute("""
                INSERT INTO alerts (type, symbol, message, price, pct_change, triggered_at, delivered)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (alert_type, symbol or '', message or '', price, pct_change, now))
            alert_id = cur.lastrowid
            logger.debug('Alert recorded id=%d type=%s symbol=%s', alert_id, alert_type, symbol)
            return alert_id
    except Exception as e:
        logger.error('record_alert failed: %s', e)
        return None


def get_alerts(
    limit: int = 50,
    alert_type: str = None,
    since_hours: int = None,
    symbol: str = None,
) -> List[Dict]:
    """
    查询预警历史。

    Args:
        limit:       最多返回条数（默认50）
        alert_type:  过滤类型 INDEX/POSITION/WATCHLIST/SECTOR_FLOW
        since_hours: 只看最近 N 小时（如 24）
        symbol:      只看某标的（如 'SH000001'）
    Returns:
        List[Dict] 按时间倒序
    """
    sql = "SELECT * FROM alerts WHERE 1=1"
    params = []

    if alert_type:
        sql += " AND type=?"
        params.append(alert_type)

    if since_hours:
        cutoff = (datetime.now() - timedelta(hours=since_hours)).strftime('%Y-%m-%d %H:%M:%S')
        sql += " AND triggered_at >= ?"
        params.append(cutoff)

    if symbol:
        sql += " AND symbol=?"
        params.append(symbol)

    sql += " ORDER BY triggered_at DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def clear_old_alerts(days: int = 7) -> int:
    """删除 N 天之前的预警记录（默认清理 7 天前）"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as conn:
        cur = conn.execute("DELETE FROM alerts WHERE triggered_at < ?", (cutoff,))
    logger.info('Cleared %d old alerts older than %s', cur.rowcount, cutoff)
    return cur.rowcount


# ─── 初始化 ──────────────────────────────────────────────────────────────
init_alerts()

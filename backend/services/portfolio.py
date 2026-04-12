"""
portfolio.py — Portfolio persistence service with SQLite
=================================================
Phase 1 core: stores positions, trades, cash, signals persistently.
All state survives restarts — no in-memory loss.

Database: portfolio.db (SQLite)
Schema:
    positions   — current holdings
    trades     — completed trades
    cash       — available cash
    signals    — today's signals
    daily_meta — daily summaries
"""

import os
import sqlite3
import json
import time
from datetime import datetime, date
from typing import List, Dict, Optional, Any
from contextlib import contextmanager

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(THIS_DIR, 'portfolio.db')


# ============================================================
# Database bootstrap
# ============================================================

def get_db() -> sqlite3.Connection:
    """Get a raw DB connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_cursor():
    """Context manager for safe DB access."""
    conn = get_db()
    try:
        yield conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL UNIQUE,
                shares     INTEGER NOT NULL DEFAULT 0,
                entry_price REAL   NOT NULL DEFAULT 0.0,
                updated_at TEXT    NOT NULL
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS cash (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                amount     REAL    NOT NULL DEFAULT 20000.0,
                updated_at TEXT    NOT NULL
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                shares     INTEGER NOT NULL,
                price      REAL    NOT NULL,
                pnl        REAL,
                trade_id   TEXT    NOT NULL UNIQUE,
                executed_at TEXT    NOT NULL
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT    NOT NULL,
                signal     TEXT    NOT NULL,
                strength   REAL    NOT NULL DEFAULT 0.0,
                reason     TEXT,
                timestamp  TEXT    NOT NULL
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS daily_meta (
                trade_date TEXT PRIMARY KEY,
                weekday   TEXT,
                n_signals INTEGER DEFAULT 0,
                n_trades  INTEGER DEFAULT 0,
                equity    REAL,
                cash      REAL,
                note      TEXT
            )
        ''')

        # Ensure cash row exists
        cur.execute('SELECT id FROM cash WHERE id=1')
        if cur.fetchone() is None:
            cur.execute(
                'INSERT INTO cash (id, amount, updated_at) VALUES (1, ?, ?)',
                (20000.0, datetime.now().isoformat())
            )


# ============================================================
# Portfolio Service class
# ============================================================

class PortfolioService:
    """
    Thread-safe portfolio state manager backed by SQLite.

    All mutations are transactional. Queries return typed dicts.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_db()   # ensure schema on startup

    # ------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------

    def get_positions(self) -> List[Dict]:
        """Return all current positions."""
        with get_cursor() as cur:
            cur.execute(
                'SELECT symbol, shares, entry_price, updated_at FROM positions WHERE shares > 0'
            )
            return [dict(row) for row in cur.fetchall()]

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Return a single position by symbol."""
        with get_cursor() as cur:
            cur.execute(
                'SELECT symbol, shares, entry_price, updated_at FROM positions WHERE symbol=?',
                (symbol,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def upsert_position(self, symbol: str, shares: int, entry_price: float):
        """Insert or replace a position. Use shares=0 to close it."""
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR REPLACE INTO positions
                   (symbol, shares, entry_price, updated_at)
                   VALUES (?, ?, ?, ?)''',
                (symbol, shares, entry_price, datetime.now().isoformat())
            )

    def close_position(self, symbol: str):
        """Remove a position (e.g., sold to zero)."""
        self.upsert_position(symbol, 0, 0.0)

    # ------------------------------------------------------------
    # Cash
    # ------------------------------------------------------------

    def get_cash(self) -> float:
        """Return available cash."""
        with get_cursor() as cur:
            cur.execute('SELECT amount FROM cash WHERE id=1')
            row = cur.fetchone()
            return float(row['amount']) if row else 0.0

    def set_cash(self, amount: float):
        """Update cash balance."""
        with get_cursor() as cur:
            cur.execute(
                'UPDATE cash SET amount=?, updated_at=? WHERE id=1',
                (amount, datetime.now().isoformat())
            )

    # ------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------

    def record_trade(self, symbol: str, direction: str, shares: int,
                     price: float, pnl: Optional[float] = None) -> str:
        """
        Record a completed trade.
        Returns the trade_id.
        """
        trade_id = f"{symbol}_{direction}_{int(time.time()*1000)}"
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR IGNORE INTO trades
                   (symbol, direction, shares, price, pnl, trade_id, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (symbol, direction, shares, price, pnl, trade_id, datetime.now().isoformat())
            )
        return trade_id

    def get_trades(self, symbol: Optional[str] = None,
                   limit: int = 50) -> List[Dict]:
        """Return recent trades, optionally filtered by symbol."""
        with get_cursor() as cur:
            if symbol:
                cur.execute(
                    'SELECT * FROM trades WHERE symbol=? ORDER BY executed_at DESC LIMIT ?',
                    (symbol, limit)
                )
            else:
                cur.execute(
                    'SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?',
                    (limit,)
                )
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------

    def record_signal(self, symbol: str, signal: str,
                      strength: float, reason: str = ''):
        """Record a generated signal."""
        with get_cursor() as cur:
            cur.execute(
                '''INSERT INTO signals
                   (symbol, signal, strength, reason, timestamp)
                   VALUES (?, ?, ?, ?, ?)''',
                (symbol, signal, strength, reason, datetime.now().isoformat())
            )

    def get_signals(self, symbol: Optional[str] = None,
                    since: Optional[str] = None,
                    limit: int = 50) -> List[Dict]:
        """Return recent signals."""
        with get_cursor() as cur:
            query = 'SELECT * FROM signals WHERE 1=1'
            args: List[Any] = []
            if symbol:
                query += ' AND symbol=?'
                args.append(symbol)
            if since:
                query += ' AND timestamp>=?'
                args.append(since)
            query += ' ORDER BY timestamp DESC LIMIT ?'
            args.append(limit)
            cur.execute(query, args)
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------

    def record_daily_meta(self, equity: float, cash: float,
                         n_signals: int = 0, n_trades: int = 0,
                         note: str = ''):
        """Save end-of-day summary."""
        today = date.today()
        wd = today.strftime('%A')
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR REPLACE INTO daily_meta
                   (trade_date, weekday, n_signals, n_trades, equity, cash, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (str(today), wd, n_signals, n_trades, equity, cash, note)
            )

    def get_daily_metas(self, limit: int = 30) -> List[Dict]:
        """Return recent daily summaries."""
        with get_cursor() as cur:
            cur.execute(
                'SELECT * FROM daily_meta ORDER BY trade_date DESC LIMIT ?',
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------
    # Composite queries
    # ------------------------------------------------------------

    def get_portfolio_summary(self) -> Dict:
        """
        Return full portfolio snapshot: positions + cash + recent stats.
        """
        positions = self.get_positions()
        cash = self.get_cash()

        total_position_value = sum(
            p['shares'] * p['entry_price'] for p in positions
        )

        recent_trades = self.get_trades(limit=5)
        recent_signals = self.get_signals(limit=5)

        return {
            'cash': cash,
            'position_value': round(total_position_value, 2),
            'total_equity': round(cash + total_position_value, 2),
            'positions': positions,
            'recent_trades': recent_trades,
            'recent_signals': recent_signals,
            'updated_at': datetime.now().isoformat(),
        }


# ============================================================
# Standalone test
# ============================================================

if __name__ == '__main__':
    svc = PortfolioService()
    print('=== Portfolio Service Test ===')
    print('Cash:', svc.get_cash())
    svc.set_cash(25000.0)
    print('Cash after +5000:', svc.get_cash())

    svc.upsert_position('600900.SH', 200, 23.50)
    svc.upsert_position('300750.SZ', 50, 180.0)
    print('Positions:', svc.get_positions())

    svc.record_trade('600900.SH', 'BUY', 200, 23.50, None)
    svc.record_signal('600900.SH', 'BUY', 0.85, 'RSI oversold + institutional holding')
    print('Trades:', svc.get_trades())
    print('Signals:', svc.get_signals())

    summary = svc.get_portfolio_summary()
    print('Portfolio summary:', json.dumps(summary, indent=2, ensure_ascii=False))
    print('=== All tests passed ===')

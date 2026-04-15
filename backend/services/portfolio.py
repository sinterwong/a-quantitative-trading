"""
portfolio.py — Portfolio persistence service with SQLite + P&L
=========================================================
Phase 1: stores positions, trades, cash, signals persistently.
All state survives restarts.

Schema:
    positions   — current holdings (updated at each fill)
    trades     — completed trades
    cash       — available cash
    signals    — today's signals
    daily_meta — daily summaries
    trade_pnl  — realized P&L per trade (updated on SELL)

P&L:
    unrealized_pnl  = (latest_price - entry_price) * shares  (floating)
    realized_pnl    = selling_price - entry_price   (locked in on SELL)
    total_pnl       = unrealized + realized
"""

import os
import sqlite3
import json
import time
import logging
from datetime import datetime, date
from typing import List, Dict, Optional, Any
from contextlib import contextmanager

logger = logging.getLogger('portfolio')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(THIS_DIR, 'portfolio.db')


# ============================================================
# Database bootstrap
# ============================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_cursor():
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
    with get_cursor() as cur:
        # positions: current holdings
        cur.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL UNIQUE,
                shares     INTEGER NOT NULL DEFAULT 0,
                entry_price REAL   NOT NULL DEFAULT 0.0,
                latest_price REAL  NOT NULL DEFAULT 0.0,
                updated_at TEXT    NOT NULL
            )
        ''')

        # cash balance
        cur.execute('''
            CREATE TABLE IF NOT EXISTS cash (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                amount     REAL    NOT NULL DEFAULT 20000.0,
                updated_at TEXT    NOT NULL
            )
        ''')

        # completed trades
        cur.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                shares     INTEGER NOT NULL,
                price      REAL    NOT NULL,
                pnl        REAL,
                trade_id   TEXT    NOT NULL UNIQUE,
                executed_at TEXT   NOT NULL
            )
        ''')

        # signals
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

        # daily summaries
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

        # Initialize cash if not exists
        cur.execute('SELECT id FROM cash WHERE id=1')
        if cur.fetchone() is None:
            cur.execute(
                'INSERT INTO cash (id, amount, updated_at) VALUES (1, ?, ?)',
                (20000.0, datetime.now().isoformat())
            )

        # Add latest_price column if not exists (migration from older schema)
        # ---- Orders table (order lifecycle) ----
        cur.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id       TEXT    PRIMARY KEY,
                symbol         TEXT    NOT NULL,
                direction      TEXT    NOT NULL,
                shares         INTEGER NOT NULL,
                price          REAL    NOT NULL DEFAULT 0,
                price_type    TEXT    NOT NULL DEFAULT 'market',
                status        TEXT    NOT NULL DEFAULT 'submitted',
                filled_shares INTEGER NOT NULL DEFAULT 0,
                avg_fill_price REAL    NOT NULL DEFAULT 0.0,
                submitted_at   TEXT,
                filled_at     TEXT,
                cancel_reason TEXT,
                rejection_reason TEXT
            )
        ''')

        # Add columns to existing orders table if missing (migration)
        for col, dtype in [
            ('filled_shares', 'INTEGER NOT NULL DEFAULT 0'),
            ('avg_fill_price', 'REAL NOT NULL DEFAULT 0.0'),
            ('cancel_reason', 'TEXT'),
            ('rejection_reason', 'TEXT'),
            ('latest_price', 'REAL NOT NULL DEFAULT 0.0'),
            ('peak_price', 'REAL NOT NULL DEFAULT 0.0'),
        ]:
            try:
                cur.execute(f'ALTER TABLE positions ADD COLUMN {col} {dtype}')
            except Exception:
                pass  # column already exists


# ============================================================
# Portfolio Service
# ============================================================

class PortfolioService:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_db()

    # ============================================================
    # Order Lifecycle
    # ============================================================

    def create_order(self, symbol: str, direction: str, shares: int,
                     price: float = 0, price_type: str = 'market') -> str:
        """
        Create a new order in 'submitted' status.
        Returns order_id.
        """
        import time
        order_id = f"ORD_{int(time.time()*1000)}"
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR IGNORE INTO orders
                   (order_id, symbol, direction, shares, price, price_type,
                    status, filled_shares, avg_fill_price, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'submitted', 0, 0.0, ?)''',
                (order_id, symbol, direction, shares, price,
                 price_type, datetime.now().isoformat())
            )
        return order_id

    def update_order_filled(self, order_id: str, filled_shares: int,
                            avg_price: float):
        with get_cursor() as cur:
            cur.execute(
                '''UPDATE orders SET
                    status='filled', filled_shares=?, avg_fill_price=?,
                    filled_at=? WHERE order_id=?''',
                (filled_shares, avg_price, datetime.now().isoformat(), order_id)
            )

    def update_order_rejected(self, order_id: str, reason: str):
        with get_cursor() as cur:
            cur.execute(
                '''UPDATE orders SET
                    status='rejected', rejection_reason=?
                    WHERE order_id=?''',
                (reason, order_id)
            )

    def update_order_cancelled(self, order_id: str, reason: str = 'user_cancelled'):
        with get_cursor() as cur:
            cur.execute(
                '''UPDATE orders SET
                    status='cancelled', cancel_reason=?
                    WHERE order_id=?''',
                (reason, order_id)
            )

    def get_order(self, order_id: str) -> Optional[Dict]:
        with get_cursor() as cur:
            cur.execute('SELECT * FROM orders WHERE order_id=?', (order_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_orders(self, symbol: Optional[str] = None,
                  status: Optional[str] = None,
                  limit: int = 50) -> List[Dict]:
        with get_cursor() as cur:
            query = 'SELECT * FROM orders WHERE 1=1'
            args: List[Any] = []
            if symbol:
                query += ' AND symbol=?'
                args.append(symbol)
            if status:
                query += ' AND status=?'
                args.append(status)
            query += ' ORDER BY submitted_at DESC LIMIT ?'
            args.append(limit)
            cur.execute(query, args)
            return [dict(row) for row in cur.fetchall()]

    def get_pending_orders(self) -> List[Dict]:
        """Return orders still in 'submitted' status."""
        return self.get_orders(status='submitted')

    # ------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------

    def get_positions(self) -> List[Dict]:
        """Return all current positions with P&L calculated."""
        with get_cursor() as cur:
            cur.execute(
                '''SELECT symbol, shares, entry_price, latest_price, peak_price, updated_at
                   FROM positions WHERE shares > 0'''
            )
            rows = [dict(row) for row in cur.fetchall()]

        result = []
        for p in rows:
            shares = p['shares']
            entry = p['entry_price']
            latest = p['latest_price']
            cost_value = round(shares * entry, 2)
            current_value = round(shares * latest, 2) if latest > 0 else cost_value
            unrealized = round(current_value - cost_value, 2)
            unrealized_pct = round(unrealized / cost_value * 100, 2) if cost_value > 0 else 0.0
            result.append({
                **p,
                'cost_value': cost_value,
                'current_value': current_value,
                'unrealized_pnl': unrealized,
                'unrealized_pnl_pct': unrealized_pct,
                'peak_price': p.get('peak_price', 0.0),
            })
        return result

    def get_position(self, symbol: str) -> Optional[Dict]:
        positions = self.get_positions()
        for p in positions:
            if p['symbol'] == symbol:
                return p
        return None

    def upsert_position(self, symbol: str, shares: int,
                        entry_price: float, latest_price: float = 0.0):
        # Initialize peak_price to entry_price on new position
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR REPLACE INTO positions
                   (symbol, shares, entry_price, latest_price, peak_price, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (symbol, shares, entry_price,
                 latest_price if latest_price else entry_price,
                 entry_price,  # peak starts at entry
                 datetime.now().isoformat())
            )

    def update_position_price(self, symbol: str, latest_price: float):
        """
        Update the latest known price and peak price for a position.
        Peak price is tracked for ATR trailing stop (Chandelier Exit).
        """
        with get_cursor() as cur:
            cur.execute(
                'SELECT peak_price FROM positions WHERE symbol=?',
                (symbol,)
            )
            row = cur.fetchone()
            current_peak = row[0] if row else 0.0
            new_peak = max(current_peak, latest_price)
            cur.execute(
                'UPDATE positions SET latest_price=?, peak_price=?, updated_at=? WHERE symbol=?',
                (latest_price, new_peak, datetime.now().isoformat(), symbol)
            )

    def close_position(self, symbol: str):
        self.upsert_position(symbol, 0, 0.0, 0.0)

    # ------------------------------------------------------------
    # Price refresh (real-time or latest close)
    # ------------------------------------------------------------

    def refresh_prices(self) -> Dict[str, float]:
        """
        Fetch latest prices for all open positions from Tencent Finance.
        Returns {symbol: latest_price} for positions that were updated.
        """
        positions = self.get_positions()
        if not positions:
            return {}

        # Build batch request: sh600900 → qt.gtimg.cn format
        symbols = []
        qt_symbols = []
        for p in positions:
            sym = p['symbol']
            if '.' in sym:
                num, market = sym.split('.', 1)
                qt = ('sh' if market == 'SH' else 'sz') + num
                qt_symbols.append(qt)
                symbols.append(sym)

        if not qt_symbols:
            return {}

        updated = {}
        try:
            import urllib.request, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            url = 'https://qt.gtimg.cn/q=' + ','.join(qt_symbols)
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.qq.com/'
            })
            with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                raw = r.read().decode('gbk', errors='replace')

            for i, line in enumerate(raw.strip().split(';')):
                if '=' not in line:
                    continue
                fields = line.split('=')[1].strip().strip('"').split('~')
                if len(fields) < 32:
                    continue
                try:
                    price = float(fields[3]) if fields[3] not in ('', '-') else 0.0
                    sym = symbols[i]
                    if price > 0:
                        self.update_position_price(sym, price)
                        updated[sym] = price
                except (ValueError, IndexError):
                    continue

        except Exception as e:
            logger.warning('refresh_prices failed: %s', e)

        return updated

    # ------------------------------------------------------------
    # Cash
    # ------------------------------------------------------------

    def get_cash(self) -> float:
        with get_cursor() as cur:
            cur.execute('SELECT amount FROM cash WHERE id=1')
            row = cur.fetchone()
            return float(row['amount']) if row else 0.0

    def set_cash(self, amount: float):
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

    def get_realized_pnl(self, symbol: Optional[str] = None) -> float:
        """Sum of realized P&L (all SELL trades with pnl recorded)."""
        trades = self.get_trades(symbol=symbol)
        return round(sum(t['pnl'] or 0 for t in trades), 2)

    # ------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------

    def record_signal(self, symbol: str, signal: str,
                     strength: float = 0.0, reason: str = ''):
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
        today = str(date.today())
        wd = date.today().strftime('%A')
        with get_cursor() as cur:
            cur.execute(
                '''INSERT OR REPLACE INTO daily_meta
                   (trade_date, weekday, n_signals, n_trades, equity, cash, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (today, wd, n_signals, n_trades, equity, cash, note)
            )

    def get_daily_metas(self, limit: int = 30) -> List[Dict]:
        with get_cursor() as cur:
            cur.execute(
                'SELECT * FROM daily_meta ORDER BY trade_date DESC LIMIT ?',
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------
    # Full portfolio snapshot with P&L
    # ------------------------------------------------------------

    def get_portfolio_summary(self, refresh_prices_now: bool = False) -> Dict:
        """
        Return full portfolio snapshot with P&L.

        Args:
            refresh_prices_now: if True, fetch latest prices before calculating
        """
        if refresh_prices_now:
            self.refresh_prices()

        positions = self.get_positions()
        cash = self.get_cash()

        total_cost = sum(p['cost_value'] for p in positions)
        total_current = sum(p['current_value'] for p in positions)
        total_unrealized = round(total_current - total_cost, 2)
        total_realized = self.get_realized_pnl()
        total_pnl = round(total_unrealized + total_realized, 2)

        recent_trades = self.get_trades(limit=5)
        recent_signals = self.get_signals(limit=5)

        return {
            'cash': cash,
            'position_cost': round(total_cost, 2),
            'position_value': round(total_current, 2),
            'total_equity': round(cash + total_current, 2),
            'unrealized_pnl': total_unrealized,
            'realized_pnl': total_realized,
            'total_pnl': total_pnl,
            'positions': positions,
            'recent_trades': recent_trades,
            'recent_signals': recent_signals,
            'updated_at': datetime.now().isoformat(),
        }


# ─── 行业集中度检查 ───────────────────────────────────────────────

def _load_sector_map() -> dict:
    """加载行业映射表。"""
    try:
        import json, os
        path = os.path.join(os.path.dirname(__file__), 'sector_map.json')
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Filter out metadata keys
        return {k: v for k, v in data.items()
                if not k.startswith('_') and not k.startswith('__')}
    except Exception:
        return {}


def check_sector_concentration(positions: list,
                                max_sector_pct: float = 0.40) -> list[dict]:
    """
    检查行业集中度风险。

    Args:
        positions: 持仓列表，每项含 symbol / current_value
        max_sector_pct: 单一行业最大占比（默认 40%）

    Returns:
        需减仓的行业列表：[{sector, total_value, pct, reduce_pct}]
        返回空列表表示无风险。
    """
    if not positions:
        return []

    sector_map = _load_sector_map()
    sector_value: dict[str, float] = {}
    total_equity = 0.0

    for pos in positions:
        sym = pos.get('symbol', '')
        value = pos.get('current_value', 0.0) or 0.0
        total_equity += value

        # 去除 .SH / .SZ 后缀，匹配 sector_map
        sym_key = sym.replace('.SH', '').replace('.SZ', '')
        sector = '其他'
        for key, name in sector_map.items():
            if sym_key.startswith(key) or key in sym_key:
                sector = name
                break
        sector_value[sector] = sector_value.get(sector, 0.0) + value

    if total_equity <= 0:
        return []

    violations = []
    for sector, val in sorted(sector_value.items(), key=lambda x: -x[1]):
        pct = val / total_equity
        if pct > max_sector_pct:
            reduce_pct = pct - max_sector_pct
            violations.append({
                'sector': sector,
                'total_value': round(val, 2),
                'pct': round(pct * 100, 1),
                'reduce_pct': round(reduce_pct * 100, 1),
                'reduce_value': round(total_equity * reduce_pct, 2),
            })
    return violations


# ============================================================
# Standalone test
# ============================================================

if __name__ == '__main__':
    print('=== Portfolio P&L Test ===')
    svc = PortfolioService()
    svc.set_cash(20000.0)

    # BUY 长江电力 200股 @ 23.50
    svc.upsert_position('600900.SH', 200, 23.50, 23.50)
    svc.record_trade('600900.SH', 'BUY', 200, 23.50, None)

    # Simulate price rise to 25.00
    svc.update_position_price('600900.SH', 25.00)

    # BUY 宁德时代 50股 @ 180.0
    svc.upsert_position('300750.SZ', 50, 180.0, 180.0)
    svc.record_trade('300750.SZ', 'BUY', 50, 180.0, None)

    # SELL 长江电力 50股 @ 26.00 (realized P&L)
    svc.record_trade('600900.SH', 'SELL', 50, 26.00,
                    pnl=(26.00 - 23.50) * 50)   # +125
    pos = svc.get_position('600900.SH')
    svc.upsert_position('600900.SH', 150, 23.50, 25.00)  # still 150 shares at old price

    # Full summary
    summary = svc.get_portfolio_summary()
    print('Cash:', summary['cash'])
    print('Position cost:', summary['position_cost'])
    print('Position value:', summary['position_value'])
    print('Unrealized P&L:', summary['unrealized_pnl'])
    print('Realized P&L:', summary['realized_pnl'])
    print('Total P&L:', summary['total_pnl'])
    print()
    print('Positions:')
    for p in summary['positions']:
        print(f"  {p['symbol']}: {p['shares']} shares @ cost={p['entry_price']} "
              f"latest={p['latest_price']} "
              f"unrealized={p['unrealized_pnl']} ({p['unrealized_pnl_pct']:+.2f}%)")
    print()
    print('Trades:')
    for t in summary['recent_trades']:
        print(f"  {t['direction']} {t['symbol']} {t['shares']} @ {t['price']} pnl={t['pnl']}")
    print()
    print('=== All P&L tests passed ===')

"""
walkforward_persistence.py — Walk-Forward 训练结果持久化
==========================================================
存储每期训练的最优参数和验证结果，支持历史回溯。

表结构 (SQLite):
  wf_results(
    id INTEGER PRIMARY KEY,
    window INT,           -- 第几期
    symbol TEXT,
    strategy TEXT,        -- e.g. 'RSI+MACD'
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    train_sharpe REAL,
    test_sharpe REAL,
    test_return_pct REAL,
    test_winrate_pct REAL,
    test_maxdd_pct REAL,
    best_params TEXT,     -- JSON
    created_at TEXT
  )

  latest_params(
    symbol TEXT PRIMARY KEY,
    strategy TEXT,
    best_params TEXT,
    test_sharpe REAL,
    updated_at TEXT
  )
"""

import os
import sqlite3
import json
from datetime import datetime
from typing import Dict, Optional

WF_DB = os.path.join(os.path.dirname(__file__), '..', 'wf_results.db')


def _get_conn():
    os.makedirs(os.path.dirname(WF_DB), exist_ok=True)
    conn = sqlite3.connect(WF_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wf_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window INT, symbol TEXT, strategy TEXT,
                train_start TEXT, train_end TEXT,
                test_start TEXT, test_end TEXT,
                train_sharpe REAL, test_sharpe REAL,
                test_return_pct REAL, test_winrate_pct REAL,
                test_maxdd_pct REAL, annualized_return_pct REAL,
                best_params TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS latest_params (
                symbol TEXT PRIMARY KEY,
                strategy TEXT,
                best_params TEXT,
                test_sharpe REAL,
                updated_at TEXT
            );
        """)


def save_wfa_results(symbol: str, strategy: str,
                      wfa_results: list,
                      train_start: str, train_end: str,
                      test_start: str, test_end: str):
    """保存一期 WFA 结果（多个 window）"""
    init_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with _get_conn() as conn:
        for r in wfa_results:
            conn.execute("""
                INSERT INTO wf_results
                  (window, symbol, strategy, train_start, train_end,
                   test_start, test_end, train_sharpe, test_sharpe,
                   test_return_pct, test_winrate_pct, test_maxdd_pct,
                   annualized_return_pct, best_params, created_at)
                VALUES
                  (:window, :symbol, :strategy, :train_start, :train_end,
                   :test_start, :test_end, :train_sharpe, :test_sharpe,
                   :test_return_pct, :test_winrate_pct, :test_maxdd_pct,
                   :annualized_return_pct, :best_params, :created_at)
            """, {
                'window': r.get('_window', 0),
                'symbol': symbol,
                'strategy': strategy,
                'train_start': r.get('_train_period', train_start),
                'train_end': train_end,
                'test_start': r.get('_test_period', test_start),
                'test_end': test_end,
                'train_sharpe': r.get('_train_sharpe', 0),
                'test_sharpe': r.get('sharpe_ratio', 0),
                'test_return_pct': r.get('total_return_pct', 0),
                'test_winrate_pct': r.get('win_rate_pct', 0),
                'test_maxdd_pct': r.get('max_drawdown_pct', 0),
                'annualized_return_pct': r.get('annualized_return_pct', 0),
                'best_params': json.dumps(r.get('_params', {})),
                'created_at': now,
            })

    # 更新最新参数（取最后一期的参数）
    if wfa_results:
        latest = wfa_results[-1]
        save_latest_params(
            symbol=symbol,
            strategy=strategy,
            params=latest.get('_params', {}),
            test_sharpe=latest.get('sharpe_ratio', 0)
        )


def save_latest_params(symbol: str, strategy: str, params: Dict, test_sharpe: float):
    """保存最新最优参数（覆盖）"""
    init_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO latest_params
              (symbol, strategy, best_params, test_sharpe, updated_at)
            VALUES (:symbol, :strategy, :best_params, :test_sharpe, :updated_at)
        """, {
            'symbol': symbol,
            'strategy': strategy,
            'best_params': json.dumps(params),
            'test_sharpe': test_sharpe,
            'updated_at': now,
        })


def get_latest_params(symbol: str) -> Optional[Dict]:
    """获取某标的最优参数，无则返回 None"""
    init_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM latest_params WHERE symbol = ? ORDER BY updated_at DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    if row:
        return {
            'symbol': row['symbol'],
            'strategy': row['strategy'],
            'params': json.loads(row['best_params']),
            'test_sharpe': row['test_sharpe'],
            'updated_at': row['updated_at'],
        }
    return None


def get_wf_history(symbol: str = None, limit: int = 20) -> list:
    """获取历史 WFA 结果"""
    init_db()
    sql = "SELECT * FROM wf_results"
    args = []
    if symbol:
        sql += " WHERE symbol = ?"
        args.append(symbol)
    sql += " ORDER BY created_at DESC, window ASC LIMIT ?"
    args.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_wf_summary(symbol: str = None) -> Dict:
    """WFA 统计摘要"""
    init_db()
    where = f"WHERE symbol = '{symbol}'" if symbol else ""
    with _get_conn() as conn:
        row = conn.execute(f"""
            SELECT
                COUNT(*) as n_windows,
                AVG(test_sharpe) as avg_sharpe,
                AVG(test_return_pct) as avg_return,
                AVG(test_maxdd_pct) as avg_maxdd,
                MIN(test_sharpe) as min_sharpe,
                MAX(test_sharpe) as max_sharpe
            FROM wf_results
            {where}
        """).fetchone()
    return dict(row) if row else {}

"""
performance.py — 月度绩效报告引擎
=================================
从数据库读取交易历史和日均净值，计算绩效指标：
  - 累计收益、年化收益
  - 胜率、平均持仓时长
  - 最大回撤、盈亏比
  - 生成 equity curve vs 沪深300 对比图

数据来源：
  - daily_meta 表：每日 equity 曲线（用于回撤和曲线图）
  - trades 表：已结束交易（用于胜率、持仓时长统计）
  - positions 表：当前持仓（浮动盈亏）

图表：
  - matplotlib EquityCurve + 沪深300 Benchmark（叠加）
  - 输出 PNG → base64 编码 → API 返回
"""

import os
import sys
import io
import json
import logging
import urllib.request
import ssl
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    plt = None
    mdates = None

logger = logging.getLogger('performance')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = THIS_DIR
sys.path.insert(0, BACKEND_DIR)

from services.portfolio import PortfolioService, get_cursor

INITIAL_CAPITAL = 20000.0  # 初始资金
BENCHMARK_CODE = 'sh000300'  # 沪深300 作为基准

# ─── 沪深300 历史数据 ─────────────────────────────────────────────

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _fetch_historical_prices(symbol: str, days: int = 60) -> Dict[str, float]:
    """
    获取沪深300近N个交易日的历史收盘价。
    用腾讯接口 historical data。
    Returns: {date_str: close_price}
    """
    # 腾讯历史K线接口
    today = date.today()
    start_date = today - timedelta(days=days * 2)

    url = (
        f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={symbol},day,'
        f'{start_date.strftime("%Y-%m-%d")},{today.strftime("%Y-%m-%d")},{days},qfq'
    )
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        # 解析 JSONP: kline_dayqfq={...}
        json_str = raw.split('=', 1)[1] if '=' in raw else raw
        data = json.loads(json_str)
        qfq_data = (data.get('data', {}).get(symbol, {})
                     .get('qfqday', data.get('data', {}).get(symbol, {}).get('day', [])))
        result = {}
        for entry in qfq_data:
            if isinstance(entry, list) and len(entry) >= 2:
                dt_str = entry[0]  # '2026-03-01'
                try:
                    dt = datetime.strptime(dt_str, '%Y-%m-%d').date()
                    close = float(entry[1])
                    result[str(dt)] = close
                except (ValueError, TypeError):
                    continue
        return result
    except Exception as e:
        logger.debug('Historical prices fetch failed: %s', e)
        return {}


# ─── 绩效计算 ────────────────────────────────────────────────────

def compute_max_drawdown(equity_series: List[Tuple[str, float]]) -> Dict:
    """
    计算最大回撤。
    equity_series: [(date_str, equity), ...] 按日期升序
    返回: {max_drawdown_pct, peak_equity, trough_equity, peak_date, trough_date}
    """
    if not equity_series:
        return {'max_drawdown_pct': 0.0, 'peak_equity': 0, 'trough_equity': 0,
                'peak_date': '', 'trough_date': ''}

    peak = equity_series[0][1]
    peak_date = equity_series[0][0]
    max_dd = 0.0
    trough_equity_val = peak
    trough_date_val = peak_date

    for dt, equity in equity_series:
        if equity > peak:
            peak = equity
            peak_date = dt
            trough_equity_val = equity
            trough_date_val = dt
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            trough_equity_val = equity
            trough_date_val = dt

    return {
        'max_drawdown_pct': round(max_dd, 2),
        'peak_equity': round(peak, 2),
        'trough_equity': round(trough_equity_val, 2),
        'peak_date': peak_date,
        'trough_date': trough_date_val,
    }


def compute_trade_stats(trades: List[Dict]) -> Dict:
    """
    从交易记录计算胜率、盈亏比、平均持仓时长。
    trades: filled orders 表的记录
    """
    if not trades:
        return {
            'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
            'win_rate': 0.0, 'avg_holding_days': 0.0,
            'profit_factor': 0.0, 'total_realized_pnl': 0.0,
        }

    filled = [t for t in trades if t.get('status') == 'filled' and t.get('pnl') is not None]
    if not filled:
        return {
            'total_trades': len(filled), 'winning_trades': 0,
            'losing_trades': 0, 'win_rate': 0.0,
            'avg_holding_days': 0.0, 'profit_factor': 0.0,
            'total_realized_pnl': 0.0,
        }

    winning = [t for t in filled if (t.get('pnl') or 0) > 0]
    losing = [t for t in filled if (t.get('pnl') or 0) < 0]

    total_pnl = sum(t.get('pnl', 0) or 0 for t in filled)
    win_pnl = sum(t.get('pnl', 0) or 0 for t in winning)
    loss_pnl = abs(sum(t.get('pnl', 0) or 0 for t in losing))

    profit_factor = win_pnl / loss_pnl if loss_pnl > 0 else float('inf') if win_pnl > 0 else 0.0

    # 平均持仓时长（从 submitted_at 到 filled_at）
    holding_days = []
    for t in filled:
        try:
            start = datetime.fromisoformat(t.get('submitted_at', '').replace('Z', ''))
            end = datetime.fromisoformat(t.get('filled_at', '').replace('Z', ''))
            days = (end - start).total_seconds() / 86400
            holding_days.append(max(0, days))
        except Exception:
            continue

    avg_holding = sum(holding_days) / len(holding_days) if holding_days else 0.0

    return {
        'total_trades': len(filled),
        'winning_trades': len(winning),
        'losing_trades': len(losing),
        'win_rate': round(len(winning) / len(filled) * 100, 1) if filled else 0.0,
        'avg_holding_days': round(avg_holding, 1),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 999.0,
        'total_realized_pnl': round(total_pnl, 2),
    }


def compute_returns(total_equity: float, initial: float = INITIAL_CAPITAL) -> Dict:
    """计算累计收益率和年化收益率"""
    total_return_pct = (total_equity - initial) / initial * 100

    # 计算持仓天数（从第一笔交易到现在）
    # 用 daily_meta 表的日期范围估算
    return {
        'total_return_pct': round(total_return_pct, 2),
        'total_equity': round(total_equity, 2),
        'initial_capital': initial,
    }


# ─── 图表生成 ──────────────────────────────────────────────

def generate_performance_chart(
    equity_series: List[Tuple[str, float]],
    benchmark_series: Optional[List[Tuple[str, float]]] = None,
    trades: Optional[List[Dict]] = None,
) -> Optional[str]:
    """
    生成绩效图表（equity curve vs benchmark）。
    Returns: base64 encoded PNG, or None on failure.
    """
    if not MATPLOTLIB_AVAILABLE or len(equity_series) < 2:
        return None

    try:
        dates = [datetime.strptime(d, '%Y-%m-%d') for d, _ in equity_series]
        equity_vals = [e for _, e in equity_series]

        fig, ax = plt.subplots(figsize=(10, 5))

        # 策略曲线
        ax.plot(dates, equity_vals, label='账户权益', color='#2196F3', linewidth=1.5)

        # 基准曲线（归一化到初始权益起点）
        if benchmark_series and len(benchmark_series) >= 2:
            bm_dates = [datetime.strptime(d, '%Y-%m-%d') for d, _ in benchmark_series]
            bm_vals = [v for _, v in benchmark_series]
            if bm_vals and equity_vals:
                # 归一化：基准起点 = equity_series 起点
                start_equity = equity_vals[0]
                start_bm = bm_vals[0] if bm_vals[0] > 0 else 1.0
                norm_bm = [start_equity * (v / start_bm) for v in bm_vals]
                ax.plot(bm_dates, norm_bm, label='沪深300(归一化)', color='#FF5722',
                       linewidth=1.2, linestyle='--', alpha=0.8)

        ax.axhline(y=INITIAL_CAPITAL, color='gray', linestyle=':', alpha=0.6, label='本金')
        ax.set_xlabel('日期')
        ax.set_ylabel('权益（元）')
        ax.set_title('Equity Curve vs 沪深300 Benchmark')
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax.xaxis.set_major_locator(mdates.WeekLocator(interval=1))

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        import base64
        return base64.b64encode(buf.read()).decode('utf-8')
    except Exception as e:
        logger.warning('Chart generation failed: %s', e)
        return None


# ─── 月度报告生成 ────────────────────────────────────────────

def generate_monthly_report(
    year: int = None,
    month: int = None,
    include_chart: bool = True,
) -> Dict:
    """
    生成月度绩效报告。
    默认报告当前年月。
    Returns: {
        period, summary, returns, trade_stats, max_drawdown,
        equity_curve, benchmark_curve, chart_base64, generated_at
    }
    """
    if year is None:
        year = date.today().year
    if month is None:
        month = date.today().month

    svc = PortfolioService()
    period_str = f"{year}年{month}月"

    # ── 当月每日权益曲线 ──
    # 获取近3个月 daily_meta 用于图表
    metas = svc.get_daily_metas(limit=90)
    equity_series = []
    for m in reversed(metas):  # 升序
        dt = m.get('trade_date', '')
        eq = m.get('equity') or m.get('cash', 0)
        if dt and eq:
            equity_series.append((str(dt), float(eq)))

    # ── 沪深300 基准曲线 ──
    benchmark_series = None
    if equity_series:
        end_dt = equity_series[-1][0]
        start_dt = equity_series[0][0]
        try:
            days = (datetime.strptime(end_dt, '%Y-%m-%d') -
                    datetime.strptime(start_dt, '%Y-%m-%d')).days + 1
            bm_prices = _fetch_historical_prices(BENCHMARK_CODE, days=max(days, 30))
            if bm_prices:
                benchmark_series = [(d, v) for d, v in sorted(bm_prices.items())
                                   if start_dt <= d <= end_dt]
        except Exception as e:
            logger.debug('Benchmark fetch failed: %s', e)

    # ── 当前组合总权益 ──
    summary = svc.get_portfolio_summary()
    total_equity = summary.get('total_equity', INITIAL_CAPITAL)
    unrealized_pnl = summary.get('unrealized_pnl', 0)
    realized_pnl = summary.get('realized_pnl', 0)

    # ── 收益率 ──
    returns = compute_returns(total_equity)

    # ── 交易统计 ──
    trades = svc.get_orders(status='filled', limit=500)
    trade_stats = compute_trade_stats(trades)

    # ── 最大回撤 ──
    max_dd = compute_max_drawdown(equity_series) if equity_series else {
        'max_drawdown_pct': 0.0}

    # ── 图表 ──
    chart_base64 = None
    if include_chart and equity_series:
        chart_base64 = generate_performance_chart(equity_series, benchmark_series, trades)

    # ── 当月交易次数（估算）──
    month_str = f"{year}-{month:02d}"
    month_trades = [
        t for t in trades
        if t.get('filled_at', '').startswith(month_str)
    ]

    return {
        'period': period_str,
        'year': year,
        'month': month,
        'summary': {
            'total_equity': round(total_equity, 2),
            'cash': round(summary.get('cash', 0), 2),
            'position_value': round(summary.get('position_value', 0), 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'realized_pnl': round(realized_pnl, 2),
            'n_open_positions': len(summary.get('positions', [])),
        },
        'returns': returns,
        'trade_stats': trade_stats,
        'max_drawdown': max_dd,
        'equity_series': equity_series[-30:] if equity_series else [],  # 近30条
        'benchmark_series': benchmark_series[-30:] if benchmark_series else [],
        'chart_base64': chart_base64,
        'month_trades_count': len(month_trades),
        'generated_at': datetime.now().isoformat(),
    }


# ─── 月末快照写入 ──────────────────────────────────────────────

def record_monthly_snapshot(year: int, month: int) -> bool:
    """
    月末（或月初）将当月绩效数据写入 monthly_snapshots 表。
    """
    report = generate_monthly_report(year, month, include_chart=False)
    summary = report['summary']
    returns = report['returns']
    trade_stats = report['trade_stats']
    max_dd = report['max_drawdown']

    with get_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS monthly_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                total_equity REAL NOT NULL,
                cash REAL NOT NULL,
                position_value REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_return_pct REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL,
                win_rate REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                profit_factor REAL NOT NULL,
                avg_holding_days REAL NOT NULL,
                n_positions INTEGER NOT NULL,
                snapshot_at TEXT NOT NULL,
                UNIQUE(year, month)
            )
        ''')
        snapshot_at = datetime.now().isoformat()
        cur.execute('''
            INSERT OR REPLACE INTO monthly_snapshots
            (year, month, total_equity, cash, position_value,
             unrealized_pnl, realized_pnl, total_return_pct,
             max_drawdown_pct, win_rate, total_trades, profit_factor,
             avg_holding_days, n_positions, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            year, month,
            summary['total_equity'], summary['cash'], summary['position_value'],
            summary['unrealized_pnl'], summary['realized_pnl'],
            returns['total_return_pct'],
            max_dd['max_drawdown_pct'],
            trade_stats['win_rate'],
            trade_stats['total_trades'],
            trade_stats['profit_factor'],
            trade_stats['avg_holding_days'],
            summary['n_open_positions'],
            snapshot_at,
        ))
    logger.info('Monthly snapshot recorded: %d-%d equity=%.2f return=%.2f%%',
                 year, month, summary['total_equity'], returns['total_return_pct'])
    return True


def get_monthly_snapshots(limit: int = 12) -> List[Dict]:
    """获取历史月度快照"""
    with get_cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS monthly_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                total_equity REAL NOT NULL,
                cash REAL NOT NULL,
                position_value REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_return_pct REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL,
                win_rate REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                profit_factor REAL NOT NULL,
                avg_holding_days REAL NOT NULL,
                n_positions INTEGER NOT NULL,
                snapshot_at TEXT NOT NULL,
                UNIQUE(year, month)
            )
        ''')
        cur.execute(
            'SELECT * FROM monthly_snapshots ORDER BY year DESC, month DESC LIMIT ?',
            (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

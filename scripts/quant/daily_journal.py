"""
daily_journal.py — 交易日志分析模块
====================================
字段：date / symbol / direction / entry_price / exit_price / shares / pnl /
      signal_reason / regime / slippage_bps

功能：
  - 统计各信号触发频率（RSI vs MACD vs BBANDS）
  - 统计各环境下胜率（Bull vs Bear vs Volatile）
  - 统计滑点分布（avg, p95）
  - 与 Backend signals/trades 表联动
"""

import os
import sys
import json
import logging
from datetime import datetime, date
from typing import Optional, Dict, List
from collections import Counter

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BK_DIR   = os.path.join(os.path.dirname(THIS_DIR), 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, BK_DIR)

_log = logging.getLogger('daily_journal')
BASE_URL = 'http://127.0.0.1:5555'


# ─── 数据获取 ────────────────────────────────────────────────────────────

def api_get(path: str) -> dict:
    try:
        import urllib.request
        req = urllib.request.Request(f'{BASE_URL}{path}')
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning('API GET %s failed: %s', path, e)
        return {}


def get_all_trades() -> list:
    """获取所有历史成交。"""
    result = api_get('/trades')
    return result.get('trades', [])


def get_all_signals() -> list:
    """获取所有信号记录。"""
    result = api_get('/signals')
    return result.get('signals', [])


# ─── Journal Entry ──────────────────────────────────────────────────────

def build_journal_entry(trade: dict, signal: dict = None) -> dict:
    """
    从单笔成交构建 journal 条目。
    关联对应信号（如有）。
    """
    return {
        'date':         str(trade.get('executed_at', ''))[:10],
        'symbol':       trade.get('symbol', ''),
        'direction':   trade.get('direction', ''),
        'entry_price':  trade.get('price', 0),
        'shares':       trade.get('shares', 0),
        'pnl':           trade.get('pnl', 0),
        'slippage_bps': (trade.get('slippage_bps') or 0),
        'signal':       signal.get('signal', '') if signal else '',
        'signal_reason': signal.get('reason', '') if signal else '',
        'regime':       _extract_regime(signal.get('reason', '')) if signal else '',
    }


def _extract_regime(reason: str) -> str:
    """从信号原因中提取 Regime 标签。"""
    for r in ['BULL', 'BEAR', 'VOLATILE', 'CALM']:
        if r in reason:
            return r
    return 'UNKNOWN'


# ─── Journal 分析 ───────────────────────────────────────────────────────

def analyze_journal(trades: list, signals: list) -> dict:
    """
    分析 journal 数据，返回统计摘要。
    """
    if not trades:
        return {'error': 'No trades to analyze'}

    # 关联信号
    sig_by_symbol = {}
    for s in signals:
        sym = s.get('symbol', '')
        if sym not in sig_by_symbol:
            sig_by_symbol[sym] = []
        sig_by_symbol[sym].append(s)

    # 构建 journal
    journal = []
    for t in trades:
        sym = t.get('symbol', '')
        # 找对应的最近信号
        matched_sig = None
        t_date = str(t.get('executed_at', ''))[:10]
        for s in sig_by_symbol.get(sym, []):
            s_date = str(s.get('emitted_at', ''))[:10]
            if s_date == t_date:
                matched_sig = s
                break
        journal.append(build_journal_entry(t, matched_sig))

    # ── 基础统计 ──────────────────────────────────────────────────────────
    total_trades   = len(journal)
    wins           = sum(1 for j in journal if (j['pnl'] or 0) > 0)
    losses         = sum(1 for j in journal if (j['pnl'] or 0) <= 0)
    win_rate       = wins / total_trades * 100 if total_trades > 0 else 0
    avg_pnl        = sum((j['pnl'] or 0) for j in journal) / total_trades if total_trades > 0 else 0
    total_pnl      = sum((j['pnl'] or 0) for j in journal)

    # ── 按信号类型统计 ────────────────────────────────────────────────────
    by_signal: Dict[str, dict] = {}
    for j in journal:
        sig_type = j['signal'] or 'UNKNOWN'
        if sig_type not in by_signal:
            by_signal[sig_type] = {'trades': 0, 'wins': 0, 'pnl_sum': 0, 'losses': 0}
        by_signal[sig_type]['trades'] += 1
        by_signal[sig_type]['pnl_sum'] += (j['pnl'] or 0)
        if (j['pnl'] or 0) > 0:
            by_signal[sig_type]['wins'] += 1
        else:
            by_signal[sig_type]['losses'] += 1

    for sig_type, stats in by_signal.items():
        n = stats['trades']
        stats['win_rate'] = stats['wins'] / n * 100 if n > 0 else 0
        stats['avg_pnl']  = stats['pnl_sum'] / n if n > 0 else 0

    # ── 按 Regime 统计 ────────────────────────────────────────────────────
    by_regime: Dict[str, dict] = {}
    for j in journal:
        regime = j['regime'] or 'UNKNOWN'
        if regime not in by_regime:
            by_regime[regime] = {'trades': 0, 'wins': 0, 'pnl_sum': 0}
        by_regime[regime]['trades'] += 1
        by_regime[regime]['pnl_sum'] += (j['pnl'] or 0)
        if (j['pnl'] or 0) > 0:
            by_regime[regime]['wins'] += 1

    for regime, stats in by_regime.items():
        n = stats['trades']
        stats['win_rate'] = stats['wins'] / n * 100 if n > 0 else 0
        stats['avg_pnl']  = stats['pnl_sum'] / n if n > 0 else 0

    # ── 滑点统计 ──────────────────────────────────────────────────────────
    slippage_bps_list = [(j['slippage_bps'] or 0) for j in journal if j['slippage_bps'] is not None != 0]
    if slippage_bps_list:
        slippage_avg = sum(slippage_bps_list) / len(slippage_bps_list)
        sorted_bps   = sorted(slippage_bps_list)
        p95_idx      = int(len(sorted_bps) * 0.95)
        slippage_p95 = sorted_bps[p95_idx] if p95_idx < len(sorted_bps) else sorted_bps[-1]
    else:
        slippage_avg = 0
        slippage_p95 = 0

    return {
        'total_trades':   total_trades,
        'wins':           wins,
        'losses':         losses,
        'win_rate':       round(win_rate, 1),
        'avg_pnl':        round(avg_pnl, 2),
        'total_pnl':      round(total_pnl, 2),
        'by_signal':      {k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                               for kk, vv in v.items()}
                           for k, v in by_signal.items()},
        'by_regime':      {k: {kk: round(vv, 2) if isinstance(vv, float) else vv
                               for kk, vv in v.items()}
                           for k, v in by_regime.items()},
        'slippage_avg':   round(slippage_avg, 2),
        'slippage_p95':   round(slippage_p95, 2),
        'slippage_n':     len(slippage_bps_list),
    }


def format_journal_summary(stats: dict) -> str:
    """将分析结果格式化为易读文本。"""
    lines = [
        f"【绩效归因】",
        f"",
        f"总交易: {stats['total_trades']}笔  胜率: {stats['win_rate']:.0f}%  "
        f"场均: {stats['avg_pnl']:+.0f}  累计: {stats['total_pnl']:+.0f}",
        f"",
    ]

    # 按信号统计
    lines.append("按信号类型:")
    by_sig = stats.get('by_signal', {})
    for sig_type, s in sorted(by_sig.items(), key=lambda x: x[1]['trades'], reverse=True):
        lines.append(
            f"  {sig_type:12s}: {s['trades']:3d}笔 "
            f"WR={s['win_rate']:5.0f}%  均值={s['avg_pnl']:+.0f}"
        )

    # 按 Regime 统计
    lines.append("")
    lines.append("按市场环境:")
    by_reg = stats.get('by_regime', {})
    for regime, s in sorted(by_reg.items(), key=lambda x: x[1]['trades'], reverse=True):
        lines.append(
            f"  {regime:12s}: {s['trades']:3d}笔 "
            f"WR={s['win_rate']:5.0f}%  均值={s['avg_pnl']:+.0f}"
        )

    # 滑点
    lines.append("")
    lines.append(
        f"滑点: 平均={stats['slippage_avg']:.1f}bps  "
        f"P95={stats['slippage_p95']:.1f}bps (n={stats['slippage_n']})"
    )

    return '\n'.join(lines)


# ─── 主程序 ─────────────────────────────────────────────────────────────

def run_summary() -> dict:
    """获取完整 journal 分析摘要。"""
    _log.info('Fetching trades and signals...')
    trades  = get_all_trades()
    signals = get_all_signals()
    _log.info('Got %d trades, %d signals', len(trades), len(signals))

    stats = analyze_journal(trades, signals)
    report_text = format_journal_summary(stats)
    _log.info('Journal Summary:\n%s', report_text)

    return stats


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    result = run_summary()
    print()
    print(format_journal_summary(result))

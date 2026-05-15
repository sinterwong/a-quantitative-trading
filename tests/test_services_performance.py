"""
tests/test_services_performance.py — backend/services/performance.py 单元测试 (P1-2)

覆盖纯计算函数:
  - compute_max_drawdown
  - compute_trade_stats
  - compute_returns

不包含 generate_monthly_report / record_monthly_snapshot 等带 DB 的方法
(已在 api 冒烟测试中通过 patch 间接覆盖)。
"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend',
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from services.performance import (
    compute_max_drawdown, compute_trade_stats, compute_returns,
)


# ── compute_max_drawdown ─────────────────────────────────

def test_max_drawdown_empty_series():
    out = compute_max_drawdown([])
    assert out['max_drawdown_pct'] == 0.0
    assert out['peak_equity'] == 0
    assert out['peak_date'] == ''


def test_max_drawdown_monotonic_increase_returns_zero():
    series = [('2026-04-01', 100), ('2026-04-02', 110), ('2026-04-03', 120)]
    out = compute_max_drawdown(series)
    assert out['max_drawdown_pct'] == 0.0
    assert out['peak_equity'] == 120


def test_max_drawdown_basic_drop():
    """100 → 80,回撤 20%。"""
    series = [('2026-04-01', 100), ('2026-04-02', 80)]
    out = compute_max_drawdown(series)
    assert out['max_drawdown_pct'] == pytest.approx(20.0, abs=0.01)
    assert out['peak_equity'] == 100
    assert out['trough_equity'] == 80
    assert out['peak_date'] == '2026-04-01'
    assert out['trough_date'] == '2026-04-02'


def test_max_drawdown_finds_deepest_after_new_peak():
    """100→90 (10%)→110 (新高)→85 (22.7%)。最大回撤应是 22.7%。"""
    series = [
        ('D1', 100), ('D2', 90), ('D3', 110), ('D4', 85),
    ]
    out = compute_max_drawdown(series)
    # 从 110 到 85 = (110-85)/110 = 22.73%
    assert out['max_drawdown_pct'] == pytest.approx(22.73, abs=0.05)
    assert out['peak_date'] == 'D3'
    assert out['trough_date'] == 'D4'


def test_max_drawdown_zero_peak_handled():
    """初始 peak=0 时不应除零。"""
    series = [('D1', 0), ('D2', 0)]
    out = compute_max_drawdown(series)
    assert out['max_drawdown_pct'] == 0.0


# ── compute_trade_stats ──────────────────────────────────

def test_trade_stats_empty():
    out = compute_trade_stats([])
    assert out['total_trades'] == 0
    assert out['win_rate'] == 0.0


def test_trade_stats_no_filled_trades_returns_zeros():
    """有交易但全部 status != 'filled' → 全 0 但不崩。"""
    trades = [{'status': 'cancelled', 'pnl': 100}]
    out = compute_trade_stats(trades)
    assert out['total_trades'] == 0
    assert out['win_rate'] == 0.0


def test_trade_stats_basic_winrate():
    trades = [
        {'status': 'filled', 'pnl': 100, 'submitted_at': '2026-04-01T09:30',
         'filled_at': '2026-04-03T09:30'},
        {'status': 'filled', 'pnl': -50, 'submitted_at': '2026-04-05T09:30',
         'filled_at': '2026-04-07T09:30'},
        {'status': 'filled', 'pnl': 200, 'submitted_at': '2026-04-10T09:30',
         'filled_at': '2026-04-12T09:30'},
    ]
    out = compute_trade_stats(trades)
    assert out['total_trades'] == 3
    assert out['winning_trades'] == 2
    assert out['losing_trades'] == 1
    assert out['win_rate'] == pytest.approx(66.7, abs=0.1)
    assert out['total_realized_pnl'] == 250.0
    # profit_factor = win/|loss| = 300/50 = 6.0
    assert out['profit_factor'] == 6.0


def test_trade_stats_no_losing_returns_capped_pf():
    """全胜 → profit_factor 显示为 999.0(代码内 sentinel)。"""
    trades = [
        {'status': 'filled', 'pnl': 100, 'submitted_at': '', 'filled_at': ''},
        {'status': 'filled', 'pnl': 200, 'submitted_at': '', 'filled_at': ''},
    ]
    out = compute_trade_stats(trades)
    assert out['winning_trades'] == 2
    assert out['profit_factor'] == 999.0


def test_trade_stats_skips_pnl_none():
    """status=filled 但 pnl=None → 被 filled list 过滤掉。"""
    trades = [
        {'status': 'filled', 'pnl': None, 'submitted_at': '', 'filled_at': ''},
        {'status': 'filled', 'pnl': 50, 'submitted_at': '', 'filled_at': ''},
    ]
    out = compute_trade_stats(trades)
    assert out['total_trades'] == 1  # 仅有效 pnl 的那笔


def test_trade_stats_holding_days_robust_to_bad_timestamps():
    """submitted_at / filled_at 格式坏 → avg_holding_days=0,不抛。"""
    trades = [
        {'status': 'filled', 'pnl': 10,
         'submitted_at': 'not a timestamp', 'filled_at': 'also bad'},
    ]
    out = compute_trade_stats(trades)
    assert out['avg_holding_days'] == 0.0
    assert out['total_realized_pnl'] == 10.0


# ── compute_returns ──────────────────────────────────────

def test_compute_returns_positive():
    out = compute_returns(120_000, initial=100_000)
    assert out['total_return_pct'] == 20.0
    assert out['total_equity'] == 120_000
    assert out['initial_capital'] == 100_000


def test_compute_returns_negative():
    out = compute_returns(80_000, initial=100_000)
    assert out['total_return_pct'] == -20.0


def test_compute_returns_default_initial():
    """默认 initial=INITIAL_CAPITAL (services.performance.INITIAL_CAPITAL = 20000)。"""
    from services.performance import INITIAL_CAPITAL
    out = compute_returns(INITIAL_CAPITAL)
    assert out['total_return_pct'] == 0.0
    assert out['initial_capital'] == INITIAL_CAPITAL

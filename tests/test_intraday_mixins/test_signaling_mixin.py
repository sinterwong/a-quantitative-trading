"""
SignalingMixin 单元测试 — 主要分支早退路径,避免触发完整 _check_new_positions 链路。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.services.intraday.signaling import (
    SignalingMixin, BUY_THRESHOLD_NEW, BUY_THRESHOLD_ADD,
)


# ── 阈值常量 sanity ─────────────────────────────────────

def test_buy_thresholds_constants_are_sensible():
    """加仓阈值应低于建仓阈值(边际成本论)。"""
    assert 0 < BUY_THRESHOLD_ADD < BUY_THRESHOLD_NEW
    assert BUY_THRESHOLD_NEW <= 1.0


# ── _check_new_positions 早退路径 ───────────────────────

def test_check_new_positions_skips_when_portfolio_in_warn(monitor):
    """组合熔断已触发 → 立即返回,不调任何下层方法。"""
    monitor._risk_warn_fired = True
    monitor._get_watched_symbols = MagicMock()  # 不应被调用

    SignalingMixin._check_new_positions(monitor, datetime.now())
    monitor._get_watched_symbols.assert_not_called()


def test_check_new_positions_skips_when_portfolio_in_stop(monitor):
    monitor._risk_warn_fired = False
    monitor._risk_stop_fired = True
    monitor._get_watched_symbols = MagicMock()

    SignalingMixin._check_new_positions(monitor, datetime.now())
    monitor._get_watched_symbols.assert_not_called()


def test_check_new_positions_skips_when_no_watched_symbols(monitor):
    monitor._risk_warn_fired = False
    monitor._risk_stop_fired = False
    monitor._get_watched_symbols = MagicMock(return_value=set())

    SignalingMixin._check_new_positions(monitor, datetime.now())
    monitor._get_watched_symbols.assert_called_once()
    # 没有 watched → 早退,不调 strategy_runner
    # 我们无法直接断言 last_scores 没被读,但至少 _submit_with_routing 不被调
    monitor._submit_with_routing.assert_not_called()


def test_check_new_positions_skips_when_no_pipeline_scores(monitor):
    """有 watched 但无 pipeline_scores(StrategyRunner=None)→ 跳过所有 symbol。"""
    monitor._risk_warn_fired = False
    monitor._risk_stop_fired = False
    monitor._get_watched_symbols = MagicMock(return_value={'A.SH', 'B.SH'})
    monitor._strategy_runner = None   # 关键:无 runner → 无 scores

    SignalingMixin._check_new_positions(monitor, datetime.now())
    # 无 score → 不进入 confirm_signal_minute / _submit_with_routing
    monitor._submit_with_routing.assert_not_called()


# ── _check_and_push 早退路径 ───────────────────────────

def test_check_and_push_increments_scan_count(monitor):
    """调用一次后 _scan_count 必然 +1。"""
    monitor._scan_count = 5
    monitor._svc.refresh_prices = MagicMock()
    monitor._svc.get_positions.return_value = []
    monitor._svc.get_portfolio_summary.return_value = {'total_equity': 100000}

    # 防止下层方法调用真实业务
    monitor._run_daily_health_check = MagicMock()
    monitor._sync_market_regime = MagicMock()
    monitor._check_market_index = MagicMock()
    monitor._check_watchlist = MagicMock()
    monitor._check_sector_flow = MagicMock()

    SignalingMixin._check_and_push(monitor, datetime.now())
    assert monitor._scan_count == 6


def test_check_and_push_returns_early_when_no_positions(monitor):
    """无持仓时,直接更新 peak_equity 并重置 risk flags,不进入信号生成。"""
    monitor._scan_count = 0
    monitor._svc.refresh_prices = MagicMock()
    monitor._svc.get_positions.return_value = []
    monitor._svc.get_portfolio_summary.return_value = {'total_equity': 200000}

    monitor._run_daily_health_check = MagicMock()
    monitor._sync_market_regime = MagicMock()
    monitor._check_market_index = MagicMock()
    monitor._check_watchlist = MagicMock()
    monitor._check_sector_flow = MagicMock()
    monitor._risk_warn_fired = True   # 应被重置
    monitor._risk_stop_fired = True

    SignalingMixin._check_and_push(monitor, datetime.now())

    assert monitor._risk_warn_fired is False
    assert monitor._risk_stop_fired is False
    # 不应进入 _check_sector_concentration / _run_exit_engine
    monitor._check_sector_concentration.assert_not_called()
    monitor._run_exit_engine.assert_not_called()


def test_check_and_push_handles_refresh_prices_error(monitor):
    """refresh_prices 抛异常 → 不传播,继续后续流程。"""
    monitor._scan_count = 0
    monitor._svc.refresh_prices.side_effect = RuntimeError('network down')
    monitor._svc.get_positions.return_value = []
    monitor._svc.get_portfolio_summary.return_value = {'total_equity': 100}

    monitor._run_daily_health_check = MagicMock()
    monitor._sync_market_regime = MagicMock()
    monitor._check_market_index = MagicMock()
    monitor._check_watchlist = MagicMock()
    monitor._check_sector_flow = MagicMock()

    # 不抛即通过
    SignalingMixin._check_and_push(monitor, datetime.now())

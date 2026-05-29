"""
ExecutionMixin 单元测试 — 交易模式 / 算法路由 / 信号→订单。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from backend.services.intraday.execution import ExecutionMixin


def _make_alert(symbol='600519.SH', signal='BUY', price=100.0,
                reason='test', prev_rsi=25):
    """构造一个 SignalAlert-like 对象。"""
    a = MagicMock()
    a.symbol = symbol
    a.signal = signal
    a.price = price
    a.reason = reason
    a.prev_rsi = prev_rsi
    a.pct = 1.2
    a.day_chg = 1.2
    return a


# ── trading_mode / _can_trade ───────────────────────────

def test_can_trade_returns_true_only_in_live(monitor):
    monitor._trading_mode = 'live'
    assert ExecutionMixin._can_trade(monitor) is True
    monitor._trading_mode = 'simulation'
    assert ExecutionMixin._can_trade(monitor) is False


def test_load_trading_mode_missing_file_defaults_simulation(monitor, tmp_path, monkeypatch):
    """trading_mode.json 不存在 → 默认 simulation。"""
    monkeypatch.setattr(
        'backend.services.intraday.execution._BACKEND_DIR', str(tmp_path))
    ExecutionMixin._load_trading_mode(monitor)
    assert monitor._trading_mode == 'simulation'


def test_load_trading_mode_reads_existing_file(monitor, tmp_path, monkeypatch):
    monkeypatch.setattr(
        'backend.services.intraday.execution._BACKEND_DIR', str(tmp_path))
    (tmp_path / 'trading_mode.json').write_text(json.dumps({'mode': 'live'}))
    ExecutionMixin._load_trading_mode(monitor)
    assert monitor._trading_mode == 'live'


def test_load_trading_mode_corrupt_file_falls_back(monitor, tmp_path, monkeypatch):
    monkeypatch.setattr(
        'backend.services.intraday.execution._BACKEND_DIR', str(tmp_path))
    (tmp_path / 'trading_mode.json').write_text('not valid json {')
    ExecutionMixin._load_trading_mode(monitor)
    assert monitor._trading_mode == 'simulation'


def test_save_trading_mode_round_trip(monitor, tmp_path, monkeypatch):
    monkeypatch.setattr(
        'backend.services.intraday.execution._BACKEND_DIR', str(tmp_path))
    monitor._trading_mode = 'live'
    ExecutionMixin._save_trading_mode(monitor)
    data = json.loads((tmp_path / 'trading_mode.json').read_text())
    assert data['mode'] == 'live'
    assert 'updated_at' in data


# ── algo routing ──────────────────────────────────────────

def test_algo_config_falls_back_when_yaml_missing(monitor):
    """core.config.load_config 抛异常 → 返回内联默认值。"""
    with patch('core.config.load_config', side_effect=RuntimeError('no yaml')):
        ec = ExecutionMixin._algo_config(monitor)
    assert ec.algo_method == 'TWAP'
    assert ec.algo_threshold_amount == 500_000.0
    assert ec.algo_threshold_shares == 10_000


def _make_algo_config(**kwargs):
    """构造 algo_config 内联默认对象,允许字段覆盖。"""
    class _Cfg:
        enable_algo_routing = True
        algo_method = 'TWAP'
        algo_threshold_amount = 1_000_000_000
        algo_threshold_shares = 1_000_000_000
        algo_duration_minutes = 30
        algo_slice_interval = 5
    cfg = _Cfg()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def test_submit_with_routing_small_order_uses_single_submit(monitor):
    """订单金额低于阈值 → 直接走 broker.submit_order。"""
    monitor._broker = MagicMock()
    monitor._broker.submit_order.return_value = MagicMock(status='filled')
    monitor._algo_config = lambda: _make_algo_config()  # 极大阈值

    result = ExecutionMixin._submit_with_routing(
        monitor, symbol='600519.SH', direction='BUY',
        shares=100, price=100.0,
    )
    monitor._broker.submit_order.assert_called_once()
    assert result.status == 'filled'


def test_submit_with_routing_disabled_uses_single_submit(monitor):
    """enable_algo_routing=False → 强制单笔。"""
    monitor._broker = MagicMock()
    monitor._broker.submit_order.return_value = MagicMock(status='filled')
    monitor._algo_config = lambda: _make_algo_config(
        enable_algo_routing=False,
        algo_threshold_amount=100, algo_threshold_shares=1,
    )

    ExecutionMixin._submit_with_routing(
        monitor, symbol='X', direction='BUY',
        shares=1_000_000, price=1000.0,
    )
    monitor._broker.submit_order.assert_called_once()


def test_submit_with_routing_slice_pretrade_rejection_stops_emission(monitor):
    """大单拆 6 个 slice 时,中途 PreTrade 拒单 → 停止发后续 slice。"""
    monitor._broker = MagicMock()
    filled = MagicMock(status='filled', filled_shares=10_000,
                       avg_price=10.0, order_id='S1',
                       submitted_at='', filled_at='')
    monitor._broker.submit_order.return_value = filled
    monitor._algo_config = lambda: _make_algo_config(
        algo_method='TWAP', algo_duration_minutes=30, algo_slice_interval=5,
        algo_threshold_amount=100, algo_threshold_shares=1,
    )

    # RiskEngine: 前两次通过,第三次拒(模拟头寸累计到上限)
    fake_re = MagicMock()
    rr_ok = MagicMock(passed=True, reason='')
    rr_reject = MagicMock(passed=False, reason='Position 26% > 25% limit')
    fake_re.check.side_effect = [rr_ok, rr_ok, rr_reject, rr_ok, rr_ok, rr_ok]
    monitor._strategy_runner = MagicMock(risk_engine=fake_re)
    # MagicMock 默认对未定义属性返回 MagicMock,会让 getattr fallback 失效。
    # 显式绑定真实 helper。
    monitor._get_pretrade_risk_engine = ExecutionMixin._get_pretrade_risk_engine.__get__(monitor)
    monitor._check_slice_pretrade = ExecutionMixin._check_slice_pretrade.__get__(monitor)

    ExecutionMixin._submit_with_routing(
        monitor, symbol='600519.SH', direction='BUY',
        shares=60_000, price=10.0,
    )
    # 30/5 = 6 个 slice,第 3 个拒单 → 实际只下了 2 单
    assert monitor._broker.submit_order.call_count == 2


# ── _submit_order_for_signal ─────────────────────────────
# 这些方法访问 self.NO_TRADE_SIGNALS / SIGNAL_TO_ORDER 等类属性,
# 需要给 monitor 注入。

def _attach_class_attrs(monitor):
    monitor.NO_TRADE_SIGNALS = ExecutionMixin.NO_TRADE_SIGNALS
    monitor.SIGNAL_TO_ORDER = ExecutionMixin.SIGNAL_TO_ORDER


def test_submit_order_skips_no_trade_signal(monitor):
    """LIMIT_UP 等不交易信号 → 直接返回 None（不再调 _record_skip，由 OrderGate 记录）。"""
    _attach_class_attrs(monitor)
    alert = _make_alert(signal='LIMIT_UP')
    result = ExecutionMixin._submit_order_for_signal(monitor, alert)
    assert result is None


def test_submit_order_skips_unmapped_signal(monitor):
    """无 SIGNAL_TO_ORDER 映射 → 返回 None。"""
    _attach_class_attrs(monitor)
    alert = _make_alert(signal='UNKNOWN_SIG')
    result = ExecutionMixin._submit_order_for_signal(monitor, alert)
    assert result is None


def test_submit_order_buy_blocked_by_portfolio_drawdown(monitor):
    """组合熔断激活时 BUY 被阻断 — 现由 OrderGate/SignalingLayer 处理。

    此测试验证 fallback 路径（无 OrderGate）在 can_trade=False 时返回 None。
    完整的 drawdown 阻断逻辑已移至 _check_new_positions() 和 OrderGate。
    """
    _attach_class_attrs(monitor)
    monitor._order_gate = None
    monitor._can_trade.return_value = False  # simulation 模式
    alert = _make_alert(signal='BUY')
    result = ExecutionMixin._submit_order_for_signal(monitor, alert)
    assert result is None

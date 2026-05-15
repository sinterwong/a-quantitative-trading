"""
DataMixin 单元测试 — 选股加载 / 参数缓存 / 大盘 / 自选股 / 板块流。
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.services.intraday.data import DataMixin


def _attach_index_config(monitor):
    """DataMixin 通过 self.INDEX_CONFIG 引用类属性,MagicMock 自动拦截,
    显式注入真值确保 _check_market_index 走真实键。"""
    monitor.INDEX_CONFIG = DataMixin.INDEX_CONFIG


# ── _load_selector_once ──────────────────────────────────

def test_load_selector_once_skips_when_no_broker(monitor):
    monitor._broker = None
    DataMixin._load_selector_once(monitor)
    assert monitor._selector_cache == []


def test_load_selector_once_returns_quickly_if_loaded_today(monitor):
    """同一天 cache 不为空 → 直接返回。"""
    today = date.today().isoformat()
    monitor._selector_loaded_date = today
    monitor._selector_cache = ['X.SH']
    DataMixin._load_selector_once(monitor)
    assert monitor._selector_cache == ['X.SH']


def test_load_selector_once_skips_on_selector_error(monitor):
    """DynamicStockSelector 抛异常 → cache 为空,不传播。"""
    monitor._broker = MagicMock()
    monitor._llm = None
    with patch('sys.modules', {**__import__('sys').modules}):
        with patch.dict('sys.modules', {
            'scripts.dynamic_selector': MagicMock(
                DynamicStockSelector=MagicMock(side_effect=RuntimeError('boom')))
        }, clear=False):
            DataMixin._load_selector_once(monitor)
    # 异常导致 cache 设为 []
    assert monitor._selector_cache == []


# ── _get_watched_symbols ─────────────────────────────────

def test_get_watched_symbols_filters_existing_positions(monitor):
    monitor._selector_cache = ['A.SH', 'B.SH', 'C.SH']
    monitor._selector_loaded_date = date.today().isoformat()
    monitor._svc.get_positions.return_value = [{'symbol': 'B.SH'}]
    result = DataMixin._get_watched_symbols(monitor)
    assert result == {'A.SH', 'C.SH'}


def test_get_watched_symbols_empty_when_cache_empty(monitor):
    monitor._selector_cache = []
    monitor._selector_loaded_date = date.today().isoformat()
    monitor._svc.get_positions.return_value = []
    monitor._broker = None  # _load_selector_once 早退
    result = DataMixin._get_watched_symbols(monitor)
    assert result == set()


# ── _get_params ──────────────────────────────────────────

def test_get_params_caches_by_day(monitor):
    """同一天再次查询 → 不调 load_symbol_params。"""
    today = date.today().isoformat()
    monitor._params_cache_date = today
    monitor._params_cache = {'X': {'rsi_buy': 30}}
    with patch('services.signals.load_symbol_params') as mock_load:
        out = DataMixin._get_params(monitor, 'X')
    assert out == {'rsi_buy': 30}
    mock_load.assert_not_called()


def test_get_params_refreshes_on_new_day(monitor):
    """缓存日期与今日不一致 → 重新加载 + 调 _refresh_kelly_from_trades。"""
    monitor._params_cache_date = '2020-01-01'
    monitor._params_cache = {'X': 'stale'}
    monitor._refresh_kelly_from_trades = MagicMock()
    with patch('services.signals.load_symbol_params', return_value={'rsi_buy': 25}) as mock_load:
        out = DataMixin._get_params(monitor, 'X')
    assert out == {'rsi_buy': 25}
    mock_load.assert_called_once_with('X')
    monitor._refresh_kelly_from_trades.assert_called_once()


# ── _sync_market_regime ──────────────────────────────────

def test_sync_market_regime_uses_runner_when_available(monitor):
    runner = MagicMock()
    regime_obj = MagicMock()
    regime_obj.regime = 'BULL'
    regime_obj.reason = 'MA20 > MA60'
    regime_obj.atr_ratio = 0.85
    runner.current_regime = regime_obj
    monitor._strategy_runner = runner

    DataMixin._sync_market_regime(monitor)
    assert monitor._market_regime == {
        'regime': 'BULL', 'reason': 'MA20 > MA60', 'atr_ratio': 0.85,
    }


def test_sync_market_regime_falls_back_to_get_regime(monitor):
    """无 runner → 调 core.regime.get_regime。"""
    monitor._strategy_runner = None
    fake_regime = MagicMock()
    fake_regime.regime = 'CALM'
    fake_regime.reason = 'low vol'
    fake_regime.atr_ratio = 0.5
    with patch('core.regime.get_regime', return_value=fake_regime):
        DataMixin._sync_market_regime(monitor)
    assert monitor._market_regime['regime'] == 'CALM'


def test_sync_market_regime_swallows_errors(monitor):
    """get_regime 抛异常 → _market_regime 保留之前的(空字典)。"""
    monitor._strategy_runner = None
    with patch('core.regime.get_regime', side_effect=RuntimeError('offline')):
        DataMixin._sync_market_regime(monitor)
    assert monitor._market_regime == {}


# ── _check_market_index ─────────────────────────────────

def test_check_market_index_skips_when_no_data(monitor):
    _attach_index_config(monitor)
    with patch('services.signals.fetch_bulk', return_value={}):
        DataMixin._check_market_index(monitor, datetime.now())
    monitor._deliver_alert.assert_not_called()


def test_check_market_index_below_threshold_no_alert(monitor):
    _attach_index_config(monitor)
    # 上证 pct=0.5% < 1.5% 阈值
    fake_data = {'sh000001': {'pct': 0.5, 'price': 3000}}
    with patch('services.signals.fetch_bulk', return_value=fake_data):
        DataMixin._check_market_index(monitor, datetime.now())
    monitor._deliver_alert.assert_not_called()

"""
Unit tests for signal_generator.py
Run with: python tests/run_tests.py  (no pytest required)
Or: pytest tests/test_signal_generator.py -v
"""

import pytest
import random
import sys
import os

QUANT_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'quant')
sys.path.insert(0, QUANT_DIR)

from signal_generator import (
    SignalType, SignalSource, RSISignalSource,
    MarketRegimeSource, SignalGenerator, BlackListFilter,
)


def fake_loader(symbol, start, end):
    """60 bars of synthetic OHLCV data, seeded for reproducibility."""
    random.seed(42)
    data = []
    price = 10.0
    for i in range(60):
        price = price * (1 + random.uniform(-0.02, 0.025))
        data.append({
            'date': '2024-01-%02d' % (i + 1),
            'open':  round(price * 0.99, 2),
            'high':  round(price * 1.01, 2),
            'low':   round(price * 0.98, 2),
            'close':  round(price, 2),
            'volume': int(random.uniform(1e6, 5e6)),
        })
    return data


class FakeDataLoader:
    def get_kline(self, symbol, start, end):
        return fake_loader(symbol, start, end)


class TestSignalType:
    def test_buy_is_buy_string(self):
        assert SignalType.BUY == 'buy'

    def test_sell_is_sell_string(self):
        assert SignalType.SELL == 'sell'

    def test_hold_is_hold_string(self):
        assert SignalType.HOLD == 'hold'

    def test_signals_are_distinct(self):
        assert len({SignalType.BUY, SignalType.SELL, SignalType.HOLD}) == 3


class TestRSISignalSourceDefaults:
    def test_default_period(self):
        assert RSISignalSource('TEST').period == 21

    def test_default_oversold(self):
        assert RSISignalSource('TEST').oversold == 35

    def test_default_overbought(self):
        assert RSISignalSource('TEST').overbought == 65

    def test_default_stop_loss(self):
        assert RSISignalSource('TEST').stop_loss == 0.05

    def test_default_take_profit(self):
        assert RSISignalSource('TEST').take_profit == 0.20

    def test_custom_params(self):
        rsi = RSISignalSource('TEST', {
            'period': 14, 'oversold': 30, 'overbought': 70,
            'stop_loss': 0.10, 'take_profit': 0.30,
        })
        assert rsi.period == 14
        assert rsi.oversold == 30
        assert rsi.overbought == 70
        assert rsi.stop_loss == 0.10
        assert rsi.take_profit == 0.30


class TestRSISignalSourceData:
    def test_load_returns_true(self):
        rsi = RSISignalSource('TEST', {'period': 14})
        loader = FakeDataLoader()
        assert rsi.load(loader, '20240101', '20241231') is True

    def test_load_produces_rsi_values(self):
        rsi = RSISignalSource('TEST', {'period': 14})
        loader = FakeDataLoader()
        rsi.load(loader, '20240101', '20241231')
        assert len(rsi.data) == 60
        assert rsi._rsi_vals is not None

    def test_evaluate_before_enough_data_returns_hold(self):
        rsi = RSISignalSource('TEST', {'period': 14})
        loader = FakeDataLoader()
        rsi.load(loader, '20240101', '20241231')
        result = rsi.evaluate(5)
        assert result['signal'] == SignalType.HOLD
        assert result['reason'] == 'data_not_ready'

    def test_evaluate_returns_valid_signal(self):
        rsi = RSISignalSource('TEST', {'period': 14})
        loader = FakeDataLoader()
        rsi.load(loader, '20240101', '20241231')
        result = rsi.evaluate(30)
        assert result['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD)
        assert 0.0 <= result['strength'] <= 1.0
        assert 'reason' in result

    def test_reset_clears_state(self):
        rsi = RSISignalSource('TEST', {'period': 14})
        rsi._entry_price = 10.0
        rsi._hold_days = 5
        rsi.reset()
        assert rsi._entry_price == 0
        assert rsi._hold_days == 0


class TestMarketRegimeSource:
    def test_default_ma_period(self):
        assert MarketRegimeSource('TEST').ma_period == 200

    def test_load_produces_data(self):
        mr = MarketRegimeSource('TEST')
        loader = FakeDataLoader()
        assert mr.load(loader, '20240101', '20241231') is True

    def test_evaluate_returns_valid_signal(self):
        mr = MarketRegimeSource('TEST')
        loader = FakeDataLoader()
        mr.load(loader, '20240101', '20241231')
        result = mr.evaluate(59)
        assert result['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD)


class TestSignalGenerator:
    def test_empty_initially(self):
        gen = SignalGenerator('TEST')
        assert len(gen.sources) == 0

    def test_add_source(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={'period': 14}, weight=1.0)
        assert len(gen.sources) == 1

    def test_add_multiple_sources_with_weights(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={}, weight=1.0)
        gen.add_source(MarketRegimeSource, params={}, weight=0.5)
        assert len(gen.sources) == 2
        weights = [w for _, w in gen.sources]
        assert weights == [1.0, 0.5]

    def test_load_all(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={'period': 14}, weight=1.0)
        loader = FakeDataLoader()
        assert gen.load_all(loader, '20240101', '20241231') is True

    def test_get_source_returns_correct_type(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={}, weight=1.0)
        src = gen.get_source('RSI')
        assert isinstance(src, RSISignalSource)

    def test_evaluate_returns_valid_signal(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={'period': 14}, weight=1.0)
        loader = FakeDataLoader()
        gen.load_all(loader, '20240101', '20241231')
        result = gen.evaluate(30)
        assert result['signal'] in (SignalType.BUY, SignalType.SELL, SignalType.HOLD)

    def test_reset_all(self):
        gen = SignalGenerator('TEST')
        gen.add_source(RSISignalSource, params={}, weight=1.0)
        src = gen.get_source('RSI')
        src._entry_price = 10.0
        gen.reset_all()
        assert src._entry_price == 0


class TestBlackListFilter:
    def test_init_defaults(self):
        filt = BlackListFilter()
        assert filt.min_volume_ratio == 0.001
        assert filt.up_limit_discount == 0.90

    def test_up_limit_blocked(self):
        """Previous day limit-up (>9.5%) should block buying."""
        filt = BlackListFilter()
        allowed, reason = filt.can_buy(
            [{'close': 10.0}, {'close': 11.0, 'volume': 1e7}], 1
        )
        assert allowed is False

    def test_suspended_blocked(self):
        """Suspended stock (volume=0) should be blocked."""
        filt = BlackListFilter()
        allowed, reason = filt.can_buy(
            [{'close': 10.0}, {'close': 10.2, 'volume': 0}], 1
        )
        assert allowed is False

    def test_normal_stock_passes(self):
        """Normal stock with volume should pass."""
        filt = BlackListFilter()
        allowed, reason = filt.can_buy(
            [{'close': 10.0}, {'close': 10.2, 'volume': 1e7}], 1
        )
        assert allowed is True

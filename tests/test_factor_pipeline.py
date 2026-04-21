"""
Phase 2 Tests — FactorRegistry + FactorPipeline

运行方式：
    python tests/test_factor_pipeline.py
    pytest tests/test_factor_pipeline.py -v
"""

from __future__ import annotations
import sys
import os
import types

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_DIR)

import pandas as pd
import numpy as np

from core.factors.base import Factor, FactorCategory, Signal
from core.factor_registry import FactorRegistry, registry as global_registry
from core.factor_pipeline import FactorPipeline, PipelineResult, FactorResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 60, seed: int = 42) -> pd.DataFrame:
    """生成合成 OHLCV DataFrame，按日期升序。"""
    rng = np.random.default_rng(seed)
    prices = 10.0 * np.cumprod(1 + rng.uniform(-0.02, 0.025, n))
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'date': dates,
        'open':   prices * 0.99,
        'high':   prices * 1.01,
        'low':    prices * 0.98,
        'close':  prices,
        'volume': rng.integers(1_000_000, 5_000_000, n).astype(float),
    }).set_index('date')


class _DummyFactor(Factor):
    """固定返回常量的测试因子。"""
    name = 'Dummy'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, value: float = 0.5, symbol: str = ''):
        self.value = value
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        return pd.Series(self.value, index=data.index)

    def signals(self, factor_values, price):
        return []


class _ErrorFactor(Factor):
    """计算时一定抛异常的测试因子。"""
    name = 'ErrorFactor'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, symbol: str = ''):
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        raise RuntimeError("deliberate error for testing")

    def signals(self, factor_values, price):
        return []


# ---------------------------------------------------------------------------
# Test utilities (mirrors tests/run_tests.py style)
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def _check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def _section(name: str) -> None:
    print(f"\n=== {name} ===")


# ===========================================================================
# Test classes (pytest-compatible)
# ===========================================================================

class TestFactorRegistry:

    def test_global_registry_has_builtin_factors(self):
        names = global_registry.list_factors()
        assert 'RSI' in names
        assert 'MACD' in names
        assert 'BollingerBands' in names
        assert 'ATR' in names

    def test_registry_len(self):
        assert len(global_registry) >= 4

    def test_contains_operator(self):
        assert 'RSI' in global_registry
        assert 'NonExistentFactor' not in global_registry

    def test_create_rsi_default_params(self):
        f = global_registry.create('RSI')
        assert f.period == 14

    def test_create_rsi_override_params(self):
        f = global_registry.create('RSI', period=21)
        assert f.period == 21

    def test_create_unknown_raises_key_error(self):
        try:
            global_registry.create('NoSuchFactor')
            assert False, "should have raised KeyError"
        except KeyError:
            pass

    def test_register_custom_factor(self):
        reg = FactorRegistry()
        reg.register(_DummyFactor, default_params={'value': 1.5})
        assert 'Dummy' in reg
        f = reg.create('Dummy')
        assert f.value == 1.5

    def test_register_custom_factor_override_name(self):
        reg = FactorRegistry()
        reg.register(_DummyFactor, name='MyDummy')
        assert 'MyDummy' in reg
        assert 'Dummy' not in reg

    def test_register_non_factor_raises_type_error(self):
        reg = FactorRegistry()
        try:
            reg.register(object)  # type: ignore
            assert False, "should raise TypeError"
        except TypeError:
            pass

    def test_unregister(self):
        reg = FactorRegistry()
        reg.register(_DummyFactor)
        assert 'Dummy' in reg
        reg.unregister('Dummy')
        assert 'Dummy' not in reg

    def test_list_factors_sorted(self):
        names = global_registry.list_factors()
        assert names == sorted(names)

    def test_get_default_params(self):
        params = global_registry.get_default_params('RSI')
        assert 'period' in params
        assert params['period'] == 14

    def test_get_default_params_unknown(self):
        params = global_registry.get_default_params('NoSuch')
        assert params == {}


class TestFactorPipeline:

    def _basic_pipeline(self) -> FactorPipeline:
        p = FactorPipeline()
        p.add('RSI', weight=1.0, symbol='TEST')
        p.add('MACD', weight=0.5, symbol='TEST')
        return p

    def test_add_by_name(self):
        p = FactorPipeline()
        p.add('RSI')
        assert 'RSI' in p.factor_names

    def test_add_by_class(self):
        p = FactorPipeline()
        p.add(_DummyFactor)
        assert 'Dummy' in p.factor_names

    def test_add_negative_weight_raises(self):
        p = FactorPipeline()
        try:
            p.add('RSI', weight=-1.0)
            assert False, "should raise ValueError"
        except ValueError:
            pass

    def test_add_invalid_type_raises(self):
        p = FactorPipeline()
        try:
            p.add(42)  # type: ignore
            assert False, "should raise TypeError"
        except TypeError:
            pass

    def test_clear(self):
        p = FactorPipeline()
        p.add('RSI')
        p.clear()
        assert p.factor_names == []

    def test_chain_add(self):
        p = FactorPipeline()
        result = p.add('RSI').add('MACD')
        assert result is p
        assert len(p.factor_names) == 2

    def test_run_returns_pipeline_result(self):
        p = self._basic_pipeline()
        df = _make_df()
        res = p.run('TEST', df)
        assert isinstance(res, PipelineResult)

    def test_run_symbol_propagated(self):
        p = self._basic_pipeline()
        df = _make_df()
        res = p.run('600519.SH', df)
        assert res.symbol == '600519.SH'

    def test_run_combined_score_is_float(self):
        p = self._basic_pipeline()
        df = _make_df()
        res = p.run('TEST', df)
        assert isinstance(res.combined_score, float)

    def test_run_dominant_signal_valid(self):
        p = self._basic_pipeline()
        df = _make_df()
        res = p.run('TEST', df)
        assert res.dominant_signal in ('BUY', 'SELL', 'HOLD')

    def test_run_factor_results_count(self):
        p = self._basic_pipeline()
        df = _make_df()
        res = p.run('TEST', df)
        assert len(res.factor_results) == 2

    def test_run_factor_score_lookup(self):
        p = FactorPipeline()
        p.add(_DummyFactor, weight=1.0, params={'value': 0.75})
        df = _make_df()
        res = p.run('TEST', df)
        score = res.factor_score('Dummy')
        assert score is not None
        assert abs(score - 0.75) < 1e-9

    def test_run_combined_score_weighted(self):
        """两个相同 Dummy 因子，不同权重，加权平均仍应等于该值。"""
        p = FactorPipeline()
        p.add(_DummyFactor, weight=2.0, params={'value': 1.0})
        p.add(_DummyFactor, weight=1.0, params={'value': 1.0})
        df = _make_df()
        res = p.run('TEST', df)
        # 1.0 * 2/3 + 1.0 * 1/3 = 1.0
        assert abs(res.combined_score - 1.0) < 1e-6

    def test_insufficient_data_returns_hold(self):
        p = FactorPipeline(min_bars=30)
        p.add('RSI')
        df = _make_df(n=10)   # 少于 min_bars
        res = p.run('TEST', df)
        assert res.dominant_signal == 'HOLD'
        assert res.combined_score == 0.0
        assert res.metadata.get('reason') == 'insufficient_data'

    def test_no_factors_returns_hold(self):
        p = FactorPipeline()
        df = _make_df()
        res = p.run('TEST', df)
        assert res.dominant_signal == 'HOLD'
        assert res.metadata.get('reason') == 'no_factors'

    def test_error_factor_does_not_crash_pipeline(self):
        p = FactorPipeline()
        p.add(_DummyFactor, weight=1.0)
        p.add(_ErrorFactor, weight=1.0)
        df = _make_df()
        res = p.run('TEST', df)
        # Pipeline 不崩溃，错误因子被隔离
        assert res.has_error()
        errors = res.errors()
        assert 'ErrorFactor' in errors

    def test_error_factor_still_uses_good_factor(self):
        p = FactorPipeline()
        p.add(_DummyFactor, weight=1.0, params={'value': 0.5})
        p.add(_ErrorFactor, weight=1.0)
        df = _make_df()
        res = p.run('TEST', df)
        # 成功因子的得分仍然体现在 combined_score
        assert abs(res.combined_score - 0.5) < 1e-6

    def test_metadata_bars_used(self):
        p = FactorPipeline()
        p.add('RSI')
        df = _make_df(n=50)
        res = p.run('TEST', df)
        assert res.metadata['bars_used'] == 50

    def test_metadata_factors_ok(self):
        p = FactorPipeline()
        p.add(_DummyFactor)
        p.add(_ErrorFactor)
        df = _make_df()
        res = p.run('TEST', df)
        assert res.metadata['factors_ok'] == 1
        assert res.metadata['factors_total'] == 2

    def test_price_defaults_to_last_close(self):
        p = FactorPipeline()
        p.add(_DummyFactor, weight=1.0)
        df = _make_df()
        last_close = float(df['close'].iloc[-1])
        res = p.run('TEST', df)
        assert abs(res.metadata['price'] - last_close) < 1e-6


class TestPipelineResult:

    def _make_result(self, buy_signals=None, sell_signals=None) -> PipelineResult:
        import pandas as pd
        from datetime import datetime
        sigs = []
        for strength in (buy_signals or []):
            sigs.append(Signal(
                timestamp=datetime.now(), symbol='X',
                direction='BUY', strength=strength,
                factor_name='Test', price=10.0
            ))
        for strength in (sell_signals or []):
            sigs.append(Signal(
                timestamp=datetime.now(), symbol='X',
                direction='SELL', strength=strength,
                factor_name='Test', price=10.0
            ))
        return PipelineResult(
            symbol='X',
            combined_score=0.0,
            factor_results=[],
            signals=sigs,
            dominant_signal='HOLD',
        )

    def test_buy_strength_capped_at_1(self):
        res = self._make_result(buy_signals=[0.8, 0.9])
        assert res.buy_strength == 1.0

    def test_sell_strength_capped_at_1(self):
        res = self._make_result(sell_signals=[0.6, 0.7])
        assert res.sell_strength == 1.0

    def test_buy_strength_partial(self):
        res = self._make_result(buy_signals=[0.3])
        assert abs(res.buy_strength - 0.3) < 1e-9

    def test_no_signals_no_error(self):
        res = self._make_result()
        assert not res.has_error()
        assert res.errors() == {}

    def test_factor_score_missing_returns_none(self):
        res = self._make_result()
        assert res.factor_score('NonExistent') is None


# ===========================================================================
# Plain-Python runner (mirrors run_tests.py style)
# ===========================================================================

def _run_class(cls):
    obj = cls()
    for attr in sorted(dir(cls)):
        if not attr.startswith('test_'):
            continue
        method = getattr(obj, attr)
        if not callable(method):
            continue
        ok = False
        try:
            method()
            ok = True
        except AssertionError as e:
            _failed_inc(attr, str(e))
        except Exception as e:
            _failed_inc(attr, f"EXCEPTION: {e}")
        if ok:
            global _passed
            _passed += 1
            print(f"  PASS: {cls.__name__}.{attr}")


def _failed_inc(name, msg):
    global _failed
    _failed += 1
    print(f"  FAIL: {name} — {msg}")


if __name__ == '__main__':
    _section('FactorRegistry')
    _run_class(TestFactorRegistry)

    _section('FactorPipeline')
    _run_class(TestFactorPipeline)

    _section('PipelineResult')
    _run_class(TestPipelineResult)

    print('\n' + '=' * 60)
    if _failed > 0:
        print(f'FAIL: {_failed} test(s) failed')
        sys.exit(1)
    else:
        print(f'Phase 2 FactorRegistry+Pipeline: {_passed} passed, 0 failed')

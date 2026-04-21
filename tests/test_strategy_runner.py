"""
Phase 3 Tests — StrategyRunner

运行方式：
    python tests/test_strategy_runner.py
    pytest tests/test_strategy_runner.py -v
"""

from __future__ import annotations
import sys
import os
import threading

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_DIR)

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from core.data_layer import BacktestDataLayer
from core.factor_pipeline import FactorPipeline
from core.factors.base import Factor, FactorCategory, Signal
from core.strategy_runner import StrategyRunner, RunnerConfig, RunResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 60, seed: int = 42) -> pd.DataFrame:
    """返回含 date 列（不作为索引）的 OHLCV DataFrame，匹配 BacktestDataLayer 预期格式。"""
    rng = np.random.default_rng(seed)
    prices = 10.0 * np.cumprod(1 + rng.uniform(-0.02, 0.025, n))
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'date':   dates,
        'open':   prices * 0.99,
        'high':   prices * 1.01,
        'low':    prices * 0.98,
        'close':  prices,
        'volume': rng.integers(1_000_000, 5_000_000, n).astype(float),
    })


def _make_backtest_layer(symbol: str = 'TEST', n: int = 60) -> BacktestDataLayer:
    df = _make_df(n)
    dl = BacktestDataLayer(data={symbol: df})
    dl.set_date(df['date'].iloc[-1].date())
    return dl


class _ConstFactor(Factor):
    """输出固定 z-score 的测试因子（方便控制信号）。"""
    name = 'ConstFactor'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, value: float = 1.0, symbol: str = '', direction: str = 'BUY'):
        self.value = value
        self.symbol = symbol
        self._direction = direction

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        return pd.Series(self.value, index=data.index)

    def signals(self, factor_values, price):
        if abs(self.value) >= 0.5:
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol,
                direction=self._direction,
                strength=min(abs(self.value), 1.0),
                factor_name=self.name,
                price=price,
            )]
        return []


def _make_pipeline(score: float = 1.0, direction: str = 'BUY') -> FactorPipeline:
    p = FactorPipeline(min_bars=5)
    p.add(_ConstFactor, weight=1.0,
          params={'value': score, 'direction': direction})
    return p


def _make_runner(
    symbols=None,
    score: float = 1.0,
    direction: str = 'BUY',
    threshold: float = 0.5,
    dry_run: bool = True,
    on_signal=None,
) -> StrategyRunner:
    if symbols is None:
        symbols = ['TEST']
    dl = _make_backtest_layer('TEST', n=60)
    pipeline = _make_pipeline(score, direction)
    cfg = RunnerConfig(
        symbols=symbols,
        pipeline=pipeline,
        dry_run=dry_run,
        signal_threshold=threshold,
        bars_lookback=60,
        on_signal=on_signal,
    )
    return StrategyRunner(cfg, data_layer=dl)


# ---------------------------------------------------------------------------
# Test utilities
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
# Test classes
# ===========================================================================

class TestRunnerConfig:

    def test_symbols_list(self):
        cfg = RunnerConfig(
            symbols=['A', 'B'],
            pipeline=FactorPipeline(),
        )
        assert cfg.symbols == ['A', 'B']

    def test_symbols_callable(self):
        cfg = RunnerConfig(
            symbols=lambda: ['X', 'Y'],
            pipeline=FactorPipeline(),
        )
        assert callable(cfg.symbols)
        assert cfg.symbols() == ['X', 'Y']

    def test_default_dry_run(self):
        cfg = RunnerConfig(symbols=[], pipeline=FactorPipeline())
        assert cfg.dry_run is True

    def test_default_interval(self):
        cfg = RunnerConfig(symbols=[], pipeline=FactorPipeline())
        assert cfg.interval == 300

    def test_default_threshold(self):
        cfg = RunnerConfig(symbols=[], pipeline=FactorPipeline())
        assert cfg.signal_threshold == 0.5


class TestRunResult:

    def _make(self, action: str) -> RunResult:
        return RunResult(
            symbol='TEST',
            timestamp=datetime.now(),
            pipeline_result=None,
            action=action,
        )

    def test_acted_buy(self):
        assert self._make('BUY').acted is True

    def test_acted_sell(self):
        assert self._make('SELL').acted is True

    def test_not_acted_none(self):
        assert self._make('NONE').acted is False

    def test_not_acted_skipped(self):
        assert self._make('SKIPPED').acted is False

    def test_not_acted_error(self):
        assert self._make('ERROR').acted is False


class TestStrategyRunnerBasic:

    def test_run_once_returns_list(self):
        runner = _make_runner()
        results = runner.run_once()
        assert isinstance(results, list)

    def test_run_once_one_result_per_symbol(self):
        runner = _make_runner(symbols=['TEST'])
        results = runner.run_once()
        assert len(results) == 1
        assert results[0].symbol == 'TEST'

    def test_run_count_increments(self):
        runner = _make_runner()
        assert runner.run_count == 0
        runner.run_once()
        assert runner.run_count == 1
        runner.run_once()
        assert runner.run_count == 2

    def test_last_results_updated(self):
        runner = _make_runner()
        runner.run_once()
        assert len(runner.last_results) == 1

    def test_last_results_is_copy(self):
        runner = _make_runner()
        runner.run_once()
        r1 = runner.last_results
        runner.run_once()
        r2 = runner.last_results
        assert r1 is not r2   # 是独立副本

    def test_callable_symbols_resolved_each_run(self):
        calls = []
        def sym_fn():
            calls.append(1)
            return ['TEST']
        dl = _make_backtest_layer('TEST')
        cfg = RunnerConfig(
            symbols=sym_fn,
            pipeline=_make_pipeline(),
            dry_run=True,
            bars_lookback=60,
        )
        runner = StrategyRunner(cfg, data_layer=dl)
        runner.run_once()
        runner.run_once()
        assert len(calls) == 2   # 每轮都重新调用


class TestStrategyRunnerSignalTrigger:

    def test_strong_signal_triggers_action(self):
        runner = _make_runner(score=1.5, direction='BUY', threshold=0.5)
        results = runner.run_once()
        assert results[0].action == 'BUY'

    def test_weak_signal_no_action(self):
        runner = _make_runner(score=0.1, threshold=0.5)
        results = runner.run_once()
        assert results[0].action == 'NONE'

    def test_sell_signal_triggers_sell(self):
        runner = _make_runner(score=1.5, direction='SELL', threshold=0.5)
        results = runner.run_once()
        assert results[0].action == 'SELL'

    def test_threshold_boundary(self):
        # score < threshold — 不应触发
        runner = _make_runner(score=0.3, threshold=0.5)
        results = runner.run_once()
        assert results[0].action == 'NONE'

    def test_pipeline_result_attached(self):
        runner = _make_runner(score=1.5)
        results = runner.run_once()
        assert results[0].pipeline_result is not None

    def test_dry_run_does_not_call_oms(self):
        oms_calls = []

        class FakeOMS:
            def submit_from_signal(self, signal):
                oms_calls.append(signal)

        runner = _make_runner(score=1.5, dry_run=True)
        runner.oms = FakeOMS()
        runner.run_once()
        assert len(oms_calls) == 0   # dry_run → OMS 未被调用

    def test_non_dry_run_calls_oms(self):
        oms_calls = []

        class FakeOMS:
            def submit_from_signal(self, signal):
                oms_calls.append(signal)
                return None

        runner = _make_runner(score=1.5, dry_run=False)
        runner.oms = FakeOMS()
        runner.run_once()
        assert len(oms_calls) == 1


class TestStrategyRunnerHooks:

    def test_on_signal_hook_blocks(self):
        def block_all(sym, pr, runner):
            return False  # 拦截

        runner = _make_runner(score=1.5, on_signal=block_all)
        results = runner.run_once()
        assert results[0].action == 'SKIPPED'
        assert 'blocked_by_on_signal_hook' in results[0].reason

    def test_on_signal_hook_allows(self):
        def allow_all(sym, pr, runner):
            return True

        runner = _make_runner(score=1.5, on_signal=allow_all)
        results = runner.run_once()
        assert results[0].action == 'BUY'

    def test_on_signal_hook_receives_args(self):
        received = []

        def capture(sym, pr, runner_ref):
            received.append((sym, pr, runner_ref))
            return True

        runner = _make_runner(score=1.5, on_signal=capture)
        runner.run_once()
        assert len(received) == 1
        assert received[0][0] == 'TEST'

    def test_hook_exception_does_not_crash(self):
        def bad_hook(sym, pr, runner):
            raise RuntimeError("hook error")

        # 异常时默认放行，不崩溃
        runner = _make_runner(score=1.5, on_signal=bad_hook)
        results = runner.run_once()
        # 不应引发异常
        assert results[0].action in ('BUY', 'SELL', 'SKIPPED', 'NONE', 'ERROR')


class TestStrategyRunnerEdgeCases:

    def test_no_data_symbol_returns_skipped(self):
        """BacktestDataLayer 中不存在的标的返回 SKIPPED 或 ERROR。"""
        dl = _make_backtest_layer('TEST')  # 只有 TEST，没有 NONEXISTENT
        cfg = RunnerConfig(
            symbols=['NONEXISTENT'],
            pipeline=_make_pipeline(),
            dry_run=True,
            bars_lookback=60,
        )
        runner = StrategyRunner(cfg, data_layer=dl)
        results = runner.run_once()
        assert results[0].action in ('SKIPPED', 'ERROR')

    def test_empty_symbols_returns_empty(self):
        runner = _make_runner(symbols=[])
        results = runner.run_once()
        assert results == []

    def test_multiple_symbols_independent(self):
        """多标的彼此独立，不会串扰。"""
        df_a = _make_df(n=60, seed=1)
        df_b = _make_df(n=60, seed=2)
        dl = BacktestDataLayer(data={'A': df_a, 'B': df_b})
        dl.set_date(df_a['date'].iloc[-1].date())
        cfg = RunnerConfig(
            symbols=['A', 'B'],
            pipeline=_make_pipeline(score=1.5),
            dry_run=True,
            bars_lookback=60,
        )
        runner = StrategyRunner(cfg, data_layer=dl)
        results = runner.run_once()
        assert len(results) == 2
        syms = {r.symbol for r in results}
        assert syms == {'A', 'B'}


class TestStrategyRunnerLoop:

    def test_stop_flag(self):
        runner = _make_runner()
        assert not runner.is_running

    def test_run_loop_stops_on_stop(self):
        runner = _make_runner()
        t = threading.Thread(
            target=runner.run_loop,
            daemon=True,
        )
        t.start()
        # 等第一轮完成后停止
        import time
        time.sleep(0.3)
        runner.stop()
        t.join(timeout=3)
        assert not t.is_alive(), "run_loop should have exited after stop()"
        assert runner.run_count >= 1


# ===========================================================================
# Plain-Python runner
# ===========================================================================

def _run_class(cls):
    global _passed, _failed
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
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — {e}")
        except Exception as e:
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — EXCEPTION: {e}")
        if ok:
            _passed += 1
            print(f"  PASS: {cls.__name__}.{attr}")


if __name__ == '__main__':
    _section('RunnerConfig')
    _run_class(TestRunnerConfig)

    _section('RunResult')
    _run_class(TestRunResult)

    _section('StrategyRunnerBasic')
    _run_class(TestStrategyRunnerBasic)

    _section('StrategyRunnerSignalTrigger')
    _run_class(TestStrategyRunnerSignalTrigger)

    _section('StrategyRunnerHooks')
    _run_class(TestStrategyRunnerHooks)

    _section('StrategyRunnerEdgeCases')
    _run_class(TestStrategyRunnerEdgeCases)

    _section('StrategyRunnerLoop')
    _run_class(TestStrategyRunnerLoop)

    print('\n' + '=' * 60)
    if _failed > 0:
        print(f'FAIL: {_failed} test(s) failed')
        sys.exit(1)
    else:
        print(f'Phase 3 StrategyRunner: {_passed} passed, 0 failed')

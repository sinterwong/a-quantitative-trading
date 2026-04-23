"""tests/test_async_runner.py — AsyncStrategyRunner / AsyncEventBus 单元测试（P3-B）"""

from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd


def _make_config(symbols=None):
    from core.strategy_runner import RunnerConfig
    pipeline = MagicMock()
    pr = MagicMock()
    pr.combined_score = 0.3
    pr.dominant_signal = 'HOLD'
    pr.signals = []
    pipeline.run.return_value = pr
    pipeline._factors = []
    return RunnerConfig(
        symbols=symbols or ['600519.SH'],
        pipeline=pipeline,
        interval=1,
        dry_run=True,
        signal_threshold=0.5,
        bars_lookback=10,
        regime_aware=False,
    )


def _make_data_layer():
    dl = MagicMock()
    dates = pd.date_range('2024-01-01', periods=30, freq='B')
    df = pd.DataFrame({
        'open': np.ones(30) * 100,
        'high': np.ones(30) * 102,
        'low': np.ones(30) * 98,
        'close': np.ones(30) * 101,
        'volume': np.ones(30) * 1e6,
    }, index=dates)
    dl.get_bars.return_value = df
    dl.get_realtime.return_value = {'price': 101.0}
    return dl


class TestAsyncStrategyRunner(unittest.TestCase):

    def test_run_once_sync_returns_results(self):
        from core.async_runner import AsyncStrategyRunner
        cfg = _make_config(['600519.SH', '000858.SZ'])
        runner = AsyncStrategyRunner(cfg, data_layer=_make_data_layer())
        results = runner.run_once_sync()
        self.assertEqual(len(results), 2)
        self.assertEqual(runner.run_count, 1)

    def test_action_none_below_threshold(self):
        from core.async_runner import AsyncStrategyRunner
        runner = AsyncStrategyRunner(_make_config(), data_layer=_make_data_layer())
        results = runner.run_once_sync()
        self.assertEqual(results[0].action, 'NONE')

    def test_buy_signal_above_threshold(self):
        from core.async_runner import AsyncStrategyRunner
        cfg = _make_config()
        pr = MagicMock()
        pr.combined_score = 0.8
        pr.dominant_signal = 'BUY'
        pr.signals = []
        cfg.pipeline.run.return_value = pr
        runner = AsyncStrategyRunner(cfg, data_layer=_make_data_layer())
        results = runner.run_once_sync()
        self.assertEqual(results[0].action, 'BUY')

    def test_no_data_skipped(self):
        from core.async_runner import AsyncStrategyRunner
        dl = MagicMock()
        dl.get_bars.return_value = pd.DataFrame()
        dl.get_realtime.return_value = None
        runner = AsyncStrategyRunner(_make_config(), data_layer=dl)
        results = runner.run_once_sync()
        self.assertEqual(results[0].action, 'SKIPPED')

    def test_error_propagation(self):
        from core.async_runner import AsyncStrategyRunner
        dl = MagicMock()
        dl.get_bars.side_effect = RuntimeError('network error')
        runner = AsyncStrategyRunner(_make_config(), data_layer=dl)
        results = runner.run_once_sync()
        self.assertEqual(results[0].action, 'ERROR')

    def test_run_sync_duration(self):
        from core.async_runner import AsyncStrategyRunner
        cfg = _make_config()
        cfg.interval = 1
        runner = AsyncStrategyRunner(cfg, data_layer=_make_data_layer())
        t0 = time.perf_counter()
        runner.run_sync(duration=0.1)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_last_results_property(self):
        from core.async_runner import AsyncStrategyRunner
        cfg = _make_config(['600519.SH', '000858.SZ'])
        runner = AsyncStrategyRunner(cfg, data_layer=_make_data_layer())
        runner.run_once_sync()
        self.assertEqual(len(runner.last_results), 2)

    def test_concurrent_multiple_symbols(self):
        from core.async_runner import AsyncStrategyRunner
        symbols = ['600519.SH', '000858.SZ', '601318.SH', '300750.SZ', '600036.SH']
        runner = AsyncStrategyRunner(_make_config(symbols), data_layer=_make_data_layer())
        results = runner.run_once_sync()
        self.assertEqual(len(results), 5)

    def test_callable_symbols(self):
        from core.async_runner import AsyncStrategyRunner
        call_count = [0]

        def dynamic():
            call_count[0] += 1
            return ['600519.SH']

        runner = AsyncStrategyRunner(_make_config(dynamic), data_layer=_make_data_layer())
        runner.run_once_sync()
        self.assertEqual(call_count[0], 1)


class TestAsyncEventBus(unittest.TestCase):

    def test_subscribe_and_consume(self):
        from core.async_runner import AsyncEventBus
        received = []

        async def run():
            bus = AsyncEventBus()
            bus.subscribe('signal', lambda p: received.append(p))
            bus.emit_nowait('signal', {'symbol': '600519.SH'})
            event_type, payload = await bus._queue.get()
            for h in bus._handlers.get(event_type, []):
                h(payload)

        asyncio.run(run())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['symbol'], '600519.SH')

    def test_emit_nowait_overflow(self):
        from core.async_runner import AsyncEventBus

        async def run():
            bus = AsyncEventBus(maxsize=2)
            bus.emit_nowait('signal', 'a')
            bus.emit_nowait('signal', 'b')
            bus.emit_nowait('signal', 'c')  # 丢弃，不崩溃
            return bus.qsize

        size = asyncio.run(run())
        self.assertEqual(size, 2)


if __name__ == '__main__':
    unittest.main()

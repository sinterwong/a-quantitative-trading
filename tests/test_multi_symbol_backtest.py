"""tests/test_multi_symbol_backtest.py — MultiSymbolBacktest 单元测试（P1-B）"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _make_mock_data(n=200) -> pd.DataFrame:
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    np.random.seed(42)
    price = 100 + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame({
        'open': price * 0.999,
        'high': price * 1.01,
        'low': price * 0.99,
        'close': price,
        'volume': np.ones(n) * 1e6,
    }, index=dates)


class TestBuildWindows(unittest.TestCase):

    def test_window_count_positive(self):
        from core.multi_symbol_backtest import MultiSymbolBacktest
        df = _make_mock_data(500)
        windows = MultiSymbolBacktest._build_windows(df, train_months=6, test_months=3, step_months=3)
        self.assertGreater(len(windows), 0)

    def test_no_overlap(self):
        from core.multi_symbol_backtest import MultiSymbolBacktest
        df = _make_mock_data(500)
        windows = MultiSymbolBacktest._build_windows(df, train_months=6, test_months=3, step_months=3)
        for train_df, test_df in windows:
            self.assertLessEqual(train_df.index.max(), test_df.index.min())

    def test_insufficient_data_returns_empty(self):
        from core.multi_symbol_backtest import MultiSymbolBacktest
        df = _make_mock_data(10)
        windows = MultiSymbolBacktest._build_windows(df, train_months=18, test_months=6, step_months=6)
        self.assertEqual(len(windows), 0)


class TestMultiSymbolResult(unittest.TestCase):

    def _make_result(self):
        from core.multi_symbol_backtest import MultiSymbolResult, SymbolWFAResult
        r1 = SymbolWFAResult('600519.SH', '贵州茅台', 3, [0.5, 0.3, 0.4], 0.4, 1.0, 0.1, -0.05, True)
        r2 = SymbolWFAResult('000858.SZ', '五粮液', 3, [-0.1, 0.2, -0.2], -0.03, 0.33, -0.05, -0.08, False)
        return MultiSymbolResult(
            run_date='2026-04-23', strategy_name='RSI',
            n_symbols=2, n_passed=1, pass_rate=0.5, passed=False,
            symbol_results=[r1, r2],
        )

    def test_to_dataframe(self):
        result = self._make_result()
        df = result.to_dataframe()
        self.assertEqual(len(df), 2)
        self.assertIn('avg_oos_sharpe', df.columns)

    def test_save_json(self):
        result = self._make_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = result.save(os.path.join(tmpdir, 'test.json'))
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data['strategy_name'], 'RSI')
            self.assertEqual(data['summary']['n_passed'], 1)


class TestMultiSymbolBacktest(unittest.TestCase):

    def test_default_symbols_length(self):
        from core.multi_symbol_backtest import DEFAULT_CSI300_TOP10
        self.assertEqual(len(DEFAULT_CSI300_TOP10), 10)
        for sym in DEFAULT_CSI300_TOP10:
            self.assertTrue(sym.endswith('.SH') or sym.endswith('.SZ'))

    def test_run_all_fail(self):
        from core.multi_symbol_backtest import MultiSymbolBacktest
        pipeline = MagicMock()
        pipeline._factors = []
        msb = MultiSymbolBacktest(pipeline=pipeline)
        with patch.object(msb, '_fetch_data', return_value=None):
            result = msb.run(symbols=['600519.SH', '000858.SZ'], years=1)
        self.assertFalse(result.passed)
        self.assertEqual(result.n_passed, 0)
        for r in result.symbol_results:
            self.assertIsNotNone(r.error)


if __name__ == '__main__':
    unittest.main()

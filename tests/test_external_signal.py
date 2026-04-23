"""tests/test_external_signal.py — ExternalSignal 单元测试（P2-C）"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


class TestGrangerTest(unittest.TestCase):

    def test_granger_known_causality(self):
        from core.external_signal import granger_test
        np.random.seed(42)
        n = 300
        x = np.random.randn(n)
        y = np.zeros(n)
        for i in range(1, n):
            y[i] = 0.5 * x[i - 1] + 0.3 * np.random.randn()
        p_val, best_lag, f_stat = granger_test(y, x, max_lag=3)
        self.assertLess(p_val, 0.05)
        self.assertGreater(f_stat, 1.0)

    def test_granger_no_causality(self):
        from core.external_signal import granger_test
        np.random.seed(123)
        n = 300
        x = np.random.randn(n)
        y = np.random.randn(n)
        p_val, best_lag, f_stat = granger_test(y, x, max_lag=3)
        self.assertGreater(p_val, 0.05)

    def test_spearman_ic(self):
        from core.external_signal import SP500GrangerAnalyzer
        analyzer = SP500GrangerAnalyzer()
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([1.1, 2.1, 2.9, 4.2, 5.1])
        ic = analyzer._spearman_ic(x, y)
        self.assertAlmostEqual(ic, 1.0, places=2)

    def test_sp500_granger_no_data(self):
        from core.external_signal import SP500GrangerAnalyzer
        analyzer = SP500GrangerAnalyzer()
        with patch.object(analyzer, '_fetch_sp500', return_value=None):
            result = analyzer.analyze(days=100)
        self.assertFalse(result.passed)
        self.assertEqual(result.n_samples, 0)

    def test_northbound_insufficient_samples(self):
        from core.external_signal import NorthboundStatsAnalyzer
        analyzer = NorthboundStatsAnalyzer(threshold_bn=50.0)
        dates = pd.date_range('2024-01-01', periods=50, freq='B')
        flow = pd.DataFrame({'日期': dates, '北向资金': [60.0] * 30 + [10.0] * 20})
        close_vals = [100.0 * (1.01 ** i) for i in range(51)]
        csi = pd.DataFrame(
            {'close': close_vals},
            index=pd.date_range('2024-01-01', periods=51, freq='B'),
        )
        with patch.object(analyzer, '_fetch_northbound', return_value=flow), \
             patch.object(analyzer, '_fetch_csi300', return_value=csi):
            result = analyzer.analyze(days=100)
        self.assertFalse(result.passed)

    def test_run_full_analysis_structure(self):
        from core.external_signal import run_full_analysis, ExternalSignalReport
        with patch('core.external_signal.SP500GrangerAnalyzer.fetch_data',
                   return_value=(None, None)), \
             patch('core.external_signal.NorthboundStatsAnalyzer._fetch_northbound',
                   return_value=None), \
             patch('core.external_signal.NorthboundStatsAnalyzer._fetch_csi300',
                   return_value=None):
            report = run_full_analysis(save=False)
        self.assertIsInstance(report, ExternalSignalReport)
        self.assertIsNotNone(report.sp500_granger)
        self.assertIsNotNone(report.northbound_stats)
        self.assertFalse(report.sp500_granger.passed)
        self.assertFalse(report.northbound_stats.passed)


if __name__ == '__main__':
    unittest.main()

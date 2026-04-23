"""tests/test_daily_diff_reporter.py — DailyDiffReporter 单元测试（P3-D）"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock


class TestDailyDiffReporter(unittest.TestCase):

    def _make_run_result(self, symbol, action, score=0.0, reason=''):
        from core.factor_pipeline import PipelineResult
        r = MagicMock()
        r.symbol = symbol
        r.timestamp = datetime(2026, 4, 23, 10, 0, 0)
        r.action = action
        r.reason = reason
        r.acted = action in ('BUY', 'SELL')
        pr = MagicMock(spec=PipelineResult)
        pr.combined_score = score
        r.pipeline_result = pr
        return r

    def _make_trade(self, symbol, direction, dt=None):
        t = MagicMock()
        t.symbol = symbol
        t.timestamp = dt or datetime(2026, 4, 23, 14, 0, 0)
        t.direction = direction
        t.price = 100.0
        t.shares = 100
        t.signal_reason = 'RSI'
        t.signal_strength = 0.8
        return t

    def test_perfect_match(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        live = [
            self._make_run_result('600519.SH', 'BUY', 0.7),
            self._make_run_result('000858.SZ', 'SELL', -0.6),
        ]
        bt_trades = [
            self._make_trade('600519.SH', 'BUY'),
            self._make_trade('000858.SZ', 'SELL'),
        ]
        report = reporter.compare(live, bt_trades, date(2026, 4, 23))
        self.assertEqual(report.n_matches, 2)
        self.assertEqual(report.n_mismatches, 0)
        self.assertAlmostEqual(report.consistency_pct, 1.0)
        self.assertTrue(report.is_healthy())

    def test_direction_mismatch(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        live = [self._make_run_result('600519.SH', 'BUY', 0.7)]
        bt_trades = [self._make_trade('600519.SH', 'SELL')]
        report = reporter.compare(live, bt_trades, date(2026, 4, 23))
        self.assertEqual(report.n_mismatches, 1)
        self.assertEqual(report.mismatches[0].mismatch_type, 'direction_mismatch')
        self.assertFalse(report.is_healthy())

    def test_bt_only_signal(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        report = reporter.compare([], [self._make_trade('600519.SH', 'BUY')], date(2026, 4, 23))
        self.assertIn('bt_only', [m.mismatch_type for m in report.mismatches])

    def test_live_only_signal(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        report = reporter.compare(
            [self._make_run_result('000858.SZ', 'BUY', 0.8)], [], date(2026, 4, 23)
        )
        self.assertIn('live_only', [m.mismatch_type for m in report.mismatches])

    def test_empty_signals(self):
        from core.daily_diff_reporter import DailyDiffReporter
        report = DailyDiffReporter().compare([], [], date(2026, 4, 23))
        self.assertEqual(report.n_matches, 0)
        self.assertGreaterEqual(report.consistency_pct, 0.0)

    def test_save_and_load(self):
        from core.daily_diff_reporter import DailyDiffReporter
        with tempfile.TemporaryDirectory() as tmpdir:
            reporter = DailyDiffReporter(reports_dir=tmpdir)
            live = [self._make_run_result('600519.SH', 'BUY', 0.7)]
            bt_trades = [self._make_trade('600519.SH', 'BUY')]
            report = reporter.compare(live, bt_trades, date(2026, 4, 23))
            path = reporter.save(report)
            self.assertTrue(os.path.exists(path))
            loaded = reporter.load(date(2026, 4, 23))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.report_date, '2026-04-23')
            self.assertEqual(loaded.n_matches, 1)

    def test_format_text(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        live = [self._make_run_result('600519.SH', 'BUY', 0.7)]
        bt_trades = [self._make_trade('600519.SH', 'BUY')]
        report = reporter.compare(live, bt_trades, date(2026, 4, 23))
        text = reporter.format_text(report)
        self.assertIn('2026-04-23', text)
        self.assertIn('一致率', text)

    def test_different_day_trades_filtered(self):
        from core.daily_diff_reporter import DailyDiffReporter
        reporter = DailyDiffReporter()
        bt_trades = [self._make_trade('600519.SH', 'BUY', datetime(2026, 4, 22, 14, 0))]
        report = reporter.compare([], bt_trades, date(2026, 4, 23))
        self.assertEqual(report.n_bt_signals, 0)

    def test_list_reports(self):
        from core.daily_diff_reporter import DailyDiffReporter
        with tempfile.TemporaryDirectory() as tmpdir:
            reporter = DailyDiffReporter(reports_dir=tmpdir)
            for d in [date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 23)]:
                reporter.save(reporter.compare([], [], d))
            reports = reporter.list_reports()
            self.assertEqual(len(reports), 3)
            self.assertEqual(reports[0][0], date(2026, 4, 21))


if __name__ == '__main__':
    unittest.main()

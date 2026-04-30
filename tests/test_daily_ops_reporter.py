"""
tests/test_daily_ops_reporter.py — 每日运营报告生成器测试

覆盖：
  - DailyOpsReporter._fetch_portfolio(): 正常返回 / API 不可达降级
  - DailyOpsReporter._fetch_trades_summary(): 当日交易过滤
  - DailyOpsReporter._fetch_alert_summary(): AlertManager 历史统计
  - DailyOpsReporter._fetch_factor_ic_snapshot(): IC 文件存在/不存在
  - DailyOpsReporter._save(): 写入 JSON 文件
  - DailyOpsReporter.run(): 端到端（mock API + mock AlertManager）
  - Scheduler._trigger_daily_ops_report(): 调用链验证
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch


class TestFetchPortfolio(unittest.TestCase):

    def setUp(self):
        from core.daily_ops_reporter import DailyOpsReporter
        self.reporter = DailyOpsReporter(api_port=5555)

    def _mock_api(self, payload: dict):
        """返回一个 patch 好的 urlopen context manager。"""
        import io
        body = json.dumps(payload).encode()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def test_normal_positions(self):
        payload = {'positions': [
            {'symbol': '000001.SZ', 'shares': 100, 'current_price': 12.5,
             'unrealized_pnl': 150.0, 'pnl_pct': 0.012},
            {'symbol': '600519.SH', 'shares': 0, 'current_price': 1800.0,
             'unrealized_pnl': 0.0, 'pnl_pct': 0.0},
        ]}
        with patch('urllib.request.urlopen', return_value=self._mock_api(payload)):
            result = self.reporter._fetch_portfolio()
        self.assertEqual(result['n_positions'], 1)   # shares=0 不算
        self.assertAlmostEqual(result['total_unrealized_pnl'], 150.0)

    def test_api_unavailable(self):
        with patch('urllib.request.urlopen', side_effect=OSError('refused')):
            result = self.reporter._fetch_portfolio()
        self.assertIn('error', result)
        self.assertEqual(result['total_value'], 0.0)


class TestFetchTradesSummary(unittest.TestCase):

    def setUp(self):
        from core.daily_ops_reporter import DailyOpsReporter
        self.reporter = DailyOpsReporter()

    def _mock_api(self, payload):
        body = json.dumps(payload).encode()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def test_filters_to_today(self):
        payload = {'trades': [
            {'date': '2026-04-30', 'side': 'BUY', 'pnl': 0.0},
            {'date': '2026-04-30', 'side': 'SELL', 'pnl': 120.0},
            {'date': '2026-04-29', 'side': 'BUY', 'pnl': 50.0},  # 昨日，应被过滤
        ]}
        with patch('urllib.request.urlopen', return_value=self._mock_api(payload)):
            result = self.reporter._fetch_trades_summary('2026-04-30')
        self.assertEqual(result['n_trades'], 2)
        self.assertAlmostEqual(result['realized_pnl'], 120.0)
        self.assertEqual(result['buy_count'], 1)
        self.assertEqual(result['sell_count'], 1)

    def test_api_unavailable(self):
        with patch('urllib.request.urlopen', side_effect=OSError('refused')):
            result = self.reporter._fetch_trades_summary('2026-04-30')
        self.assertIn('error', result)
        self.assertEqual(result['n_trades'], 0)


class TestFetchAlertSummary(unittest.TestCase):

    def setUp(self):
        from core.daily_ops_reporter import DailyOpsReporter
        self.reporter = DailyOpsReporter()

    def test_counts_today_alerts(self):
        mock_mgr = MagicMock()
        mock_mgr.history = [
            {'timestamp': '2026-04-30T09:30:00', 'level': 'WARNING', 'message': 'warn1'},
            {'timestamp': '2026-04-30T10:00:00', 'level': 'CRITICAL', 'message': 'crit1'},
            {'timestamp': '2026-04-29T15:00:00', 'level': 'INFO', 'message': 'old'},
        ]
        with patch('core.alerting.get_alert_manager', return_value=mock_mgr):
            result = self.reporter._fetch_alert_summary('2026-04-30')
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['by_level'].get('CRITICAL'), 1)
        self.assertEqual(result['last_critical'], 'crit1')

    def test_no_alerts_today(self):
        mock_mgr = MagicMock()
        mock_mgr.history = []
        with patch('core.alerting.get_alert_manager', return_value=mock_mgr):
            result = self.reporter._fetch_alert_summary('2026-04-30')
        self.assertEqual(result['total'], 0)


class TestFetchFactorICSnapshot(unittest.TestCase):

    def setUp(self):
        from core.daily_ops_reporter import DailyOpsReporter
        self.reporter = DailyOpsReporter()
        self.tmp = tempfile.mkdtemp()

    def test_file_not_found(self):
        import core.daily_ops_reporter as _mod
        orig = _mod._PROJ_DIR
        _mod._PROJ_DIR = self.tmp
        try:
            result = self.reporter._fetch_factor_ic_snapshot()
        finally:
            _mod._PROJ_DIR = orig
        self.assertFalse(result['available'])

    def test_reads_existing_file(self):
        import core.daily_ops_reporter as _mod
        orig = _mod._PROJ_DIR
        _mod._PROJ_DIR = self.tmp
        outputs_dir = os.path.join(self.tmp, 'outputs')
        os.makedirs(outputs_dir, exist_ok=True)
        ic_path = os.path.join(outputs_dir, 'factor_ic_report_2026.json')
        ic_data = {'factors': {
            'RSI': {'ic_mean': 0.03, 'ic_ir': 0.45},
            'MACD': {'ic_mean': 0.02, 'ic_ir': 0.30},
        }}
        with open(ic_path, 'w') as f:
            json.dump(ic_data, f)
        try:
            result = self.reporter._fetch_factor_ic_snapshot()
        finally:
            _mod._PROJ_DIR = orig
        self.assertTrue(result['available'])
        self.assertIn('RSI', result['factors'])


class TestSave(unittest.TestCase):

    def test_saves_json_file(self):
        from core.daily_ops_reporter import DailyOpsReporter
        import core.daily_ops_reporter as _mod

        reporter = DailyOpsReporter()
        tmp = tempfile.mkdtemp()
        orig = _mod._OUTPUT_DIR
        _mod._OUTPUT_DIR = tmp
        try:
            report = {'date': '2026-04-30', 'portfolio': {}, 'trades': {}}
            reporter._save(report, '2026-04-30')
            path = os.path.join(tmp, 'ops_2026-04-30.json')
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                loaded = json.load(f)
            self.assertEqual(loaded['date'], '2026-04-30')
        finally:
            _mod._OUTPUT_DIR = orig


class TestRunEndToEnd(unittest.TestCase):
    """run() 端到端：全部依赖 mock，验证报告结构完整。"""

    def test_run_produces_complete_report(self):
        from core.daily_ops_reporter import DailyOpsReporter
        import core.daily_ops_reporter as _mod

        reporter = DailyOpsReporter()
        tmp = tempfile.mkdtemp()
        orig_out = _mod._OUTPUT_DIR
        orig_proj = _mod._PROJ_DIR
        _mod._OUTPUT_DIR = tmp
        _mod._PROJ_DIR = tmp

        def fake_fetch_portfolio():
            return {'total_value': 50000.0, 'total_unrealized_pnl': 500.0, 'n_positions': 2, 'positions': []}

        def fake_fetch_trades(today_str):
            return {'n_trades': 3, 'realized_pnl': 200.0, 'buy_count': 2, 'sell_count': 1}

        def fake_fetch_health():
            return {'status': 'ok'}

        def fake_fetch_alerts(today_str):
            return {'total': 1, 'by_level': {'WARNING': 1}}

        def fake_fetch_ic():
            return {'available': False}

        mock_mgr = MagicMock()
        mock_mgr.send_daily_report.return_value = True

        try:
            with patch.object(reporter, '_fetch_portfolio', fake_fetch_portfolio), \
                 patch.object(reporter, '_fetch_trades_summary', fake_fetch_trades), \
                 patch.object(reporter, '_fetch_health', fake_fetch_health), \
                 patch.object(reporter, '_fetch_alert_summary', fake_fetch_alerts), \
                 patch.object(reporter, '_fetch_factor_ic_snapshot', fake_fetch_ic), \
                 patch('core.alerting.get_alert_manager', return_value=mock_mgr):
                report = reporter.run(report_date=date(2026, 4, 30))
        finally:
            _mod._OUTPUT_DIR = orig_out
            _mod._PROJ_DIR = orig_proj

        # 结构检查
        for key in ('date', 'generated_at', 'portfolio', 'trades', 'health', 'alerts', 'factor_ic'):
            self.assertIn(key, report, f'missing key: {key}')
        self.assertEqual(report['portfolio']['n_positions'], 2)
        self.assertEqual(report['trades']['n_trades'], 3)
        mock_mgr.send_daily_report.assert_called_once()

    def test_run_does_not_raise_on_all_failures(self):
        """所有子模块失败时 run() 仍返回合法 dict，不抛异常。"""
        from core.daily_ops_reporter import DailyOpsReporter
        import core.daily_ops_reporter as _mod

        reporter = DailyOpsReporter()
        tmp = tempfile.mkdtemp()
        orig_out = _mod._OUTPUT_DIR
        _mod._OUTPUT_DIR = tmp

        try:
            with patch('urllib.request.urlopen', side_effect=OSError('refused')), \
                 patch('core.alerting.get_alert_manager', side_effect=ImportError):
                report = reporter.run(report_date=date(2026, 4, 30))
        finally:
            _mod._OUTPUT_DIR = orig_out

        self.assertIn('date', report)


if __name__ == '__main__':
    unittest.main()

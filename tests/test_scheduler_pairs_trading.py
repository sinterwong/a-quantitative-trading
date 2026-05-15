"""
test_scheduler_pairs_trading.py — P1-10 配对交易接入主调度测试

验证：
  1. _trigger_pairs_trading 在 watchlist 不足 2 个时安全跳过
  2. 调用 /analysis/pairs_trading 端点（mock urlopen）
  3. 找到 actionable 配对（|z| ≥ entry_z）→ 触发 AlertManager.send_warning
  4. JSON 输出文件被写入 outputs/pairs_signals/
  5. _trigger_analysis 在周三调用 _trigger_pairs_trading
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# 让 backend 可以被 import
PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self._body


class TestPairsTradingScheduler(unittest.TestCase):

    def _build_scheduler(self):
        from backend.main import Scheduler
        return Scheduler(api_port=5555)

    def test_skip_when_watchlist_too_small(self):
        """watchlist 仅 1 个标的 → 跳过，不调用配对交易 API。"""
        sched = self._build_scheduler()
        watchlist_resp = json.dumps({'watchlist': [{'symbol': 'A.SH'}]}).encode()

        # 第一个 urlopen 调用是 /watchlist
        with patch('urllib.request.urlopen',
                   return_value=_FakeResponse(watchlist_resp)) as mock_url:
            sched._trigger_pairs_trading()

        # 仅 1 次调用（/watchlist），不应继续调 /analysis/pairs_trading
        self.assertEqual(mock_url.call_count, 1)

    def test_calls_pairs_trading_api_with_symbols(self):
        """watchlist 充足 → 调用 /analysis/pairs_trading 携带 symbols。"""
        sched = self._build_scheduler()
        wl_resp = json.dumps({
            'watchlist': [{'symbol': 'A.SH'}, {'symbol': 'B.SH'}, {'symbol': 'C.SH'}]
        }).encode()
        pairs_resp = json.dumps({
            'data': {
                'pairs': [
                    {'symbol_a': 'A.SH', 'symbol_b': 'B.SH',
                     'signal': {'spread_zscore': 2.5,
                                'action_a': 'SELL', 'action_b': 'BUY'}},
                ],
                'n_pairs_found': 1,
            }
        }).encode()

        responses = [_FakeResponse(wl_resp), _FakeResponse(pairs_resp)]
        call_index = {'i': 0}

        def fake_urlopen(req_or_url, *args, **kwargs):
            r = responses[call_index['i']]
            call_index['i'] += 1
            return r

        # 用临时 outputs 目录
        with tempfile.TemporaryDirectory() as tmp:
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                with patch('quant_app.run_worker.PROJ_DIR', tmp):
                    with patch('core.alerting.get_alert_manager') as mock_mgr:
                        mock_mgr.return_value = MagicMock()
                        sched._trigger_pairs_trading()
                        # 应触发告警（z=2.5 ≥ 2.0）
                        mock_mgr.return_value.send_warning.assert_called_once()
                        msg = mock_mgr.return_value.send_warning.call_args[0][0]
                        self.assertIn('A.SH', msg)
                        self.assertIn('B.SH', msg)

            # JSON 文件应写入
            files = list((Path(tmp) / 'outputs' / 'pairs_signals').glob('pairs_*.json'))
            self.assertEqual(len(files), 1)
            with open(files[0]) as f:
                payload = json.load(f)
            self.assertEqual(payload['n_pairs_found'], 1)

    def test_no_alert_below_entry_z(self):
        """spread_zscore 未达 entry_z=2.0 时不告警。"""
        sched = self._build_scheduler()
        wl_resp = json.dumps({
            'watchlist': [{'symbol': 'A.SH'}, {'symbol': 'B.SH'}]
        }).encode()
        pairs_resp = json.dumps({
            'data': {
                'pairs': [
                    {'symbol_a': 'A.SH', 'symbol_b': 'B.SH',
                     'signal': {'spread_zscore': 1.0,
                                'action_a': 'HOLD', 'action_b': 'HOLD'}},
                ],
                'n_pairs_found': 1,
            }
        }).encode()
        responses = [_FakeResponse(wl_resp), _FakeResponse(pairs_resp)]
        idx = {'i': 0}

        def fake_urlopen(*args, **kw):
            r = responses[idx['i']]
            idx['i'] += 1
            return r

        with tempfile.TemporaryDirectory() as tmp:
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                with patch('quant_app.run_worker.PROJ_DIR', tmp):
                    with patch('core.alerting.get_alert_manager') as mock_mgr:
                        mock_mgr.return_value = MagicMock()
                        sched._trigger_pairs_trading()
                        mock_mgr.return_value.send_warning.assert_not_called()


class TestWeekdayDispatch(unittest.TestCase):

    def test_pairs_trading_called_on_wednesday(self):
        """_trigger_analysis 在周三调用 _trigger_pairs_trading。"""
        from backend.main import Scheduler
        sched = Scheduler(api_port=5555)
        sched._trigger_pairs_trading = MagicMock()
        sched._trigger_sector_rotation = MagicMock()

        # 周三 = weekday 2
        from datetime import datetime as _dt
        wed = _dt(2026, 5, 6)   # 2026-05-06 是周三
        self.assertEqual(wed.weekday(), 2)

        with patch('quant_app.run_worker.datetime') as mock_dt:
            mock_dt.now.return_value = wed
            with patch('urllib.request.urlopen') as mock_url:
                mock_url.return_value = _FakeResponse(b'{}')
                sched._trigger_analysis()

        sched._trigger_pairs_trading.assert_called_once()
        sched._trigger_sector_rotation.assert_not_called()  # 周三不触发轮动

    def test_sector_rotation_called_on_monday(self):
        from backend.main import Scheduler
        sched = Scheduler(api_port=5555)
        sched._trigger_pairs_trading = MagicMock()
        sched._trigger_sector_rotation = MagicMock()

        from datetime import datetime as _dt
        mon = _dt(2026, 5, 4)   # 2026-05-04 周一
        self.assertEqual(mon.weekday(), 0)

        with patch('quant_app.run_worker.datetime') as mock_dt:
            mock_dt.now.return_value = mon
            with patch('urllib.request.urlopen') as mock_url:
                mock_url.return_value = _FakeResponse(b'{}')
                sched._trigger_analysis()

        sched._trigger_sector_rotation.assert_called_once()
        sched._trigger_pairs_trading.assert_not_called()


if __name__ == '__main__':
    unittest.main()

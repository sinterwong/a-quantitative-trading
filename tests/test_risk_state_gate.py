"""
test_risk_state_gate.py — daily_risk_report → IntradayMonitor 硬闸门测试
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestRiskState(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self._tmp.close()
        self._prev = os.environ.get('QUANT_RISK_STATE_PATH')
        os.environ['QUANT_RISK_STATE_PATH'] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('QUANT_RISK_STATE_PATH', None)
        else:
            os.environ['QUANT_RISK_STATE_PATH'] = self._prev
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_write_with_breach_halts(self):
        from core.risk_state import write_risk_state, is_new_buys_halted
        self.assertTrue(write_risk_state(['CVaR_5%', 'drawdown_15%']))
        halted, reason = is_new_buys_halted()
        self.assertTrue(halted)
        self.assertIn('CVaR_5%', reason)

    def test_write_empty_clears(self):
        from core.risk_state import write_risk_state, is_new_buys_halted
        write_risk_state(['CVaR_5%'])  # 先 halt
        write_risk_state([])           # 再 clear
        halted, reason = is_new_buys_halted()
        self.assertFalse(halted)

    def test_stale_state_treated_as_cleared(self):
        from core.risk_state import write_risk_state, is_new_buys_halted
        write_risk_state(['CVaR_5%'])
        # 手动改 updated_at 让它 "过期"
        with open(self._tmp.name) as f:
            data = json.load(f)
        data['updated_at'] = (datetime.now() - timedelta(hours=48)).isoformat()
        with open(self._tmp.name, 'w') as f:
            json.dump(data, f)
        halted, _ = is_new_buys_halted()
        self.assertFalse(halted, '超过 ttl 应视为已恢复,不再拦截')

    def test_missing_file_does_not_halt(self):
        from core.risk_state import is_new_buys_halted
        os.unlink(self._tmp.name)
        halted, _ = is_new_buys_halted()
        self.assertFalse(halted)


class TestIntradayMonitorGate(unittest.TestCase):
    """SignalingMixin._check_new_positions 在闸门激活时直接 return。"""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self._tmp.close()
        self._prev = os.environ.get('QUANT_RISK_STATE_PATH')
        os.environ['QUANT_RISK_STATE_PATH'] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('QUANT_RISK_STATE_PATH', None)
        else:
            os.environ['QUANT_RISK_STATE_PATH'] = self._prev
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_check_new_positions_skipped_when_halted(self):
        from core.risk_state import write_risk_state
        from backend.services.intraday.signaling import SignalingMixin

        write_risk_state(['CVaR_5%'])

        m = MagicMock()
        m._risk_warn_fired = False
        m._risk_stop_fired = False
        # 让 _get_watched_symbols 被调用即可证明流程没短路;
        # 但闸门生效时应在它之前就 return
        m._get_watched_symbols = MagicMock(return_value=['X'])
        m._strategy_runner = MagicMock()
        m._strategy_runner.last_scores = {'X': 1.0}
        m._strategy_runner.config.signal_threshold = 0.5

        SignalingMixin._check_new_positions(m, datetime.now())
        m._get_watched_symbols.assert_not_called()


if __name__ == '__main__':
    unittest.main()

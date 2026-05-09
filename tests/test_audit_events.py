"""
tests/test_audit_events.py — P2-18 续 通用审计事件测试

覆盖：
  - log_order_cancel / log_liquidation / log_param_change / log_ml_retrain
  - 写入 jsonl 含 kind 字段 + entry_hash 指纹
  - 读取并正确反序列化
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest


class TestAuditEvents(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='audit_test_')
        from core import audit_log
        # 重定向全局 audit dir + 注入新 logger 单例（直接构造避免默认参数缓存）
        audit_log._AUDIT_DIR = self.tmpdir
        audit_log._default_audit_logger = audit_log.AuditLogger(audit_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_today_lines(self):
        from core.audit_log import get_audit_logger
        path = get_audit_logger()._log_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_log_order_cancel(self):
        from core.audit_log import log_order_cancel
        log_order_cancel('ORD123', '600519.SH', 'risk_max_position', origin='risk_engine')
        lines = self._read_today_lines()
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(rec['kind'], 'order_cancel')
        self.assertEqual(rec['order_id'], 'ORD123')
        self.assertEqual(rec['origin'], 'risk_engine')
        self.assertIn('entry_hash', rec)
        self.assertIn('timestamp', rec)

    def test_log_liquidation(self):
        from core.audit_log import log_liquidation
        log_liquidation('600519.SH', 'max_drawdown_breach',
                        drawdown_pct=0.18, equity=820_000.0, shares=100)
        lines = self._read_today_lines()
        self.assertEqual(lines[0]['kind'], 'liquidation')
        self.assertEqual(lines[0]['trigger'], 'max_drawdown_breach')
        self.assertAlmostEqual(lines[0]['drawdown_pct'], 0.18)
        self.assertEqual(lines[0]['shares'], 100)

    def test_log_param_change(self):
        from core.audit_log import log_param_change
        log_param_change('RiskEngine', 'max_position_pct',
                         before=0.25, after=0.20, changed_by='admin')
        lines = self._read_today_lines()
        self.assertEqual(lines[0]['kind'], 'param_change')
        self.assertEqual(lines[0]['component'], 'RiskEngine')
        self.assertEqual(lines[0]['before'], 0.25)
        self.assertEqual(lines[0]['after'], 0.20)
        self.assertEqual(lines[0]['changed_by'], 'admin')

    def test_log_ml_retrain(self):
        from core.audit_log import log_ml_retrain
        log_ml_retrain('510310.SH', 'xgboost', oos_accuracy=0.53,
                       oos_sharpe=0.42, persisted=True, reason='scheduled')
        lines = self._read_today_lines()
        self.assertEqual(lines[0]['kind'], 'ml_retrain')
        self.assertEqual(lines[0]['model'], 'xgboost')
        self.assertAlmostEqual(lines[0]['oos_accuracy'], 0.53)
        self.assertTrue(lines[0]['persisted'])
        self.assertEqual(lines[0]['reason'], 'scheduled')

    def test_multiple_events_appended(self):
        from core.audit_log import log_order_cancel, log_liquidation
        log_order_cancel('ORD-A', 'X', 'reason1')
        log_liquidation('Y', 'P0_emergency_stop', 0.20, 800_000, 200)
        lines = self._read_today_lines()
        self.assertEqual(len(lines), 2)
        kinds = [l['kind'] for l in lines]
        self.assertEqual(kinds, ['order_cancel', 'liquidation'])

    def test_extra_fields_preserved(self):
        from core.audit_log import log_order_cancel
        log_order_cancel('O1', 'X', 'r', origin='manual',
                         extra={'note': 'admin override', 'severity': 'high'})
        lines = self._read_today_lines()
        self.assertEqual(lines[0]['note'], 'admin override')
        self.assertEqual(lines[0]['severity'], 'high')

    def test_entry_hash_deterministic(self):
        """相同业务负载 → 相同 hash（不含 timestamp）。"""
        from core.audit_log import log_param_change
        log_param_change('OMS', 'kelly_cap', before=0.5, after=0.3)
        log_param_change('OMS', 'kelly_cap', before=0.5, after=0.3)
        lines = self._read_today_lines()
        self.assertEqual(lines[0]['entry_hash'], lines[1]['entry_hash'])


if __name__ == '__main__':
    unittest.main()

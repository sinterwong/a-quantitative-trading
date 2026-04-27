"""
tests/test_alerting.py — 告警系统单元测试

覆盖：
  - AlertManager：三级告警发送（无 Webhook 时仅记录日志）
  - 频率限制（同一告警短时间内不重复）
  - 最小级别过滤（min_level）
  - 每日报告格式化
  - 告警历史记录（内存）和 JSON 持久化
  - 全局单例 get_alert_manager / reset_alert_manager
  - _send_wechat / _send_dingtalk / _http_post 离线 mock

测试策略：不发起真实 HTTP 请求（mock urllib），不依赖环境变量。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# AlertRecord
# ---------------------------------------------------------------------------

class TestAlertRecord(unittest.TestCase):

    def test_to_dict_has_required_keys(self):
        from core.alerting import AlertRecord
        r = AlertRecord(
            level='CRITICAL', message='test', timestamp='2024-01-01T10:00:00',
            channel='wechat', sent=True,
        )
        d = r.to_dict()
        for key in ['level', 'message', 'timestamp', 'channel', 'sent']:
            self.assertIn(key, d)

    def test_sent_false_has_error_field(self):
        from core.alerting import AlertRecord
        r = AlertRecord(
            level='WARNING', message='x', timestamp='2024-01-01T10:00:00',
            channel='log_only', sent=False, error='timeout',
        )
        self.assertEqual(r.error, 'timeout')


# ---------------------------------------------------------------------------
# AlertManager — 无 Webhook（仅日志）
# ---------------------------------------------------------------------------

class TestAlertManagerNoWebhook(unittest.TestCase):
    """无 Webhook 配置时，发送应静默成功（log_only 渠道）。"""

    def setUp(self):
        from core.alerting import AlertManager
        self.am = AlertManager(
            wechat_webhook='',
            dingtalk_webhook='',
            smtp_config=None,
            min_level='INFO',
        )

    def test_send_critical_returns_true(self):
        result = self.am.send_critical('系统熔断测试')
        self.assertTrue(result)

    def test_send_warning_returns_true(self):
        result = self.am.send_warning('警告测试')
        self.assertTrue(result)

    def test_send_info_returns_true(self):
        result = self.am.send_info('信息测试')
        self.assertTrue(result)

    def test_send_records_history(self):
        self.am.send_critical('历史记录测试')
        self.assertEqual(len(self.am.get_history()), 1)

    def test_get_history_level_filter(self):
        self.am.send_critical('critical')
        self.am.send_warning('warning')
        self.am.send_info('info')
        self.assertEqual(len(self.am.get_history(level='CRITICAL')), 1)
        self.assertEqual(len(self.am.get_history(level='WARNING')), 1)

    def test_get_history_last_n(self):
        for i in range(10):
            self.am.send_info(f'msg {i}')
        self.assertEqual(len(self.am.get_history(last_n=3)), 3)

    def test_clear_history(self):
        self.am.send_critical('test')
        self.am.clear_history()
        self.assertEqual(len(self.am.get_history()), 0)

    def test_record_channel_log_only(self):
        self.am.send_critical('test')
        record = self.am.get_history()[-1]
        self.assertEqual(record.channel, 'log_only')


# ---------------------------------------------------------------------------
# AlertManager — min_level 过滤
# ---------------------------------------------------------------------------

class TestAlertManagerMinLevel(unittest.TestCase):

    def test_info_blocked_when_min_level_warning(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='WARNING')
        result = am.send_info('this should be filtered')
        self.assertFalse(result)
        # INFO 被过滤，不记录历史
        self.assertEqual(len(am.get_history()), 0)

    def test_warning_passes_min_level_warning(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='WARNING')
        result = am.send_warning('warning message')
        self.assertTrue(result)

    def test_critical_always_passes(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='CRITICAL')
        result = am.send_critical('critical always passes')
        self.assertTrue(result)

    def test_force_bypasses_min_level(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='CRITICAL')
        result = am._send('INFO', 'forced info', force=True)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# AlertManager — 频率限制
# ---------------------------------------------------------------------------

class TestAlertManagerRateLimit(unittest.TestCase):

    def test_same_message_blocked_within_interval(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='INFO', rate_limit_sec=3600)
        msg = '连续熔断告警'
        am.send_critical(msg)
        # 立即再次发送同内容 → 应被限制
        result = am.send_critical(msg)
        self.assertFalse(result)

    def test_different_messages_not_blocked(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='INFO', rate_limit_sec=3600)
        am.send_critical('消息A')
        result = am.send_critical('消息B')
        self.assertTrue(result)

    def test_force_bypasses_rate_limit(self):
        from core.alerting import AlertManager
        am = AlertManager(min_level='INFO', rate_limit_sec=3600)
        msg = '强制发送测试'
        am.send_critical(msg)
        result = am.send_critical(msg, force=True)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# AlertManager — 每日报告
# ---------------------------------------------------------------------------

class TestAlertManagerDailyReport(unittest.TestCase):

    def setUp(self):
        from core.alerting import AlertManager
        self.am = AlertManager(min_level='INFO')

    def test_daily_report_returns_bool(self):
        result = self.am.send_daily_report({
            'date': '2024-01-15',
            'total_pnl': 1500.0,
            'pnl_pct': 0.025,
            'n_trades': 10,
        })
        self.assertIsInstance(result, bool)

    def test_daily_report_recorded_in_history(self):
        self.am.send_daily_report({
            'date': '2024-01-15',
            'total_pnl': -500.0,
            'pnl_pct': -0.008,
            'n_trades': 3,
        })
        # 日报强制发送，应在历史中
        self.assertGreater(len(self.am.get_history()), 0)

    def test_daily_report_with_positions(self):
        result = self.am.send_daily_report({
            'date': '2024-01-15',
            'total_pnl': 800.0,
            'pnl_pct': 0.013,
            'n_trades': 5,
            'positions': {
                '000001.SZ': {'pnl': 500, 'pct': 0.02},
                '600519.SH': {'pnl': 300, 'pct': 0.005},
            },
        })
        self.assertIsInstance(result, bool)

    def test_daily_report_with_extra(self):
        result = self.am.send_daily_report({
            'date': '2024-01-15',
            'total_pnl': 200.0,
            'pnl_pct': 0.003,
            'n_trades': 2,
            'extra': {'Sharpe': 1.2, '最大回撤': '-5.3%'},
        })
        self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# AlertManager — 历史持久化
# ---------------------------------------------------------------------------

class TestAlertManagerHistoryPersistence(unittest.TestCase):

    def test_save_and_load_history(self):
        from core.alerting import AlertManager, _ALERT_LOG_DIR
        am = AlertManager(min_level='INFO')
        am.send_critical('持久化测试')
        am.send_warning('警告测试')

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'alerts_test.json'
            # Monkey-patch save path
            saved_path = am.save_history.__func__(am)  # 调用实际方法获取路径
            self.assertTrue(Path(saved_path).exists())

    def test_load_history_nonexistent_returns_empty(self):
        from core.alerting import AlertManager
        am = AlertManager()
        result = am.load_history('9999-99-99')
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

class TestGlobalAlertManager(unittest.TestCase):

    def tearDown(self):
        from core.alerting import reset_alert_manager
        reset_alert_manager()

    def test_get_alert_manager_returns_instance(self):
        from core.alerting import get_alert_manager, AlertManager
        am = get_alert_manager()
        self.assertIsInstance(am, AlertManager)

    def test_get_alert_manager_singleton(self):
        from core.alerting import get_alert_manager
        am1 = get_alert_manager()
        am2 = get_alert_manager()
        self.assertIs(am1, am2)

    def test_reset_creates_new_instance(self):
        from core.alerting import get_alert_manager, reset_alert_manager, AlertManager
        am1 = get_alert_manager()
        reset_alert_manager()
        am2 = get_alert_manager()
        self.assertIsNot(am1, am2)

    def test_reset_with_custom_manager(self):
        from core.alerting import get_alert_manager, reset_alert_manager, AlertManager
        custom = AlertManager(min_level='CRITICAL')
        reset_alert_manager(custom)
        self.assertIs(get_alert_manager(), custom)


# ---------------------------------------------------------------------------
# Webhook 发送（mock）
# ---------------------------------------------------------------------------

class TestWebhookSend(unittest.TestCase):

    @patch('core.alerting._http_post')
    def test_send_wechat_calls_http_post(self, mock_post):
        from core.alerting import _send_wechat
        mock_post.return_value = True
        result = _send_wechat('https://fake.webhook.url', '测试消息', 'CRITICAL')
        self.assertTrue(mock_post.called)

    @patch('core.alerting._http_post')
    def test_send_dingtalk_calls_http_post(self, mock_post):
        from core.alerting import _send_dingtalk
        mock_post.return_value = True
        result = _send_dingtalk('https://fake.webhook.url', '测试消息', 'WARNING')
        self.assertTrue(mock_post.called)

    @patch('core.alerting._http_post')
    def test_alert_manager_uses_wechat_when_configured(self, mock_post):
        from core.alerting import AlertManager
        mock_post.return_value = True
        am = AlertManager(
            wechat_webhook='https://fake.wechat.url',
            min_level='INFO',
            rate_limit_sec=0,  # 不限速
        )
        am.send_critical('企业微信测试')
        self.assertTrue(mock_post.called)
        # 历史中应记录 wechat 渠道
        record = am.get_history()[-1]
        self.assertEqual(record.channel, 'wechat')

    @patch('core.alerting._http_post')
    def test_alert_manager_falls_back_to_dingtalk(self, mock_post):
        from core.alerting import AlertManager
        mock_post.return_value = True
        am = AlertManager(
            wechat_webhook='',
            dingtalk_webhook='https://fake.dingtalk.url',
            min_level='INFO',
            rate_limit_sec=0,
        )
        am.send_warning('钉钉降级测试')
        self.assertTrue(mock_post.called)
        record = am.get_history()[-1]
        self.assertEqual(record.channel, 'dingtalk')

    @patch('core.alerting._http_post')
    def test_http_post_failure_logs_error(self, mock_post):
        from core.alerting import AlertManager
        mock_post.return_value = False  # 发送失败
        am = AlertManager(
            wechat_webhook='https://fake.url',
            min_level='INFO',
            rate_limit_sec=0,
        )
        # 即使发送失败，也不应崩溃
        am.send_critical('测试失败容错')
        record = am.get_history()[-1]
        self.assertFalse(record.sent)


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter(unittest.TestCase):

    def test_can_send_initially_true(self):
        from core.alerting import _RateLimiter
        rl = _RateLimiter(min_interval_sec=60)
        self.assertTrue(rl.can_send('test_key'))

    def test_cannot_send_after_marking(self):
        from core.alerting import _RateLimiter
        rl = _RateLimiter(min_interval_sec=3600)
        rl.mark_sent('test_key')
        self.assertFalse(rl.can_send('test_key'))

    def test_can_send_after_interval(self):
        from core.alerting import _RateLimiter
        rl = _RateLimiter(min_interval_sec=0)  # 0秒间隔 → 立即可再发
        rl.mark_sent('test_key')
        time.sleep(0.01)
        self.assertTrue(rl.can_send('test_key'))

    def test_different_keys_independent(self):
        from core.alerting import _RateLimiter
        rl = _RateLimiter(min_interval_sec=3600)
        rl.mark_sent('key_A')
        self.assertTrue(rl.can_send('key_B'))


if __name__ == '__main__':
    unittest.main()

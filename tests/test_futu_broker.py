"""
tests/test_futu_broker.py — FutuBroker & FutuPaperValidator 单元测试

所有测试均在 OpenD 未运行（离线）的环境下执行：
  - FutuBroker 离线时优雅降级（返回空/零值，不崩溃）
  - FutuPaperValidator 自动退化到 SimulatedBroker
  - 代码格式转换工具函数覆盖

不需要真实 OpenD 进程或 futu-api 账户。
"""

from __future__ import annotations

import unittest
from datetime import datetime


# ---------------------------------------------------------------------------
# 代码格式转换
# ---------------------------------------------------------------------------

class TestCodeConversion(unittest.TestCase):

    def test_standard_to_futu_sh(self):
        from core.brokers.futu import _standard_to_futu
        self.assertEqual(_standard_to_futu('600519.SH'), 'SH.600519')

    def test_standard_to_futu_sz(self):
        from core.brokers.futu import _standard_to_futu
        self.assertEqual(_standard_to_futu('000001.SZ'), 'SZ.000001')

    def test_standard_to_futu_hk(self):
        from core.brokers.futu import _standard_to_futu
        self.assertEqual(_standard_to_futu('00700.HK'), 'HK.00700')

    def test_futu_to_standard_sh(self):
        from core.brokers.futu import _futu_to_standard
        self.assertEqual(_futu_to_standard('SH.600519'), '600519.SH')

    def test_futu_to_standard_sz(self):
        from core.brokers.futu import _futu_to_standard
        self.assertEqual(_futu_to_standard('SZ.000001'), '000001.SZ')

    def test_futu_to_standard_hk(self):
        from core.brokers.futu import _futu_to_standard
        self.assertEqual(_futu_to_standard('HK.00700'), '00700.HK')

    def test_roundtrip_sh(self):
        from core.brokers.futu import _standard_to_futu, _futu_to_standard
        original = '600519.SH'
        self.assertEqual(_futu_to_standard(_standard_to_futu(original)), original)

    def test_roundtrip_sz(self):
        from core.brokers.futu import _standard_to_futu, _futu_to_standard
        original = '000001.SZ'
        self.assertEqual(_futu_to_standard(_standard_to_futu(original)), original)

    def test_no_dot_passthrough(self):
        from core.brokers.futu import _standard_to_futu
        self.assertEqual(_standard_to_futu('AAPL'), 'AAPL')


# ---------------------------------------------------------------------------
# FutuBroker — 离线模式
# ---------------------------------------------------------------------------

class TestFutuBrokerOffline(unittest.TestCase):
    """OpenD 未运行时，所有方法应优雅降级，不抛出异常。"""

    def setUp(self):
        from core.brokers.futu import FutuBroker
        self.broker = FutuBroker(host='127.0.0.1', port=19999, trade_env='SIMULATE')

    def test_connect_returns_false_offline(self):
        result = self.broker.connect()
        self.assertFalse(result)

    def test_is_connected_false_after_failed_connect(self):
        self.broker.connect()
        self.assertFalse(self.broker.is_connected())

    def test_disconnect_no_crash_when_not_connected(self):
        self.broker.disconnect()  # should not raise

    def test_get_account_offline_returns_default(self):
        from core.brokers.base import AccountInfo
        acc = self.broker.get_account()
        self.assertIsInstance(acc, AccountInfo)
        self.assertEqual(acc.total_assets, 0.0)

    def test_get_cash_offline_returns_zero(self):
        cash = self.broker.get_cash()
        self.assertEqual(cash, 0.0)

    def test_get_positions_offline_returns_empty(self):
        positions = self.broker.get_positions()
        self.assertIsInstance(positions, list)
        self.assertEqual(len(positions), 0)

    def test_get_quote_offline_returns_default(self):
        from core.brokers.base import QuoteData
        q = self.broker.get_quote('000001.SZ')
        self.assertIsInstance(q, QuoteData)
        self.assertEqual(q.last, 0.0)

    def test_is_market_open_offline_returns_false(self):
        result = self.broker.is_market_open()
        self.assertFalse(result)

    def test_submit_order_offline_returns_zero_fill(self):
        from core.oms import Order, Fill
        order = Order(symbol='000001.SZ', direction='BUY',
                      order_type='MARKET', shares=100, price=15.0)
        fill = self.broker.submit_order(order)
        self.assertIsInstance(fill, Fill)
        self.assertEqual(fill.shares, 0)

    def test_cancel_order_offline_returns_false(self):
        result = self.broker.cancel_order('ORDER_ID_123')
        self.assertFalse(result)

    def test_get_orders_offline_returns_empty(self):
        orders = self.broker.get_orders()
        self.assertIsInstance(orders, list)
        self.assertEqual(len(orders), 0)

    def test_get_fills_offline_returns_empty(self):
        fills = self.broker.get_fills()
        self.assertIsInstance(fills, list)
        self.assertEqual(len(fills), 0)

    def test_send_compat_offline(self):
        """send() 是 BrokerAdapter 兼容接口，应等价于 submit_order。"""
        from core.oms import Order
        order = Order(symbol='000001.SZ', direction='BUY',
                      order_type='MARKET', shares=100, price=15.0)
        fill = self.broker.send(order)
        self.assertEqual(fill.shares, 0)

    def test_cancel_compat_offline(self):
        """cancel() 是 BrokerAdapter 兼容接口。"""
        result = self.broker.cancel('ORDER_123')
        self.assertFalse(result)

    def test_supported_markets(self):
        from core.brokers.base import MarketType
        markets = self.broker.supported_markets()
        self.assertIn(MarketType.A_SHARE, markets)
        self.assertIn(MarketType.HK_STOCK, markets)
        self.assertIn(MarketType.US_STOCK, markets)

    def test_name(self):
        self.assertEqual(self.broker.name, 'FutuBroker')

    def test_simulate_default_env(self):
        from core.brokers.futu import FutuBroker
        broker = FutuBroker()
        self.assertEqual(broker.trade_env, 'SIMULATE')


# ---------------------------------------------------------------------------
# FutuBroker — 状态转换映射
# ---------------------------------------------------------------------------

class TestFutuOrderStatusMap(unittest.TestCase):

    def test_submitted_maps_to_pending(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('SUBMITTED'), 'PENDING')

    def test_filled_all_maps_to_filled(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('FILLED_ALL'), 'FILLED')

    def test_cancelled_maps_to_cancelled(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('CANCELLED_ALL'), 'CANCELLED')

    def test_failed_maps_to_rejected(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('FAILED'), 'REJECTED')

    def test_unknown_maps_to_pending(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('WHATEVER'), 'PENDING')

    def test_case_insensitive(self):
        from core.brokers.futu import FutuBroker
        self.assertEqual(FutuBroker._map_order_status('filled_all'), 'FILLED')


# ---------------------------------------------------------------------------
# FutuPaperValidator — 离线降级验证
# ---------------------------------------------------------------------------

class TestFutuPaperValidatorOffline(unittest.TestCase):
    """OpenD 未运行时，FutuPaperValidator 自动退化到 SimulatedBroker。"""

    def setUp(self):
        from core.paper_trade_validator import FutuPaperValidator
        self.validator = FutuPaperValidator(
            futu_host='127.0.0.1',
            futu_port=19999,  # 不存在的端口 → connect 失败
            threshold_bps=20.0,
        )

    def test_connect_returns_false_offline(self):
        ok = self.validator.connect()
        self.assertFalse(ok)

    def test_validate_signals_no_crash(self):
        """离线时应退化到 SimulatedBroker，不崩溃。"""
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.5, 'shares': 200},
        ]
        report = self.validator.validate_signals(signals, use_futu=False)
        self.assertIsNotNone(report)

    def test_validate_signals_pass_rate_is_float(self):
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
        ]
        report = self.validator.validate_signals(signals, use_futu=False)
        self.assertIsInstance(report.pass_rate, float)

    def test_validate_signals_deviation_within_threshold(self):
        """模拟撮合偏差应在合理范围内（SimulatedBroker 模式）。"""
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
            {'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100},
        ]
        report = self.validator.validate_signals(signals, use_futu=False)
        # 纯模拟时，偏差应该很小
        self.assertLessEqual(report.avg_deviation_bps, 100.0)

    def test_daily_report_empty_returns_no_data(self):
        report = self.validator.generate_daily_report()
        self.assertEqual(report['status'], 'no_data')

    def test_daily_report_after_validation(self):
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
        ]
        self.validator.validate_signals(signals, use_futu=False)
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            report = self.validator.generate_daily_report(save_path=path)
            self.assertIn('status', report)
            self.assertIn('avg_pass_rate', report)
            self.assertTrue(os.path.exists(path))
        finally:
            os.unlink(path)

    def test_daily_report_clears_log(self):
        """generate_daily_report 后日志应被清空。"""
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
        ]
        self.validator.validate_signals(signals, use_futu=False)
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            self.validator.generate_daily_report(save_path=path)
            # 第二次报告应是 no_data
            with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f2:
                path2 = f2.name
            try:
                r2 = self.validator.generate_daily_report(save_path=path2)
                self.assertEqual(r2['status'], 'no_data')
            finally:
                os.unlink(path2)
        finally:
            os.unlink(path)

    def test_disconnect_no_crash(self):
        self.validator.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# FutuPaperValidator — 验证逻辑
# ---------------------------------------------------------------------------

class TestFutuPaperValidatorLogic(unittest.TestCase):
    """使用 SimulatedBroker（强制离线），验证业务逻辑。"""

    def setUp(self):
        from core.paper_trade_validator import FutuPaperValidator
        self.validator = FutuPaperValidator(threshold_bps=20.0, signal_match_target=0.9)

    def test_multiple_batches_accumulate(self):
        """多次 validate_signals 的日志应累积。"""
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
        ]
        self.validator.validate_signals(signals, use_futu=False)
        self.validator.validate_signals(signals, use_futu=False)

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            report = self.validator.generate_daily_report(save_path=path)
            self.assertEqual(report['n_batches'], 2)
        finally:
            os.unlink(path)

    def test_empty_signals_returns_report(self):
        report = self.validator.validate_signals([], use_futu=False)
        self.assertIsNotNone(report)
        self.assertEqual(report.n_trades, 0)

    def test_signal_match_target_in_daily_report(self):
        signals = [
            {'symbol': '000001.SZ', 'direction': 'BUY', 'price': 15.0, 'shares': 100},
        ]
        self.validator.validate_signals(signals, use_futu=False)
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            report = self.validator.generate_daily_report(save_path=path)
            self.assertEqual(report['signal_match_target'], 0.9)
            self.assertIn('signal_match_ok', report)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()

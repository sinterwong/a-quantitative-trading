"""
tests/test_metrics_p2_18.py — P2-18 可观测性补齐测试

覆盖新增指标方法：
  - set_risk_metrics(): VaR / CVaR / drawdown / MC P95
  - set_broker_online(): broker 在线状态（带 broker label）
  - record_order_status(): 订单状态计数
  - record_data_source_failure(): 数据源失败计数
  - 静默降级：prometheus_client 不可用时新方法不抛异常
  - generate() 输出包含新指标名
"""

from __future__ import annotations

import threading
import unittest


class TestSetRiskMetrics(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_sets_all_four_values(self):
        self.reg.set_risk_metrics(
            var_pct=0.025,
            cvar_pct=0.038,
            drawdown_pct=0.12,
            max_drawdown_p95=0.18,
        )
        self.assertAlmostEqual(self.reg.var_pct._value.get(), 0.025)
        self.assertAlmostEqual(self.reg.cvar_pct._value.get(), 0.038)
        self.assertAlmostEqual(self.reg.drawdown_pct._value.get(), 0.12)
        self.assertAlmostEqual(self.reg.max_drawdown_p95._value.get(), 0.18)

    def test_partial_update_skips_none(self):
        # 先全部设一遍
        self.reg.set_risk_metrics(0.01, 0.02, 0.03, 0.04)
        # 仅更新 var_pct，其他保持
        self.reg.set_risk_metrics(var_pct=0.05)
        self.assertAlmostEqual(self.reg.var_pct._value.get(), 0.05)
        self.assertAlmostEqual(self.reg.cvar_pct._value.get(), 0.02)
        self.assertAlmostEqual(self.reg.drawdown_pct._value.get(), 0.03)
        self.assertAlmostEqual(self.reg.max_drawdown_p95._value.get(), 0.04)

    def test_no_args_no_raise(self):
        self.reg.set_risk_metrics()   # 全 None — no exception


class TestSetBrokerOnline(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_online_sets_one(self):
        self.reg.set_broker_online('futu', True)
        val = self.reg.broker_online.labels(broker='futu')._value.get()
        self.assertEqual(val, 1)

    def test_offline_sets_zero(self):
        self.reg.set_broker_online('futu', False)
        val = self.reg.broker_online.labels(broker='futu')._value.get()
        self.assertEqual(val, 0)

    def test_multiple_brokers_independent(self):
        self.reg.set_broker_online('futu', True)
        self.reg.set_broker_online('paper', False)
        self.assertEqual(self.reg.broker_online.labels(broker='futu')._value.get(), 1)
        self.assertEqual(self.reg.broker_online.labels(broker='paper')._value.get(), 0)


class TestRecordOrderStatus(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_filled_count(self):
        self.reg.record_order_status('filled')
        val = self.reg.order_status_count.labels(status='filled')._value.get()
        self.assertEqual(val, 1)

    def test_multiple_increments(self):
        for _ in range(3):
            self.reg.record_order_status('rejected')
        val = self.reg.order_status_count.labels(status='rejected')._value.get()
        self.assertEqual(val, 3)

    def test_status_labels_independent(self):
        self.reg.record_order_status('filled')
        self.reg.record_order_status('filled')
        self.reg.record_order_status('cancelled')
        f = self.reg.order_status_count.labels(status='filled')._value.get()
        c = self.reg.order_status_count.labels(status='cancelled')._value.get()
        self.assertEqual(f, 2)
        self.assertEqual(c, 1)


class TestRecordDataSourceFailure(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_increments_per_source(self):
        self.reg.record_data_source_failure('akshare')
        self.reg.record_data_source_failure('akshare')
        self.reg.record_data_source_failure('tencent')
        ak = self.reg.data_source_failures.labels(source='akshare')._value.get()
        tc = self.reg.data_source_failures.labels(source='tencent')._value.get()
        self.assertEqual(ak, 2)
        self.assertEqual(tc, 1)


class TestGenerateContainsNewMetrics(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_generate_includes_risk_metrics(self):
        self.reg.set_risk_metrics(0.02, 0.03, 0.05, 0.10)
        out = self.reg.generate().decode('utf-8')
        self.assertIn('trading_var_pct', out)
        self.assertIn('trading_cvar_pct', out)
        self.assertIn('trading_drawdown_pct', out)
        self.assertIn('trading_max_drawdown_p95', out)

    def test_generate_includes_broker_status(self):
        self.reg.set_broker_online('futu', True)
        out = self.reg.generate().decode('utf-8')
        self.assertIn('trading_broker_online', out)
        self.assertIn('broker="futu"', out)

    def test_generate_includes_order_status(self):
        self.reg.record_order_status('filled')
        out = self.reg.generate().decode('utf-8')
        self.assertIn('trading_order_status_total', out)

    def test_generate_includes_data_source_failures(self):
        self.reg.record_data_source_failure('akshare')
        out = self.reg.generate().decode('utf-8')
        self.assertIn('trading_data_source_failures_total', out)


class TestUnavailableFallback(unittest.TestCase):
    """prometheus_client 不可用时新方法不抛异常。"""

    def setUp(self):
        from core.metrics import reset_registry
        reset_registry()

    def tearDown(self):
        from core.metrics import reset_registry
        reset_registry()

    def _unavailable_reg(self):
        from core.metrics import MetricsRegistry
        reg = MetricsRegistry.__new__(MetricsRegistry)
        reg._available = False
        reg._lock = threading.Lock()
        reg._last_update = 0.0
        return reg

    def test_set_risk_metrics_no_raise(self):
        reg = self._unavailable_reg()
        reg.set_risk_metrics(0.1, 0.2, 0.05, 0.15)   # no exception

    def test_set_broker_online_no_raise(self):
        reg = self._unavailable_reg()
        reg.set_broker_online('futu', True)   # no exception

    def test_record_order_status_no_raise(self):
        reg = self._unavailable_reg()
        reg.record_order_status('filled')   # no exception

    def test_record_data_source_failure_no_raise(self):
        reg = self._unavailable_reg()
        reg.record_data_source_failure('akshare')   # no exception


if __name__ == '__main__':
    unittest.main()

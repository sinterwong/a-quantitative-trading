"""
tests/test_metrics.py — Prometheus 指标模块测试

覆盖：
  - MetricsRegistry 初始化（prometheus_client 可用时正常建立）
  - update_from_portfolio(): 设置指标值
  - set_health(): 字符串 level → 数值映射
  - set_factor_ic(): 带 label 的 Gauge 设置
  - record_api_request(): 请求数 / 延迟 / 错误计数
  - generate(): 输出包含已定义的指标名
  - prometheus_client 缺失时所有方法静默降级
  - get_registry() 返回单例
  - reset_registry() 重置单例
  - update_from_api(): API 不可达时静默忽略
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock


class TestMetricsRegistryInit(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry
        reset_registry()

    def tearDown(self):
        from core.metrics import reset_registry
        reset_registry()

    def test_registry_available(self):
        from core.metrics import MetricsRegistry
        reg = MetricsRegistry()
        self.assertTrue(reg.available)

    def test_singleton_returns_same_instance(self):
        from core.metrics import get_registry
        r1 = get_registry()
        r2 = get_registry()
        self.assertIs(r1, r2)

    def test_reset_creates_new_instance(self):
        from core.metrics import get_registry, reset_registry
        r1 = get_registry()
        reset_registry()
        r2 = get_registry()
        self.assertIsNot(r1, r2)


class TestUpdateFromPortfolio(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_sets_net_value(self):
        self.reg.update_from_portfolio(net_value=1.05, total_pnl=500.0,
                                        n_positions=3, cash=10000.0)
        self.assertAlmostEqual(self.reg.net_value._value.get(), 1.05)

    def test_sets_cash(self):
        self.reg.update_from_portfolio(cash=50000.0)
        self.assertAlmostEqual(self.reg.cash._value.get(), 50000.0)

    def test_sets_n_positions(self):
        self.reg.update_from_portfolio(n_positions=5)
        self.assertEqual(int(self.reg.n_positions._value.get()), 5)

    def test_does_not_raise_on_zero_values(self):
        self.reg.update_from_portfolio(0.0, 0.0, 0, 0.0)   # no exception


class TestSetHealth(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_ok_maps_to_zero(self):
        self.reg.set_health('OK')
        self.assertEqual(self.reg.health_status._value.get(), 0)

    def test_warn_maps_to_one(self):
        self.reg.set_health('WARN')
        self.assertEqual(self.reg.health_status._value.get(), 1)

    def test_critical_maps_to_two(self):
        self.reg.set_health('CRITICAL')
        self.assertEqual(self.reg.health_status._value.get(), 2)

    def test_case_insensitive(self):
        self.reg.set_health('warn')
        self.assertEqual(self.reg.health_status._value.get(), 1)

    def test_unknown_maps_to_zero(self):
        self.reg.set_health('UNKNOWN')
        self.assertEqual(self.reg.health_status._value.get(), 0)


class TestSetFactorIC(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_sets_ic_value(self):
        self.reg.set_factor_ic('RSI', 0.032)
        val = self.reg.factor_ic.labels(factor_name='RSI')._value.get()
        self.assertAlmostEqual(val, 0.032)

    def test_multiple_factors(self):
        self.reg.set_factor_ic('RSI', 0.032)
        self.reg.set_factor_ic('MACD', 0.021)
        self.reg.set_factor_ic('Bollinger', -0.005)
        rsi_val = self.reg.factor_ic.labels(factor_name='RSI')._value.get()
        macd_val = self.reg.factor_ic.labels(factor_name='MACD')._value.get()
        self.assertGreater(rsi_val, macd_val)


class TestRecordApiRequest(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_increments_request_counter(self):
        self.reg.record_api_request('/positions', 'GET', 25.0, 200)
        val = self.reg.api_requests.labels(endpoint='/positions', method='GET')._value.get()
        self.assertEqual(val, 1)

    def test_records_error_on_4xx(self):
        self.reg.record_api_request('/positions', 'GET', 10.0, 404)
        err_val = self.reg.api_errors.labels(endpoint='/positions', status_code='404')._value.get()
        self.assertEqual(err_val, 1)

    def test_no_error_on_2xx(self):
        self.reg.record_api_request('/positions', 'GET', 10.0, 200)
        # 404 counter should still be 0
        err_val = self.reg.api_errors.labels(endpoint='/positions', status_code='404')._value.get()
        self.assertEqual(err_val, 0)

    def test_multiple_requests_accumulate(self):
        for _ in range(5):
            self.reg.record_api_request('/trades', 'GET', 30.0, 200)
        val = self.reg.api_requests.labels(endpoint='/trades', method='GET')._value.get()
        self.assertEqual(val, 5)


class TestGenerate(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_generate_returns_bytes(self):
        output = self.reg.generate()
        self.assertIsInstance(output, bytes)

    def test_generate_contains_metric_names(self):
        self.reg.update_from_portfolio(net_value=1.1, cash=5000.0)
        output = self.reg.generate().decode('utf-8')
        self.assertIn('trading_net_value', output)
        self.assertIn('trading_cash_yuan', output)

    def test_content_type_is_prometheus_format(self):
        ct = self.reg.content_type
        self.assertIn('text/plain', ct)


class TestUnavailableFallback(unittest.TestCase):
    """prometheus_client 不可用时所有方法静默降级。"""

    def setUp(self):
        from core.metrics import reset_registry
        reset_registry()

    def tearDown(self):
        from core.metrics import reset_registry
        reset_registry()

    def _make_unavailable_registry(self):
        import sys
        with patch.dict(sys.modules, {
            'prometheus_client': None,
        }):
            from core import metrics as _m
            # 直接构造，绕过单例
            reg = object.__new__(_m.MetricsRegistry)
            reg._available = False
            reg._lock = __import__('threading').Lock()
            reg._last_update = 0.0
            return reg

    def test_update_from_portfolio_no_raise(self):
        from core.metrics import MetricsRegistry
        # 构造一个 _available=False 的实例
        reg = MetricsRegistry.__new__(MetricsRegistry)
        reg._available = False
        reg._lock = __import__('threading').Lock()
        reg._last_update = 0.0
        reg.update_from_portfolio(1.0, 100.0, 2, 5000.0)   # no exception

    def test_generate_returns_placeholder(self):
        from core.metrics import MetricsRegistry
        reg = MetricsRegistry.__new__(MetricsRegistry)
        reg._available = False
        reg._lock = __import__('threading').Lock()
        output = reg.generate()
        self.assertIn(b'not available', output)


class TestUpdateFromApi(unittest.TestCase):

    def setUp(self):
        from core.metrics import reset_registry, MetricsRegistry
        reset_registry()
        self.reg = MetricsRegistry()

    def test_api_unreachable_does_not_raise(self):
        with patch('urllib.request.urlopen', side_effect=OSError('refused')):
            self.reg.update_from_api(api_port=9999)  # should not raise

    def test_api_success_updates_metrics(self):
        import json
        payload = json.dumps({
            'summary': {
                'net_value': 1.08,
                'total_pnl': 800.0,
                'n_positions': 4,
                'cash': 20000.0,
            }
        }).encode()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=payload)))
        ctx.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=ctx):
            self.reg.update_from_api()
        self.assertAlmostEqual(self.reg.net_value._value.get(), 1.08)


if __name__ == '__main__':
    unittest.main()

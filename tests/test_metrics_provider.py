"""tests/test_metrics_provider.py — DataGateway provider 指标接 Prometheus。

覆盖：
  - observe_provider() 写入 data_gateway_provider_requests_total / _latency_seconds
  - HealthTracker.record() 旁路调用 observe_provider
  - DataGateway._candidates_for 在熔断 open 时记 status=circuit_open
  - /metrics 输出包含新指标的标签
"""

from __future__ import annotations

import unittest

from core.data_gateway.capabilities import Capability
from core.data_gateway.health import HealthTracker
from core.metrics import get_registry, reset_registry


def _count_for(reg, status: str, provider: str, capability: str) -> float:
    """从 Counter._metrics 中取某 label 组合的当前值（prometheus_client 内部 API）。"""
    metric = reg.provider_requests.labels(
        provider=provider, capability=capability, status=status,
    )
    return metric._value.get()


def _histogram_count(reg, provider: str, capability: str) -> float:
    metric = reg.provider_latency.labels(provider=provider, capability=capability)
    return metric._sum.get()


class TestObserveProvider(unittest.TestCase):

    def setUp(self):
        reset_registry()
        self.reg = get_registry()

    def tearDown(self):
        reset_registry()

    def test_ok_increments_counter_and_histogram(self):
        self.reg.observe_provider('tencent', 'quote', 'ok', 120.0)
        self.assertEqual(_count_for(self.reg, 'ok', 'tencent', 'quote'), 1)
        # 120ms → 0.12s, histogram sum 应等于 0.12
        self.assertAlmostEqual(
            _histogram_count(self.reg, 'tencent', 'quote'), 0.12, places=6,
        )

    def test_error_increments_counter_with_error_status(self):
        self.reg.observe_provider('sina', 'fundamentals', 'error', 30.0)
        self.assertEqual(_count_for(self.reg, 'error', 'sina', 'fundamentals'), 1)

    def test_circuit_open_skips_histogram(self):
        """status=circuit_open 不写延迟直方图（observe_provider 内置约束）。"""
        self.reg.observe_provider('em', 'quote', 'circuit_open', 0.0)
        self.assertEqual(_count_for(self.reg, 'circuit_open', 'em', 'quote'), 1)
        # 直方图 sum 仍为 0
        self.assertEqual(_histogram_count(self.reg, 'em', 'quote'), 0.0)

    def test_unavailable_registry_silent(self):
        """prometheus_client 缺失时 observe_provider 应静默不报错。"""
        # 直接构造一个 _available=False 的 registry 拷贝行为
        from core.metrics import MetricsRegistry
        r = MetricsRegistry()
        r._available = False
        r.observe_provider('x', 'quote', 'ok', 50.0)   # should not raise


class TestHealthTrackerIntegration(unittest.TestCase):

    def setUp(self):
        reset_registry()
        self.reg = get_registry()

    def tearDown(self):
        reset_registry()

    def test_record_success_writes_metric(self):
        ht = HealthTracker()
        ht.record('tencent', Capability.QUOTE, success=True, latency_ms=100.0)
        self.assertEqual(
            _count_for(self.reg, 'ok', 'tencent', 'quote'), 1,
        )

    def test_record_failure_writes_error_metric(self):
        ht = HealthTracker()
        ht.record('akshare', Capability.FUNDAMENTALS, success=False, latency_ms=2500.0)
        self.assertEqual(
            _count_for(self.reg, 'error', 'akshare', 'fundamentals'), 1,
        )


class TestCircuitOpenIntegration(unittest.TestCase):
    """模拟熔断打开后 _candidates_for 应写 circuit_open 计数。"""

    def setUp(self):
        reset_registry()
        self.reg = get_registry()

    def tearDown(self):
        reset_registry()

    def test_breaker_open_marks_circuit_open_label(self):
        from unittest.mock import MagicMock, patch
        from core.data_gateway.capabilities import (
            Capability, Market, ProviderCapability,
        )
        from core.data_gateway.gateway import DataGateway

        gw = DataGateway(enable_disk_cache=False)

        class _StubProvider:
            name = 'baostock'

            def declare(self):
                return ProviderCapability(
                    capabilities=frozenset({Capability.FUNDAMENTALS}),
                    markets=frozenset({Market.A}),
                    priority_hint=0.5,
                )

            def supports(self, *_a, **_kw):
                return True

            def field_authority(self):
                return {}

        gw.register_provider(_StubProvider())

        fake_breaker = MagicMock()
        fake_breaker.allow.return_value = False  # circuit open

        with patch('core.data_gateway.gateway._breaker_for',
                   return_value=fake_breaker):
            out = gw._candidates_for(Capability.FUNDAMENTALS, Market.A)
            self.assertEqual(out, [])  # 全部 provider 被跳过

        self.assertGreaterEqual(
            _count_for(self.reg, 'circuit_open', 'baostock', 'fundamentals'),
            1,
        )


class TestGenerateExposesProviderMetrics(unittest.TestCase):
    """/metrics 文本应包含新指标名 + label。"""

    def setUp(self):
        reset_registry()
        self.reg = get_registry()

    def tearDown(self):
        reset_registry()

    def test_provider_metrics_appear_in_generate_output(self):
        self.reg.observe_provider('tencent', 'quote', 'ok', 80.0)
        body = self.reg.generate().decode('utf-8')
        self.assertIn('data_gateway_provider_requests_total', body)
        self.assertIn('data_gateway_provider_latency_seconds', body)
        self.assertIn('provider="tencent"', body)
        self.assertIn('capability="quote"', body)
        self.assertIn('status="ok"', body)


if __name__ == '__main__':
    unittest.main()

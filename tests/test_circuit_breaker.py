"""
tests/test_circuit_breaker.py — P2-16 熔断器测试

覆盖：
  - closed → open 触发条件（连续 N 次失败）
  - open 状态 allow() 返回 False
  - cooldown 后转 half_open；half_open 失败 → 立即 re-open
  - on_success 重置 failure 计数
  - on_open 回调被调用
  - 全局注册表 get_breaker 单例语义
  - data_layer 集成：模拟 AKShare 连续失败后熔断短路
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from freezegun import freeze_time


class TestCircuitBreakerCore(unittest.TestCase):

    def setUp(self):
        from core.circuit_breaker import reset_all
        reset_all()

    def test_initial_state_closed(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x')
        self.assertEqual(cb.state(), 'closed')
        self.assertTrue(cb.allow())

    def test_opens_after_threshold_failures(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x', failure_threshold=3)
        cb.on_failure()
        self.assertEqual(cb.state(), 'closed')   # 1
        cb.on_failure()
        self.assertEqual(cb.state(), 'closed')   # 2
        cb.on_failure()
        self.assertEqual(cb.state(), 'open')     # 3 → open
        self.assertFalse(cb.allow())

    def test_success_resets_count(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x', failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        cb.on_failure()
        self.assertEqual(cb.state(), 'closed')   # 重置后只 1 次失败

    def test_cooldown_transitions_to_half_open(self):
        """R3-3: freeze_time 替代 time.sleep(0.06)。"""
        from core.circuit_breaker import CircuitBreaker
        with freeze_time('2026-05-19 10:00:00') as frozen:
            cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=60)
            cb.on_failure()
            cb.on_failure()
            self.assertEqual(cb.state(), 'open')
            frozen.tick(delta=61)  # 跨过 cooldown
            self.assertEqual(cb.state(), 'half_open')
            self.assertTrue(cb.allow())

    def test_half_open_failure_reopens(self):
        from core.circuit_breaker import CircuitBreaker
        with freeze_time('2026-05-19 10:00:00') as frozen:
            cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=60)
            cb.on_failure()
            cb.on_failure()
            frozen.tick(delta=61)
            # half_open 状态下再失败应立即 re-open
            cb.on_failure()
            self.assertEqual(cb.state(), 'open')

    def test_half_open_success_closes(self):
        from core.circuit_breaker import CircuitBreaker
        with freeze_time('2026-05-19 10:00:00') as frozen:
            cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=60)
            cb.on_failure()
            cb.on_failure()
            frozen.tick(delta=61)
            cb.on_success()
            self.assertEqual(cb.state(), 'closed')

    def test_on_open_callback(self):
        from core.circuit_breaker import CircuitBreaker
        called = []
        cb = CircuitBreaker('x', failure_threshold=2,
                            on_open=lambda name: called.append(name))
        cb.on_failure()
        cb.on_failure()
        self.assertEqual(called, ['x'])

    def test_on_open_callback_only_fires_once_per_open(self):
        from core.circuit_breaker import CircuitBreaker
        called = []
        cb = CircuitBreaker('x', failure_threshold=2,
                            on_open=lambda name: called.append(name))
        cb.on_failure()
        cb.on_failure()
        cb.on_failure()   # 已 open；额外失败不应再触发回调
        self.assertEqual(len(called), 1)


class TestGlobalRegistry(unittest.TestCase):

    def setUp(self):
        from core.circuit_breaker import _REGISTRY, reset_all
        reset_all()
        _REGISTRY.clear()

    def test_get_breaker_returns_same_instance(self):
        from core.circuit_breaker import get_breaker
        cb1 = get_breaker('akshare')
        cb2 = get_breaker('akshare')
        self.assertIs(cb1, cb2)

    def test_different_names_distinct(self):
        from core.circuit_breaker import get_breaker
        cb1 = get_breaker('akshare')
        cb2 = get_breaker('tencent')
        self.assertIsNot(cb1, cb2)

    def test_all_states_returns_snapshot(self):
        from core.circuit_breaker import get_breaker, all_states
        get_breaker('a')
        get_breaker('b')
        states = all_states()
        self.assertIn('a', states)
        self.assertIn('b', states)
        self.assertEqual(states['a'], 'closed')


class TestDataGatewayBreakerIntegration(unittest.TestCase):
    """DataGateway 与 circuit_breaker 集成验证。"""

    def setUp(self):
        from core.circuit_breaker import _REGISTRY, reset_all
        reset_all()
        _REGISTRY.clear()

    def test_open_breaker_filters_provider(self):
        """熔断器 open 时,gateway._candidates_for 不再返回该 provider。"""
        from core.data_gateway.capabilities import Capability, Market
        from core.data_gateway.gateway import DataGateway
        from core.data_gateway.health import HealthTracker
        from core.data_gateway.providers.base import Provider
        from core.data_gateway.capabilities import ProviderCapability
        from core.circuit_breaker import get_breaker

        class _P(Provider):
            name = "p_open"
            def declare(self):
                return ProviderCapability(
                    capabilities=frozenset({Capability.QUOTE}),
                    markets=frozenset({Market.A}),
                    priority_hint=0.8,
                )

        gw = DataGateway(health=HealthTracker(warmup_count=1))
        gw.register_provider(_P())

        # 手动触发熔断
        cb = get_breaker('gw_p_open_quote', failure_threshold=2, cooldown_seconds=10.0)
        cb.on_failure()
        cb.on_failure()
        self.assertEqual(cb.state(), 'open')

        # candidates 应为空
        cands = gw._candidates_for(Capability.QUOTE, Market.A)
        self.assertEqual(cands, [])

    def test_provider_error_records_to_breaker(self):
        """provider 抛 ProviderError 时累计触发熔断。"""
        from core.data_gateway.capabilities import (
            Capability, Market, ProviderCapability,
        )
        from core.data_gateway.gateway import DataGateway
        from core.data_gateway.health import HealthTracker
        from core.data_gateway.providers.base import Provider, ProviderError
        from core.circuit_breaker import get_breaker

        class _Failing(Provider):
            name = "failing"
            def declare(self):
                return ProviderCapability(
                    capabilities=frozenset({Capability.QUOTE}),
                    markets=frozenset({Market.A}),
                    priority_hint=0.8,
                )
            def fetch_quote(self, sym):
                raise ProviderError("boom")

        gw = DataGateway(health=HealthTracker(warmup_count=1))
        gw.register_provider(_Failing())
        cb = get_breaker(
            "gw_failing_quote", failure_threshold=3, cooldown_seconds=10.0,
        )

        for _ in range(3):
            gw.invalidate_cache()
            gw.quote("sh510300")
        self.assertEqual(cb.state(), 'open')


if __name__ == '__main__':
    unittest.main()

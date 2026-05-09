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

import time
import unittest
from unittest.mock import patch


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
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=0.05)
        cb.on_failure()
        cb.on_failure()
        self.assertEqual(cb.state(), 'open')
        time.sleep(0.06)
        self.assertEqual(cb.state(), 'half_open')
        self.assertTrue(cb.allow())

    def test_half_open_failure_reopens(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=0.05)
        cb.on_failure()
        cb.on_failure()
        time.sleep(0.06)
        # half_open 状态下再失败应立即 re-open
        cb.on_failure()
        self.assertEqual(cb.state(), 'open')

    def test_half_open_success_closes(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker('x', failure_threshold=2, cooldown_seconds=0.05)
        cb.on_failure()
        cb.on_failure()
        time.sleep(0.06)
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


class TestDataLayerIntegration(unittest.TestCase):
    """data_layer._http_get / _fetch_minute_bars_akshare 短路验证。"""

    def setUp(self):
        from core.circuit_breaker import _REGISTRY, reset_all
        reset_all()
        _REGISTRY.clear()

    def test_http_get_short_circuits_when_open(self):
        from core import data_layer as dl
        from core.circuit_breaker import get_breaker
        cb = get_breaker('tencent', failure_threshold=2, cooldown_seconds=10.0)
        cb.on_failure()
        cb.on_failure()
        self.assertEqual(cb.state(), 'open')

        # 即使 mock 一个成功的 urlopen，open 状态也直接返回 None
        with patch('urllib.request.urlopen') as m:
            result = dl._http_get('https://qt.gtimg.cn/q=sh510300')
        self.assertIsNone(result)
        m.assert_not_called()

    def test_http_get_records_failures_into_breaker(self):
        from core import data_layer as dl
        from core.circuit_breaker import get_breaker
        cb = get_breaker('tencent', failure_threshold=3, cooldown_seconds=10.0)

        with patch('urllib.request.urlopen', side_effect=OSError('boom')):
            for _ in range(3):
                dl._http_get('https://qt.gtimg.cn/q=sh510300')

        self.assertEqual(cb.state(), 'open')


if __name__ == '__main__':
    unittest.main()

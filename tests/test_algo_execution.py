"""
tests/test_algo_execution.py — 算法订单执行框架单元测试

覆盖：
  - ImpactEstimator：基点估算、分解、最大量
  - VWAPExecutor：子单生成、股数守恒、分布归一化
  - TWAPExecutor：均匀分配、股数守恒、抖动
  - AlgoOrder.simulate()：模拟成交、结果汇总
  - OMS.submit_algo_order()：VWAP/TWAP 集成接口
"""

from __future__ import annotations

import unittest
import numpy as np
import pandas as pd
from datetime import datetime


# ---------------------------------------------------------------------------
# ImpactEstimator
# ---------------------------------------------------------------------------

class TestImpactEstimator(unittest.TestCase):

    def test_zero_qty_returns_zero(self):
        from core.execution.impact_estimator import ImpactEstimator
        self.assertEqual(ImpactEstimator.estimate(0, 1_000_000), 0.0)

    def test_zero_vol_returns_zero(self):
        from core.execution.impact_estimator import ImpactEstimator
        self.assertEqual(ImpactEstimator.estimate(10000, 0), 0.0)

    def test_estimate_positive(self):
        from core.execution.impact_estimator import ImpactEstimator
        bps = ImpactEstimator.estimate(50000, 2_000_000)
        self.assertGreater(bps, 0.0)

    def test_estimate_increases_with_qty(self):
        from core.execution.impact_estimator import ImpactEstimator
        small = ImpactEstimator.estimate(10000, 2_000_000)
        large = ImpactEstimator.estimate(100000, 2_000_000)
        self.assertGreater(large, small)

    def test_decompose_sums_to_estimate(self):
        from core.execution.impact_estimator import ImpactEstimator
        total = ImpactEstimator.estimate(50000, 2_000_000)
        perm, temp = ImpactEstimator.decompose(50000, 2_000_000)
        self.assertAlmostEqual(total, perm + temp, places=3)

    def test_decompose_both_nonnegative(self):
        from core.execution.impact_estimator import ImpactEstimator
        perm, temp = ImpactEstimator.decompose(50000, 2_000_000)
        self.assertGreaterEqual(perm, 0.0)
        self.assertGreaterEqual(temp, 0.0)

    def test_participation_cap_limits_impact(self):
        """超过参与率上限时，冲击应被截断（不随 qty 线性增长）。"""
        from core.execution.impact_estimator import ImpactEstimator
        bps_50pct = ImpactEstimator.estimate(1_000_000, 1_000_000, participation_cap=0.30)
        bps_80pct = ImpactEstimator.estimate(2_000_000, 1_000_000, participation_cap=0.30)
        self.assertAlmostEqual(bps_50pct, bps_80pct, places=2)

    def test_estimate_cost_returns_positive(self):
        from core.execution.impact_estimator import ImpactEstimator
        cost = ImpactEstimator.estimate_cost(10000, 2_000_000, price=15.0)
        self.assertGreater(cost, 0.0)

    def test_max_order_size_nonzero(self):
        from core.execution.impact_estimator import ImpactEstimator
        max_qty = ImpactEstimator.max_order_size(2_000_000, max_impact_bps=10.0)
        self.assertGreater(max_qty, 0)
        self.assertEqual(max_qty % 100, 0)  # 整手

    def test_max_order_size_tighter_limit_smaller(self):
        from core.execution.impact_estimator import ImpactEstimator
        large_limit = ImpactEstimator.max_order_size(2_000_000, max_impact_bps=20.0)
        small_limit = ImpactEstimator.max_order_size(2_000_000, max_impact_bps=5.0)
        self.assertGreaterEqual(large_limit, small_limit)


# ---------------------------------------------------------------------------
# VWAPExecutor
# ---------------------------------------------------------------------------

class TestVWAPExecutor(unittest.TestCase):

    def _make_executor(self, total_shares=10000, duration=60, interval=5):
        from core.execution.vwap_executor import VWAPExecutor
        return VWAPExecutor(
            symbol='000001.SZ',
            direction='BUY',
            total_shares=total_shares,
            duration_minutes=duration,
            reference_price=15.0,
            slice_interval=interval,
            start_time=datetime(2024, 1, 15, 9, 30),
        )

    def test_generate_slices_returns_list(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        self.assertIsInstance(slices, list)
        self.assertGreater(len(slices), 0)

    def test_slice_count_matches_duration(self):
        ex = self._make_executor(duration=60, interval=5)
        slices = ex.generate_slices()
        self.assertEqual(len(slices), 60 // 5)

    def test_total_shares_conserved(self):
        ex = self._make_executor(total_shares=10000)
        slices = ex.generate_slices()
        total = sum(s.target_shares for s in slices)
        # 允许±100股的舍入误差（整手取整导致）
        self.assertAlmostEqual(total, 10000, delta=100)

    def test_all_shares_multiple_of_100(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        for sl in slices:
            self.assertEqual(sl.target_shares % 100, 0)

    def test_slice_scheduled_times_ascending(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        times = [s.scheduled_time for s in slices]
        self.assertEqual(times, sorted(times))

    def test_slice_parent_order_id_set(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        for sl in slices:
            self.assertEqual(sl.parent_order_id, ex.order_id)

    def test_slice_symbol_correct(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        for sl in slices:
            self.assertEqual(sl.symbol, '000001.SZ')

    def test_custom_volume_profile_used(self):
        """自定义 profile 应影响子单分配（高权重时间片股数更多）。"""
        from core.execution.vwap_executor import VWAPExecutor
        n = 12  # 60min / 5min = 12 slices
        profile = [0.0] * n
        profile[0] = 1.0  # 全部集中在第一片
        ex = VWAPExecutor(
            symbol='000001.SZ',
            direction='BUY',
            total_shares=10000,
            duration_minutes=60,
            reference_price=15.0,
            slice_interval=5,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        slices = ex.generate_slices(volume_profile=profile)
        # 第一片应获得最多股数
        self.assertEqual(slices[0].target_shares, max(s.target_shares for s in slices))

    def test_sell_direction_preserved(self):
        from core.execution.vwap_executor import VWAPExecutor
        ex = VWAPExecutor(
            symbol='000001.SZ', direction='SELL',
            total_shares=5000, duration_minutes=30,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        slices = ex.generate_slices()
        for sl in slices:
            self.assertEqual(sl.direction, 'SELL')

    def test_simulate_returns_result(self):
        from core.execution.vwap_executor import VWAPExecutor
        from core.execution.algo_base import AlgoOrderResult
        ex = VWAPExecutor(
            symbol='000001.SZ', direction='BUY',
            total_shares=10000, duration_minutes=60,
            reference_price=15.0,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        n = 60 // 5
        prices = [15.0 + i * 0.01 for i in range(n)]
        volumes = [200000] * n
        result = ex.simulate(prices, volumes)
        self.assertIsInstance(result, AlgoOrderResult)
        self.assertGreater(result.filled_shares, 0)

    def test_simulate_avg_price_near_reference(self):
        """模拟成交均价应接近参考价格（小滑点）。"""
        from core.execution.vwap_executor import VWAPExecutor
        ex = VWAPExecutor(
            symbol='000001.SZ', direction='BUY',
            total_shares=10000, duration_minutes=60,
            reference_price=15.0,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        n = 12
        prices = [15.0] * n
        volumes = [1_000_000] * n
        result = ex.simulate(prices, volumes)
        self.assertAlmostEqual(result.avg_fill_price, 15.0, places=1)


# ---------------------------------------------------------------------------
# TWAPExecutor
# ---------------------------------------------------------------------------

class TestTWAPExecutor(unittest.TestCase):

    def _make_executor(self, total_shares=10000, duration=60, interval=5):
        from core.execution.twap_executor import TWAPExecutor
        return TWAPExecutor(
            symbol='600519.SH',
            direction='BUY',
            total_shares=total_shares,
            duration_minutes=duration,
            reference_price=1800.0,
            slice_interval=interval,
            start_time=datetime(2024, 1, 15, 9, 30),
        )

    def test_generate_slices_returns_list(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        self.assertIsInstance(slices, list)
        self.assertGreater(len(slices), 0)

    def test_slice_count_matches_duration(self):
        ex = self._make_executor(duration=60, interval=5)
        slices = ex.generate_slices()
        self.assertEqual(len(slices), 60 // 5)

    def test_total_shares_conserved(self):
        ex = self._make_executor(total_shares=10000)
        slices = ex.generate_slices()
        total = sum(s.target_shares for s in slices)
        self.assertAlmostEqual(total, 10000, delta=100)

    def test_shares_roughly_equal(self):
        """TWAP 每片股数应大致相等（允许整手取整误差）。"""
        ex = self._make_executor(total_shares=12000, duration=60, interval=10)
        slices = ex.generate_slices()
        shares = [s.target_shares for s in slices]
        # 最大与最小的差不超过一手
        self.assertLessEqual(max(shares) - min(shares[:-1]), 100)

    def test_all_shares_multiple_of_100(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        for sl in slices:
            self.assertEqual(sl.target_shares % 100, 0)

    def test_slice_times_ascending(self):
        ex = self._make_executor()
        slices = ex.generate_slices()
        times = [s.scheduled_time for s in slices]
        self.assertEqual(times, sorted(times))

    def test_slice_interval_correct(self):
        from core.execution.twap_executor import TWAPExecutor
        ex = TWAPExecutor(
            symbol='000001.SZ', direction='BUY',
            total_shares=5000, duration_minutes=30,
            slice_interval=10,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        slices = ex.generate_slices()
        for i in range(1, len(slices)):
            delta = (slices[i].scheduled_time - slices[i-1].scheduled_time).total_seconds()
            self.assertAlmostEqual(delta, 600, delta=60)  # 10分钟 ± 1分钟

    def test_jitter_changes_times(self):
        """jitter_pct > 0 时，相邻子单时间不完全等间隔。"""
        from core.execution.twap_executor import TWAPExecutor
        import random
        random.seed(99)
        ex = TWAPExecutor(
            symbol='000001.SZ', direction='BUY',
            total_shares=10000, duration_minutes=60,
            slice_interval=5, jitter_pct=0.2,
            start_time=datetime(2024, 1, 15, 9, 30),
        )
        slices = ex.generate_slices()
        deltas = [
            (slices[i].scheduled_time - slices[i-1].scheduled_time).total_seconds()
            for i in range(1, len(slices))
        ]
        # 至少有一个间隔不等于标准间隔（5分钟=300秒）
        self.assertFalse(all(abs(d - 300) < 1 for d in deltas))

    def test_slice_count_property(self):
        from core.execution.twap_executor import TWAPExecutor
        ex = TWAPExecutor(
            symbol='000001.SZ', direction='BUY',
            total_shares=5000, duration_minutes=60, slice_interval=5,
        )
        self.assertEqual(ex.slice_count, 12)

    def test_simulate_returns_result(self):
        from core.execution.algo_base import AlgoOrderResult
        ex = self._make_executor()
        n = 60 // 5
        prices = [1800.0] * n
        volumes = [500_000] * n
        result = ex.simulate(prices, volumes)
        self.assertIsInstance(result, AlgoOrderResult)
        self.assertGreater(result.filled_shares, 0)

    def test_simulate_fill_rate(self):
        """充分流动性下，成交率应接近 100%。"""
        ex = self._make_executor(total_shares=1000)
        n = 12
        prices = [1800.0] * n
        volumes = [10_000_000] * n  # 极高流动性
        result = ex.simulate(prices, volumes)
        self.assertGreater(result.fill_rate, 0.8)


# ---------------------------------------------------------------------------
# OMS.submit_algo_order 集成测试
# ---------------------------------------------------------------------------

class TestOMSAlgoOrder(unittest.TestCase):

    def setUp(self):
        # 重置 OMS 单例（使用新实例）
        from core.oms import OMS
        OMS._instance = None

    def test_submit_vwap_returns_result(self):
        from core.oms import OMS
        from core.execution.algo_base import AlgoOrderResult
        oms = OMS()
        result = oms.submit_algo_order(
            algo='VWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=10000,
            duration_minutes=60,
            reference_price=15.0,
        )
        self.assertIsInstance(result, AlgoOrderResult)

    def test_submit_twap_returns_result(self):
        from core.oms import OMS
        from core.execution.algo_base import AlgoOrderResult
        oms = OMS()
        result = oms.submit_algo_order(
            algo='TWAP',
            symbol='000001.SZ',
            direction='SELL',
            total_shares=5000,
            duration_minutes=30,
            reference_price=15.5,
        )
        self.assertIsInstance(result, AlgoOrderResult)

    def test_submit_unknown_algo_raises(self):
        from core.oms import OMS
        oms = OMS()
        with self.assertRaises(ValueError):
            oms.submit_algo_order(
                algo='POVA',
                symbol='000001.SZ',
                direction='BUY',
                total_shares=1000,
            )

    def test_vwap_result_filled_shares_positive(self):
        from core.oms import OMS
        oms = OMS()
        result = oms.submit_algo_order(
            algo='VWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=10000,
            reference_price=15.0,
        )
        self.assertGreater(result.filled_shares, 0)

    def test_result_avg_price_near_reference(self):
        from core.oms import OMS
        oms = OMS()
        result = oms.submit_algo_order(
            algo='TWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=5000,
            reference_price=20.0,
        )
        # 成交均价应接近参考价（小随机噪声）
        self.assertAlmostEqual(result.avg_fill_price, 20.0, delta=0.5)

    def test_result_has_market_impact(self):
        from core.oms import OMS
        oms = OMS()
        result = oms.submit_algo_order(
            algo='VWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=100000,  # 大单，冲击明显
            reference_price=15.0,
        )
        self.assertGreater(result.market_impact_bps, 0.0)

    def test_result_fill_rate_equals_one(self):
        """模拟执行（无实际流动性限制）时，成交率应为 1.0。"""
        from core.oms import OMS
        oms = OMS()
        result = oms.submit_algo_order(
            algo='TWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=10000,
            reference_price=15.0,
        )
        self.assertAlmostEqual(result.fill_rate, 1.0, places=2)

    def test_result_n_slices_correct(self):
        from core.oms import OMS
        oms = OMS()
        result = oms.submit_algo_order(
            algo='TWAP',
            symbol='000001.SZ',
            direction='BUY',
            total_shares=5000,
            duration_minutes=60,
            slice_interval=5,
        )
        self.assertEqual(result.n_slices, 60 // 5)


if __name__ == '__main__':
    unittest.main()

"""
tests/test_fill_simulator.py — P2-14 共享撮合工具测试

覆盖：
  - simulate_fill_price: 市价单滑点 / 限价单原价 / 0 价格降级
  - slippage_bps_actual: 正/负滑点计算
  - is_limit_breach: 涨跌停判定
  - compute_commission: 万 3 + 5 元最低
  - fill_summary 端到端
"""

from __future__ import annotations

import random
import unittest


class TestSimulateFillPrice(unittest.TestCase):

    def test_limit_order_returns_ref_price(self):
        from core.brokers.fill_simulator import simulate_fill_price
        p = simulate_fill_price(100.0, 'BUY', price_type='limit')
        self.assertEqual(p, 100.0)

    def test_market_order_within_bps_band(self):
        from core.brokers.fill_simulator import simulate_fill_price
        rng = random.Random(42)
        p = simulate_fill_price(100.0, 'BUY', price_type='market',
                                slippage_bps=15.0, rng=rng)
        # 100 * (1 ± 0.0015) = [99.85, 100.15]
        self.assertGreaterEqual(p, 99.85)
        self.assertLessEqual(p, 100.15)

    def test_zero_price_returns_zero(self):
        from core.brokers.fill_simulator import simulate_fill_price
        self.assertEqual(simulate_fill_price(0.0, 'BUY'), 0.0)


class TestSlippageBpsActual(unittest.TestCase):

    def test_positive_slip_for_high_buy(self):
        from core.brokers.fill_simulator import slippage_bps_actual
        # 信号 100，成交 100.5 → +50 bps
        self.assertAlmostEqual(slippage_bps_actual(100.5, 100.0), 50.0)

    def test_negative_slip(self):
        from core.brokers.fill_simulator import slippage_bps_actual
        self.assertAlmostEqual(slippage_bps_actual(99.7, 100.0), -30.0)

    def test_zero_signal_price(self):
        from core.brokers.fill_simulator import slippage_bps_actual
        self.assertEqual(slippage_bps_actual(100.0, 0.0), 0.0)


class TestLimitBreach(unittest.TestCase):

    def test_buy_at_limit_up(self):
        from core.brokers.fill_simulator import is_limit_breach
        self.assertTrue(is_limit_breach('BUY', 11.00, 10.0))   # +10%

    def test_buy_below_limit_up(self):
        from core.brokers.fill_simulator import is_limit_breach
        self.assertFalse(is_limit_breach('BUY', 10.99, 10.0))

    def test_sell_at_limit_down(self):
        from core.brokers.fill_simulator import is_limit_breach
        self.assertTrue(is_limit_breach('SELL', 9.00, 10.0))   # -10%

    def test_no_prev_close(self):
        from core.brokers.fill_simulator import is_limit_breach
        self.assertFalse(is_limit_breach('BUY', 11.0, 0.0))


class TestCommission(unittest.TestCase):

    def test_uses_min_when_amount_below_5(self):
        from core.brokers.fill_simulator import compute_commission
        # 100 * 100 * 0.0003 = 3 < 5 → 5
        self.assertEqual(compute_commission(100.0, 100), 5.0)

    def test_proportional_when_above_min(self):
        from core.brokers.fill_simulator import compute_commission
        # 100 * 1000 * 0.0003 = 30 → 30
        self.assertAlmostEqual(compute_commission(100.0, 1000), 30.0)

    def test_zero_inputs(self):
        from core.brokers.fill_simulator import compute_commission
        self.assertEqual(compute_commission(0.0, 100), 0.0)
        self.assertEqual(compute_commission(100.0, 0), 0.0)


class TestFillSummary(unittest.TestCase):

    def test_returns_three_tuple(self):
        from core.brokers.fill_simulator import fill_summary
        rng = random.Random(0)
        fp, comm, slip = fill_summary(
            ref_price=100.0,
            direction='BUY',
            shares=1000,
            price_type='market',
            signal_price=100.0,
            slippage_bps=10.0,
            rng=rng,
        )
        self.assertGreaterEqual(fp, 99.9)
        self.assertLessEqual(fp, 100.1)
        # 100 * 1000 * 0.0003 = 30 ≥ 5
        self.assertGreaterEqual(comm, 25.0)
        self.assertGreaterEqual(slip, -10.0)
        self.assertLessEqual(slip, 10.0)


if __name__ == '__main__':
    unittest.main()

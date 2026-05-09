"""
test_oms_kelly_drawdown.py — P0-4 Kelly + 回撤折扣测试

验证：
  1. Kelly 在峰值（dd=0）时 = full kelly_fraction
  2. 回撤接近 max_dd_limit 时 discount → 0
  3. dd >= max_dd_limit 时仓位归零
  4. Kelly 上限被 max_position_pct 截断（不再出现 32x 杠杆）
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass


@dataclass
class _MockSignal:
    symbol: str = 'TEST.SH'
    direction: str = 'BUY'
    price: float = 10.0
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class TestOmsKellyDrawdown(unittest.TestCase):

    def setUp(self):
        from core.oms import OMS, EventDrivenPaperBroker
        # 重置单例避免测试间污染
        OMS._instance = None
        # 使用一个不会调网络的 broker 占位
        self.oms = OMS(broker=_NullBroker())

    def tearDown(self):
        from core.oms import OMS
        OMS._instance = None

    def test_kelly_capped_at_max_position_pct(self):
        """Kelly 原始值 32.5 应被 max_position_pct 截断到 0.25。"""
        # 注入 equity = 100k，price = 10
        self.oms._fetch_equity = lambda: 100_000.0
        self.oms._peak_equity = 0.0

        sig = _MockSignal(price=10.0)
        shares = self.oms._kelly_shares(sig)

        # 上限：100k * 0.25 / 10 = 2500 股，整手 2500
        # 旧实现会返回 100k * 16.25 / 10 = 162500（无法忍受）
        self.assertLessEqual(shares, 2500)
        # 不应为 0（处于峰值，无回撤折扣）
        self.assertGreater(shares, 0)

    def test_kelly_zero_at_max_drawdown(self):
        """当前回撤 = max_dd_limit 时仓位为 0。"""
        self.oms._peak_equity = 100_000.0      # 峰值
        # 假设 max_drawdown = 0.15，equity 跌到 85k 即触发上限
        self.oms._fetch_equity = lambda: 85_000.0

        sig = _MockSignal(price=10.0)
        shares = self.oms._kelly_shares(sig)

        self.assertEqual(shares, 0)

    def test_kelly_partial_discount_in_drawdown(self):
        """处于一半回撤时，仓位应介于 0 和峰值仓位之间。"""
        self.oms._peak_equity = 100_000.0
        # 7.5% 回撤 = max_dd 0.15 的一半
        self.oms._fetch_equity = lambda: 92_500.0

        sig = _MockSignal(price=10.0)
        shares_dd = self.oms._kelly_shares(sig)

        # 同条件下峰值仓位
        self.oms._peak_equity = 0.0
        self.oms._fetch_equity = lambda: 92_500.0
        shares_peak = self.oms._kelly_shares(sig)

        # 折扣仓位 < 峰值仓位
        self.assertLess(shares_dd, shares_peak)
        # 但 > 0
        self.assertGreater(shares_dd, 0)

    def test_kelly_zero_when_equity_zero(self):
        """权益 0 时仓位 0（避免除零）。"""
        self.oms._fetch_equity = lambda: 0.0
        sig = _MockSignal(price=10.0)
        self.assertEqual(self.oms._kelly_shares(sig), 0)

    def test_kelly_zero_when_price_zero(self):
        """signal 价格 0 时仓位 0。"""
        self.oms._fetch_equity = lambda: 100_000.0
        sig = _MockSignal(price=0.0)
        self.assertEqual(self.oms._kelly_shares(sig), 0)

    def test_peak_equity_updates_on_new_high(self):
        """新高时 _peak_equity 应更新，下一次回撤计算使用新峰值。"""
        self.oms._peak_equity = 100_000.0
        self.oms._fetch_equity = lambda: 110_000.0  # 创新高

        sig = _MockSignal(price=10.0)
        self.oms._kelly_shares(sig)
        self.assertEqual(self.oms._peak_equity, 110_000.0)

    def test_drawdown_discount_formula(self):
        """直接测 _drawdown_discount 的折扣曲线。"""
        self.oms._peak_equity = 100_000.0
        # equity = peak → discount = 1.0
        self.assertAlmostEqual(self.oms._drawdown_discount(100_000.0, 0.15), 1.0)
        # equity = peak * (1 - max_dd) → discount = 0
        self.assertAlmostEqual(
            self.oms._drawdown_discount(85_000.0, 0.15), 0.0, places=4
        )
        # equity = peak * (1 - max_dd/2) → discount = 0.5
        self.assertAlmostEqual(
            self.oms._drawdown_discount(92_500.0, 0.15), 0.5, places=4
        )
        # equity 低于阈值 → discount = 0（不会变负）
        self.assertEqual(
            self.oms._drawdown_discount(50_000.0, 0.15), 0.0
        )


class _NullBroker:
    """测试用空 Broker，避免 EventDrivenPaperBroker 加载持仓时的网络调用。"""

    def send(self, order):
        from core.oms import Fill
        return Fill(order_id=order.order_id, symbol=order.symbol,
                    direction=order.direction, shares=0, price=0)

    def cancel(self, order_id):
        return True

    def quote(self, symbol):
        return {'last': 10.0}

    def get_positions(self):
        return []


if __name__ == '__main__':
    unittest.main()

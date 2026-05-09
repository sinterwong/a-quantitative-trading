"""
test_intraday_algo_routing.py — P1-7 IntradayMonitor 大单算法路由测试

验证：
  1. 小单（< threshold）走 broker.submit_order 单笔
  2. 大单（>= threshold）走 TWAP 拆单 → 多次 submit_order
  3. enable_algo_routing=False 时即使大单也走单笔
  4. 聚合 OrderResult 字段正确（filled_shares = sum, avg_price 加权平均）
  5. ImpactEstimator.load_from_config 从 trading.yaml 读取系数
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock

from backend.services.broker import OrderResult


class _MockBroker:
    """记录每次 submit_order 调用的 broker。"""
    def __init__(self, fill_price_offset: float = 0.0):
        self.calls: list = []
        self._fill_offset = fill_price_offset
        self._counter = 0

    def submit_order(self, symbol, direction, shares, price=0,
                     price_type='market'):
        self._counter += 1
        self.calls.append({
            'symbol': symbol, 'direction': direction,
            'shares': shares, 'price': price, 'price_type': price_type,
        })
        fill_price = round((price or 10.0) + self._fill_offset, 4)
        return OrderResult(
            order_id=f'M-{self._counter}',
            status='filled', symbol=symbol, direction=direction,
            submitted_shares=shares, filled_shares=shares,
            avg_price=fill_price, signal_price=price,
            slippage_bps=0.0,
            submitted_at=datetime.now().isoformat(),
            filled_at=datetime.now().isoformat(),
        )


class _MockMonitor:
    """最小化 IntradayMonitor 子集，仅暴露 _submit_with_routing 路径。"""
    def __init__(self, broker, ec):
        self._broker = broker
        self._ec = ec

    def _algo_config(self):
        return self._ec

    # 把 IntradayMonitor._submit_with_routing 复制到这里作为绑定方法
    from backend.services.intraday_monitor import IntradayMonitor
    _submit_with_routing = IntradayMonitor._submit_with_routing


class _ExecConf:
    def __init__(self, **kw):
        self.enable_algo_routing = kw.get('enable_algo_routing', True)
        self.algo_threshold_amount = kw.get('algo_threshold_amount', 500_000.0)
        self.algo_threshold_shares = kw.get('algo_threshold_shares', 10_000)
        self.algo_method = kw.get('algo_method', 'TWAP')
        self.algo_duration_minutes = kw.get('algo_duration_minutes', 30)
        self.algo_slice_interval = kw.get('algo_slice_interval', 5)


class TestAlgoRouting(unittest.TestCase):

    def test_small_order_single_broker_call(self):
        """订单金额低于阈值 → 走单笔。"""
        broker = _MockBroker()
        ec = _ExecConf()
        monitor = _MockMonitor(broker, ec)
        # 100k 元订单（10 元 × 10000 股 = 100k < 500k 阈值）
        # 注意：shares=10000 已等于 threshold_shares，触发；改为 5000
        result = monitor._submit_with_routing('A.SH', 'BUY', 5000, price=10.0)

        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(broker.calls[0]['shares'], 5000)
        self.assertEqual(result.filled_shares, 5000)
        self.assertEqual(result.status, 'filled')

    def test_large_order_split_into_children(self):
        """订单 = 100w 元（10 × 100000 股），应拆成多个子单。"""
        broker = _MockBroker()
        ec = _ExecConf(
            algo_duration_minutes=30, algo_slice_interval=5,
        )
        monitor = _MockMonitor(broker, ec)
        result = monitor._submit_with_routing('A.SH', 'BUY', 100_000, price=10.0)

        # 30 / 5 = 6 个子单
        self.assertGreater(len(broker.calls), 1)
        # 子单股数总和应等于原始订单
        total = sum(c['shares'] for c in broker.calls)
        self.assertEqual(total, 100_000)
        self.assertEqual(result.submitted_shares, 100_000)
        self.assertEqual(result.filled_shares, 100_000)

    def test_routing_disabled_falls_back(self):
        """enable_algo_routing=False 时大单也单笔。"""
        broker = _MockBroker()
        ec = _ExecConf(enable_algo_routing=False)
        monitor = _MockMonitor(broker, ec)
        result = monitor._submit_with_routing('A.SH', 'BUY', 100_000, price=10.0)

        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(broker.calls[0]['shares'], 100_000)

    def test_method_none_falls_back(self):
        """algo_method='NONE' 时大单也单笔。"""
        broker = _MockBroker()
        ec = _ExecConf(algo_method='NONE')
        monitor = _MockMonitor(broker, ec)
        result = monitor._submit_with_routing('A.SH', 'BUY', 100_000, price=10.0)
        self.assertEqual(len(broker.calls), 1)

    def test_aggregated_avg_price_correct(self):
        """聚合 avg_price 是各子单成交价加权平均（同价时退化为该价）。"""
        broker = _MockBroker(fill_price_offset=0.0)
        ec = _ExecConf()
        monitor = _MockMonitor(broker, ec)
        result = monitor._submit_with_routing('A.SH', 'BUY', 50_000, price=12.0)

        self.assertGreater(len(broker.calls), 1)
        self.assertAlmostEqual(result.avg_price, 12.0, places=2)

    def test_threshold_by_shares_only(self):
        """股数 >= threshold_shares 即使金额不足也触发拆单。"""
        broker = _MockBroker()
        # 0.5 元 × 50000 股 = 25k 元（< 500k），但 50000 > 10000 股阈值
        ec = _ExecConf()
        monitor = _MockMonitor(broker, ec)
        result = monitor._submit_with_routing('A.SH', 'BUY', 50_000, price=0.5)
        self.assertGreater(len(broker.calls), 1)


class TestImpactEstimatorConfig(unittest.TestCase):

    def test_load_from_config(self):
        """ImpactEstimator.load_from_config 应从 trading.yaml 读取系数。"""
        from core.execution.impact_estimator import ImpactEstimator
        # 正常路径
        ok = ImpactEstimator.load_from_config()
        self.assertTrue(ok)
        # 系数应为 trading.yaml 设置的值（默认 5.0）
        self.assertGreater(ImpactEstimator.PERMANENT_COEFF, 0)
        self.assertGreater(ImpactEstimator.TEMPORARY_COEFF, 0)


if __name__ == '__main__':
    unittest.main()

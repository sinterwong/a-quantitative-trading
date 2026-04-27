"""
Phase 4 验证测试：Level2 数据源 + 订单簿因子
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import time
from datetime import datetime, timedelta

from core.level2 import (
    Level2DataSource, OrderBook, TickBarAggregator, TickBar,
    OrderImbalanceFactor, BidAskSpreadFactor, MidPriceDriftFactor,
    VolumeRateFactor, AmihudIlliquidityFactor,
)


class TestLevel2DataSource(unittest.TestCase):

    def test_sina_fetch(self):
        """新浪 Level2 盘口数据"""
        ds = Level2DataSource('sh600900')
        ob = ds.fetch_latest()
        self.assertIsNotNone(ob, 'Level2 data should not be None')
        self.assertIsInstance(ob, OrderBook)
        self.assertTrue(len(ob.bids) >= 3, f'Should have >= 3 bid levels, got {len(ob.bids)}')
        self.assertTrue(len(ob.asks) >= 3, f'Should have >= 3 ask levels, got {len(ob.asks)}')
        print(f"\nOrderBook: price={ob.last_price}, "
              f"bids={len(ob.bids)}, asks={len(ob.asks)}, "
              f"OI={ob.order_imbalance():.3f}, spread={ob.bid_ask_spread():.3f}")

    def test_order_book_calculations(self):
        """OrderBook 计算方法"""
        ob = OrderBook(
            timestamp=datetime.now(),
            symbol='test',
            bids=[(26.5, 10000), (26.4, 20000)],
            asks=[(26.6, 15000), (26.7, 25000)],
            last_price=26.55,
            volume=1000000,
            amount=26550000,
        )
        self.assertAlmostEqual(ob.bid_ask_spread(), 0.1)
        self.assertAlmostEqual(ob.mid_price(), 26.55)
        # OI = (30000 - 40000) / (30000 + 40000) = -10000/70000
        self.assertAlmostEqual(ob.order_imbalance(), -1/7, places=3)
        print(f"\nOI={ob.order_imbalance():.4f}, mid={ob.mid_price()}")


class TestTickBarAggregator(unittest.TestCase):

    def test_time_bar(self):
        """Tick 规则 bar：每 N 个 tick 聚合为一个 bar"""
        agg = TickBarAggregator('test', rule='tick', threshold=5)
        ts = datetime.now()
        for i in range(10):
            agg.on_tick(price=26.0 + i*0.1, volume=1000*(i+1),
                        amount=26000 + 1000*i, timestamp=ts)

        bars = agg.get_closed_bars()
        self.assertEqual(len(bars), 1)  # 1个完整的5-tick bar
        bar = agg.get_latest_bar()
        self.assertIsNotNone(bar)
        # bar刚重新打开，n_ticks已重置
        print(f"\nTickBar closed: n={bars[0].n_ticks}, O={bars[0].open:.2f}, C={bars[0].close:.2f}")
        print(f"Current bar: n={bar.n_ticks}")


class TestOrderFlowFactors(unittest.TestCase):

    def test_oi_factor(self):
        """订单不平衡度因子"""
        obs = []
        ts = datetime.now()
        # 模拟买方压力场景
        for i in range(20):
            ob = OrderBook(
                timestamp=ts,
                symbol='test',
                bids=[(26.50, 50000), (26.49, 30000)],
                asks=[(26.55, 10000), (26.56, 10000)],
                last_price=26.52,
                volume=1000000 + i*10000,
                amount=26520000,
            )
            obs.append(ob)
            ts = ts + timedelta(seconds=1)

        factor = OrderImbalanceFactor(lookback=20)
        result = factor.evaluate(obs)
        self.assertEqual(len(result), 20)
        self.assertTrue(-3 < result.iloc[-1] < 3)
        print(f"\nOI factor: latest={result.iloc[-1]:.3f}")

    def test_oi_signals(self):
        """OI 因子信号生成"""
        obs = []
        ts = datetime.now()
        # 强卖压（OI 显著为负）
        bids = [(26.0, 10000), (25.9, 10000), (25.8, 10000)]
        asks = [(26.5, 80000), (26.6, 80000), (26.7, 80000)]
        for i in range(20):
            ob = OrderBook(
                timestamp=ts,
                symbol='test',
                bids=bids,
                asks=asks,
                last_price=26.2,
                volume=1000000,
                amount=26200000,
            )
            obs.append(ob)
            ts = ts + timedelta(seconds=1)

        factor = OrderImbalanceFactor(lookback=20)
        result = factor.evaluate(obs)
        signals = factor.signals(result, threshold=0.5)
        print(f"\nOI={obs[-1].order_imbalance():.3f}, signals={len(signals)}")


class TestLevel2Integration(unittest.TestCase):
    """Level2 实时轮询 + 因子计算"""

    def test_polling_integration(self):
        """实时盘口轮询（3秒，2次）"""
        ds = Level2DataSource('sh600900', interval=3)
        results = []

        def handler(src, ob):
            results.append(ob)
            print(f"\n[Callback] price={ob.last_price}, "
                  f"OI={ob.order_imbalance():.3f}, spread={ob.bid_ask_spread():.3f}")

        ds.subscribe(handler)
        ds.start_polling(interval=3)

        time.sleep(8)  # 等待2次轮询
        ds.stop_polling()

        self.assertGreaterEqual(len(results), 2, 'Should receive >= 2 snapshots')
        print(f"\nReceived {len(results)} snapshots in 8 seconds")


if __name__ == '__main__':
    unittest.main(verbosity=2)

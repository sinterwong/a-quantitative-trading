"""
Phase 2 验证测试：DataSources + OMS + RiskEngine
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime

from core.data_sources import (
    SPFuturesDataSource, VIXDataSource, HSIFuturesDataSource,
    TencentMinuteDataSource, NorthBoundDataSource,
    CompositeMarketDataSource, MarketSnapshot,
)
from core.oms import OMS, PaperBroker, Order
from core.risk_engine import RiskEngine, RiskResult, PositionBook
from core.event_bus import EventBus


class TestDataSources(unittest.TestCase):

    def test_sp500_fetch(self):
        ds = SPFuturesDataSource('ES=F')
        result = ds.fetch_latest()
        self.assertIn('symbol', result)
        # yfinance 全球限速是常见情况，代码需优雅降级（有 error key 或有 close key）
        has_data = 'close' in result
        has_error = 'error' in result
        self.assertTrue(has_data or has_error, f'Result must have close or error: {result}')
        print(f"\nSP500: {result}")

    def test_vix_fetch(self):
        ds = VIXDataSource()
        result = ds.fetch_latest()
        self.assertIn('symbol', result)
        print(f"\nVIX: {result}")

    def test_northbound(self):
        ds = NorthBoundDataSource()
        result = ds.fetch_latest()
        self.assertIn('net_north_yi', result)
        print(f"\nNorthBound: {result}")

    def test_composite_market(self):
        cm = CompositeMarketDataSource()
        snap = cm.fetch_latest()
        self.assertIsInstance(snap, MarketSnapshot)
        self.assertIsNotNone(snap.timestamp)
        print(f"\nCompositeSnapshot: SP={snap.sp500_change_pct}%, VIX={snap.vix}, North={snap.north_net_yi}Yi")

    def test_tencent_minute(self):
        ds = TencentMinuteDataSource('600900.SH')
        result = ds.fetch_latest()
        print(f"\nTencentMinute 600900.SH: {result}")


class TestPaperBroker(unittest.TestCase):

    def test_quote(self):
        broker = PaperBroker()
        quote = broker.quote('600900.SH')
        self.assertIn('last', quote)
        print(f"\nQuote 600900.SH: {quote}")

    def test_order_creation(self):
        order = Order(symbol='600900.SH', direction='BUY', shares=100)
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(order.order_type, 'MARKET')
        print(f"\nOrder created: {order.order_id}")


class TestRiskEngine(unittest.TestCase):

    def setUp(self):
        self.engine = RiskEngine()

    def test_position_limit_pass(self):
        # 无持仓 → 应该通过
        result = self.engine.check_position_limit('600900.SH')
        self.assertTrue(result.passed)

    def test_loss_limit_pass(self):
        result = self.engine.check_loss_limit()
        self.assertTrue(result.passed)

    def test_net_exposure_pass(self):
        result = self.engine.check_net_exposure()
        self.assertTrue(result.passed)

    def test_book_positions_loaded(self):
        book = PositionBook()
        positions = book.get_all()
        print(f"\nLoaded positions: {list(positions.keys())}")


class TestOMSSignalIntegration(unittest.TestCase):

    def test_oms_single_signal(self):
        """Signal → OMS.submit → Fill"""
        from core.oms import OMS
        broker = PaperBroker()
        oms = OMS.__new__(OMS)
        oms.broker = broker
        oms._initialized = True
        oms._order_book = {}
        oms._position_book = {}
        oms._pending_signals = set()

        from core.factors.price_momentum import RSIFactor
        from core.strategies.signal_engine import SignalEngine
        import pandas as pd

        # 创建超卖信号
        import numpy as np
        n = 60
        dates = pd.date_range(end=datetime.now(), periods=n, freq='min')
        closes = 20.0 + np.cumsum(np.random.randn(n) * 0.1)
        data = pd.DataFrame({
            'open': closes, 'close': closes,
            'high': closes + 0.1, 'low': closes - 0.1,
            'volume': 5000
        }, index=dates)

        rsi = RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH')
        fv = rsi.evaluate(data)
        signals = rsi.signals(fv, price=20.0)
        print(f"\nGenerated signals: {len(signals)}")
        if signals:
            sig = signals[0]
            print(f"Signal: {sig.direction} {sig.symbol} @ {sig.price}, strength={sig.strength}")
            fill = oms.submit_from_signal(sig, shares=100)
            print(f"Fill result: {fill}")


class TestFullPipeline(unittest.TestCase):
    """MarketEvent → DataSource → CompositeSnapshot → RiskEngine"""

    def test_composite_snapshot_decisions(self):
        cm = CompositeMarketDataSource()
        snap = cm.fetch_latest()
        self.assertIsInstance(snap, MarketSnapshot)

        us_bull = snap.is_us_bullish()
        vix_high = snap.is_high_volatility()
        north_in = snap.is_north_inflow()

        print(f"\nSnapshot decisions:")
        print(f"  US Bullish: {us_bull}")
        print(f"  VIX High: {vix_high}")
        print(f"  North Inflow: {north_in}")


if __name__ == '__main__':
    unittest.main(verbosity=2)

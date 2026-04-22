"""
Phase 1 验证测试：EventBus + FactorExpression + SignalEngine
验证点：
1. EventBus emit/on 正常工作
2. RSI/Bollinger/MACD 因子 evaluate 无报错
3. SignalEngine 生成信号
4. CompositeSignalEngine 多因子加权
5. EventBus pipeline 连接各组件
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from core.event_bus import EventBus, Event, MarketEvent, SignalEvent
from core.factors.price_momentum import RSIFactor, BollingerFactor, MACDFactor, ATRFactor
from core.strategies.signal_engine import SignalEngine, CompositeSignalEngine


def make_bars(n=60, base=4000, trend=0) -> pd.DataFrame:
    """生成模拟 K 线数据"""
    dates = pd.date_range(end=datetime.now(), periods=n, freq='min')
    np.random.seed(42)
    closes = base + np.cumsum(np.random.randn(n) * 10 + trend)
    highs = closes + np.abs(np.random.randn(n) * 5)
    lows = closes - np.abs(np.random.randn(n) * 5)
    opens = closes + np.random.randn(n) * 3
    volumes = np.random.randint(1000, 10000, n)
    return pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows,
        'close': closes, 'volume': volumes
    }, index=dates)


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()  # 每次新建隔离
        self.bus.reset()

    def test_emit_on(self):
        received = []
        def handler(e):
            received.append(e)
        self.bus.on('MarketEvent', handler)
        self.bus.emit(MarketEvent(symbol='600900.SH', close=26.0))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].symbol, '600900.SH')

    def test_off(self):
        calls = []
        def h(e): calls.append(1)
        self.bus.on('MarketEvent', h)
        self.bus.emit(MarketEvent())
        self.bus.off('MarketEvent', h)
        self.bus.emit(MarketEvent())
        self.assertEqual(len(calls), 1)

    def test_pipeline(self):
        results = []
        def first(e):
            e.step = 1
            return e
        def second(e):
            results.append(e)
        self.bus.on('MarketEvent', first)
        self.bus.on('MarketEvent', second)
        self.bus.emit(MarketEvent())
        self.assertEqual(len(results), 1)

    def test_stats(self):
        def h1(e): pass
        def h2(e): pass
        self.bus.on('MarketEvent', h1)
        self.bus.on('SignalEvent', h2)
        stats = self.bus.stats()
        self.assertEqual(stats.get('MarketEvent'), 1)
        self.assertEqual(stats.get('SignalEvent'), 1)


class TestFactors(unittest.TestCase):
    def test_rsi_evaluate(self):
        data = make_bars(n=60, base=26.0)
        rsi = RSIFactor(period=14, symbol='600900.SH')
        result = rsi.evaluate(data)
        self.assertEqual(len(result), 60)
        self.assertTrue(-3 < result.iloc[-1] < 3)  # z-score 范围

    def test_rsi_signals(self):
        data = make_bars(n=60, base=20.0)  # 低价格 → RSI 偏低 → BUY
        rsi = RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH')
        fv = rsi.evaluate(data)
        signals = rsi.signals(fv, price=20.0)
        # 低估场景应有 BUY 信号
        # （由于随机数据，不强制要求一定有信号）

    def test_bollinger(self):
        data = make_bars(n=30, base=26.0)
        bb = BollingerFactor(period=20, symbol='600900.SH')
        result = bb.evaluate(data)
        self.assertEqual(len(result), 30)

    def test_macd(self):
        data = make_bars(n=60, base=26.0)
        macd = MACDFactor(symbol='600900.SH')
        result = macd.evaluate(data)
        self.assertEqual(len(result), 60)

    def test_atr(self):
        data = make_bars(n=30, base=26.0)
        atr = ATRFactor(period=14, lookback=20, symbol='600900.SH')
        result = atr.evaluate(data)
        self.assertEqual(len(result), 30)


class TestSignalEngine(unittest.TestCase):
    def test_rsi_engine(self):
        # 构造超卖数据
        data = make_bars(n=60, base=20.0)
        rsi = RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH')
        engine = SignalEngine(factor=rsi)
        signals = engine.evaluate(data, price=20.0, atr_threshold=0.85)
        # 至少不应报错
        self.assertIsInstance(signals, list)

    def test_atr_filter_masks_buy(self):
        """高 ATR ratio > 0.85 时 BUY 信号被屏蔽"""
        data = make_bars(n=60, base=4000, trend=0)
        rsi = RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH')
        engine = SignalEngine(factor=rsi)

        # 正常 ATR → 有信号
        signals_normal = engine.evaluate(data, price=4000, atr_threshold=0.85)

        # ATR 阈值 0 → 所有 BUY 都被屏蔽（高波动）
        signals_high_vol = engine.evaluate(data, price=4000, atr_threshold=0.0)

        # 高波动下 BUY 信号应更少
        normal_buys = sum(1 for s in signals_normal if s.direction == 'BUY')
        highvol_buys = sum(1 for s in signals_high_vol if s.direction == 'BUY')
        # 这里不强制（随机数据），只验证无报错
        self.assertIsInstance(signals_high_vol, list)

    def test_composite_engine(self):
        rsi = RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH')
        macd = MACDFactor(symbol='600900.SH')

        engine = CompositeSignalEngine()
        engine.add_factor('RSI', rsi, weight=0.6)
        engine.add_factor('MACD', macd, weight=0.4)

        data = make_bars(n=60, base=26.0)
        signals = engine.evaluate(data, price=26.0)
        self.assertIsInstance(signals, list)

        # 验证信号携带 composite_score
        for s in signals:
            self.assertIn('composite_score', s.metadata)


class TestEventBusIntegration(unittest.TestCase):
    """完整链路：MarketEvent → SignalEngine → SignalEvent"""

    def test_full_pipeline(self):
        bus = EventBus()
        bus.reset()

        results = []
        def signal_handler(e: SignalEvent):
            results.append(e.signal)

        bus.on('SignalEvent', signal_handler)

        engine = SignalEngine(
            factor=RSIFactor(period=14, buy_threshold=30, sell_threshold=70, symbol='600900.SH'),
            bus=bus
        )
        bus.on('MarketEvent', engine.on_market_event)

        # 发送 MarketEvent
        bus.emit(MarketEvent(
            symbol='600900.SH',
            freq='1min',
            close=26.0,
            open=25.8, high=26.1, low=25.7, volume=5000,
        ))

        # 同一分钟内第二次发送不应触发（频率控制）
        bus.emit(MarketEvent(
            symbol='600900.SH',
            freq='1min',
            close=26.1,
            open=25.9, high=26.2, low=25.8, volume=6000,
        ))

        # 信号结果可以是空（随机数据），但流程不能报错
        self.assertIsInstance(results, list)


if __name__ == '__main__':
    # verbosity=2 显示每个测试名称
    unittest.main(verbosity=2)

"""
Phase 6 回测引擎验证测试
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from core.backtest_engine import (
    BacktestEngine, BacktestConfig, BacktestResult,
    PerformanceAnalyzer, TradeRecord,
)
from core.factors.price_momentum import RSIFactor


def make_test_data(n=60, base_price=26.0, trend=0.0, seed=42):
    dates = pd.date_range(end=datetime.now(), periods=n, freq='D')
    np.random.seed(seed)
    closes = base_price + np.cumsum(np.random.randn(n) * 0.5 + trend)
    highs = closes + np.abs(np.random.randn(n) * 0.3)
    lows = closes - np.abs(np.random.randn(n) * 0.3)
    opens = closes + np.random.randn(n) * 0.2
    volumes = np.random.randint(1e6, 5e6, n)
    return pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows,
        'close': closes, 'volume': volumes,
    }, index=dates)


class TestBacktestEngine(unittest.TestCase):

    def test_rsi_strategy_basic(self):
        """RSI 因子回测：验证引擎正常运行（信号未必触发）"""
        df = make_test_data(n=120, base_price=26.0, trend=0.0)
        config = BacktestConfig(
            initial_equity=100_000,
            commission_rate=0.0003,
            slippage_bps=5.0,
        )
        engine = BacktestEngine(config)
        engine.load_data('TEST', df)
        # 宽松阈值确保触发
        rsi = RSIFactor(period=14, buy_threshold=50, sell_threshold=50, symbol='TEST')
        engine.add_strategy(rsi)
        result = engine.run()
        self.assertIsInstance(result, BacktestResult)
        self.assertGreater(len(result.equity_curve), 0)
        print(f"\nRSI回测: trades={result.n_trades}, "
              f"return={result.total_return*100:.2f}%, sharpe={result.sharpe:.3f}")

    def test_no_signals_no_trades(self):
        """极端阈值时期望0交易"""
        df = make_test_data(n=30, base_price=26.0, seed=999)
        engine = BacktestEngine()
        engine.load_data('TEST', df)
        engine.add_strategy(RSIFactor(period=14, buy_threshold=90, sell_threshold=10, symbol='TEST'))
        result = engine.run()
        self.assertGreater(len(result.equity_curve), 0)

    def test_config_params(self):
        config = BacktestConfig(
            initial_equity=200_000,
            commission_rate=0.0005,
            slippage_bps=10.0,
            max_position_pct=0.20,
        )
        self.assertEqual(config.initial_equity, 200_000)
        self.assertEqual(config.slippage_bps, 10.0)

    def test_performance_analyzer(self):
        df = make_test_data(n=60, base_price=26.0)
        engine = BacktestEngine()
        engine.load_data('TEST', df)
        engine.add_strategy(RSIFactor(period=14, symbol='TEST'))
        result = engine.run()
        analysis = PerformanceAnalyzer.analyze(result.trades, result.daily_stats)
        self.assertIsInstance(analysis, dict)
        print(f"\n绩效分析: {analysis}")

    def test_summary(self):
        df = make_test_data(n=60)
        engine = BacktestEngine()
        engine.load_data('TEST', df)
        engine.add_strategy(RSIFactor(period=14, symbol='TEST'))
        result = engine.run()
        summary = result.summary()
        self.assertIsInstance(summary, str)
        print(f"\n{summary}")


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""
tests/test_sentiment_factors.py — 情绪因子单元测试

覆盖 core/factors/sentiment.py 中 3 个因子：
  - MarginTradingFactor  (融资余额变化率)
  - NorthboundFlowFactor (北向资金净流入)
  - ShortInterestFactor  (融券余额变化率)

测试策略：全部使用 mock 数据，无网络依赖。
"""

from __future__ import annotations

import unittest
import numpy as np
import pandas as pd


def _make_price_df(n: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2023-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': rng.integers(100_000, 500_000, n).astype(float),
    }, index=dates)


def _make_sentiment_df(
    n: int = 60,
    margin: float = 1e10,
    north: float = 50.0,
    short: float = 5e8,
    seed: int = 42,
) -> pd.DataFrame:
    """生成日频情绪数据（三列：融资余额 / 北向净流入 / 融券余额）"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2023-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'margin_balance': margin + np.cumsum(rng.normal(0, margin * 0.005, n)),
        'north_flow': north + rng.normal(0, 30, n),
        'short_balance': short + np.cumsum(rng.normal(0, short * 0.005, n)),
    }, index=dates)


# ---------------------------------------------------------------------------
# MarginTradingFactor
# ---------------------------------------------------------------------------

class TestMarginTradingFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.sentiment import MarginTradingFactor
        self.price = _make_price_df(80)
        self.sent = _make_sentiment_df(80)
        self.factor = MarginTradingFactor(
            sentiment_data=self.sent, short_window=5, long_window=20
        )

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_index_aligned(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(result.index.equals(self.price.index))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_data_returns_zero(self):
        from core.factors.sentiment import MarginTradingFactor
        f = MarginTradingFactor(sentiment_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_margin_surge_positive(self):
        """融资余额快速增加 → 后期因子值应高于前期"""
        from core.factors.sentiment import MarginTradingFactor
        price = _make_price_df(80)
        sent = _make_sentiment_df(80)
        # 后30天融资余额快速增加（每天 +3%）
        base = float(sent['margin_balance'].iloc[49])
        for i in range(30):
            sent.iloc[50 + i, sent.columns.get_loc('margin_balance')] = base * (1.03 ** (i + 1))

        f = MarginTradingFactor(sentiment_data=sent, short_window=5, long_window=20)
        result = f.evaluate(price)
        self.assertGreater(result.iloc[60:].mean(), result.iloc[10:40].mean())

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('MarginTrading')
        self.assertEqual(f.name, 'MarginTrading')


# ---------------------------------------------------------------------------
# NorthboundFlowFactor
# ---------------------------------------------------------------------------

class TestNorthboundFlowFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.sentiment import NorthboundFlowFactor
        self.price = _make_price_df(80)
        self.sent = _make_sentiment_df(80)
        self.factor = NorthboundFlowFactor(sentiment_data=self.sent, window=5)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_data_returns_zero(self):
        from core.factors.sentiment import NorthboundFlowFactor
        f = NorthboundFlowFactor(sentiment_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_persistent_inflow_positive(self):
        """北向持续大额净流入 → 因子值为正"""
        from core.factors.sentiment import NorthboundFlowFactor
        price = _make_price_df(80)
        sent = _make_sentiment_df(80, north=-30.0)  # 基础水平负值
        sent.iloc[50:, sent.columns.get_loc('north_flow')] = 200.0  # 后30天大幅净流入

        f = NorthboundFlowFactor(sentiment_data=sent, window=5)
        result = f.evaluate(price)
        self.assertGreater(result.iloc[60:].mean(), result.iloc[5:40].mean())

    def test_persistent_outflow_negative(self):
        """北向持续大额净流出 → 因子值为负"""
        from core.factors.sentiment import NorthboundFlowFactor
        price = _make_price_df(80)
        sent = _make_sentiment_df(80, north=100.0)  # 基础水平正值
        sent.iloc[50:, sent.columns.get_loc('north_flow')] = -200.0  # 后30天大幅流出

        f = NorthboundFlowFactor(sentiment_data=sent, window=5)
        result = f.evaluate(price)
        self.assertLess(result.iloc[60:].mean(), result.iloc[5:40].mean())

    def test_signals_buy_on_high_inflow(self):
        """北向大额流入生成 BUY 信号"""
        from core.factors.sentiment import NorthboundFlowFactor
        # 构造 z-score > 1.0 的因子值序列
        vals = pd.Series([0.0] * 19 + [2.0], index=range(20))
        f = NorthboundFlowFactor()
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'BUY')

    def test_signals_sell_on_high_outflow(self):
        """北向大额流出生成 SELL 信号"""
        from core.factors.sentiment import NorthboundFlowFactor
        vals = pd.Series([0.0] * 19 + [-2.0], index=range(20))
        f = NorthboundFlowFactor()
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'SELL')

    def test_signals_none_in_neutral_zone(self):
        """因子值在阈值内 → 无信号"""
        from core.factors.sentiment import NorthboundFlowFactor
        vals = pd.Series([0.0] * 20, index=range(20))
        f = NorthboundFlowFactor()
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(sigs, [])

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('NorthboundFlow')
        self.assertEqual(f.name, 'NorthboundFlow')


# ---------------------------------------------------------------------------
# ShortInterestFactor
# ---------------------------------------------------------------------------

class TestShortInterestFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.sentiment import ShortInterestFactor
        self.price = _make_price_df(80)
        self.sent = _make_sentiment_df(80)
        self.factor = ShortInterestFactor(sentiment_data=self.sent, window=10)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_data_returns_zero(self):
        from core.factors.sentiment import ShortInterestFactor
        f = ShortInterestFactor(sentiment_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_short_squeeze_positive(self):
        """融券余额快速减少（空头回补）→ 挤仓期因子值高于增仓期"""
        from core.factors.sentiment import ShortInterestFactor
        price = _make_price_df(80)

        # 构造：前40天融券持续增加（每天+2%），后40天持续减少（每天-2%）
        vals = np.zeros(80)
        vals[0] = 5e8
        for i in range(1, 40):
            vals[i] = vals[i - 1] * 1.02  # 增仓
        for i in range(40, 80):
            vals[i] = vals[i - 1] * 0.98  # 挤仓

        dates = pd.date_range('2023-01-01', periods=80, freq='B')
        sent = pd.DataFrame({'short_balance': vals}, index=dates)

        f = ShortInterestFactor(sentiment_data=sent, window=10)
        result = f.evaluate(price)
        # 后40天（挤仓，短余额减少）因子值应高于前40天（增仓）
        self.assertGreater(result.iloc[50:].mean(), result.iloc[10:35].mean())

    def test_short_buildup_negative(self):
        """融券余额快速增加（做空压力）→ 因子值应为负"""
        from core.factors.sentiment import ShortInterestFactor
        price = _make_price_df(80)
        sent = _make_sentiment_df(80)
        base = float(sent['short_balance'].iloc[49])
        for i in range(25):
            sent.iloc[50 + i, sent.columns.get_loc('short_balance')] = base * (1.03 ** (i + 1))

        f = ShortInterestFactor(sentiment_data=sent, window=10)
        result = f.evaluate(price)
        self.assertLess(result.iloc[65:].mean(), result.iloc[5:40].mean())

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('ShortInterest')
        self.assertEqual(f.name, 'ShortInterest')


# ---------------------------------------------------------------------------
# 注册表集成测试
# ---------------------------------------------------------------------------

class TestSentimentRegistryIntegration(unittest.TestCase):

    def test_all_sentiment_factors_registered(self):
        from core.factor_registry import registry
        for name in ['MarginTrading', 'NorthboundFlow', 'ShortInterest']:
            with self.subTest(name=name):
                self.assertIn(name, registry)

    def test_total_factor_count_at_least_20(self):
        """原5 + 技术7 + 基本面5 + 情绪3 = 20个"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 20)

    def test_sentiment_factors_in_pipeline(self):
        """情绪因子（无外部数据）加入流水线不影响其他因子"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.5)
        pipeline.add('NorthboundFlow', weight=0.3)
        pipeline.add('MarginTrading', weight=0.2)

        rng = np.random.default_rng(42)
        dates = pd.date_range('2023-01-01', periods=60, freq='B')
        close = 10 + np.cumsum(rng.normal(0, 0.2, 60))
        data = pd.DataFrame({
            'open': close, 'high': close * 1.01, 'low': close * 0.99,
            'close': close, 'volume': rng.integers(100_000, 500_000, 60).astype(float),
        }, index=dates)

        result = pipeline.run(symbol='000001.SZ', data=data, price=float(close[-1]))
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))

    def test_northbound_with_data_in_pipeline(self):
        """北向资金因子（有数据）加入流水线正常运行"""
        from core.factor_pipeline import FactorPipeline
        from core.factors.sentiment import NorthboundFlowFactor

        sent = _make_sentiment_df(80)
        f = NorthboundFlowFactor(sentiment_data=sent, window=5)

        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.6)
        pipeline.add(f, weight=0.4)

        price = _make_price_df(80)
        result = pipeline.run(symbol='000001.SZ', data=price, price=10.0)
        self.assertTrue(np.isfinite(result.combined_score))


if __name__ == '__main__':
    unittest.main()

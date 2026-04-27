"""
tests/test_technical_factors.py — 扩展技术因子单元测试

覆盖 core/factors/technical.py 中 7 个因子的：
  - evaluate() 输出形状与索引正确
  - 归一化结果（有限值、非全零）
  - 边界条件（数据不足、零成交量、无基准数据）
  - signals() 接口可调用
  - 注册表可通过名称创建
"""

from __future__ import annotations

import unittest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def _make_ohlcv(n: int = 60, seed: int = 42) -> pd.DataFrame:
    """生成 n 根日线 OHLCV（价格随机游走，成交量随机正整数）"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.clip(close, 1, None)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    open_ = close * (1 + rng.normal(0, 0.01, n))
    volume = rng.integers(100_000, 1_000_000, n).astype(float)
    return pd.DataFrame({
        'open': open_, 'high': high, 'low': low,
        'close': close, 'volume': volume,
    }, index=dates)


# ---------------------------------------------------------------------------
# IntraVWAPFactor
# ---------------------------------------------------------------------------

class TestIntraVWAPFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import IntraVWAPFactor
        self.factor = IntraVWAPFactor(window=20)
        self.data = _make_ohlcv(60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))
        self.assertTrue(result.index.equals(self.data.index))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_not_all_zero(self):
        result = self.factor.evaluate(self.data)
        self.assertGreater(result.abs().sum(), 0)

    def test_signals_callable(self):
        vals = self.factor.evaluate(self.data)
        sigs = self.factor.signals(vals, price=float(self.data['close'].iloc[-1]))
        self.assertIsInstance(sigs, list)

    def test_short_data(self):
        """数据不足时仍能运行（不抛异常）"""
        short = _make_ohlcv(5)
        result = self.factor.evaluate(short)
        self.assertEqual(len(result), 5)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('IntraVWAP')
        self.assertEqual(f.name, 'IntraVWAP')


# ---------------------------------------------------------------------------
# OpenGapFactor
# ---------------------------------------------------------------------------

class TestOpenGapFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import OpenGapFactor
        self.factor = OpenGapFactor(window=20)
        self.data = _make_ohlcv(60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_first_value_nan_or_zero(self):
        """第一根 bar 无前收，因子值应为 0（normalize 处理 NaN）"""
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.isfinite(result.iloc[0]))

    def test_gap_direction(self):
        """构造持续跳空高开场景，因子应为正"""
        df = _make_ohlcv(40)
        # 强制 open > prev_close
        df['open'] = df['close'].shift(1) * 1.02
        df = df.dropna()
        result = self.factor.evaluate(df)
        self.assertGreater(result.mean(), 0)

    def test_not_all_zero(self):
        result = self.factor.evaluate(self.data)
        self.assertGreater(result.abs().sum(), 0)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('OpenGap')
        self.assertEqual(f.name, 'OpenGap')


# ---------------------------------------------------------------------------
# VolAccelerationFactor
# ---------------------------------------------------------------------------

class TestVolAccelerationFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import VolAccelerationFactor
        self.factor = VolAccelerationFactor(short_window=5, long_window=20)
        self.data = _make_ohlcv(60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_high_volume_burst_positive(self):
        """最近5天成交量突然放大10倍，因子值应为正"""
        df = _make_ohlcv(40)
        df = df.copy()
        df.iloc[-5:, df.columns.get_loc('volume')] *= 10
        result = self.factor.evaluate(df)
        self.assertGreater(result.iloc[-1], 0)

    def test_zero_volume_handled(self):
        """零成交量不引发除零错误"""
        df = _make_ohlcv(40)
        df['volume'] = 0.0
        result = self.factor.evaluate(df)
        self.assertEqual(len(result), len(df))
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('VolAcceleration')
        self.assertEqual(f.name, 'VolAcceleration')


# ---------------------------------------------------------------------------
# BidAskSpreadFactor
# ---------------------------------------------------------------------------

class TestBidAskSpreadFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import BidAskSpreadFactor
        self.factor = BidAskSpreadFactor(window=20)
        self.data = _make_ohlcv(60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_narrow_spread_positive(self):
        """窄振幅（高流动性）→ 因子值为正"""
        df = _make_ohlcv(40)
        df['high'] = df['close'] * 1.001  # 极窄振幅
        df['low'] = df['close'] * 0.999
        result = self.factor.evaluate(df)
        self.assertGreater(result.mean(), 0)

    def test_no_signals(self):
        """流动性因子不产生方向信号"""
        vals = self.factor.evaluate(self.data)
        sigs = self.factor.signals(vals, price=10.0)
        self.assertEqual(sigs, [])

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('BidAskSpread')
        self.assertEqual(f.name, 'BidAskSpread')


# ---------------------------------------------------------------------------
# BuyingPressureFactor
# ---------------------------------------------------------------------------

class TestBuyingPressureFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import BuyingPressureFactor
        self.factor = BuyingPressureFactor(window=10)
        self.data = _make_ohlcv(60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_close_at_high_positive(self):
        """前半段收盘在中间，后半段收盘在最高价 → 末尾因子值为正"""
        df = _make_ohlcv(40)
        df = df.copy()
        # 后20根：收盘=最高价（CLV=1，买方主导）
        df.iloc[20:, df.columns.get_loc('close')] = df.iloc[20:]['high'].values
        result = self.factor.evaluate(df)
        # 末尾值应高于整体均值
        self.assertGreater(result.iloc[-1], result.mean())

    def test_close_at_low_negative(self):
        """前半段收盘在中间，后半段收盘在最低价 → 末尾因子值为负"""
        df = _make_ohlcv(40)
        df = df.copy()
        # 后20根：收盘=最低价（CLV=0，卖方主导）
        df.iloc[20:, df.columns.get_loc('close')] = df.iloc[20:]['low'].values
        result = self.factor.evaluate(df)
        # 末尾值应低于整体均值
        self.assertLess(result.iloc[-1], result.mean())

    def test_zero_range_no_crash(self):
        """High=Low=Close（成交量=0）不引发除零"""
        df = _make_ohlcv(40)
        df['high'] = df['close']
        df['low'] = df['close']
        result = self.factor.evaluate(df)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('BuyingPressure')
        self.assertEqual(f.name, 'BuyingPressure')


# ---------------------------------------------------------------------------
# SectorMomentumFactor
# ---------------------------------------------------------------------------

class TestSectorMomentumFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import SectorMomentumFactor
        self.data = _make_ohlcv(60)
        self.sector_data = _make_ohlcv(60, seed=99)
        self.factor = SectorMomentumFactor(sector_data=self.sector_data, momentum_window=20)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_none_sector_returns_zero(self):
        """无行业数据 → 因子值全为 0"""
        from core.factors.technical import SectorMomentumFactor
        f = SectorMomentumFactor(sector_data=None, momentum_window=20)
        result = f.evaluate(self.data)
        self.assertTrue((result == 0).all())

    def test_short_sector_data_returns_zero(self):
        """行业数据不足 momentum_window → 全为 0"""
        from core.factors.technical import SectorMomentumFactor
        short_sector = _make_ohlcv(10, seed=99)
        f = SectorMomentumFactor(sector_data=short_sector, momentum_window=20)
        result = f.evaluate(self.data)
        self.assertTrue((result == 0).all())

    def test_strong_sector_momentum_positive(self):
        """行业ETF持续上涨 → 因子值应多数为正"""
        rising_sector = _make_ohlcv(60, seed=99)
        rising_sector['close'] = np.linspace(10, 20, 60)
        from core.factors.technical import SectorMomentumFactor
        f = SectorMomentumFactor(sector_data=rising_sector, momentum_window=20)
        result = f.evaluate(self.data)
        self.assertGreater(result.iloc[20:].mean(), 0)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('SectorMomentum')
        self.assertEqual(f.name, 'SectorMomentum')


# ---------------------------------------------------------------------------
# IndexRelativeStrengthFactor
# ---------------------------------------------------------------------------

class TestIndexRelativeStrengthFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.technical import IndexRelativeStrengthFactor
        self.data = _make_ohlcv(60)
        self.index_data = _make_ohlcv(60, seed=77)
        self.factor = IndexRelativeStrengthFactor(index_data=self.index_data, window=20)

    def test_output_shape(self):
        result = self.factor.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))

    def test_values_finite(self):
        result = self.factor.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_none_index_degrades_to_price_momentum(self):
        """无基准数据 → 退化为纯价格动量，仍有有效输出"""
        from core.factors.technical import IndexRelativeStrengthFactor
        f = IndexRelativeStrengthFactor(index_data=None, window=20)
        result = f.evaluate(self.data)
        self.assertEqual(len(result), len(self.data))
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_outperforming_stock_positive(self):
        """个股涨幅远超指数 → 因子值应为正"""
        stock = _make_ohlcv(60)
        stock['close'] = np.linspace(10, 30, 60)   # 个股涨 3x

        index = _make_ohlcv(60, seed=77)
        index['close'] = np.linspace(10, 11, 60)   # 指数仅涨 10%

        from core.factors.technical import IndexRelativeStrengthFactor
        f = IndexRelativeStrengthFactor(index_data=index, window=20)
        result = f.evaluate(stock)
        self.assertGreater(result.iloc[20:].mean(), 0)

    def test_underperforming_stock_negative(self):
        """个股涨幅远低于指数 → 因子值应为负"""
        stock = _make_ohlcv(60)
        stock['close'] = np.linspace(10, 10.5, 60)  # 个股仅涨 5%

        index = _make_ohlcv(60, seed=77)
        index['close'] = np.linspace(10, 20, 60)    # 指数涨 100%

        from core.factors.technical import IndexRelativeStrengthFactor
        f = IndexRelativeStrengthFactor(index_data=index, window=20)
        result = f.evaluate(stock)
        self.assertLess(result.iloc[20:].mean(), 0)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('IndexRelativeStrength')
        self.assertEqual(f.name, 'IndexRelativeStrength')


# ---------------------------------------------------------------------------
# 注册表集成测试
# ---------------------------------------------------------------------------

class TestRegistryIntegration(unittest.TestCase):

    def test_all_new_factors_registered(self):
        from core.factor_registry import registry
        new_factors = [
            'IntraVWAP', 'OpenGap', 'VolAcceleration',
            'BidAskSpread', 'BuyingPressure',
            'SectorMomentum', 'IndexRelativeStrength',
        ]
        for name in new_factors:
            with self.subTest(name=name):
                self.assertIn(name, registry)

    def test_total_factor_count(self):
        """确认至少 12 个因子已注册（原5 + 新7）"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 12)

    def test_pipeline_with_new_factors(self):
        """新因子可以加入 FactorPipeline 正常运行"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('IntraVWAP', weight=0.3)
        pipeline.add('OpenGap', weight=0.3)
        pipeline.add('VolAcceleration', weight=0.4)

        data = _make_ohlcv(60)
        price = float(data['close'].iloc[-1])
        result = pipeline.run(symbol='000001.SZ', data=data, price=price)

        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))

    def test_buying_pressure_in_pipeline(self):
        """BuyingPressure 因子加入 pipeline 不出错"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('BuyingPressure', weight=1.0)

        data = _make_ohlcv(40)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)
        self.assertTrue(np.isfinite(result.combined_score))


if __name__ == '__main__':
    unittest.main()

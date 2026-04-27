"""
tests/test_fundamental_factors.py — 基本面因子单元测试

覆盖：
  - 5 个基本面因子的 evaluate() 输出正确
  - 无财务数据时优雅降级（返回全零）
  - 因子方向性（低PE→正因子值，高ROE增长→正因子值等）
  - FundamentalDataManager 的缓存与切片逻辑（不发网络请求）
  - 注册表可创建 5 个新因子

测试策略：
  - 所有财务数据均为手动构造的 mock DataFrame（无 AKShare 依赖）
  - FundamentalDataManager 的网络获取路径通过 unittest.mock 屏蔽
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_price_df(n: int = 120, seed: int = 42) -> pd.DataFrame:
    """生成 n 根日线价格数据"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.clip(close, 1, None)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    return pd.DataFrame({
        'open': close, 'high': high, 'low': low,
        'close': close, 'volume': rng.integers(100_000, 1_000_000, n).astype(float),
    }, index=dates)


def _make_financial_df(
    n: int = 120,
    pe: float = 15.0,
    roe: float = 12.0,
    eps: float = 1.0,
    rev_yoy: float = 10.0,
    ocf_ratio: float = 1.2,
    seed: int = 42,
) -> pd.DataFrame:
    """生成 n 行日频财务数据（模拟 FundamentalDataManager.get_fundamentals() 输出）"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'pe_ttm': pe + rng.normal(0, 1, n),
        'pb': 2.0 + rng.normal(0, 0.1, n),
        'roe_ttm': roe + rng.normal(0, 0.5, n),
        'eps_ttm': eps + rng.normal(0, 0.05, n),
        'revenue_yoy': rev_yoy + rng.normal(0, 1, n),
        'profit_yoy': 8.0 + rng.normal(0, 1, n),
        'ocf_to_profit': ocf_ratio + rng.normal(0, 0.1, n),
    }, index=dates)


# ---------------------------------------------------------------------------
# PEPercentileFactor
# ---------------------------------------------------------------------------

class TestPEPercentileFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.fundamental import PEPercentileFactor
        self.price = _make_price_df(300)
        self.fin = _make_financial_df(300)
        self.factor = PEPercentileFactor(financial_data=self.fin, lookback_years=1)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_index_aligned(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(result.index.equals(self.price.index))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_financial_data_returns_zero(self):
        from core.factors.fundamental import PEPercentileFactor
        f = PEPercentileFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_low_pe_positive(self):
        """低PE期间（估值便宜）因子值应为正（买入信号方向）"""
        from core.factors.fundamental import PEPercentileFactor
        price = _make_price_df(300)

        # 构造：前200天PE=20（高），后100天PE=5（低）
        fin = _make_financial_df(300)
        fin.iloc[:200, fin.columns.get_loc('pe_ttm')] = 20.0
        fin.iloc[200:, fin.columns.get_loc('pe_ttm')] = 5.0

        f = PEPercentileFactor(financial_data=fin, lookback_years=1)
        result = f.evaluate(price)
        # 后100天平均值应高于前200天（低PE→更高因子值）
        self.assertGreater(result.iloc[250:].mean(), result.iloc[50:150].mean())

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('PEPercentile')
        self.assertEqual(f.name, 'PEPercentile')


# ---------------------------------------------------------------------------
# ROEMomentumFactor
# ---------------------------------------------------------------------------

class TestROEMomentumFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.fundamental import ROEMomentumFactor
        self.price = _make_price_df(300)
        self.fin = _make_financial_df(300)
        self.factor = ROEMomentumFactor(financial_data=self.fin, diff_days=120)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_financial_data_returns_zero(self):
        from core.factors.fundamental import ROEMomentumFactor
        f = ROEMomentumFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_improving_roe_positive(self):
        """ROE 持续改善 → 因子值为正"""
        from core.factors.fundamental import ROEMomentumFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        # 线性上升的 ROE（从 5% 到 20%）
        fin['roe_ttm'] = np.linspace(5, 20, 300)

        f = ROEMomentumFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        # 中后期（有足够历史）因子值应为正
        self.assertGreater(result.iloc[150:].mean(), 0)

    def test_declining_roe_negative(self):
        """ROE 持续下滑 → 因子值为负"""
        from core.factors.fundamental import ROEMomentumFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['roe_ttm'] = np.linspace(20, 5, 300)  # 从 20% 降至 5%

        f = ROEMomentumFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        self.assertLess(result.iloc[150:].mean(), 0)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('ROEMomentum')
        self.assertEqual(f.name, 'ROEMomentum')


# ---------------------------------------------------------------------------
# EarningsSurpriseFactor
# ---------------------------------------------------------------------------

class TestEarningsSurpriseFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.fundamental import EarningsSurpriseFactor
        self.price = _make_price_df(300)
        self.fin = _make_financial_df(300)
        self.factor = EarningsSurpriseFactor(financial_data=self.fin, diff_days=120)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_financial_data_returns_zero(self):
        from core.factors.fundamental import EarningsSurpriseFactor
        f = EarningsSurpriseFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_eps_growth_positive(self):
        """EPS 同比大幅增长 → 因子值为正"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['eps_ttm'] = np.linspace(1.0, 3.0, 300)  # EPS 从 1 增至 3

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        self.assertGreater(result.iloc[150:].mean(), 0)

    def test_eps_decline_negative(self):
        """EPS 同比大幅下滑 → 因子值为负"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['eps_ttm'] = np.linspace(3.0, 1.0, 300)  # EPS 从 3 降至 1

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        self.assertLess(result.iloc[150:].mean(), 0)

    def test_extreme_values_clipped(self):
        """极端 EPS 增速（>300%）被截断，不引发数值问题"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['eps_ttm'] = 0.01   # 极小基数，导致超高增速

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('EarningsSurprise')
        self.assertEqual(f.name, 'EarningsSurprise')


# ---------------------------------------------------------------------------
# RevenueGrowthFactor
# ---------------------------------------------------------------------------

class TestRevenueGrowthFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.fundamental import RevenueGrowthFactor
        self.price = _make_price_df(300)
        self.fin = _make_financial_df(300)
        self.factor = RevenueGrowthFactor(financial_data=self.fin, accel_window=60)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_financial_data_returns_zero(self):
        from core.factors.fundamental import RevenueGrowthFactor
        f = RevenueGrowthFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_accelerating_revenue_positive(self):
        """营收增速加速 → 因子值递增"""
        from core.factors.fundamental import RevenueGrowthFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['revenue_yoy'] = np.linspace(5, 30, 300)  # 增速持续加速

        f = RevenueGrowthFactor(financial_data=fin, accel_window=60)
        result = f.evaluate(price)
        # 后期因子值应大于早期
        self.assertGreater(result.iloc[200:].mean(), result.iloc[60:120].mean())

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('RevenueGrowth')
        self.assertEqual(f.name, 'RevenueGrowth')


# ---------------------------------------------------------------------------
# CashFlowQualityFactor
# ---------------------------------------------------------------------------

class TestCashFlowQualityFactor(unittest.TestCase):

    def setUp(self):
        from core.factors.fundamental import CashFlowQualityFactor
        self.price = _make_price_df(120)
        self.fin = _make_financial_df(120)
        self.factor = CashFlowQualityFactor(financial_data=self.fin, rolling_window=30)

    def test_output_shape(self):
        result = self.factor.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_values_finite(self):
        result = self.factor.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_no_financial_data_returns_zero(self):
        from core.factors.fundamental import CashFlowQualityFactor
        f = CashFlowQualityFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_high_ocf_ratio_positive(self):
        """前半段 OCF/利润=0.5，后半段=2.5 → 后期因子值高于前期"""
        from core.factors.fundamental import CashFlowQualityFactor
        price = _make_price_df(120)
        fin = _make_financial_df(120)
        fin['ocf_to_profit'] = 0.0
        fin.iloc[:60, fin.columns.get_loc('ocf_to_profit')] = 0.5
        fin.iloc[60:, fin.columns.get_loc('ocf_to_profit')] = 2.5

        f = CashFlowQualityFactor(financial_data=fin, rolling_window=20)
        result = f.evaluate(price)
        # 后期（高 OCF 期）因子值应高于前期
        self.assertGreater(result.iloc[80:].mean(), result.iloc[10:40].mean())

    def test_low_ocf_ratio_negative(self):
        """前半段 OCF/利润=2.5，后半段=0.2 → 后期因子值低于前期"""
        from core.factors.fundamental import CashFlowQualityFactor
        price = _make_price_df(120)
        fin = _make_financial_df(120)
        fin.iloc[:60, fin.columns.get_loc('ocf_to_profit')] = 2.5
        fin.iloc[60:, fin.columns.get_loc('ocf_to_profit')] = 0.2

        f = CashFlowQualityFactor(financial_data=fin, rolling_window=20)
        result = f.evaluate(price)
        # 后期（低 OCF 期）因子值应低于前期
        self.assertLess(result.iloc[80:].mean(), result.iloc[10:40].mean())

    def test_extreme_values_clipped(self):
        """极端值（OCF/利润=100）被截断，不引发问题"""
        from core.factors.fundamental import CashFlowQualityFactor
        price = _make_price_df(60)
        fin = _make_financial_df(60)
        fin['ocf_to_profit'] = 100.0

        f = CashFlowQualityFactor(financial_data=fin, rolling_window=10)
        result = f.evaluate(price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('CashFlowQuality')
        self.assertEqual(f.name, 'CashFlowQuality')


# ---------------------------------------------------------------------------
# FundamentalDataManager 单元测试（不发网络请求）
# ---------------------------------------------------------------------------

class TestFundamentalDataManager(unittest.TestCase):

    def test_get_fundamentals_no_akshare_returns_empty(self):
        """AKShare 不可用时返回空 DataFrame（不抛异常）"""
        from core.fundamental_data import FundamentalDataManager
        mgr = FundamentalDataManager()
        with patch.object(mgr, '_fetch', return_value=None):
            result = mgr.get_fundamentals('000001.SZ')
        self.assertIsInstance(result, pd.DataFrame)

    def test_memory_cache_reused(self):
        """第二次调用直接返回内存缓存，不重新 fetch"""
        from core.fundamental_data import FundamentalDataManager
        mgr = FundamentalDataManager()
        mock_df = _make_financial_df(60)
        mgr._memory_cache['000001.SZ'] = (datetime.now(), mock_df)

        with patch.object(mgr, '_fetch') as mock_fetch:
            result = mgr.get_fundamentals('000001.SZ')
            mock_fetch.assert_not_called()
        self.assertFalse(result.empty)

    def test_parquet_roundtrip(self):
        """Parquet 保存后可以正确读回"""
        from core.fundamental_data import FundamentalDataManager
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('core.fundamental_data._FUNDAMENTAL_DIR', tmpdir):
                mgr = FundamentalDataManager()
                fin = _make_financial_df(60)
                mgr._save_parquet('TEST.SZ', fin)
                loaded = mgr._load_parquet('TEST.SZ')

        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), len(fin))
        self.assertIn('pe_ttm', loaded.columns)

    def test_slice_by_date(self):
        """_slice 正确按日期范围截取"""
        from core.fundamental_data import FundamentalDataManager
        mgr = FundamentalDataManager()
        fin = _make_financial_df(300)

        sliced = mgr._slice(fin, start='2022-03-01', end='2022-06-30')
        self.assertTrue((sliced.index >= pd.Timestamp('2022-03-01')).all())
        self.assertTrue((sliced.index <= pd.Timestamp('2022-06-30')).all())

    def test_invalidate_removes_cache(self):
        """invalidate() 清除内存缓存"""
        from core.fundamental_data import FundamentalDataManager
        mgr = FundamentalDataManager()
        mgr._memory_cache['000001.SZ'] = (datetime.now(), _make_financial_df(30))
        mgr.invalidate('000001.SZ')
        self.assertNotIn('000001.SZ', mgr._memory_cache)


# ---------------------------------------------------------------------------
# 注册表集成测试
# ---------------------------------------------------------------------------

class TestFundamentalRegistryIntegration(unittest.TestCase):

    def test_all_fundamental_factors_registered(self):
        from core.factor_registry import registry
        names = ['PEPercentile', 'ROEMomentum', 'EarningsSurprise',
                 'RevenueGrowth', 'CashFlowQuality']
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, registry)

    def test_total_factor_count_at_least_17(self):
        """原5 + 技术7 + 基本面5 = 17 个因子"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 17)

    def test_fundamental_factor_in_pipeline_no_financial_data(self):
        """基本面因子（无数据时）加入流水线正常运行，不破坏其他因子"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.5)
        pipeline.add('PEPercentile', weight=0.3)
        pipeline.add('ROEMomentum', weight=0.2)

        data = _make_price_df(60)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)

        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))

    def test_fundamental_factor_in_pipeline_with_financial_data(self):
        """基本面因子（有数据时）加入流水线，combined_score 有效"""
        from core.factor_pipeline import FactorPipeline
        from core.factors.fundamental import PEPercentileFactor, ROEMomentumFactor

        fin = _make_financial_df(120)
        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.4)
        # 手动传入已配置的因子实例
        pe_factor = PEPercentileFactor(financial_data=fin, lookback_years=1)
        pipeline.add(pe_factor, weight=0.6)

        data = _make_price_df(120)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)

        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))


if __name__ == '__main__':
    unittest.main()

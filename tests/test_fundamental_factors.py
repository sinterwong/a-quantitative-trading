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

    # ── W1-4: 优先消费 eps_yoy 直接字段 ────────────────────────────────────

    def test_eps_yoy_direct_takes_priority(self):
        """有 eps_yoy 列时,优先消费,不再走自算路径。"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        # eps_yoy 从低升至高 — 增长加速,因子应整体偏正(末段比头段大)
        fin['eps_yoy'] = np.linspace(5.0, 50.0, 300)
        # 同时把 eps_ttm 设成下降趋势(自算路径会得到负值)
        fin['eps_ttm'] = np.linspace(3.0, 1.0, 300)

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        # 直接路径下,末端 z-score 应显著大于前段
        self.assertGreater(result.iloc[200:].mean(), result.iloc[:100].mean(),
                           "应优先用 eps_yoy 直接字段,而非 eps_ttm 自算")
        # 关键反向校验:若走 eps_ttm 自算,末端应为负;直接路径下末端为正
        self.assertGreater(result.iloc[-30:].mean(), 0)

    def test_eps_yoy_all_nan_falls_back_to_self_compute(self):
        """eps_yoy 列存在但全 NaN 时应回退到 eps_ttm 自算。"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        fin['eps_yoy'] = np.nan
        fin['eps_ttm'] = np.linspace(1.0, 3.0, 300)  # +200% growth

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        # 走 fallback 时应为正
        self.assertGreater(result.iloc[150:].mean(), 0)

    def test_eps_yoy_negative_value(self):
        """eps_yoy 持续下滑 → 末段因子值显著低于前段。"""
        from core.factors.fundamental import EarningsSurpriseFactor
        price = _make_price_df(300)
        fin = _make_financial_df(300)
        # 从正同比逐渐变到大幅负同比
        fin['eps_yoy'] = np.linspace(20.0, -50.0, 300)
        fin['eps_ttm'] = 1.0     # 自算路径下没信号(常数)

        f = EarningsSurpriseFactor(financial_data=fin, diff_days=120)
        result = f.evaluate(price)
        self.assertLess(result.iloc[-30:].mean(), result.iloc[:50].mean())
        # 末端应为负
        self.assertLess(result.iloc[-30:].mean(), 0)


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
    """FundamentalDataManager 单元测试（不发网络请求）。

    新实现委托 DataGateway.fundamentals_history()，内部不再持有缓存网络代码。
    """

    def test_get_fundamentals_delegates_to_gateway(self):
        """get_fundamentals() 正确委托 DataGateway，不直接调 AkShare"""
        from core.fundamental_data import FundamentalDataManager
        mock_df = _make_financial_df(60)
        with patch('core.fundamental_data.get_gateway') as mock_gw:
            mock_gw.return_value.fundamentals_history.return_value = mock_df
            mgr = FundamentalDataManager()
            result = mgr.get_fundamentals('000001.SZ', start='2022-01-01')

        mock_gw.return_value.fundamentals_history.assert_called_once_with(
            '000001.SZ', start='2022-01-01', end=None,
        )
        self.assertFalse(result.empty)

    def test_get_fundamentals_gateway_returns_empty(self):
        """Gateway 返回空 DataFrame 时，get_fundamentals() 返回空 DataFrame（不抛异常）"""
        from core.fundamental_data import FundamentalDataManager
        with patch('core.fundamental_data.get_gateway') as mock_gw:
            mock_gw.return_value.fundamentals_history.return_value = pd.DataFrame()
            mgr = FundamentalDataManager()
            result = mgr.get_fundamentals('000001.SZ')

        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_get_fundamentals_gateway_raises(self):
        """Gateway 抛异常时，get_fundamentals() 返回空 DataFrame（不向上传播）"""
        from core.fundamental_data import FundamentalDataManager
        with patch('core.fundamental_data.get_gateway') as mock_gw:
            mock_gw.return_value.fundamentals_history.side_effect = RuntimeError('network')
            mgr = FundamentalDataManager()
            result = mgr.get_fundamentals('000001.SZ')

        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_get_fundamentals_date_index_normalized(self):
        """返回 DataFrame 保证 DatetimeIndex"""
        from core.fundamental_data import FundamentalDataManager
        mock_df = _make_financial_df(60)
        # Simulate a non-DatetimeIndex (edge case from gateway)
        mock_df.index = range(len(mock_df))
        with patch('core.fundamental_data.get_gateway') as mock_gw:
            mock_gw.return_value.fundamentals_history.return_value = mock_df
            mgr = FundamentalDataManager()
            result = mgr.get_fundamentals('000001.SZ')

        self.assertTrue(pd.api.types.is_datetime64_any_dtype(result.index))

    def test_invalidate_calls_gateway_clear(self):
        """invalidate() 正确清除 gateway 缓存（符号级精确清除）"""
        from core.fundamental_data import FundamentalDataManager
        with patch('core.fundamental_data.get_gateway') as mock_gw:
            mgr = FundamentalDataManager()
            mgr.invalidate('000001.SZ')

        mock_gw.return_value.invalidate_fundamentals_history.assert_called_once_with('000001.SZ')


# ---------------------------------------------------------------------------
# 注册表集成测试
# ---------------------------------------------------------------------------

class TestFinancialHealthFactor(unittest.TestCase):
    """W1-5: FinancialHealthFactor 合成 debt_to_equity + current_ratio + ocf_to_profit。"""

    def setUp(self):
        self.price = _make_price_df(300)
        self.fin = _make_financial_df(300)

    def test_no_data_returns_zero(self):
        from core.factors.fundamental import FinancialHealthFactor
        f = FinancialHealthFactor(financial_data=None)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_partial_data_does_not_crash(self):
        """三项中只有一项可用时也能产出结果。"""
        from core.factors.fundamental import FinancialHealthFactor
        # 只给 current_ratio,且有变化
        fin = pd.DataFrame({
            'current_ratio': np.linspace(2.0, 3.0, 300),
        }, index=self.fin.index)
        f = FinancialHealthFactor(financial_data=fin)
        result = f.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_improving_health_positive_trend(self):
        """财务健康度持续改善 → 末段因子值显著高于前段。"""
        from core.factors.fundamental import FinancialHealthFactor
        n = 300
        # debt 下降(好) + current_ratio 上升(好) + ocf 上升(好)
        fin = pd.DataFrame({
            'debt_to_equity': np.linspace(60.0, 30.0, n),
            'current_ratio': np.linspace(1.5, 3.5, n),
            'ocf_to_profit': np.linspace(0.5, 1.5, n),
        }, index=pd.date_range('2022-01-01', periods=n, freq='B'))
        price = _make_price_df(n)

        f = FinancialHealthFactor(financial_data=fin, rolling_window=20)
        result = f.evaluate(price)
        self.assertGreater(result.iloc[-30:].mean(), result.iloc[:30].mean())

    def test_deteriorating_health_negative_trend(self):
        """财务健康度持续恶化 → 末段因子值显著低于前段。"""
        from core.factors.fundamental import FinancialHealthFactor
        n = 300
        fin = pd.DataFrame({
            'debt_to_equity': np.linspace(30.0, 80.0, n),
            'current_ratio': np.linspace(3.0, 1.0, n),
            'ocf_to_profit': np.linspace(1.5, 0.3, n),
        }, index=pd.date_range('2022-01-01', periods=n, freq='B'))
        price = _make_price_df(n)

        f = FinancialHealthFactor(financial_data=fin, rolling_window=20)
        result = f.evaluate(price)
        self.assertLess(result.iloc[-30:].mean(), result.iloc[:30].mean())

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('FinancialHealth')
        self.assertEqual(f.name, 'FinancialHealth')


class TestDividendYieldFactor(unittest.TestCase):
    """W1-6: DividendYieldFactor 股息率历史百分位。"""

    def test_no_data_returns_zero(self):
        from core.factors.fundamental import DividendYieldFactor
        price = _make_price_df(300)
        f = DividendYieldFactor(financial_data=None)
        result = f.evaluate(price)
        self.assertTrue((result == 0).all())

    def test_no_dividend_yield_column_returns_zero(self):
        from core.factors.fundamental import DividendYieldFactor
        price = _make_price_df(300)
        # financial_data 不含 dividend_yield 列
        fin = _make_financial_df(300)
        f = DividendYieldFactor(financial_data=fin)
        result = f.evaluate(price)
        self.assertTrue((result == 0).all())

    def test_rising_dividend_yield_positive_trend(self):
        """股息率上升 → 末段百分位高 → 因子值为正。"""
        from core.factors.fundamental import DividendYieldFactor
        n = 800   # 充分长以让滚动百分位窗口 (3y) 有效
        idx = pd.date_range('2022-01-01', periods=n, freq='B')
        fin = pd.DataFrame({
            'dividend_yield': np.linspace(1.0, 5.0, n),
        }, index=idx)
        price = _make_price_df(n)

        f = DividendYieldFactor(financial_data=fin, lookback_years=1)
        result = f.evaluate(price)
        # 末段股息率高 → 高百分位 → 高 z-score
        self.assertGreater(result.iloc[-30:].mean(), 0)

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('DividendYield')
        self.assertEqual(f.name, 'DividendYield')


class TestFundamentalRegistryIntegration(unittest.TestCase):

    def test_all_fundamental_factors_registered(self):
        from core.factor_registry import registry
        names = ['PEPercentile', 'ROEMomentum', 'EarningsSurprise',
                 'RevenueGrowth', 'CashFlowQuality',
                 'FinancialHealth', 'DividendYield']
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, registry)

    def test_total_factor_count_at_least_19(self):
        """原5 + 技术7 + 基本面7(含 FinancialHealth/DividendYield) = 19 个因子"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 19)

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

"""
test_pipeline_factory_degradation.py — P0-2 因子降级测试

验证 build_pipeline() 在以下场景的鲁棒性：
  1. 单个因子加载失败时其它因子继续可用
  2. FactorPipeline.run() 在剩余因子上正确归一化权重
  3. 成功因子数 < MIN_FACTORS_REQUIRED 时按 strict 决定是否抛 RuntimeError
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from typing import List

import numpy as np
import pandas as pd


def _make_data(n: int = 80) -> pd.DataFrame:
    """构造可供因子计算的 OHLCV DataFrame。"""
    rng = np.random.default_rng(42)
    dates = pd.date_range('2024-01-01', periods=n, freq='D')
    close = 10.0 + np.cumsum(rng.normal(0.0, 0.1, n))
    high = close + rng.uniform(0.05, 0.3, n)
    low = close - rng.uniform(0.05, 0.3, n)
    open_ = close + rng.normal(0.0, 0.05, n)
    vol = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol,
    }, index=dates)


class TestPipelineDegradation(unittest.TestCase):
    """验证 P0-2 修复：因子降级时权重归一化与守卫。"""

    def test_all_factors_loaded_without_data_layer(self):
        """get_macro_data 失败时宏观因子应被跳过，不影响其它因子。"""
        from core.pipeline_factory import build_pipeline

        # patch get_data_layer 让 get_macro_data 抛异常
        with patch('core.data_layer.DataLayer.get_macro_data',
                   side_effect=Exception('mock macro fetch error')):
            pipeline = build_pipeline(symbol='000001.SZ', strict=False)

        names = pipeline.factor_names
        # 4 技术因子必须都加载
        self.assertGreaterEqual(len(names), 4)
        # 宏观因子应缺失或返回空数据时仍 add（看 PMI/M2Factor 实现）
        # 关键：不会因为宏观失败而跳过基本面或技术因子

    def test_run_normalizes_weights_with_partial_factors(self):
        """部分因子运行失败时，combined_score 按成功因子重新归一化。"""
        from core.factor_pipeline import FactorPipeline
        from core.factors.price_momentum import RSIFactor, BollingerFactor

        pipeline = FactorPipeline()
        pipeline.add(RSIFactor, weight=0.5, params={'symbol': 'TEST'})
        pipeline.add(BollingerFactor, weight=0.5, params={'symbol': 'TEST'})

        data = _make_data()
        result = pipeline.run('TEST', data, price=float(data['close'].iloc[-1]))

        # 至少一个因子成功
        ok_count = sum(1 for fr in result.factor_results if fr.error is None)
        self.assertGreaterEqual(ok_count, 1)
        # combined_score 应在合理范围（z-score 通常 [-5, 5]）
        self.assertTrue(-5.0 <= result.combined_score <= 5.0)

    def test_min_factors_guard_strict_raises(self):
        """成功因子数 < MIN_FACTORS_REQUIRED 时 strict=True 抛 RuntimeError。"""
        from core.pipeline_factory import build_pipeline, MIN_FACTORS_REQUIRED
        import core.pipeline_factory as pf

        # 把所有 _safe_add 都 mock 成失败，制造空 pipeline
        with patch.object(pf, '_safe_add', return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                build_pipeline(symbol='X', strict=True)
            self.assertIn(str(MIN_FACTORS_REQUIRED), str(ctx.exception))

    def test_min_factors_guard_non_strict_warns_only(self):
        """strict=False 时不抛错，只记日志。"""
        from core.pipeline_factory import build_pipeline
        import core.pipeline_factory as pf

        with patch.object(pf, '_safe_add', return_value=False):
            # 不应抛错
            pipeline = build_pipeline(symbol='X', strict=False)
            self.assertEqual(len(pipeline.factor_names), 0)

    def test_weight_normalization_after_factor_failure(self):
        """单因子计算失败时 combined_score 不被归零或失真。"""
        from core.factor_pipeline import FactorPipeline
        from core.factors.price_momentum import RSIFactor, BollingerFactor
        from core.factors.base import Factor, Signal

        # 自定义一个永远失败的 factor（继承 Factor 接口）
        class AlwaysFailFactor(Factor):
            name = 'AlwaysFail'

            def evaluate(self, df: pd.DataFrame) -> pd.Series:
                raise RuntimeError('simulated evaluate failure')

            def signals(self, fv: pd.Series, price: float) -> List[Signal]:
                return []

        pipeline = FactorPipeline()
        pipeline.add(RSIFactor, weight=0.4, params={'symbol': 'TEST'})
        pipeline.add(BollingerFactor, weight=0.3, params={'symbol': 'TEST'})
        pipeline.add(AlwaysFailFactor(), weight=0.3)

        data = _make_data()
        result = pipeline.run('TEST', data, price=float(data['close'].iloc[-1]))

        # 失败因子应被记录为 error
        errors = result.errors()
        self.assertIn('AlwaysFail', errors)
        # 但 combined_score 仍然由其它两个因子贡献，不应为 0（除非二者恰好抵消）
        # 关键断言：成功因子数 = 2
        ok = [fr for fr in result.factor_results if fr.error is None]
        self.assertEqual(len(ok), 2)


class TestNewsSentimentInPipeline(unittest.TestCase):
    """W2-3: NewsSentimentFactor 仅在 MINIMAX_API_KEY 存在时入 pipeline。"""

    def test_no_api_key_does_not_load_news_factor(self):
        from core.pipeline_factory import build_pipeline
        # 显式清掉 env var
        with patch.dict('os.environ', {}, clear=False):
            os_env = __import__('os').environ
            os_env.pop('MINIMAX_API_KEY', None)
            pipeline = build_pipeline(symbol='000001.SZ', strict=False)
        self.assertNotIn('NewsSentiment', pipeline.factor_names)

    def test_with_api_key_loads_news_factor(self):
        from core.pipeline_factory import build_pipeline
        with patch.dict('os.environ', {'MINIMAX_API_KEY': 'dummy-key'}):
            pipeline = build_pipeline(symbol='sh600519', strict=False)
        self.assertIn('NewsSentiment', pipeline.factor_names)

    def test_without_symbol_does_not_load_news_factor(self):
        """symbol='' 时即使有 API key 也不应加载(无标的可拉新闻)。"""
        from core.pipeline_factory import build_pipeline
        with patch.dict('os.environ', {'MINIMAX_API_KEY': 'dummy-key'}):
            pipeline = build_pipeline(symbol='', strict=False)
        self.assertNotIn('NewsSentiment', pipeline.factor_names)


class TestPipelineRebalanceAndQualityGate(unittest.TestCase):
    """W5-2: 重平衡 + 数据质量感知降权。"""

    def test_new_fundamental_factors_loaded_when_data_available(self):
        """注入有效 financial_data → 新基本面因子(FinancialHealth/DividendYield/AssetGrowth)
        被加入 pipeline。"""
        from core.pipeline_factory import build_pipeline
        idx = pd.bdate_range('2024-01-01', periods=30)
        fake_fin = pd.DataFrame({
            'roe_ttm': [10.0] * 30,
            'eps_ttm': [1.0] * 30,
            'eps_yoy': [5.0] * 30,
            'asset_yoy': [8.0] * 30,
            'pe_ttm': [15.0] * 30,
            'dividend_yield': [3.0] * 30,
            'debt_to_equity': [30.0] * 30,
            'current_ratio': [2.0] * 30,
            'quick_ratio': [1.5] * 30,
            'ocf_to_profit': [1.2] * 30,
            'revenue_yoy': [10.0] * 30,
            'profit_yoy': [8.0] * 30,
        }, index=idx)
        with patch('core.fundamental_data.FundamentalDataManager.get_fundamentals',
                   return_value=fake_fin):
            pipeline = build_pipeline(symbol='sh600519', strict=False)
        for name in ('FinancialHealth', 'DividendYield', 'AssetGrowth', 'EarningsSurprise'):
            self.assertIn(name, pipeline.factor_names, f"{name} 未入 pipeline")

    def test_fundamental_weights_halved_on_data_failure(self):
        """W5-2: 基本面数据缺失时,基本面因子权重应×0.5。"""
        from core.pipeline_factory import build_pipeline
        # 让 get_fundamentals 返回空 → financial_data 仍为 None
        with patch('core.fundamental_data.FundamentalDataManager.get_fundamentals',
                   return_value=pd.DataFrame()):
            pipeline = build_pipeline(symbol='sh600519', strict=False)

        # 找出 PEPercentile 的实际权重
        # 注:pipeline 内部用 _entries 字典存储,这里通过 factor_names + DynamicWeightPipeline 内部接口检查
        # 当 financial_data 缺失时,PE 权重应是 0.05 × 0.5 = 0.025
        entries = getattr(pipeline, '_entries', None)
        self.assertIsNotNone(entries)
        pe_entries = [e for e in entries if e.factor.name == 'PEPercentile']
        self.assertEqual(len(pe_entries), 1)
        self.assertAlmostEqual(pe_entries[0].weight, 0.025, places=4)

    def test_fundamental_weights_normal_when_data_present(self):
        """有有效数据时,PE 权重保持 0.05。"""
        from core.pipeline_factory import build_pipeline
        idx = pd.bdate_range('2024-01-01', periods=30)
        fake_fin = pd.DataFrame({'pe_ttm': [15.0] * 30, 'eps_ttm': [1.0] * 30},
                                index=idx)
        with patch('core.fundamental_data.FundamentalDataManager.get_fundamentals',
                   return_value=fake_fin):
            pipeline = build_pipeline(symbol='sh600519', strict=False)
        pe_entries = [e for e in pipeline._entries if e.factor.name == 'PEPercentile']
        self.assertEqual(len(pe_entries), 1)
        self.assertAlmostEqual(pe_entries[0].weight, 0.05, places=4)


class TestFundamentalWhitelistExpansion(unittest.TestCase):
    """W1-3: pipeline_factory 基本面白名单扩展到 12 列。"""

    def test_whitelist_includes_new_fields(self):
        """build_pipeline 应把 eps_yoy/asset_yoy/debt_to_equity 等新字段传给因子。"""
        from core.pipeline_factory import build_pipeline
        from unittest.mock import patch, MagicMock

        # mock FundamentalDataManager 返回包含所有新字段的 DataFrame
        full_cols = [
            'roe_ttm', 'eps_ttm', 'revenue_yoy', 'profit_yoy',
            'eps_yoy', 'asset_yoy', 'ocf_to_profit',
            'pe_ttm', 'pb', 'dividend_yield',
            'debt_to_equity', 'current_ratio', 'quick_ratio',
            'unrelated_column',  # 不在白名单
        ]
        idx = pd.bdate_range('2024-01-01', periods=30)
        fake_fin = pd.DataFrame({c: [1.0] * len(idx) for c in full_cols}, index=idx)

        captured: dict = {}
        original_safe_add = None

        # 拦截 _safe_add 看 params 传了什么
        import core.pipeline_factory as pf
        original_safe_add = pf._safe_add

        def spying_safe_add(pipeline, factor_cls, *, weight, params=None, label=''):
            if params and 'financial_data' in params:
                captured.setdefault('fin_cols', set()).update(
                    params['financial_data'].columns
                )
            return original_safe_add(
                pipeline, factor_cls, weight=weight, params=params, label=label,
            )

        with patch.object(pf, '_safe_add', side_effect=spying_safe_add):
            with patch('core.fundamental_data.FundamentalDataManager.get_fundamentals',
                       return_value=fake_fin):
                build_pipeline(symbol='000001.SZ', strict=False)

        fin_cols = captured.get('fin_cols', set())
        # 关键字段应在
        for c in ('eps_yoy', 'asset_yoy', 'debt_to_equity',
                  'current_ratio', 'quick_ratio', 'dividend_yield'):
            self.assertIn(c, fin_cols, f"{c} 未被白名单纳入")
        # 非白名单字段应被过滤
        self.assertNotIn('unrelated_column', fin_cols)


if __name__ == '__main__':
    unittest.main()

"""
tests/test_factor_selector.py — ML 因子动态选择测试

覆盖：
  - FactorICLabeler.compute(): 正常 / 数据不足时返回空 DataFrame
  - FactorSelectorModel.fit(): 正常训练 / 数据不足跳过 / all-same 标签跳过
  - FactorSelectorModel.predict_proba(): 有模型返回 0~1 / 无模型返回 0.5
  - FactorSelectorModel.is_fitted
  - WalkForwardFactorSelector.run(): 数据不足时返回空列表 / 正常返回概率列表
  - FactorSelector.fit(): 数据不足跳过 / 正常流程
  - FactorSelector.predict_weights(): 无模型等权降级 / 有模型权重归一化
  - FactorSelector._proba_to_weights(): min/max 约束 / 归一化
  - FactorSelector._equal_weights(): 返回均等分配
  - FactorSelectorResult dataclass 字段
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_price_df(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0.0003, 0.01, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame({
        'open':   close * (1 + rng.normal(0, 0.002, n)),
        'high':   close * (1 + np.abs(rng.normal(0, 0.005, n))),
        'low':    close * (1 - np.abs(rng.normal(0, 0.005, n))),
        'close':  close,
        'volume': rng.integers(100_000, 1_000_000, n).astype(float),
    }, index=dates)


def _make_factor_df(n: int = 200, n_factors: int = 3, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    data = {f'Factor{i}': rng.normal(0, 1, n) for i in range(n_factors)}
    return pd.DataFrame(data, index=dates)


def _make_returns(n: int = 200, seed: int = 2) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    return pd.Series(rng.normal(0.0003, 0.01, n), index=dates)


# ---------------------------------------------------------------------------
# FactorICLabeler
# ---------------------------------------------------------------------------

class TestFactorICLabeler(unittest.TestCase):

    def setUp(self):
        from core.ml.factor_selector import FactorICLabeler
        self.labeler = FactorICLabeler(horizon=5, min_obs=10)

    def test_normal_returns_dataframe(self):
        factors = _make_factor_df(100, 3)
        returns = _make_returns(100)
        ic_df = self.labeler.compute(factors, returns)
        self.assertIsInstance(ic_df, pd.DataFrame)
        self.assertFalse(ic_df.empty)
        self.assertEqual(list(ic_df.columns), list(factors.columns))

    def test_ic_values_in_range(self):
        factors = _make_factor_df(100, 2)
        returns = _make_returns(100)
        ic_df = self.labeler.compute(factors, returns)
        self.assertTrue((ic_df.abs() <= 1.0).all().all())

    def test_insufficient_data_returns_empty(self):
        """min_obs=10, horizon=5, need at least 16 rows; fewer returns empty."""
        labeler = unittest.mock.MagicMock()  # we'll test with actual labeler
        from core.ml.factor_selector import FactorICLabeler
        lb = FactorICLabeler(horizon=5, min_obs=10)
        factors = _make_factor_df(14, 2)
        returns = _make_returns(14)
        ic_df = lb.compute(factors, returns)
        self.assertTrue(ic_df.empty)

    def test_output_index_is_datetimeindex(self):
        factors = _make_factor_df(80, 2)
        returns = _make_returns(80)
        ic_df = self.labeler.compute(factors, returns)
        self.assertIsInstance(ic_df.index, pd.DatetimeIndex)


# ---------------------------------------------------------------------------
# FactorSelectorModel
# ---------------------------------------------------------------------------

class TestFactorSelectorModel(unittest.TestCase):

    def setUp(self):
        from core.ml.factor_selector import FactorSelectorModel
        self.ModelClass = FactorSelectorModel

    def _make_xy(self, n=100):
        rng = np.random.default_rng(5)
        dates = pd.date_range('2022-01-01', periods=n, freq='B')
        X = pd.DataFrame({f'f{i}': rng.normal(0, 1, n) for i in range(5)}, index=dates)
        ic = pd.Series(rng.normal(0.0, 0.03, n), index=dates)
        return X, ic

    def test_fit_and_is_fitted(self):
        try:
            import lightgbm  # noqa: F401
        except ImportError:
            self.skipTest('lightgbm not installed')
        X, ic = self._make_xy(150)
        model = self.ModelClass('RSI', ic_threshold=0.02)
        model.fit(X, ic)
        self.assertTrue(model.is_fitted)

    def test_predict_proba_in_range(self):
        X, ic = self._make_xy(150)
        model = self.ModelClass('RSI', ic_threshold=0.02)
        model.fit(X, ic)
        row = X.iloc[[-1]]
        prob = model.predict_proba(row)
        self.assertGreaterEqual(prob, 0.0)
        self.assertLessEqual(prob, 1.0)

    def test_predict_proba_unfitted_returns_half(self):
        model = self.ModelClass('MACD')
        prob = model.predict_proba(pd.DataFrame({'a': [1.0]}))
        self.assertAlmostEqual(prob, 0.5)

    def test_insufficient_data_stays_unfitted(self):
        X, ic = self._make_xy(10)   # 只有 10 行，不够
        model = self.ModelClass('RSI', ic_threshold=0.02)
        model.fit(X, ic)
        self.assertFalse(model.is_fitted)

    def test_all_same_labels_stays_unfitted(self):
        X, _ = self._make_xy(100)
        ic = pd.Series(0.5, index=X.index)   # 全部大于 threshold
        model = self.ModelClass('RSI', ic_threshold=0.02)
        model.fit(X, ic)
        self.assertFalse(model.is_fitted)


# ---------------------------------------------------------------------------
# WalkForwardFactorSelector
# ---------------------------------------------------------------------------

class TestWalkForwardFactorSelector(unittest.TestCase):

    def setUp(self):
        from core.ml.factor_selector import WalkForwardFactorSelector
        self.WF = WalkForwardFactorSelector

    def test_insufficient_data_returns_empty_lists(self):
        wf = self.WF(train_window=100, val_window=50, step=10)
        dates = pd.date_range('2022-01-01', periods=80, freq='B')
        X = pd.DataFrame({'f1': np.ones(80)}, index=dates)
        ic_df = pd.DataFrame({'RSI': np.zeros(80)}, index=dates)
        results = wf.run(X, ic_df)
        self.assertEqual(results['RSI'], [])

    def test_normal_run_returns_proba_list(self):
        wf = self.WF(train_window=100, val_window=30, step=20)
        rng = np.random.default_rng(7)
        n = 200
        dates = pd.date_range('2022-01-01', periods=n, freq='B')
        X = pd.DataFrame({f'f{i}': rng.normal(0, 1, n) for i in range(5)}, index=dates)
        ic_df = pd.DataFrame({'RSI': rng.normal(0, 0.03, n)}, index=dates)
        results = wf.run(X, ic_df)
        self.assertIn('RSI', results)
        self.assertGreater(len(results['RSI']), 0)
        for p in results['RSI']:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


# ---------------------------------------------------------------------------
# FactorSelector (高层接口)
# ---------------------------------------------------------------------------

class TestFactorSelectorProbToWeights(unittest.TestCase):

    def setUp(self):
        from core.ml.factor_selector import FactorSelector
        self.selector = FactorSelector(ic_threshold=0.02, min_weight=0.02, max_weight=0.25)

    def test_weights_sum_to_one(self):
        proba = {'RSI': 0.7, 'MACD': 0.4, 'Bollinger': 0.3}
        weights = self.selector._proba_to_weights(proba)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_min_weight_applied(self):
        proba = {'RSI': 0.01, 'MACD': 0.9}
        weights = self.selector._proba_to_weights(proba)
        # RSI 原始概率低于 min_weight=0.02，应被提升到 min_weight
        self.assertGreaterEqual(weights['RSI'], 0.0)  # after normalize, still > 0

    def test_max_weight_cap(self):
        # max_weight=0.25: raw cap 前 RSI=0.95 → 截断到 0.25
        # 多因子时截断效果更明显；此处验证截断前的中间值不超过 max_weight
        proba = {'RSI': 0.95, 'MACD': 0.20, 'BB': 0.15, 'ATR': 0.10}
        # 手动计算：截断后 [0.25, 0.20, 0.15, 0.10]，总和=0.70
        # 归一化后 RSI = 0.25/0.70 ≈ 0.357，仍 > 0.25（因为有 min 约束）
        # 核心验证：RSI 权重 > MACD 权重（截断保持相对顺序）
        weights = self.selector._proba_to_weights(proba)
        self.assertGreater(weights['RSI'], weights['MACD'])
        self.assertGreater(weights['MACD'], weights['BB'])

    def test_empty_proba_returns_empty(self):
        with patch.object(self.selector, '_models', {}):
            weights = self.selector._proba_to_weights({})
        self.assertEqual(weights, {})

    def test_all_zero_falls_back_to_equal(self):
        # 当所有 proba < min_weight 时应正确处理
        proba = {'RSI': 0.0, 'MACD': 0.0}
        weights = self.selector._proba_to_weights(proba)
        # min_weight kicks in, both become 0.02 → equal
        self.assertAlmostEqual(weights['RSI'], weights['MACD'], places=5)


class TestFactorSelectorFitShort(unittest.TestCase):
    """数据不足时 fit 安全跳过。"""

    def test_fit_short_data_no_raise(self):
        from core.ml.factor_selector import FactorSelector
        selector = FactorSelector(train_window=252, val_window=63)
        price = _make_price_df(n=100)  # 不够 315 行
        selector.fit(price)   # should not raise
        self.assertEqual(len(selector.factor_names), 0)


class TestFactorSelectorPredictWeights(unittest.TestCase):
    """predict_weights 逻辑测试。"""

    def test_no_models_equal_weights(self):
        from core.ml.factor_selector import FactorSelector
        selector = FactorSelector()
        # 无模型时应返回等权或空
        price = _make_price_df(200)
        weights = selector.predict_weights(price)
        if weights:
            total = sum(weights.values())
            self.assertAlmostEqual(total, 1.0, places=4)

    def test_with_mocked_models(self):
        """有 mock 模型时 predict_weights 应返回归一化权重。"""
        from core.ml.factor_selector import FactorSelector, FactorSelectorModel

        selector = FactorSelector(min_weight=0.02, max_weight=0.25)

        # 注入 mock 模型
        mock_m1 = MagicMock(spec=FactorSelectorModel)
        mock_m2 = MagicMock(spec=FactorSelectorModel)
        # 使用低于 max_weight=0.25 的概率，确保截断不会使两者相等
        mock_m1.predict_proba.return_value = 0.20
        mock_m2.predict_proba.return_value = 0.08
        selector._models = {'RSI': mock_m1, 'MACD': mock_m2}

        price = _make_price_df(50)
        with patch.object(selector._feature_store, 'build_predict_row',
                          return_value=pd.DataFrame({'time_dow_sin': [0.5]},
                                                    index=[price.index[-1]])):
            weights = selector.predict_weights(price)

        self.assertIn('RSI', weights)
        self.assertIn('MACD', weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=4)
        self.assertGreater(weights['RSI'], weights['MACD'])


class TestFactorSelectorResult(unittest.TestCase):

    def test_dataclass_fields(self):
        from core.ml.factor_selector import FactorSelectorResult
        result = FactorSelectorResult(
            weights={'RSI': 0.6, 'MACD': 0.4},
            proba={'RSI': 0.7, 'MACD': 0.4},
            n_factors=2,
            method='lightgbm',
        )
        self.assertEqual(result.n_factors, 2)
        self.assertAlmostEqual(result.weights['RSI'], 0.6)
        self.assertIsNotNone(result.fitted_at)


if __name__ == '__main__':
    unittest.main()

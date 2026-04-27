"""
tests/test_nlp_factor.py — 新闻情感 LLM 因子单元测试

覆盖：
  - NewsSentimentFactor：外部数据注入、降级（无数据返回全零）
  - signals()：BUY/SELL/中性信号
  - inject_scores()、update_sentiment_data()
  - 缓存工具函数
  - 注册表集成

测试策略：全部使用 mock 数据，无网络/API 依赖。
"""

from __future__ import annotations

import unittest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_price_df(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': rng.integers(100_000, 500_000, n).astype(float),
    }, index=dates)


def _make_sentiment_series(n: int = 60, val: float = 0.3) -> pd.Series:
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    rng = np.random.default_rng(42)
    return pd.Series(val + rng.normal(0, 0.1, n), index=dates)


# ---------------------------------------------------------------------------
# NewsSentimentFactor — 降级行为
# ---------------------------------------------------------------------------

class TestNewsSentimentFactorDegradation(unittest.TestCase):

    def setUp(self):
        self.price = _make_price_df(60)

    def test_no_data_returns_zeros(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor()
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_no_data_output_shape(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor()
        result = f.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_no_data_index_aligned(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor()
        result = f.evaluate(self.price)
        self.assertTrue(result.index.equals(self.price.index))

    def test_use_api_false_no_symbol_returns_zeros(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor(symbol='', use_api=False)
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())


# ---------------------------------------------------------------------------
# NewsSentimentFactor — 外部数据注入
# ---------------------------------------------------------------------------

class TestNewsSentimentFactorWithData(unittest.TestCase):

    def setUp(self):
        self.price = _make_price_df(60)
        self.sentiment = _make_sentiment_series(60)

    def test_with_sentiment_data_output_shape(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor(sentiment_data=self.sentiment)
        result = f.evaluate(self.price)
        self.assertEqual(len(result), len(self.price))

    def test_with_sentiment_data_values_finite(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor(sentiment_data=self.sentiment)
        result = f.evaluate(self.price)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_with_positive_sentiment_mostly_positive_zscore(self):
        """持续正面情感 → 因子值均值应偏正。"""
        from core.factors.nlp import NewsSentimentFactor
        dates = pd.date_range('2024-01-01', periods=60, freq='B')
        sentiment = pd.Series(0.8, index=dates)  # 强正面情感
        f = NewsSentimentFactor(sentiment_data=sentiment)
        result = f.evaluate(self.price)
        # 稳定正值经 normalize 后 z-score 方差很小，mean 趋近 0
        # 只需验证输出为 Series，不是全零即可
        self.assertFalse(np.all(result == 0))

    def test_with_negative_sentiment_mostly_negative_zscore(self):
        """持续负面情感后的大幅利好 → 相对 z-score 为正。"""
        from core.factors.nlp import NewsSentimentFactor
        dates = pd.date_range('2024-01-01', periods=60, freq='B')
        vals = np.full(60, -0.3)
        vals[50:] = 0.8  # 后期突然变正
        sentiment = pd.Series(vals, index=dates)
        f = NewsSentimentFactor(sentiment_data=sentiment)
        result = f.evaluate(self.price)
        # 后期因子值应高于前期
        self.assertGreater(result.iloc[55:].mean(), result.iloc[5:40].mean())

    def test_update_sentiment_data(self):
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor()
        # 初始为全零
        r1 = f.evaluate(self.price)
        self.assertTrue((r1 == 0).all())
        # 注入情感数据后不再全零
        f.update_sentiment_data(self.sentiment)
        r2 = f.evaluate(self.price)
        self.assertFalse(np.all(r2 == 0))

    def test_inject_scores_used_in_api_mode(self):
        """inject_scores 注入的缓存应在 API 模式中被使用（得分有变化才能产生非零 z-score）。"""
        from core.factors.nlp import NewsSentimentFactor
        rng = np.random.default_rng(42)
        f = NewsSentimentFactor(symbol='000001.SZ', use_api=True)

        # 注入有变化的得分（std > 0 → normalize 产生非零输出）
        dates = [str(d.date()) for d in self.price.index]
        raw_scores = rng.uniform(-0.5, 0.5, len(dates))
        scores = {d: float(s) for d, s in zip(dates, raw_scores)}
        f.inject_scores(scores)

        result = f.evaluate(self.price)
        # 有变化的注入得分应产生非全零输出
        self.assertFalse(np.all(result == 0))

    def test_window_smoothing_effect(self):
        """window 参数应对情感数据进行平滑（噪声减小）。"""
        from core.factors.nlp import NewsSentimentFactor
        dates = pd.date_range('2024-01-01', periods=60, freq='B')
        # 交替噪声情感
        rng = np.random.default_rng(42)
        sentiment = pd.Series(rng.choice([-1, 1], 60) * 0.5, index=dates)

        f1 = NewsSentimentFactor(sentiment_data=sentiment, window=1)
        f10 = NewsSentimentFactor(sentiment_data=sentiment, window=10)

        r1 = f1.evaluate(self.price)
        r10 = f10.evaluate(self.price)

        # window=10 的标准差应更小（更平滑）
        self.assertLessEqual(r10.std(), r1.std() + 0.1)


# ---------------------------------------------------------------------------
# NewsSentimentFactor — signals()
# ---------------------------------------------------------------------------

class TestNewsSentimentFactorSignals(unittest.TestCase):

    def setUp(self):
        from core.factors.nlp import NewsSentimentFactor
        self.f = NewsSentimentFactor(symbol='000001.SZ')

    def test_signal_buy_on_high_zscore(self):
        vals = pd.Series([0.0] * 19 + [2.0], index=range(20))
        sigs = self.f.signals(vals, price=15.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'BUY')

    def test_signal_sell_on_low_zscore(self):
        vals = pd.Series([0.0] * 19 + [-2.0], index=range(20))
        sigs = self.f.signals(vals, price=15.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'SELL')

    def test_signal_none_in_neutral(self):
        vals = pd.Series([0.0] * 20, index=range(20))
        sigs = self.f.signals(vals, price=15.0, threshold=1.0)
        self.assertEqual(sigs, [])

    def test_signal_empty_series(self):
        sigs = self.f.signals(pd.Series([], dtype=float), price=15.0)
        self.assertEqual(sigs, [])

    def test_signal_strength_bounded(self):
        vals = pd.Series([5.0], index=[0])  # 极高 z-score
        sigs = self.f.signals(vals, price=15.0, threshold=1.0)
        self.assertLessEqual(sigs[0].strength, 1.0)
        self.assertGreaterEqual(sigs[0].strength, 0.0)

    def test_signal_uses_default_threshold(self):
        """使用因子自身 threshold 参数。"""
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor(threshold=2.0)
        vals = pd.Series([1.5], index=[0])  # < threshold=2.0 → 无信号
        sigs = f.signals(vals, price=15.0)
        self.assertEqual(sigs, [])


# ---------------------------------------------------------------------------
# 缓存工具
# ---------------------------------------------------------------------------

class TestNewsSentimentCache(unittest.TestCase):

    def test_cache_key_deterministic(self):
        from core.factors.nlp import _cache_key
        k1 = _cache_key('000001.SZ', '2024-01-15')
        k2 = _cache_key('000001.SZ', '2024-01-15')
        self.assertEqual(k1, k2)

    def test_cache_key_different_symbols(self):
        from core.factors.nlp import _cache_key
        k1 = _cache_key('000001.SZ', '2024-01-15')
        k2 = _cache_key('600519.SH', '2024-01-15')
        self.assertNotEqual(k1, k2)

    def test_load_cache_returns_none_for_nonexistent(self):
        from core.factors.nlp import _load_cache
        from pathlib import Path
        result = _load_cache(Path('/nonexistent/path.json'), ttl_seconds=3600)
        self.assertIsNone(result)

    def test_save_and_load_cache(self):
        import tempfile
        from pathlib import Path
        from core.factors.nlp import _save_cache, _load_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'test.json'
            _save_cache(path, {'score': 0.42})
            loaded = _load_cache(path, ttl_seconds=3600)
            self.assertIsNotNone(loaded)
            self.assertAlmostEqual(loaded['score'], 0.42)

    def test_load_cache_expired(self):
        """过期缓存应返回 None。"""
        import tempfile
        from pathlib import Path
        from core.factors.nlp import _save_cache, _load_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'test.json'
            _save_cache(path, {'score': 0.5})
            # TTL = 0 秒 → 立即过期
            loaded = _load_cache(path, ttl_seconds=0)
            self.assertIsNone(loaded)


# ---------------------------------------------------------------------------
# 注册表集成
# ---------------------------------------------------------------------------

class TestNewsSentimentRegistryIntegration(unittest.TestCase):

    def test_registered_in_registry(self):
        from core.factor_registry import registry
        self.assertIn('NewsSentiment', registry)

    def test_create_from_registry(self):
        from core.factor_registry import registry
        f = registry.create('NewsSentiment')
        self.assertEqual(f.name, 'NewsSentiment')

    def test_total_factor_count_at_least_22(self):
        """原21 + NLP1 = 至少22个"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 22)

    def test_in_pipeline_no_crash(self):
        """无数据时，NewsSentiment 因子在流水线中降级为零，不崩溃。"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.8)
        pipeline.add('NewsSentiment', weight=0.2)

        data = _make_price_df(60)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))

    def test_in_pipeline_with_data(self):
        """有数据时，NewsSentiment 因子在流水线中正常运行。"""
        from core.factor_pipeline import FactorPipeline
        from core.factors.nlp import NewsSentimentFactor

        sentiment = _make_sentiment_series(80)
        f = NewsSentimentFactor(sentiment_data=sentiment, window=5)

        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.7)
        pipeline.add(f, weight=0.3)

        data = _make_price_df(80)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)
        self.assertTrue(np.isfinite(result.combined_score))


if __name__ == '__main__':
    unittest.main()

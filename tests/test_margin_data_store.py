"""
tests/test_margin_data_store.py — MarginDataStore 及融资融券因子自动拉取测试

覆盖：
  - MarginDataStore._normalize(): 各种 AKShare 原始列名归一化
  - MarginDataStore.get(): 内存缓存命中
  - MarginDataStore.get(): Parquet 命中（TTL 未过期）
  - MarginDataStore.get(): AKShare 拉取成功 → 写入 Parquet
  - MarginDataStore.get(): AKShare 失败 → 返回空 DataFrame
  - MarginDataStore.invalidate(): 清除内存缓存
  - MarginDataStore._slice(): start 参数过滤
  - MarginTradingFactor: sentiment_data=None + symbol → 调用 MarginDataStore
  - MarginTradingFactor: AKShare 不可达 → 降级全零
  - ShortInterestFactor: sentiment_data=None + symbol → 调用 MarginDataStore
  - ShortInterestFactor: AKShare 不可达 → 降级全零
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _make_price_df(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    dates = pd.date_range('2023-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        'open': close, 'high': close * 1.01,
        'low': close * 0.99, 'close': close,
        'volume': rng.integers(1_000, 100_000, n).astype(float),
    }, index=dates)


def _make_raw_akshare(n: int = 60, use_cn_cols: bool = False) -> pd.DataFrame:
    """模拟 AKShare stock_margin_detail 原始输出。"""
    dates = pd.date_range('2023-01-01', periods=n, freq='B')
    rng = np.random.default_rng(1)
    vals_m = 1e10 + np.cumsum(rng.normal(0, 1e8, n))
    vals_s = 5e8  + np.cumsum(rng.normal(0, 1e7, n))
    if use_cn_cols:
        return pd.DataFrame({
            '信用交易日期': dates,
            '融资余额': vals_m,
            '融券余额': vals_s,
        })
    return pd.DataFrame({
        'date': dates,
        'rz_ye': vals_m,
        'rq_ye': vals_s,
    })


class TestMarginDataStoreNormalize(unittest.TestCase):
    """_normalize 列名归一化测试。"""

    def setUp(self):
        from core.factors.sentiment import MarginDataStore
        self.store = MarginDataStore()

    def test_english_cols(self):
        raw = _make_raw_akshare(10, use_cn_cols=False)
        df = self.store._normalize(raw)
        self.assertIn('margin_balance', df.columns)
        self.assertIn('short_balance', df.columns)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(len(df), 10)

    def test_chinese_cols(self):
        raw = _make_raw_akshare(10, use_cn_cols=True)
        df = self.store._normalize(raw)
        self.assertIn('margin_balance', df.columns)
        self.assertIn('short_balance', df.columns)

    def test_sorted_ascending(self):
        raw = _make_raw_akshare(20, use_cn_cols=False)
        raw = raw.iloc[::-1].reset_index(drop=True)  # reverse order
        df = self.store._normalize(raw)
        self.assertTrue(df.index.is_monotonic_increasing)

    def test_numeric_values(self):
        raw = _make_raw_akshare(5)
        df = self.store._normalize(raw)
        self.assertTrue(np.isfinite(df['margin_balance'].values).all())


class TestMarginDataStoreCache(unittest.TestCase):
    """内存缓存 / Parquet 缓存 / 网络拉取流程测试。"""

    def setUp(self):
        from core.factors.sentiment import MarginDataStore
        self.tmp = tempfile.mkdtemp()
        self.store = MarginDataStore()
        # 重定向 Parquet 目录到临时目录
        import core.factors.sentiment as _mod
        self._orig_dir = _mod._SENTIMENT_DIR
        _mod._SENTIMENT_DIR = self.tmp

    def tearDown(self):
        import core.factors.sentiment as _mod
        _mod._SENTIMENT_DIR = self._orig_dir

    # --- 内存缓存命中 ---
    def test_memory_cache_hit(self):
        df_cached = _make_raw_akshare(10)
        df_cached = self.store._normalize(df_cached)
        self.store._memory['000001.SZ'] = (datetime.now(), df_cached)

        with patch.object(self.store, '_fetch') as mock_fetch, \
             patch.object(self.store, '_load_parquet') as mock_parq:
            result = self.store.get('000001.SZ')

        mock_fetch.assert_not_called()
        mock_parq.assert_not_called()
        self.assertFalse(result.empty)

    # --- 内存缓存过期后走 Parquet ---
    def test_memory_expired_falls_to_parquet(self):
        from core.factors.sentiment import _MARGIN_TTL_HOURS
        df_cached = self.store._normalize(_make_raw_akshare(10))
        old_time = datetime.now() - timedelta(hours=_MARGIN_TTL_HOURS + 1)
        self.store._memory['000001.SZ'] = (old_time, df_cached)

        df_parquet = self.store._normalize(_make_raw_akshare(15))
        with patch.object(self.store, '_load_parquet', return_value=df_parquet) as mock_parq, \
             patch.object(self.store, '_fetch') as mock_fetch:
            result = self.store.get('000001.SZ')

        mock_parq.assert_called_once()
        mock_fetch.assert_not_called()
        self.assertEqual(len(result), 15)

    # --- Parquet 未命中 → 网络拉取 ---
    def test_network_fetch_on_parquet_miss(self):
        raw = _make_raw_akshare(20)

        with patch.object(self.store, '_load_parquet', return_value=None), \
             patch.object(self.store, '_fetch',
                          return_value=self.store._normalize(raw)) as mock_fetch, \
             patch.object(self.store, '_save_parquet') as mock_save:
            result = self.store.get('600519.SH')

        mock_fetch.assert_called_once_with('600519.SH')
        mock_save.assert_called_once()
        self.assertEqual(len(result), 20)

    # --- AKShare 失败 → 空 DataFrame ---
    def test_akshare_failure_returns_empty(self):
        with patch.object(self.store, '_load_parquet', return_value=None), \
             patch.object(self.store, '_fetch', return_value=None):
            result = self.store.get('000001.SZ')

        self.assertTrue(result.empty)

    # --- invalidate ---
    def test_invalidate_clears_memory(self):
        df = self.store._normalize(_make_raw_akshare(5))
        self.store._memory['000001.SZ'] = (datetime.now(), df)
        self.store.invalidate('000001.SZ')
        self.assertNotIn('000001.SZ', self.store._memory)

    # --- _slice ---
    def test_slice_by_start(self):
        df = self.store._normalize(_make_raw_akshare(60))
        sliced = self.store._slice(df, '2023-03-01')
        self.assertTrue((sliced.index >= pd.Timestamp('2023-03-01')).all())

    def test_slice_none_returns_all(self):
        df = self.store._normalize(_make_raw_akshare(60))
        sliced = self.store._slice(df, None)
        self.assertEqual(len(sliced), len(df))


class TestMarginTradingFactorAutoFetch(unittest.TestCase):
    """MarginTradingFactor 自动拉取路径测试。"""

    def setUp(self):
        self.price = _make_price_df(60)

    def _make_margin_df(self, n=60):
        rng = np.random.default_rng(2)
        dates = pd.date_range('2023-01-01', periods=n, freq='B')
        vals = 1e10 + np.cumsum(rng.normal(0, 1e8, n))
        return pd.DataFrame({'margin_balance': vals, 'short_balance': vals * 0.05},
                            index=dates)

    def test_auto_fetch_called_when_no_sentiment_data(self):
        """symbol 有值且 sentiment_data=None 时应调用 MarginDataStore.get。"""
        from core.factors.sentiment import MarginTradingFactor
        margin_df = self._make_margin_df()

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            mock_instance = MagicMock()
            mock_instance.get.return_value = margin_df
            MockStore.return_value = mock_instance

            f = MarginTradingFactor(symbol='000001.SZ')
            result = f.evaluate(self.price)

        mock_instance.get.assert_called_once()
        self.assertEqual(len(result), len(self.price))
        self.assertFalse(result.isna().any())

    def test_sentiment_data_takes_priority(self):
        """sentiment_data 显式传入时不调用 MarginDataStore。"""
        from core.factors.sentiment import MarginTradingFactor
        sent = self._make_margin_df()

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            f = MarginTradingFactor(sentiment_data=sent, symbol='000001.SZ')
            f.evaluate(self.price)

        MockStore.assert_not_called()

    def test_fallback_to_zero_when_auto_fetch_fails(self):
        """MarginDataStore 返回空 DataFrame 时因子降级为全零。"""
        from core.factors.sentiment import MarginTradingFactor

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            mock_instance = MagicMock()
            mock_instance.get.return_value = pd.DataFrame()
            MockStore.return_value = mock_instance

            f = MarginTradingFactor(symbol='000001.SZ')
            result = f.evaluate(self.price)

        self.assertTrue((result == 0.0).all())

    def test_no_symbol_no_fetch(self):
        """symbol='' 且 sentiment_data=None 时直接返回全零，不调用 MarginDataStore。"""
        from core.factors.sentiment import MarginTradingFactor

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            f = MarginTradingFactor()
            result = f.evaluate(self.price)

        MockStore.assert_not_called()
        self.assertTrue((result == 0.0).all())


class TestShortInterestFactorAutoFetch(unittest.TestCase):
    """ShortInterestFactor 自动拉取路径测试。"""

    def setUp(self):
        self.price = _make_price_df(60)

    def _make_short_df(self, n=60):
        rng = np.random.default_rng(3)
        dates = pd.date_range('2023-01-01', periods=n, freq='B')
        vals = 5e8 + np.cumsum(rng.normal(0, 1e7, n))
        return pd.DataFrame({'margin_balance': vals * 20, 'short_balance': vals},
                            index=dates)

    def test_auto_fetch_called(self):
        from core.factors.sentiment import ShortInterestFactor
        short_df = self._make_short_df()

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            mock_instance = MagicMock()
            mock_instance.get.return_value = short_df
            MockStore.return_value = mock_instance

            f = ShortInterestFactor(symbol='000001.SZ')
            result = f.evaluate(self.price)

        mock_instance.get.assert_called_once()
        self.assertEqual(len(result), len(self.price))

    def test_fallback_to_zero_on_failure(self):
        from core.factors.sentiment import ShortInterestFactor

        with patch('core.factors.sentiment.MarginDataStore') as MockStore:
            mock_instance = MagicMock()
            mock_instance.get.return_value = pd.DataFrame()
            MockStore.return_value = mock_instance

            f = ShortInterestFactor(symbol='000001.SZ')
            result = f.evaluate(self.price)

        self.assertTrue((result == 0.0).all())


if __name__ == '__main__':
    unittest.main()

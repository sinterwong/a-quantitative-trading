"""
test_nlp_batch.py — P1-9 NLP 因子工业化测试

验证：
  1. NewsSentimentFactor.evaluate 优先从 Parquet 缓存读取
  2. 缓存不存在时回退到原有路径（API / 零）
  3. nlp_batch_score.run_batch 写入 Parquet
  4. 重复运行合并历史（不丢失旧数据）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


def _make_price_data(n: int = 20) -> pd.DataFrame:
    dates = pd.date_range('2026-04-01', periods=n, freq='B')
    close = 10 + np.arange(n) * 0.1
    return pd.DataFrame({
        'open': close, 'high': close * 1.01,
        'low': close * 0.99, 'close': close, 'volume': 1e6,
    }, index=dates)


class TestNLPParquetPriority(unittest.TestCase):

    def test_evaluate_uses_parquet_when_present(self):
        """outputs/nlp_sentiment/{symbol}.parquet 存在时优先读。"""
        from core.factors.nlp import NewsSentimentFactor

        df_data = _make_price_data(n=10)
        symbol = '__TESTNLP_SYM__'
        # 写一个 mock parquet（值全 = 0.5）
        sentiment = pd.Series(
            [0.5] * 10, index=df_data.index, name='score',
        )
        sentiment_df = sentiment.to_frame()
        sentiment_df.index.name = 'date'

        cache_dir = PROJ_ROOT / 'outputs' / 'nlp_sentiment'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f'{symbol}.parquet'
        try:
            sentiment_df.reset_index().to_parquet(cache_path, index=False)
            f = NewsSentimentFactor(symbol=symbol, use_api=False)
            result = f.evaluate(df_data)
            # z-score 归一化后应非全零（除非全相等才返回 0）— 这里全 0.5 实际归一化后 = 0
            # 但是 _sentiment_data 应被设置
            self.assertIsNotNone(f._sentiment_data)
            self.assertEqual(len(result), len(df_data))
        finally:
            cache_path.unlink(missing_ok=True)

    def test_evaluate_falls_through_when_no_parquet(self):
        """无 parquet + 无 API + 无注入数据 → 全零。"""
        from core.factors.nlp import NewsSentimentFactor
        f = NewsSentimentFactor(symbol='__NONEXIST_SYM_99__', use_api=False)
        df_data = _make_price_data()
        result = f.evaluate(df_data)
        self.assertTrue((result == 0).all())


class TestNlpBatchScript(unittest.TestCase):

    def test_run_batch_writes_parquet(self):
        from scripts.nlp_batch_score import run_batch

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # mock _score_one_symbol 避免真的调 API
            with patch('scripts.nlp_batch_score._score_one_symbol') as mock_score:
                mock_score.return_value = pd.Series(
                    [0.1, 0.2, 0.3],
                    index=pd.to_datetime(
                        ['2026-04-01', '2026-04-02', '2026-04-03']
                    ),
                    name='score',
                )
                result = run_batch(
                    symbols=['A.SH'], days=5, use_api=False,
                    output_dir=out,
                )

            self.assertEqual(len(result), 1)
            self.assertIn('A.SH', result)
            files = list(out.glob('A.SH.parquet'))
            self.assertEqual(len(files), 1)
            df = pd.read_parquet(files[0])
            self.assertIn('score', df.columns)
            self.assertEqual(len(df), 3)

    def test_run_batch_merges_existing(self):
        """二次运行应合并历史，不丢失旧记录。"""
        from scripts.nlp_batch_score import run_batch

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # 第一次写
            with patch('scripts.nlp_batch_score._score_one_symbol') as ms:
                ms.return_value = pd.Series(
                    [0.1, 0.2],
                    index=pd.to_datetime(['2026-04-01', '2026-04-02']),
                    name='score',
                )
                run_batch(symbols=['A.SH'], days=2, use_api=False,
                          output_dir=out)
            # 第二次写（不同日期）
            with patch('scripts.nlp_batch_score._score_one_symbol') as ms:
                ms.return_value = pd.Series(
                    [0.5, 0.6],
                    index=pd.to_datetime(['2026-04-03', '2026-04-04']),
                    name='score',
                )
                run_batch(symbols=['A.SH'], days=2, use_api=False,
                          output_dir=out)

            df = pd.read_parquet(out / 'A.SH.parquet')
            # 应包含 4 个日期（2 旧 + 2 新合并）
            self.assertEqual(len(df), 4)


if __name__ == '__main__':
    unittest.main()

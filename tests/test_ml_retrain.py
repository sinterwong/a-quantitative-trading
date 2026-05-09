"""
test_ml_retrain.py — P1-8 ML 重训机制测试

验证：
  1. _bars_since_train 在每次 evaluate() 调用后递增
  2. retrain_every=0 时不自动触发重训
  3. retrain_every>0 且数据充足时自动调用 fit()
  4. 自动重训失败时不打断 evaluate（返回零序列）
  5. ml_train_all 脚本 _train_one 的核心路径
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _make_data(n: int = 350) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10 + np.cumsum(rng.normal(0, 0.05, n))
    return pd.DataFrame({
        'open': close * 0.999, 'high': close * 1.005,
        'low': close * 0.995, 'close': close, 'volume': 1e6,
    }, index=dates)


class TestBarsCounter(unittest.TestCase):

    def test_bars_since_train_increments(self):
        """_bars_since_train 在每次 evaluate() 后递增。"""
        from core.ml.price_predictor import MLPredictionFactor

        factor = MLPredictionFactor(symbol='', retrain_every=0)
        # 假装已 fit（注入虚假 predictor 让 evaluate 不进 retrain 分支）
        factor._predictor = MagicMock()
        factor._predictor.predict_proba.return_value = np.array([0.5] * 10)
        factor._feature_names = []

        df = _make_data(n=10)
        # 第一次
        before = factor._bars_since_train
        factor.evaluate(df)
        self.assertEqual(factor._bars_since_train, before + 1)
        # 第二次
        factor.evaluate(df)
        self.assertEqual(factor._bars_since_train, before + 2)

    def test_retrain_zero_no_auto_trigger(self):
        """retrain_every=0 时即使 _bars_since_train 很大也不触发 fit。"""
        from core.ml.price_predictor import MLPredictionFactor

        factor = MLPredictionFactor(symbol='', retrain_every=0)
        factor._predictor = MagicMock()
        factor._predictor.predict_proba.return_value = np.array([0.5] * 10)
        factor._feature_names = []
        factor.fit = MagicMock()   # 监视

        factor._bars_since_train = 100
        df = _make_data(n=10)
        factor.evaluate(df)
        factor.fit.assert_not_called()


class TestAutoRetrain(unittest.TestCase):

    def test_auto_retrain_triggers_fit(self):
        """retrain_every>0, threshold reached, data sufficient → fit() 被调用。"""
        from core.ml.price_predictor import MLPredictionFactor

        factor = MLPredictionFactor(symbol='X', retrain_every=21)
        factor._predictor = MagicMock()
        factor._predictor.predict_proba.return_value = np.array([0.5] * 350)
        factor._feature_names = []
        factor.fit = MagicMock()

        factor._bars_since_train = 21   # 达阈值
        df = _make_data(n=350)
        factor.evaluate(df)
        factor.fit.assert_called_once()

    def test_auto_retrain_skipped_when_data_insufficient(self):
        """数据少于 252+63 时不触发重训。"""
        from core.ml.price_predictor import MLPredictionFactor

        factor = MLPredictionFactor(symbol='X', retrain_every=21)
        factor._predictor = MagicMock()
        factor._predictor.predict_proba.return_value = np.array([0.5] * 100)
        factor._feature_names = []
        factor.fit = MagicMock()
        factor._bars_since_train = 21
        df = _make_data(n=100)   # 不足
        factor.evaluate(df)
        factor.fit.assert_not_called()

    def test_auto_retrain_failure_does_not_propagate(self):
        """fit() 异常时 evaluate 仍返回 Series（不抛错）。"""
        from core.ml.price_predictor import MLPredictionFactor

        factor = MLPredictionFactor(symbol='X', retrain_every=21)
        factor._predictor = MagicMock()
        factor._predictor.predict_proba.return_value = np.array([0.5] * 350)
        factor._feature_names = []
        factor.fit = MagicMock(side_effect=RuntimeError('mock fail'))
        factor._bars_since_train = 21

        df = _make_data(n=350)
        # 不应抛出
        result = factor.evaluate(df)
        self.assertIsInstance(result, pd.Series)


class TestMLTrainAllScript(unittest.TestCase):

    def test_run_training_no_symbols(self):
        """无 symbols 时返回空 summary。"""
        from scripts.ml_train_all import run_training
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scripts.ml_train_all._fetch_training_symbols',
                       return_value=[]):
                summary = run_training(api_port=5555, output_dir=Path(tmp))
        self.assertEqual(summary['n_symbols'], 0)
        self.assertEqual(summary.get('note'), 'no_symbols')

    def test_run_training_with_skipped(self):
        """数据不足时记为 skipped。"""
        from scripts.ml_train_all import run_training
        # mock get_bars 返回少量数据
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scripts.ml_train_all._fetch_training_symbols',
                       return_value=['A.SH']), \
                 patch('core.data_layer.DataLayer.get_bars',
                       return_value=_make_data(n=100)):  # < 500
                summary = run_training(
                    min_history=500, api_port=5555,
                    output_dir=Path(tmp),
                )
        self.assertEqual(summary['n_skipped'], 1)
        records = summary['records']
        self.assertEqual(records[0]['status'], 'skipped')

    def test_run_training_writes_json(self):
        from scripts.ml_train_all import run_training
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scripts.ml_train_all._fetch_training_symbols',
                       return_value=[]):
                summary = run_training(api_port=5555, output_dir=Path(tmp))
            files = list(Path(tmp).glob('training_*.json'))
        self.assertEqual(len(files), 1)


if __name__ == '__main__':
    unittest.main()

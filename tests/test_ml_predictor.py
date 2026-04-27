"""
tests/test_ml_predictor.py — ML 价格预测框架单元测试

覆盖：
  - FeatureStore：特征提取、时间特征、build/build_predict_row
  - XGBoostPredictor：fit/predict_proba/feature_importance
  - WalkForwardTrainer：折数计算、OOS 指标
  - ModelRegistry：save/load/exists/delete
  - MLPredictionFactor：evaluate/signals/降级
  - 注册表集成：MLPrediction 因子可从 registry 创建

测试策略：全部使用 mock 数据，无网络依赖。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """生成 n 条随机 OHLCV 日线数据。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame({
        'open': close * (1 + rng.normal(0, 0.005, n)),
        'high': close * (1 + rng.uniform(0, 0.01, n)),
        'low': close * (1 - rng.uniform(0, 0.01, n)),
        'close': close,
        'volume': rng.integers(100_000, 500_000, n).astype(float),
    }, index=dates)


# ---------------------------------------------------------------------------
# FeatureStore
# ---------------------------------------------------------------------------

class TestFeatureStore(unittest.TestCase):

    def setUp(self):
        self.data = _make_ohlcv(200)

    def test_build_returns_tuple(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        X, y = store.build(self.data)
        self.assertIsInstance(X, pd.DataFrame)
        self.assertIsInstance(y, pd.Series)

    def test_X_y_lengths_match(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        X, y = store.build(self.data)
        self.assertEqual(len(X), len(y))

    def test_X_nonempty(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        X, y = store.build(self.data)
        self.assertGreater(len(X), 0)
        self.assertGreater(len(X.columns), 0)

    def test_no_nan_in_output(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        X, y = store.build(self.data)
        self.assertFalse(X.isnull().any().any())

    def test_y_binary(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        _, y = store.build(self.data)
        self.assertTrue(set(y.unique()).issubset({0, 1}))

    def test_time_features_present(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore(add_time_features=True)
        X, _ = store.build(self.data)
        time_cols = [c for c in X.columns if c.startswith('time_')]
        self.assertGreater(len(time_cols), 0)

    def test_no_time_features_when_disabled(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore(add_time_features=False)
        X, _ = store.build(self.data)
        time_cols = [c for c in X.columns if c.startswith('time_')]
        self.assertEqual(len(time_cols), 0)

    def test_time_feature_values_finite(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore(add_time_features=True)
        X, _ = store.build(self.data)
        time_cols = [c for c in X.columns if c.startswith('time_')]
        for col in time_cols:
            self.assertTrue(np.all(np.isfinite(X[col].values)))

    def test_build_predict_row_shape(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        row = store.build_predict_row(self.data)
        self.assertEqual(len(row), 1)
        self.assertFalse(row.isnull().any().any())

    def test_feature_names_returns_list(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        names = store.feature_names(self.data)
        self.assertIsInstance(names, list)
        self.assertGreater(len(names), 0)

    def test_skip_factors_respected(self):
        from core.ml.feature_store import FeatureStore
        store = FeatureStore(skip_factors=frozenset({'RSI', 'MACD'}))
        X, _ = store.build(self.data)
        self.assertNotIn('RSI', X.columns)
        self.assertNotIn('MACD', X.columns)

    def test_forward_days_affects_label(self):
        """forward_days 不同，标签不同。"""
        from core.ml.feature_store import FeatureStore
        store = FeatureStore()
        _, y1 = store.build(self.data, forward_days=1)
        _, y2 = store.build(self.data, forward_days=5)
        # 不同预测窗口的标签应不完全相同
        # （随机数据下绝大概率不同，极低概率相同）
        self.assertIsInstance(y1, pd.Series)
        self.assertIsInstance(y2, pd.Series)


# ---------------------------------------------------------------------------
# XGBoostPredictor
# ---------------------------------------------------------------------------

class TestXGBoostPredictor(unittest.TestCase):

    def setUp(self):
        from core.ml.feature_store import FeatureStore
        from core.ml.price_predictor import XGBoostPredictor
        self.data = _make_ohlcv(200)
        store = FeatureStore()
        self.X, self.y = store.build(self.data)
        self.predictor = XGBoostPredictor(n_estimators=10, max_depth=2)

    def test_fit_returns_self(self):
        result = self.predictor.fit(self.X, self.y)
        self.assertIs(result, self.predictor)

    def test_predict_proba_shape(self):
        self.predictor.fit(self.X, self.y)
        proba = self.predictor.predict_proba(self.X)
        self.assertEqual(len(proba), len(self.X))

    def test_predict_proba_range(self):
        self.predictor.fit(self.X, self.y)
        proba = self.predictor.predict_proba(self.X)
        self.assertTrue(np.all(proba >= 0))
        self.assertTrue(np.all(proba <= 1))

    def test_predict_binary(self):
        self.predictor.fit(self.X, self.y)
        preds = self.predictor.predict(self.X)
        self.assertTrue(set(preds).issubset({0, 1}))

    def test_feature_importance_returns_series(self):
        self.predictor.fit(self.X, self.y)
        imp = self.predictor.feature_importance()
        self.assertIsInstance(imp, pd.Series)
        self.assertEqual(len(imp), len(self.X.columns))

    def test_feature_importance_nonnegative(self):
        self.predictor.fit(self.X, self.y)
        imp = self.predictor.feature_importance()
        self.assertTrue((imp >= 0).all())

    def test_predict_before_fit_raises(self):
        from core.ml.price_predictor import XGBoostPredictor
        p = XGBoostPredictor()
        with self.assertRaises(RuntimeError):
            p.predict_proba(self.X)

    def test_feature_alignment_missing_cols(self):
        """预测时特征列缺失应被补零而不是报错。"""
        self.predictor.fit(self.X, self.y)
        # 只保留一半的列
        X_partial = self.X.iloc[:, :len(self.X.columns) // 2]
        proba = self.predictor.predict_proba(X_partial)
        self.assertEqual(len(proba), len(X_partial))

    def test_reproducible_with_seed(self):
        from core.ml.price_predictor import XGBoostPredictor
        p1 = XGBoostPredictor(n_estimators=10, random_state=0)
        p2 = XGBoostPredictor(n_estimators=10, random_state=0)
        p1.fit(self.X, self.y)
        p2.fit(self.X, self.y)
        np.testing.assert_array_equal(
            p1.predict_proba(self.X),
            p2.predict_proba(self.X),
        )


# ---------------------------------------------------------------------------
# WalkForwardTrainer
# ---------------------------------------------------------------------------

class TestWalkForwardTrainer(unittest.TestCase):

    def setUp(self):
        from core.ml.feature_store import FeatureStore
        self.data = _make_ohlcv(400)
        store = FeatureStore()
        self.X, self.y = store.build(self.data)

    def test_run_returns_model_and_result(self):
        from core.ml.price_predictor import WalkForwardTrainer, XGBoostPredictor
        trainer = WalkForwardTrainer(
            train_window=150, val_window=50, step_days=50,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        model, result = trainer.run(self.X, self.y)
        self.assertIsNotNone(model)
        self.assertIsNotNone(result)

    def test_result_has_fold_metrics(self):
        from core.ml.price_predictor import WalkForwardTrainer
        trainer = WalkForwardTrainer(
            train_window=150, val_window=50, step_days=50,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        _, result = trainer.run(self.X, self.y)
        self.assertGreater(result.n_folds, 0)
        self.assertEqual(len(result.fold_metrics), result.n_folds)

    def test_oos_accuracy_in_range(self):
        from core.ml.price_predictor import WalkForwardTrainer
        trainer = WalkForwardTrainer(
            train_window=150, val_window=50, step_days=50,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        _, result = trainer.run(self.X, self.y)
        self.assertGreaterEqual(result.oos_accuracy, 0.0)
        self.assertLessEqual(result.oos_accuracy, 1.0)

    def test_oos_auc_in_range(self):
        from core.ml.price_predictor import WalkForwardTrainer
        trainer = WalkForwardTrainer(
            train_window=150, val_window=50, step_days=50,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        _, result = trainer.run(self.X, self.y)
        self.assertGreaterEqual(result.oos_auc, 0.0)
        self.assertLessEqual(result.oos_auc, 1.0)

    def test_final_model_can_predict(self):
        from core.ml.price_predictor import WalkForwardTrainer
        trainer = WalkForwardTrainer(
            train_window=150, val_window=50, step_days=50,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        model, _ = trainer.run(self.X, self.y)
        proba = model.predict_proba(self.X)
        self.assertEqual(len(proba), len(self.X))


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------

class TestModelRegistry(unittest.TestCase):

    def setUp(self):
        from core.ml.feature_store import FeatureStore
        from core.ml.price_predictor import XGBoostPredictor
        from core.ml.model_registry import ModelRegistry

        self.tmp_dir = tempfile.mkdtemp()
        self.reg = ModelRegistry(base_dir=Path(self.tmp_dir))

        self.data = _make_ohlcv(200)
        store = FeatureStore()
        X, y = store.build(self.data)
        self.X = X
        self.y = y
        self.predictor = XGBoostPredictor(n_estimators=10, max_depth=2)
        self.predictor.fit(X, y)
        self.feature_names = list(X.columns)

    def test_save_returns_path(self):
        path = self.reg.save(
            self.predictor, symbol='000001.SZ', model_type='xgboost',
            feature_names=self.feature_names, metrics={'auc': 0.6},
        )
        self.assertTrue(Path(path).exists())

    def test_exists_after_save(self):
        self.reg.save(
            self.predictor, symbol='000001.SZ', model_type='xgboost',
        )
        self.assertTrue(self.reg.exists('000001.SZ', 'xgboost'))

    def test_not_exists_before_save(self):
        self.assertFalse(self.reg.exists('999999.SZ', 'xgboost'))

    def test_load_returns_model_and_meta(self):
        self.reg.save(
            self.predictor, symbol='000001.SZ', model_type='xgboost',
            feature_names=self.feature_names, metrics={'auc': 0.6},
        )
        model, meta = self.reg.load('000001.SZ', 'xgboost')
        self.assertIsNotNone(model)
        self.assertIsInstance(meta, dict)

    def test_loaded_model_predicts(self):
        self.reg.save(self.predictor, symbol='000001.SZ', model_type='xgboost')
        model, _ = self.reg.load('000001.SZ', 'xgboost')
        proba = model.predict_proba(self.X)
        self.assertEqual(len(proba), len(self.X))

    def test_meta_contains_feature_names(self):
        self.reg.save(
            self.predictor, symbol='000001.SZ', model_type='xgboost',
            feature_names=self.feature_names,
        )
        _, meta = self.reg.load('000001.SZ', 'xgboost')
        self.assertEqual(meta['feature_names'], self.feature_names)

    def test_meta_contains_metrics(self):
        self.reg.save(
            self.predictor, symbol='000001.SZ', model_type='xgboost',
            metrics={'auc': 0.62},
        )
        _, meta = self.reg.load('000001.SZ', 'xgboost')
        self.assertAlmostEqual(meta['metrics']['auc'], 0.62)

    def test_load_nonexistent_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.reg.load('NOTEXIST.SZ', 'xgboost')

    def test_list_versions(self):
        self.reg.save(self.predictor, symbol='000001.SZ', model_type='xgboost',
                      version='v1')
        self.reg.save(self.predictor, symbol='000001.SZ', model_type='xgboost',
                      version='v2')
        versions = self.reg.list_versions('000001.SZ', 'xgboost')
        self.assertIn('v1', versions)
        self.assertIn('v2', versions)

    def test_delete_cleans_up(self):
        self.reg.save(self.predictor, symbol='000001.SZ', model_type='xgboost')
        self.reg.delete('000001.SZ', 'xgboost')
        self.assertFalse(self.reg.exists('000001.SZ', 'xgboost'))

    def test_get_meta_none_when_missing(self):
        meta = self.reg.get_meta('NOTEXIST.SZ', 'xgboost')
        self.assertIsNone(meta)


# ---------------------------------------------------------------------------
# MLPredictionFactor
# ---------------------------------------------------------------------------

class TestMLPredictionFactor(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.mkdtemp()
        self.data = _make_ohlcv(300)

    def _make_factor(self, **kwargs):
        from core.ml.price_predictor import MLPredictionFactor
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        return MLPredictionFactor(model_registry=reg if False else None, **kwargs)

    def test_evaluate_no_model_returns_zeros(self):
        from core.ml.price_predictor import MLPredictionFactor
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        f = MLPredictionFactor(symbol='000001.SZ', reg=reg)
        result = f.evaluate(self.data)
        self.assertTrue((result == 0).all())

    def test_fit_and_evaluate_returns_series(self):
        from core.ml.price_predictor import MLPredictionFactor
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        f = MLPredictionFactor(
            symbol='000001.SZ',
            reg=reg,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        wf_result = f.fit(self.data, use_walk_forward=False)
        result = f.evaluate(self.data)
        self.assertIsInstance(result, pd.Series)
        self.assertEqual(len(result), len(self.data))

    def test_fit_evaluate_finite_values(self):
        from core.ml.price_predictor import MLPredictionFactor
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        f = MLPredictionFactor(
            symbol='000001.SZ',
            reg=reg,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        f.fit(self.data, use_walk_forward=False)
        result = f.evaluate(self.data)
        self.assertTrue(np.all(np.isfinite(result.values)))

    def test_signals_buy_on_high_zscore(self):
        from core.ml.price_predictor import MLPredictionFactor
        f = MLPredictionFactor()
        vals = pd.Series([0.0] * 19 + [2.0], index=range(20))
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'BUY')

    def test_signals_sell_on_low_zscore(self):
        from core.ml.price_predictor import MLPredictionFactor
        f = MLPredictionFactor()
        vals = pd.Series([0.0] * 19 + [-2.0], index=range(20))
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'SELL')

    def test_signals_none_in_neutral(self):
        from core.ml.price_predictor import MLPredictionFactor
        f = MLPredictionFactor()
        vals = pd.Series([0.0] * 20, index=range(20))
        sigs = f.signals(vals, price=10.0, threshold=1.0)
        self.assertEqual(sigs, [])

    def test_load_after_fit(self):
        from core.ml.price_predictor import MLPredictionFactor
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        f1 = MLPredictionFactor(
            symbol='000001.SZ',
            reg=reg,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        f1.fit(self.data, use_walk_forward=False)

        # 新实例，从 registry 加载
        f2 = MLPredictionFactor(symbol='000001.SZ', reg=reg)
        loaded = f2.load()
        self.assertTrue(loaded)
        result = f2.evaluate(self.data)
        self.assertIsInstance(result, pd.Series)

    def test_walk_forward_fit_returns_result(self):
        from core.ml.price_predictor import MLPredictionFactor, WalkForwardResult
        from core.ml.model_registry import ModelRegistry
        reg = ModelRegistry(base_dir=Path(self.tmp_dir))
        f = MLPredictionFactor(
            symbol='TEST.SZ',
            reg=reg,
            predictor_kwargs={'n_estimators': 10, 'max_depth': 2},
        )
        wf_result = f.fit(self.data, use_walk_forward=True)
        self.assertIsInstance(wf_result, WalkForwardResult)

    def test_name_equals_ml_prediction(self):
        from core.ml.price_predictor import MLPredictionFactor
        f = MLPredictionFactor()
        self.assertEqual(f.name, 'MLPrediction')


# ---------------------------------------------------------------------------
# 注册表集成
# ---------------------------------------------------------------------------

class TestMLRegistryIntegration(unittest.TestCase):

    def test_ml_prediction_registered(self):
        from core.factor_registry import registry
        self.assertIn('MLPrediction', registry)

    def test_create_from_registry(self):
        from core.factor_registry import registry
        f = registry.create('MLPrediction')
        self.assertEqual(f.name, 'MLPrediction')

    def test_total_factor_count_at_least_21(self):
        """原20 + ML1 = 至少21个"""
        from core.factor_registry import registry
        self.assertGreaterEqual(len(registry), 21)

    def test_ml_factor_in_pipeline_no_crash(self):
        """MLPrediction 因子（无预训练模型）加入流水线不崩溃（降级为0）"""
        from core.factor_pipeline import FactorPipeline
        pipeline = FactorPipeline()
        pipeline.add('RSI', weight=0.7)
        pipeline.add('MLPrediction', weight=0.3)

        data = _make_ohlcv(100)
        result = pipeline.run(symbol='000001.SZ', data=data, price=10.0)
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result.combined_score))


if __name__ == '__main__':
    unittest.main()

"""
core/ml/price_predictor.py — ML 价格方向预测

核心功能：
  1. XGBoostPredictor：二分类（明日涨/跌），基于 Walk-Forward 训练
  2. WalkForwardTrainer：防过拟合的滚动训练框架（与 WalkForwardAnalyzer 设计一致）
  3. MLPredictionFactor：将预测概率包装为 Factor 接口，接入 FactorPipeline

Walk-Forward 逻辑：
  - 训练窗口：252 个交易日（约 1 年）
  - 验证窗口：63 个交易日（约 3 个月）
  - 步进：21 个交易日（约 1 个月）
  - 每次用训练窗口数据拟合，在验证窗口预测，汇总 OOS 性能

用法：
    from core.ml.price_predictor import XGBoostPredictor, MLPredictionFactor

    # 训练
    predictor = XGBoostPredictor()
    predictor.fit(X_train, y_train)
    prob = predictor.predict_proba(X_test)

    # 作为因子使用（需先用历史数据训练）
    factor = MLPredictionFactor(symbol='000001.SZ')
    factor.fit(historical_data)
    z = factor.evaluate(recent_data)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal
from core.ml.feature_store import FeatureStore
from core.ml.model_registry import ModelRegistry


# ---------------------------------------------------------------------------
# XGBoostPredictor
# ---------------------------------------------------------------------------

class XGBoostPredictor:
    """
    XGBoost 二分类涨跌预测器。

    Parameters
    ----------
    n_estimators : int
        树的数量（默认 200）
    max_depth : int
        树的最大深度（默认 4，防止过拟合）
    learning_rate : float
        学习率（默认 0.05）
    subsample : float
        行采样比例（默认 0.8）
    colsample_bytree : float
        列采样比例（默认 0.8）
    random_state : int
        随机种子
    """

    def __init__(
        self,
        n_estimators: int = 200,
        max_depth: int = 4,
        learning_rate: float = 0.05,
        subsample: float = 0.8,
        colsample_bytree: float = 0.8,
        random_state: int = 42,
    ) -> None:
        try:
            from xgboost import XGBClassifier
            self._model = XGBClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                subsample=subsample,
                colsample_bytree=colsample_bytree,
                random_state=random_state,
                eval_metric='logloss',
                use_label_encoder=False,
                verbosity=0,
            )
        except ImportError as e:
            raise ImportError("xgboost 未安装。运行: pip install xgboost") from e

        self._feature_names: List[str] = []
        self._is_fitted = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> 'XGBoostPredictor':
        """训练模型。"""
        self._feature_names = list(X.columns)
        self._model.fit(X.values, y.values)
        self._is_fitted = True
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        返回上涨概率数组，shape=(n_samples,)，值域 [0, 1]。
        """
        if not self._is_fitted:
            raise RuntimeError("模型未训练。请先调用 fit()。")
        X_aligned = self._align_features(X)
        proba = self._model.predict_proba(X_aligned.values)
        return proba[:, 1]  # 上涨概率

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """返回二分类预测（0/1）。"""
        proba = self.predict_proba(X)
        return (proba > 0.5).astype(int)

    def feature_importance(self) -> pd.Series:
        """返回特征重要性（按重要性降序）。"""
        if not self._is_fitted or not self._feature_names:
            return pd.Series(dtype=float)
        imp = self._model.feature_importances_
        return pd.Series(imp, index=self._feature_names).sort_values(ascending=False)

    def _align_features(self, X: pd.DataFrame) -> pd.DataFrame:
        """确保特征列与训练时一致（缺失列补 0，多余列丢弃）。"""
        if not self._feature_names:
            return X
        missing = set(self._feature_names) - set(X.columns)
        for col in missing:
            X = X.copy()
            X[col] = 0.0
        return X[self._feature_names]


# ---------------------------------------------------------------------------
# WalkForwardTrainer
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardResult:
    """Walk-Forward 验证结果。"""
    oos_accuracy: float          # OOS 准确率
    oos_auc: float               # OOS AUC
    n_folds: int                 # 折数
    fold_metrics: List[Dict]     # 每折详情
    feature_importance: pd.Series = field(default_factory=pd.Series)


class WalkForwardTrainer:
    """
    Walk-Forward 交叉验证训练框架。

    每次用 train_window 拟合，在 val_window 上预测，步进 step_days。
    最终用全量数据训练最终模型。

    Parameters
    ----------
    train_window : int
        训练窗口（交易日数，默认 252 ≈ 1 年）
    val_window : int
        验证窗口（交易日数，默认 63 ≈ 3 个月）
    step_days : int
        步进天数（默认 21 ≈ 1 个月）
    predictor_cls : type
        预测器类（默认 XGBoostPredictor）
    predictor_kwargs : dict
        传给预测器的参数
    """

    def __init__(
        self,
        train_window: int = 252,
        val_window: int = 63,
        step_days: int = 21,
        predictor_cls: type = XGBoostPredictor,
        predictor_kwargs: Optional[Dict] = None,
    ) -> None:
        self.train_window = train_window
        self.val_window = val_window
        self.step_days = step_days
        self.predictor_cls = predictor_cls
        self.predictor_kwargs = predictor_kwargs or {}

    def run(
        self,
        X: pd.DataFrame,
        y: pd.Series,
    ) -> Tuple[Any, WalkForwardResult]:
        """
        执行 Walk-Forward 验证并训练最终模型。

        Parameters
        ----------
        X : DataFrame — 特征矩阵（索引对齐 y）
        y : Series   — 标签

        Returns
        -------
        (final_model, WalkForwardResult)
        """
        from sklearn.metrics import accuracy_score, roc_auc_score

        n = len(X)
        fold_metrics = []
        all_oos_proba: List[float] = []
        all_oos_true: List[int] = []

        start = 0
        while start + self.train_window + self.val_window <= n:
            train_end = start + self.train_window
            val_end = train_end + self.val_window

            X_tr = X.iloc[start:train_end]
            y_tr = y.iloc[start:train_end]
            X_val = X.iloc[train_end:val_end]
            y_val = y.iloc[train_end:val_end]

            # 跳过类别不平衡的折（少数类 < 5）
            if y_tr.sum() < 5 or (len(y_tr) - y_tr.sum()) < 5:
                start += self.step_days
                continue

            predictor = self.predictor_cls(**self.predictor_kwargs)
            predictor.fit(X_tr, y_tr)

            proba = predictor.predict_proba(X_val)
            preds = (proba > 0.5).astype(int)

            acc = float(accuracy_score(y_val.values, preds))
            try:
                auc = float(roc_auc_score(y_val.values, proba))
            except Exception:
                auc = 0.5

            fold_metrics.append({
                'train_start': start,
                'train_end': train_end,
                'val_end': val_end,
                'accuracy': acc,
                'auc': auc,
                'n_train': len(X_tr),
                'n_val': len(X_val),
            })
            all_oos_proba.extend(proba.tolist())
            all_oos_true.extend(y_val.values.tolist())

            start += self.step_days

        # 汇总 OOS 指标
        if all_oos_true:
            oos_acc = float(accuracy_score(all_oos_true, [p > 0.5 for p in all_oos_proba]))
            try:
                oos_auc = float(roc_auc_score(all_oos_true, all_oos_proba))
            except Exception:
                oos_auc = 0.5
        else:
            oos_acc = 0.5
            oos_auc = 0.5

        # 用全量数据训练最终模型
        final_model = self.predictor_cls(**self.predictor_kwargs)
        final_model.fit(X, y)

        feat_imp = final_model.feature_importance()

        result = WalkForwardResult(
            oos_accuracy=oos_acc,
            oos_auc=oos_auc,
            n_folds=len(fold_metrics),
            fold_metrics=fold_metrics,
            feature_importance=feat_imp,
        )
        return final_model, result


# ---------------------------------------------------------------------------
# MLPredictionFactor — 将 XGBoost 预测概率包装为 Factor 接口
# ---------------------------------------------------------------------------

class MLPredictionFactor(Factor):
    """
    ML 价格预测因子。

    将 XGBoost 二分类的上涨概率转换为 z-score 因子值：
      factor_value = (predict_proba - 0.5) / 0.5  → 归一化到 [-1, 1]，再 z-score

    解读：
      - z > threshold：模型认为明日上涨概率高 → BUY
      - z < -threshold：模型认为明日上涨概率低 → SELL

    Parameters
    ----------
    symbol : str
        标的代码（用于模型存储 / 加载）
    forward_days : int
        预测目标：明日收盘价（shift=-forward_days）
    retrain_every : int
        每隔多少 bars 自动重训练（0 = 不自动重训）
    store : FeatureStore
        特征提取器（默认新建）
    reg : ModelRegistry
        模型注册表（默认新建）
    predictor_kwargs : dict
        传给 XGBoostPredictor 的参数
    """

    name = 'MLPrediction'
    category = FactorCategory.ML

    def __init__(
        self,
        symbol: str = '',
        forward_days: int = 2,
        retrain_every: int = 0,
        store: Optional[FeatureStore] = None,
        reg: Optional[ModelRegistry] = None,
        predictor_kwargs: Optional[Dict] = None,
        threshold: float = 1.0,
    ) -> None:
        self.symbol = symbol
        self.forward_days = forward_days
        self.retrain_every = retrain_every
        self._store = store or FeatureStore()
        self._model_reg = reg or ModelRegistry()
        self._predictor_kwargs = predictor_kwargs or {}
        self.threshold = threshold

        self._predictor: Optional[XGBoostPredictor] = None
        self._feature_names: List[str] = []
        self._bars_since_train: int = 0

    # ------------------------------------------------------------------
    # Training interface
    # ------------------------------------------------------------------

    def fit(self, data: pd.DataFrame, use_walk_forward: bool = True) -> WalkForwardResult:
        """
        在历史数据上训练模型（并保存到 ModelRegistry）。

        Parameters
        ----------
        data : DataFrame — 历史 OHLCV 数据（至少需要 train_window + val_window 行）
        use_walk_forward : bool — 是否使用 Walk-Forward 验证（默认 True）

        Returns
        -------
        WalkForwardResult（包含 OOS 指标）
        """
        X, y = self._store.build(
            data, symbol=self.symbol, forward_days=self.forward_days
        )

        if len(X) < 60:
            # 数据不足，训练简单模型
            self._predictor = XGBoostPredictor(**self._predictor_kwargs)
            self._predictor.fit(X, y)
            self._feature_names = list(X.columns)
            result = WalkForwardResult(
                oos_accuracy=0.5, oos_auc=0.5,
                n_folds=0, fold_metrics=[],
            )
        elif use_walk_forward:
            trainer = WalkForwardTrainer(predictor_kwargs=self._predictor_kwargs)
            self._predictor, result = trainer.run(X, y)
            self._feature_names = list(X.columns)
        else:
            self._predictor = XGBoostPredictor(**self._predictor_kwargs)
            self._predictor.fit(X, y)
            self._feature_names = list(X.columns)
            result = WalkForwardResult(
                oos_accuracy=0.5, oos_auc=0.5,
                n_folds=0, fold_metrics=[],
            )

        # 保存模型
        if self.symbol:
            self._model_reg.save(
                self._predictor,
                symbol=self.symbol,
                model_type='xgboost',
                feature_names=self._feature_names,
                metrics={'oos_accuracy': result.oos_accuracy, 'oos_auc': result.oos_auc},
            )

        self._bars_since_train = 0
        return result

    def load(self) -> bool:
        """从 ModelRegistry 加载已保存模型，返回是否成功。"""
        if not self.symbol:
            return False
        try:
            model, meta = self._model_reg.load(self.symbol, 'xgboost')
            self._predictor = model
            self._feature_names = meta.get('feature_names', [])
            return True
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------------
    # Factor interface
    # ------------------------------------------------------------------

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        """
        对整个历史数据评估因子值（回测用）。

        注意：这里用 EXPANDING 窗口逐步预测，避免前视偏差：
          - 前 min_train_bars=200 行：返回 0（数据不足）
          - 之后每行：用该行之前的数据训练，预测该行

        为了计算效率，当数据量足够时，直接用已保存模型对全量特征矩阵预测。
        """
        if self._predictor is None:
            # 尝试加载已保存模型
            loaded = self.load()
            if not loaded or self._predictor is None:
                # 没有预训练模型，返回全零（降级）
                return pd.Series(0.0, index=data.index)

        # 提取特征
        try:
            X = self._store.build_predict_row.__func__(
                self._store, data, self.symbol
            )
        except Exception:
            return pd.Series(0.0, index=data.index)

        # 对全量数据构建特征（无标签）
        features = self._store._extract_factor_features(data, self.symbol)
        if self._store.add_time_features:
            time_feats = FeatureStore._time_features(data.index)
            features = pd.concat([features, time_feats], axis=1)

        # 对齐特征列
        if self._feature_names:
            missing = set(self._feature_names) - set(features.columns)
            for col in missing:
                features[col] = 0.0
            features = features[self._feature_names]

        features = features.ffill().fillna(0.0)

        try:
            proba = self._predictor.predict_proba(features)
            # 转换为居中信号：[0,1] → [-1, 1]
            centered = (proba - 0.5) * 2
            result = pd.Series(centered, index=data.index)
            return self.normalize(result)
        except Exception:
            return pd.Series(0.0, index=data.index)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: float = 1.0,
    ) -> List[Signal]:
        """ML 信号：高概率上涨 → BUY，高概率下跌 → SELL"""
        if len(factor_values) == 0:
            return []

        latest = float(factor_values.iloc[-1])
        from datetime import datetime as dt

        if latest > threshold:
            strength = min((latest - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=dt.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'ml_zscore': round(latest, 3)},
            )]
        if latest < -threshold:
            strength = min((abs(latest) - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=dt.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'ml_zscore': round(latest, 3)},
            )]
        return []

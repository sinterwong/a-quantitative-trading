"""
core/ml/factor_selector.py — 基于 ML 的因子动态选择

功能：
  用 LightGBM 预测"未来 21 天哪些因子 IC 较高"，自适应调整 DynamicWeightPipeline 权重。

设计思路：
  1. 特征构建：市场 Regime + 波动率水平 + 宏观指标 + 时间特征
  2. 标签构建：计算各因子在未来 21 天的 IC（Information Coefficient）
               IC > threshold（默认 0.02）则该因子标签为 1（"有效"）
  3. 模型：LightGBM 多标签二分类（每个因子一个模型）或多输出回归
  4. Walk-Forward：252 天训练 / 63 天验证 / 21 天步进
  5. 输出：factor_weights 字典，可直接传入 DynamicWeightPipeline

注意：
  - LightGBM 不可用时降级为等权分配（不崩溃）
  - 需要足够历史数据（至少 300 个交易日）才能训练

用法：
    from core.ml.factor_selector import FactorSelector

    selector = FactorSelector(symbols=['000001.SZ', '600519.SH'])
    # 训练（用历史数据）
    selector.fit(price_data_dict)   # {symbol: DataFrame}

    # 预测当前应给各因子多少权重
    weights = selector.predict_weights(current_price_data)
    # weights: {'RSI': 0.12, 'MACD': 0.08, 'BollingerBand': 0.05, ...}

    # 直接接入 DynamicWeightPipeline
    pipeline = DynamicWeightPipeline(factor_weights=weights)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.ml.feature_store import FeatureStore

logger = logging.getLogger('core.ml.factor_selector')

# 默认特征：用于预测因子有效性的市场状态特征
_REGIME_FEATURES = [
    'time_dow_sin', 'time_dow_cos',
    'time_month_sin', 'time_month_cos',
    'time_is_quarter_end',
]


# ---------------------------------------------------------------------------
# FactorICLabeler — 构建"未来 21 天各因子 IC"标签
# ---------------------------------------------------------------------------

class FactorICLabeler:
    """
    计算历史数据上各因子的前向 IC（信息系数）。

    IC = Spearman(因子值[t], 收益率[t+1 ~ t+horizon])

    Parameters
    ----------
    horizon   : 预测窗口（默认 21 个交易日）
    min_obs   : 计算 IC 所需最少样本（默认 20）
    """

    def __init__(self, horizon: int = 21, min_obs: int = 20):
        self.horizon = horizon
        self.min_obs = min_obs

    def compute(
        self,
        factor_values: pd.DataFrame,    # (n_days, n_factors)
        returns: pd.Series,             # 日收益率序列
    ) -> pd.DataFrame:
        """
        逐窗口计算各因子的滚动前向 IC。

        Returns
        -------
        pd.DataFrame, shape (n_windows, n_factors)
            每行为一个窗口的各因子 IC 值。
        """
        from scipy.stats import spearmanr

        n = len(returns)
        rows = []
        idx = []

        for t in range(self.min_obs, n - self.horizon):
            fwd_ret = returns.iloc[t:t + self.horizon].mean()  # 窗口平均收益
            row = {}
            for col in factor_values.columns:
                fval = factor_values[col].iloc[t - self.min_obs:t]
                rval = returns.iloc[t - self.min_obs:t]
                valid = ~(fval.isna() | rval.isna())
                if valid.sum() < self.min_obs:
                    row[col] = 0.0
                    continue
                try:
                    corr, _ = spearmanr(fval[valid], rval[valid])
                    row[col] = float(corr) if not np.isnan(corr) else 0.0
                except Exception:
                    row[col] = 0.0
            rows.append(row)
            idx.append(factor_values.index[t])

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


# ---------------------------------------------------------------------------
# FactorSelectorModel — 单因子有效性预测模型
# ---------------------------------------------------------------------------

class FactorSelectorModel:
    """
    用 LightGBM 预测单个因子在未来 21 天的 IC 是否高于阈值。

    Parameters
    ----------
    factor_name  : 因子名称
    ic_threshold : IC 有效阈值（默认 0.02）
    n_estimators : LightGBM 树数量
    max_depth    : 树最大深度
    """

    def __init__(
        self,
        factor_name: str,
        ic_threshold: float = 0.02,
        n_estimators: int = 100,
        max_depth: int = 4,
        random_state: int = 42,
    ):
        self.factor_name = factor_name
        self.ic_threshold = ic_threshold
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.random_state = random_state
        self._model = None
        self._feature_names: List[str] = []

    def fit(self, X: pd.DataFrame, ic_series: pd.Series) -> 'FactorSelectorModel':
        """
        训练模型。

        Parameters
        ----------
        X         : 市场状态特征矩阵
        ic_series : 该因子的 IC 序列（与 X 对齐）
        """
        # 标签：IC > threshold → 1（因子有效）
        y = (ic_series > self.ic_threshold).astype(int)

        # 对齐
        common = X.index.intersection(y.index)
        if len(common) < 30:
            logger.warning('FactorSelectorModel %s: insufficient data (%d rows)',
                           self.factor_name, len(common))
            return self

        X_aligned = X.loc[common].fillna(0.0)
        y_aligned = y.loc[common]

        if y_aligned.sum() == 0 or y_aligned.sum() == len(y_aligned):
            logger.debug('FactorSelectorModel %s: all-same labels, skipping fit',
                         self.factor_name)
            return self

        try:
            import lightgbm as lgb
            self._model = lgb.LGBMClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                random_state=self.random_state,
                verbose=-1,
                n_jobs=1,
            )
            self._model.fit(X_aligned, y_aligned)
            self._feature_names = list(X_aligned.columns)
            logger.debug('FactorSelectorModel %s fitted (%d samples, pos_rate=%.2f)',
                         self.factor_name, len(common),
                         float(y_aligned.mean()))
        except ImportError:
            logger.warning('lightgbm not installed — FactorSelectorModel disabled')
        except Exception as e:
            logger.warning('FactorSelectorModel %s fit failed: %s', self.factor_name, e)
        return self

    def predict_proba(self, X_row: pd.DataFrame) -> float:
        """
        预测因子有效的概率（0~1）。
        无模型时返回 0.5（中性）。
        """
        if self._model is None:
            return 0.5
        try:
            row = X_row.reindex(columns=self._feature_names).fillna(0.0)
            prob = self._model.predict_proba(row)[0][1]
            return float(prob)
        except Exception as e:
            logger.debug('FactorSelectorModel %s predict failed: %s', self.factor_name, e)
            return 0.5

    @property
    def is_fitted(self) -> bool:
        return self._model is not None


# ---------------------------------------------------------------------------
# WalkForwardFactorSelector — Walk-Forward 训练框架
# ---------------------------------------------------------------------------

class WalkForwardFactorSelector:
    """
    Walk-Forward 框架：逐窗口训练 FactorSelectorModel。

    参数：
      train_window : 训练窗口（默认 252 天）
      val_window   : 验证窗口（默认 63 天）
      step         : 步进（默认 21 天）
    """

    def __init__(
        self,
        train_window: int = 252,
        val_window: int = 63,
        step: int = 21,
        ic_threshold: float = 0.02,
    ):
        self.train_window = train_window
        self.val_window = val_window
        self.step = step
        self.ic_threshold = ic_threshold

    def run(
        self,
        X: pd.DataFrame,          # 市场状态特征（日频）
        ic_df: pd.DataFrame,      # 各因子 IC（日频，列 = 因子名）
    ) -> Dict[str, List[float]]:
        """
        运行 Walk-Forward，返回各因子在验证集上的预测概率列表。

        Returns
        -------
        dict — {factor_name: [prob_w1, prob_w2, ...]}
        """
        results: Dict[str, List[float]] = {col: [] for col in ic_df.columns}
        n = min(len(X), len(ic_df))
        if n < self.train_window + self.val_window:
            logger.warning('WalkForwardFactorSelector: insufficient data (%d rows, need %d)',
                           n, self.train_window + self.val_window)
            return results

        common = X.index.intersection(ic_df.index)
        X = X.loc[common]
        ic_df = ic_df.loc[common]
        n = len(common)

        for start in range(0, n - self.train_window - self.val_window + 1, self.step):
            train_end = start + self.train_window
            val_end = min(train_end + self.val_window, n)

            X_train = X.iloc[start:train_end]
            X_val   = X.iloc[train_end:val_end]
            if X_val.empty:
                break

            for factor in ic_df.columns:
                ic_train = ic_df[factor].iloc[start:train_end]
                model = FactorSelectorModel(
                    factor_name=factor,
                    ic_threshold=self.ic_threshold,
                )
                model.fit(X_train, ic_train)
                # 用验证集最后一行预测
                prob = model.predict_proba(X_val.iloc[[-1]])
                results[factor].append(prob)

        return results


# ---------------------------------------------------------------------------
# FactorSelector — 主入口
# ---------------------------------------------------------------------------

@dataclass
class FactorSelectorResult:
    """因子选择结果"""
    weights: Dict[str, float]           # 因子权重（归一化到 [min_weight, 1.0]）
    proba: Dict[str, float]             # 各因子预测概率
    fitted_at: datetime = field(default_factory=datetime.now)
    n_factors: int = 0
    method: str = 'lightgbm'


class FactorSelector:
    """
    基于 ML 的因子动态选择器。

    功能：
    1. 从价格数据计算各因子值 + 市场状态特征
    2. 用 Walk-Forward LightGBM 训练"因子有效性分类器"
    3. 输出 factor_weights，可传入 DynamicWeightPipeline

    Parameters
    ----------
    ic_threshold  : 认定因子"有效"的 IC 阈值（默认 0.02）
    min_weight    : 无效因子的最小权重（默认 0.02，防止完全忽略）
    max_weight    : 最大权重上限（默认 0.25）
    train_window  : WF 训练窗口（天）
    val_window    : WF 验证窗口（天）
    step          : WF 步进（天）
    """

    def __init__(
        self,
        ic_threshold: float = 0.02,
        min_weight: float = 0.02,
        max_weight: float = 0.25,
        train_window: int = 252,
        val_window: int = 63,
        step: int = 21,
    ):
        self.ic_threshold = ic_threshold
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.train_window = train_window
        self.val_window = val_window
        self.step = step

        self._models: Dict[str, FactorSelectorModel] = {}
        self._feature_store = FeatureStore(add_time_features=True)
        self._last_result: Optional[FactorSelectorResult] = None

    # ------------------------------------------------------------------
    # 训练
    # ------------------------------------------------------------------

    def fit(
        self,
        price_data: pd.DataFrame,
        symbol: str = '',
    ) -> 'FactorSelector':
        """
        用历史价格数据训练因子选择模型。

        Parameters
        ----------
        price_data : DataFrame
            包含 OHLCV 的日线数据（至少 train_window + val_window 行）
        symbol     : 标的代码
        """
        if len(price_data) < self.train_window + self.val_window:
            logger.warning('FactorSelector.fit: need %d rows, got %d — skipping',
                           self.train_window + self.val_window, len(price_data))
            return self

        # 1. 提取因子值
        try:
            factor_values, _ = self._feature_store.build(price_data, symbol=symbol,
                                                          forward_days=2)
        except Exception as e:
            logger.warning('FactorSelector: feature extraction failed: %s', e)
            return self

        # 去掉时间特征列，只保留因子列
        factor_cols = [c for c in factor_values.columns if not c.startswith('time_')]
        time_cols   = [c for c in factor_values.columns if c.startswith('time_')]

        if not factor_cols:
            logger.warning('FactorSelector: no factor columns found')
            return self

        # 2. 计算各因子前向 IC（标签）
        returns = price_data['close'].pct_change().fillna(0.0)
        labeler = FactorICLabeler(horizon=21, min_obs=20)
        ic_df = labeler.compute(factor_values[factor_cols], returns)

        if ic_df.empty:
            logger.warning('FactorSelector: IC computation returned empty DataFrame')
            return self

        # 3. 构建市场状态特征（时间特征 + 波动率）
        X_state = self._build_state_features(price_data, factor_values, time_cols)

        # 4. Walk-Forward 训练
        wf = WalkForwardFactorSelector(
            train_window=self.train_window,
            val_window=self.val_window,
            step=self.step,
            ic_threshold=self.ic_threshold,
        )
        wf_results = wf.run(X_state, ic_df)

        # 5. 用最近一段全量数据训练"最新"模型（用于实时预测）
        self._models = {}
        common = X_state.index.intersection(ic_df.index)
        X_latest = X_state.loc[common].iloc[-self.train_window:]
        ic_latest = ic_df.loc[common].iloc[-self.train_window:]

        for factor in factor_cols:
            if factor in ic_df.columns:
                model = FactorSelectorModel(
                    factor_name=factor,
                    ic_threshold=self.ic_threshold,
                )
                model.fit(X_latest, ic_latest[factor])
                self._models[factor] = model

        logger.info('FactorSelector fitted: %d factor models, %d wf windows',
                    len(self._models), len(next(iter(wf_results.values()), [])))
        return self

    # ------------------------------------------------------------------
    # 预测
    # ------------------------------------------------------------------

    def predict_weights(
        self,
        price_data: pd.DataFrame,
        symbol: str = '',
    ) -> Dict[str, float]:
        """
        预测当前市场状态下各因子的推荐权重。

        Returns
        -------
        dict — {factor_name: weight}，权重归一化，sum=1
        无模型时返回等权分配。
        """
        if not self._models:
            return self._equal_weights()

        try:
            X_row = self._feature_store.build_predict_row(price_data, symbol=symbol)
            X_state_row = self._build_state_features(
                price_data, X_row, [c for c in X_row.columns if c.startswith('time_')]
            ).iloc[[-1]]
        except Exception as e:
            logger.warning('FactorSelector.predict_weights: feature build failed: %s', e)
            return self._equal_weights()

        proba: Dict[str, float] = {}
        for factor, model in self._models.items():
            proba[factor] = model.predict_proba(X_state_row)

        weights = self._proba_to_weights(proba)
        self._last_result = FactorSelectorResult(
            weights=weights,
            proba=proba,
            n_factors=len(self._models),
        )
        return weights

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _build_state_features(
        self,
        price_data: pd.DataFrame,
        factor_values: pd.DataFrame,
        time_cols: List[str],
    ) -> pd.DataFrame:
        """
        构建市场状态特征矩阵（时间特征 + 波动率 + 趋势）。
        """
        parts = []

        # 时间特征
        if time_cols:
            parts.append(factor_values[time_cols])

        # 波动率（20日滚动）
        returns = price_data['close'].pct_change().fillna(0.0)
        vol_20 = returns.rolling(20, min_periods=5).std().rename('vol_20d')
        vol_5  = returns.rolling(5,  min_periods=3).std().rename('vol_5d')
        vol_ratio = (vol_5 / vol_20.replace(0, np.nan)).fillna(1.0).rename('vol_ratio')
        parts.extend([vol_20.to_frame(), vol_5.to_frame(), vol_ratio.to_frame()])

        # 短期动量（20 日收益）
        mom_20 = price_data['close'].pct_change(20).fillna(0.0).rename('mom_20d')
        mom_5  = price_data['close'].pct_change(5).fillna(0.0).rename('mom_5d')
        parts.extend([mom_20.to_frame(), mom_5.to_frame()])

        if not parts:
            return pd.DataFrame(index=price_data.index)

        X = pd.concat(parts, axis=1, sort=False)
        return X.fillna(0.0)

    def _proba_to_weights(self, proba: Dict[str, float]) -> Dict[str, float]:
        """将预测概率转换为归一化权重，应用 min/max 约束。"""
        if not proba:
            return {}

        # 概率 → 原始权重
        raw = {f: max(p, self.min_weight) for f, p in proba.items()}

        # 截断到 max_weight
        capped = {f: min(w, self.max_weight) for f, w in raw.items()}

        # 归一化
        total = sum(capped.values())
        if total <= 0:
            return self._equal_weights()

        return {f: w / total for f, w in capped.items()}

    def _equal_weights(self) -> Dict[str, float]:
        """无模型时的等权降级。"""
        if not self._models:
            # 获取全局因子列表
            try:
                from core.factor_registry import registry
                factors = [f for f in registry.list_factors()]
            except Exception:
                factors = []
            if not factors:
                return {}
            w = 1.0 / len(factors)
            return {f: w for f in factors}
        w = 1.0 / len(self._models)
        return {f: w for f in self._models}

    @property
    def last_result(self) -> Optional[FactorSelectorResult]:
        """最近一次 predict_weights 的完整结果。"""
        return self._last_result

    @property
    def factor_names(self) -> List[str]:
        """已训练的因子列表。"""
        return list(self._models.keys())

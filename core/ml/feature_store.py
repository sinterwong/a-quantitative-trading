"""
core/ml/feature_store.py — 特征工程

从已注册因子自动构建特征矩阵，供 ML 模型训练和预测使用。

设计：
  - 遍历 FactorRegistry 中的所有因子，调用 evaluate() 取 z-score 序列
  - 补充时间特征（星期、月份、季度末标志）
  - 返回标准化的 (n_samples, n_features) DataFrame
  - 特征名为 factor_name（与注册名一致）

用法：
    from core.ml.feature_store import FeatureStore

    store = FeatureStore()
    X, y = store.build(symbol='000001.SZ', data=df)
    # X: DataFrame，y: Series（1=明日涨，0=明日跌）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from core.factor_registry import FactorRegistry


# 跳过这些因子：需要外部数据注入且无降级为零以外意义的因子
# 在有数据注入时才加入
_SKIP_IN_DEFAULT = frozenset([
    'SectorMomentum',       # 需要 sector_data
    'IndexRelativeStrength', # 需要 index_data
    'PEPercentile',         # 需要 fundamental_data
    'ROEMomentum',
    'EarningsSurprise',
    'RevenueGrowth',
    'CashFlowQuality',
    'MarginTrading',        # 需要 sentiment_data
    'NorthboundFlow',
    'ShortInterest',
])


class FeatureStore:
    """
    从因子注册表自动构建 ML 特征矩阵。

    Parameters
    ----------
    reg : FactorRegistry
        使用的注册表（默认全局）
    skip_factors : set[str]
        跳过的因子名称（默认跳过需外部数据的因子）
    add_time_features : bool
        是否添加时间特征（星期、月份、季末标志）
    """

    def __init__(
        self,
        reg: Optional[Any] = None,
        skip_factors: Optional[frozenset] = None,
        add_time_features: bool = True,
    ) -> None:
        if reg is None:
            from core.factor_registry import registry as _global_registry
            self._reg = _global_registry
        else:
            self._reg = reg
        self._skip = skip_factors if skip_factors is not None else _SKIP_IN_DEFAULT
        self.add_time_features = add_time_features

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        data: pd.DataFrame,
        symbol: str = '',
        forward_days: int = 2,
        min_return: float = 0.0,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        构建特征矩阵和标签。

        Parameters
        ----------
        data : DataFrame
            包含 OHLCV 列的日线 K 线（索引为 DatetimeIndex）
        symbol : str
            标的代码（写入 Signal.symbol，影响部分因子）
        forward_days : int
            预测未来第 N 日的涨跌（默认 2，避免前视偏差）
        min_return : float
            最小收益阈值，超过则 y=1（默认 0.0 即正负两分）

        Returns
        -------
        X : DataFrame, shape (n_valid, n_features)
            特征矩阵（已去掉含 NaN 的行）
        y : Series, shape (n_valid,)
            标签：1=明日收益>min_return，0=否
        """
        features = self._extract_factor_features(data, symbol)

        if self.add_time_features:
            time_feats = self._time_features(data.index)
            features = pd.concat([features, time_feats], axis=1)

        # 构造标签：收盘价 N 日后收益
        fwd_ret = data['close'].pct_change().shift(-forward_days)
        y_raw = (fwd_ret > min_return).astype(int)

        # 对齐并去掉 NaN 行
        combined = pd.concat([features, y_raw.rename('__label__')], axis=1)
        combined = combined.dropna()

        X = combined.drop(columns=['__label__'])
        y = combined['__label__']

        return X, y

    def build_predict_row(
        self,
        data: pd.DataFrame,
        symbol: str = '',
    ) -> pd.DataFrame:
        """
        为实时预测构建最新一行特征（无标签）。

        Returns
        -------
        DataFrame, shape (1, n_features) — 最新一行特征
        """
        features = self._extract_factor_features(data, symbol)
        if self.add_time_features:
            time_feats = self._time_features(data.index)
            features = pd.concat([features, time_feats], axis=1)

        row = features.iloc[[-1]].copy()
        # 用前向填充处理 NaN
        row = row.ffill().fillna(0.0)
        return row

    def feature_names(self, data: pd.DataFrame, symbol: str = '') -> List[str]:
        """返回特征列名列表（用于模型解释）。"""
        X, _ = self.build(data, symbol)
        return list(X.columns)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_factor_features(
        self,
        data: pd.DataFrame,
        symbol: str,
    ) -> pd.DataFrame:
        """对所有注册因子（跳过名单外）调用 evaluate()，返回宽格式 DataFrame。"""
        series_dict: Dict[str, pd.Series] = {}

        for name in self._reg.list_factors():
            if name in self._skip:
                continue
            try:
                factor = self._reg.create(name)
                if hasattr(factor, 'symbol'):
                    factor.symbol = symbol
                vals = factor.evaluate(data)
                series_dict[name] = vals
            except Exception:
                # 单个因子失败不影响整体
                pass

        if not series_dict:
            return pd.DataFrame(index=data.index)

        return pd.DataFrame(series_dict, index=data.index)

    @staticmethod
    def _time_features(idx: pd.DatetimeIndex) -> pd.DataFrame:
        """生成时间相关特征（周期性编码）。"""
        dow = idx.dayofweek  # 0=Monday
        month = idx.month
        quarter = idx.quarter

        # 季度末标志（3、6、9、12 月最后 5 个交易日附近）
        is_quarter_end = (month % 3 == 0).astype(int)

        # 用正弦/余弦编码（保留周期连续性）
        dow_sin = np.sin(2 * np.pi * dow / 5)
        dow_cos = np.cos(2 * np.pi * dow / 5)
        month_sin = np.sin(2 * np.pi * (month - 1) / 12)
        month_cos = np.cos(2 * np.pi * (month - 1) / 12)

        return pd.DataFrame({
            'time_dow_sin': dow_sin,
            'time_dow_cos': dow_cos,
            'time_month_sin': month_sin,
            'time_month_cos': month_cos,
            'time_is_quarter_end': is_quarter_end,
        }, index=idx)

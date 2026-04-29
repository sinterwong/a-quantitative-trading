"""
core/factors/macro.py — 宏观经济因子库

3 个宏观因子，作为 Regime 判断辅助信号（非直接选股信号）：

  1. PMIFactor              : PMI 制造业扩张/收缩信号（> 50 → 扩张）
  2. M2GrowthFactor         : M2 货币供应增速（流动性宽松 → 看多）
  3. CreditImpulseFactor    : 信贷脉冲（社融增量同比变化，领先经济 6-12 个月）

设计原则：
  - 宏观数据为月度频率；通过前向填充（ffill）对齐到日线索引
  - 无数据时自动降级为全零 Series，不影响流水线运行
  - 所有因子均使用已公布数据，无前视偏差
  - 建议与 Regime 模块联用：宏观因子负值 → 可加大 BEAR 状态触发灵敏度

数据来源（AKShare，均可离线 mock）：
  - PMI：`akshare.macro_china_pmi_monthly()`
  - M2：`akshare.macro_china_money_supply_bal()` 列 'm2_yoy'
  - 社融：`akshare.macro_china_shrzgm()` 列 'value'

用法::

    from core.factors.macro import PMIFactor, M2GrowthFactor, CreditImpulseFactor
    import pandas as pd

    # 直接传入宏观数据（月度 DataFrame，index 为日期）
    factor = PMIFactor(pmi_data=pmi_df)
    scores = factor.evaluate(price_data)   # pd.Series，z-score

    # 无数据时自动降级（适合单元测试）
    factor_no_data = PMIFactor()
    scores = factor_no_data.evaluate(price_data)   # 全零 Series
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _align_macro(
    macro_data: Optional[pd.DataFrame],
    target_index: pd.Index,
    col: str,
) -> pd.Series:
    """
    将月度宏观数据前向填充对齐到日线索引。

    Parameters
    ----------
    macro_data : pd.DataFrame or None
        宏观数据，index 为日期（月末或任意月内日期），含 col 列
    target_index : pd.Index
        目标日线索引（DatetimeIndex 或可转换类型）
    col : str
        需要提取的列名

    Returns
    -------
    pd.Series
        对齐后的 Series，无数据时返回全 NaN
    """
    if macro_data is None or macro_data.empty or col not in macro_data.columns:
        return pd.Series(np.nan, index=target_index)

    try:
        macro_series = macro_data[col].copy()
        macro_series.index = pd.to_datetime(macro_series.index)
        target_dt = pd.to_datetime(target_index)
        combined = macro_series.reindex(
            macro_series.index.union(target_dt)
        ).ffill()
        aligned = combined.reindex(target_dt)
        aligned.index = target_index
        return aligned
    except Exception:
        return pd.Series(np.nan, index=target_index)


# ---------------------------------------------------------------------------
# 1. PMI 因子
# ---------------------------------------------------------------------------

class PMIFactor(Factor):
    """
    PMI 制造业景气因子。

    PMI > 50：制造业扩张 → 正向信号
    PMI < 50：制造业收缩 → 负向信号
    PMI 变化趋势（加速/减速）提供更及时的信号。

    因子值 = Z-score((PMI - 50) + momentum)
      momentum = PMI - PMI.shift(trend_window)（捕捉趋势变化）

    Parameters
    ----------
    pmi_data : pd.DataFrame, optional
        月度 PMI 数据，需含 'pmi' 列（数值在 45-60 之间），index 为日期
    trend_window : int
        趋势动量窗口（月度数据，默认 3 个月）
    """

    name = 'PMI'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        pmi_data: Optional[pd.DataFrame] = None,
        trend_window: int = 3,
    ):
        self.pmi_data = pmi_data
        self.trend_window = trend_window

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        pmi = _align_macro(self.pmi_data, data.index, 'pmi')

        if pmi.isna().all():
            return pd.Series(0.0, index=data.index)

        # 中心化：PMI - 50（>0 扩张，<0 收缩）
        centered = pmi - 50.0

        # 趋势动量（捕捉 PMI 加速改善/恶化）
        momentum = centered - centered.shift(self.trend_window)

        raw = centered + momentum * 0.5  # 结合水平与动量
        raw = raw.ffill()

        return self.normalize(raw.fillna(0.0))


# ---------------------------------------------------------------------------
# 2. M2 货币供应增速因子
# ---------------------------------------------------------------------------

class M2GrowthFactor(Factor):
    """
    M2 货币供应量同比增速因子。

    M2 增速加快 → 流动性宽松 → 估值扩张 → 看多
    M2 增速放缓 → 流动性收紧 → 估值压制 → 看空

    因子值 = Z-score(m2_yoy + acceleration)
      acceleration = m2_yoy - m2_yoy.shift(accel_window)

    Parameters
    ----------
    m2_data : pd.DataFrame, optional
        月度 M2 数据，需含 'm2_yoy' 列（同比增速，单位 %），index 为日期
    accel_window : int
        加速度计算窗口（月度，默认 3）
    smooth_window : int
        日线平滑窗口（天，默认 60）
    """

    name = 'M2Growth'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        m2_data: Optional[pd.DataFrame] = None,
        accel_window: int = 3,
        smooth_window: int = 60,
    ):
        self.m2_data = m2_data
        self.accel_window = accel_window
        self.smooth_window = smooth_window

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        m2_yoy = _align_macro(self.m2_data, data.index, 'm2_yoy')

        if m2_yoy.isna().all():
            return pd.Series(0.0, index=data.index)

        # 增速加速度（捕捉货币政策转向）
        acceleration = m2_yoy - m2_yoy.shift(self.accel_window)

        raw = m2_yoy + acceleration * 0.3
        raw = raw.ffill()

        # 日线平滑（月度数据前向填充后有阶梯跳跃，滚动平均更平滑）
        smoothed = raw.rolling(self.smooth_window, min_periods=1).mean()

        return self.normalize(smoothed.fillna(0.0))


# ---------------------------------------------------------------------------
# 3. 信贷脉冲因子
# ---------------------------------------------------------------------------

class CreditImpulseFactor(Factor):
    """
    信贷脉冲因子（社融增量同比变化）。

    信贷脉冲 = 社融增量 / GDP（或月度社融增量同比变化率）
    领先实体经济 6-12 个月，可作为股市中期趋势的前瞻指标。

    正向信贷脉冲 → 未来经济扩张 → 看多
    负向信贷脉冲 → 未来经济收缩 → 看空

    因子值 = Z-score(社融同比增量的月度变化率)

    Parameters
    ----------
    credit_data : pd.DataFrame, optional
        月度社融数据，需含 'credit_yoy' 列（社融同比增速，%），index 为日期
        或含 'value' 列（社融绝对规模，亿元）
    impulse_window : int
        脉冲计算窗口（月度，默认 6 个月）
    smooth_window : int
        日线平滑窗口（天，默认 90）
    """

    name = 'CreditImpulse'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        credit_data: Optional[pd.DataFrame] = None,
        impulse_window: int = 6,
        smooth_window: int = 90,
    ):
        self.credit_data = credit_data
        self.impulse_window = impulse_window
        self.smooth_window = smooth_window

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        # 优先使用同比增速列，fallback 到绝对值列
        credit = _align_macro(self.credit_data, data.index, 'credit_yoy')
        if credit.isna().all():
            credit = _align_macro(self.credit_data, data.index, 'value')
            if not credit.isna().all():
                # 绝对值转同比增速（近似）
                credit = credit.pct_change(periods=self.impulse_window) * 100.0

        if credit.isna().all():
            return pd.Series(0.0, index=data.index)

        # 信贷脉冲 = 同比增速的变化量（二阶导数，捕捉增速加速/减速）
        impulse = credit - credit.shift(self.impulse_window)
        impulse = impulse.ffill()

        # 日线平滑
        smoothed = impulse.rolling(self.smooth_window, min_periods=1).mean()

        return self.normalize(smoothed.fillna(0.0))

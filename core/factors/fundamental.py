"""
core/factors/fundamental.py — 基本面因子库

5 个因子，均继承 Factor 基类：

  1. PEPercentileFactor      : PE 百分位（估值低→超卖，BUY 信号）
  2. ROEMomentumFactor       : ROE 季度同比变化（盈利改善→BUY）
  3. EarningsSurpriseFactor  : 财报超预期因子（实际 vs 预期 EPS）
  4. RevenueGrowthFactor     : 营收同比增速（增速加速→BUY）
  5. CashFlowQualityFactor   : 现金流质量（OCF/净利润，>1 为高质量）

设计原则：
  - 构建时传入 financial_data（FundamentalDataManager.get_fundamentals() 返回值）
  - evaluate(price_data) 将财务日频数据对齐至 price_data 的索引
  - 无财务数据时返回全零 Series（退化降级，不影响流水线运行）
  - 所有因子均使用已公告数据，无前视偏差

用法：
    from core.fundamental_data import FundamentalDataManager
    from core.factors.fundamental import PEPercentileFactor

    mgr = FundamentalDataManager()
    fin = mgr.get_fundamentals('000001.SZ')

    factor = PEPercentileFactor(financial_data=fin, lookback_years=3)
    z_scores = factor.evaluate(price_df)
"""

from __future__ import annotations

from typing import List, Optional
import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal


def _align_financial(
    financial_data: Optional[pd.DataFrame],
    price_index: pd.Index,
    column: str,
) -> pd.Series:
    """
    将财务数据的指定列对齐到价格数据索引。
    缺失点前向填充，若列不存在或无数据则返回全 NaN Series。
    """
    if financial_data is None or financial_data.empty or column not in financial_data.columns:
        return pd.Series(np.nan, index=price_index)

    series = financial_data[column].reindex(price_index, method='ffill')
    return series


# ---------------------------------------------------------------------------
# 1. PE 百分位因子
# ---------------------------------------------------------------------------

class PEPercentileFactor(Factor):
    """
    PE 百分位因子（估值分位数）。

    因子值 = -percentile_rank(pe_ttm, lookback)
    （取负：PE 百分位越低 → 估值越便宜 → 因子值越高 → BUY 信号）

    解读：
      - z > threshold：PE 处于历史低位，低估，BUY
      - z < -threshold：PE 处于历史高位，高估，SELL

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    lookback_years : 百分位计算的回溯年数（默认 3 年 = ~756 个交易日）
    """

    name = 'PEPercentile'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        lookback_years: int = 3,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.lookback_years = lookback_years
        self.lookback_days = lookback_years * 252
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        pe = _align_financial(self.financial_data, data.index, 'pe_ttm')

        if pe.isna().all():
            return pd.Series(0.0, index=data.index)

        # 滚动百分位（当前 PE 在过去 N 天中的分位数）
        def rolling_pct_rank(s: pd.Series, window: int) -> pd.Series:
            def _rank(x: np.ndarray) -> float:
                # raw=True: x 已是 numpy array，无需 .values
                if len(x) < 2 or np.isnan(x[-1]):
                    return np.nan
                valid = x[~np.isnan(x)]
                if len(valid) < 2:
                    return np.nan
                return float(np.sum(valid <= x[-1]) / len(valid))
            return s.rolling(window, min_periods=max(10, window // 10)).apply(
                _rank, raw=True
            )

        pct_rank = rolling_pct_rank(pe, self.lookback_days)
        # 取负：低 PE 百分位 → 高因子值
        raw = -pct_rank.fillna(0.0)

        return self.normalize(raw)


# ---------------------------------------------------------------------------
# 2. ROE 动量因子
# ---------------------------------------------------------------------------

class ROEMomentumFactor(Factor):
    """
    ROE 季度同比变化因子（盈利改善动量）。

    因子值 = ROE_ttm_t - ROE_ttm_{t-252}（一年前对比）
    解读：ROE 持续改善 → 盈利质量提升 → 中期 BUY 信号

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    diff_days      : 同比比较窗口（默认 252 = 约 1 年）
    """

    name = 'ROEMomentum'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        diff_days: int = 252,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.diff_days = diff_days
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        roe = _align_financial(self.financial_data, data.index, 'roe_ttm')

        if roe.isna().all():
            return pd.Series(0.0, index=data.index)

        roe_diff = roe - roe.shift(self.diff_days)

        return self.normalize(roe_diff.fillna(0.0))


# ---------------------------------------------------------------------------
# 3. 财报超预期因子
# ---------------------------------------------------------------------------

class EarningsSurpriseFactor(Factor):
    """
    财报超预期因子（Earnings Surprise）。

    由于 A 股无统一分析师预期数据库（Bloomberg/Wind 收费），
    此处用 EPS 自身动量代理：

    因子值 = (EPS_ttm_t - EPS_ttm_{t-252}) / |EPS_ttm_{t-252}|
           = EPS 同比增长率

    正值：EPS 同比增长（超预期方向）→ BUY
    负值：EPS 同比下滑（低于预期）→ SELL

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    diff_days      : 同比窗口（默认 252 天 ≈ 1 年）
    """

    name = 'EarningsSurprise'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        diff_days: int = 252,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.diff_days = diff_days
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        eps = _align_financial(self.financial_data, data.index, 'eps_ttm')

        if eps.isna().all():
            return pd.Series(0.0, index=data.index)

        eps_lag = eps.shift(self.diff_days)
        # 同比增长率（避免除零：分母取绝对值 + 1e-6）
        yoy = (eps - eps_lag) / (eps_lag.abs() + 1e-6)
        # 截断极端值（>300% 或 <-100% 视为异常）
        yoy = yoy.clip(-1.0, 3.0)

        return self.normalize(yoy.fillna(0.0))


# ---------------------------------------------------------------------------
# 4. 营收增速因子
# ---------------------------------------------------------------------------

class RevenueGrowthFactor(Factor):
    """
    营收同比增速因子。

    若 financial_data 包含 'revenue_yoy' 列（单位：%），直接使用。
    否则返回全零。

    解读：
      - 营收增速高且加速 → 业绩驱动向上 → BUY
      - 营收增速低且减速 → 基本面恶化 → SELL

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    accel_window   : 增速加速度窗口（比较近期增速 vs 更早增速），默认 60 天
    """

    name = 'RevenueGrowth'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        accel_window: int = 60,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.accel_window = accel_window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        rev_yoy = _align_financial(self.financial_data, data.index, 'revenue_yoy')

        if rev_yoy.isna().all():
            return pd.Series(0.0, index=data.index)

        # 增速水平 + 增速加速度（近期增速 - accel_window 前的增速）
        accel = rev_yoy - rev_yoy.shift(self.accel_window)
        raw = rev_yoy + accel  # 结合绝对水平与加速度

        return self.normalize(raw.fillna(0.0))


# ---------------------------------------------------------------------------
# 5. 现金流质量因子
# ---------------------------------------------------------------------------

class CashFlowQualityFactor(Factor):
    """
    现金流质量因子（OCF / 净利润）。

    比率 > 1：现金流充裕，利润质量高（现金回收好）
    比率 < 1：利润无法转化为现金（应收账款堆积等）
    比率 < 0：经营现金流为负（严重预警）

    因子值 = 滚动均值(ocf_to_profit, window) - 1
    （正值表示现金流质量高于平价）

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    rolling_window : 平滑窗口（默认 60 天）
    """

    name = 'CashFlowQuality'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        rolling_window: int = 60,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.rolling_window = rolling_window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        ocf_ratio = _align_financial(self.financial_data, data.index, 'ocf_to_profit')

        if ocf_ratio.isna().all():
            return pd.Series(0.0, index=data.index)

        # 截断极端值（OCF/利润极端高时意义有限）
        ocf_clipped = ocf_ratio.clip(-3.0, 5.0)
        # 滚动均值（季报数据前向填充，滚动平滑减少噪声）
        smoothed = ocf_clipped.rolling(self.rolling_window, min_periods=1).mean()
        raw = smoothed - 1.0  # 中心化：>0 质量好，<0 质量差

        return self.normalize(raw.fillna(0.0))

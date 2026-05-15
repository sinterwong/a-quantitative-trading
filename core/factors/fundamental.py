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

        # 数据不足 1 年窗口（< 252 个有效点）时同比值全为 NaN，
        # 直接返回零而非静默替换为价值信号（避免因子语义漂移）
        if roe_diff.isna().all():
            return pd.Series(0.0, index=data.index)

        return self.normalize(roe_diff.fillna(0.0))


# ---------------------------------------------------------------------------
# 3. 财报超预期因子
# ---------------------------------------------------------------------------

class EarningsSurpriseFactor(Factor):
    """
    财报超预期因子（Earnings Surprise）。

    由于 A 股无统一分析师预期数据库（Bloomberg/Wind 收费），
    此处用 EPS 自身动量代理。两种数据路径(W1-4 起):

    优先(数据源直接提供 eps_yoy 时):
      因子值 = eps_yoy / 100   (AkShare EPSJBHBZC 已是百分比)

    Fallback(无 eps_yoy 时):
      因子值 = (EPS_ttm_t - EPS_ttm_{t-252}) / |EPS_ttm_{t-252}|

    正值：EPS 同比增长（超预期方向）→ BUY
    负值：EPS 同比下滑（低于预期）→ SELL

    Parameters
    ----------
    financial_data : FundamentalDataManager.get_fundamentals() 的返回值
    diff_days      : 同比窗口（默认 252 天 ≈ 1 年，仅 fallback 路径使用）
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
        # 优先路径:直接消费数据源提供的 eps_yoy(%)
        eps_yoy_direct = _align_financial(self.financial_data, data.index, 'eps_yoy')
        if not eps_yoy_direct.isna().all():
            # eps_yoy 是百分比(20.0 = 20%),除以 100 与 fallback 自算口径一致
            yoy = (eps_yoy_direct / 100.0).clip(-1.0, 3.0)
            return self.normalize(yoy.fillna(0.0))

        # Fallback:从 eps_ttm 自算同比
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


# ---------------------------------------------------------------------------
# 6. 股东变动因子（筹码集中度）
# ---------------------------------------------------------------------------

class ShareholderConcentrationFactor(Factor):
    """
    股东变动因子（筹码集中度信号）。

    逻辑：
      - 股东人数减少 → 筹码向大户集中 → 机构/大股东看好 → 正向信号
      - 股东人数增加 → 筹码分散 → 散户涌入 → 中性/负向信号

    因子值 = -1 × 股东人数季度变化率（单位：%）
      正值 = 股东人数下降（筹码集中）→ 看多
      负值 = 股东人数上升（筹码分散）→ 看空

    数据来源：
      AKShare `stock_hold_num_cninfo(symbol)` 季度股东人数
      financial_data 需包含 'holder_num' 列（股东人数，单位：万人）

    无数据时自动降级为全零，不影响流水线运行。

    Parameters
    ----------
    financial_data : pd.DataFrame, optional
        含 'holder_num' 列，index 为日期（季度末）
    rolling_window : int
        滚动平滑窗口（默认 120 天，跨越约 2 个季报）
    """

    name = 'ShareholderConcentration'
    category = FactorCategory.FUNDAMENTAL

    def __init__(
        self,
        financial_data: Optional[pd.DataFrame] = None,
        rolling_window: int = 120,
        symbol: str = '',
    ):
        self.financial_data = financial_data
        self.rolling_window = rolling_window
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        holder_num = _align_financial(self.financial_data, data.index, 'holder_num')

        if holder_num.isna().all():
            return pd.Series(0.0, index=data.index)

        # 季度环比变化率（约 63 个交易日 ≈ 1 季度）
        qoq_change = holder_num.pct_change(periods=63) * 100.0  # 单位：%

        # 取反：股东人数下降 → 因子值为正（筹码集中看多）
        raw = -qoq_change

        # 滚动平滑（季报数据非连续，跨周期平均降低噪声）
        smoothed = raw.rolling(self.rolling_window, min_periods=1).mean()

        return self.normalize(smoothed.fillna(0.0))


# ---------------------------------------------------------------------------
# 7. 财务健康因子（W1-5）
# ---------------------------------------------------------------------------

class FinancialHealthFactor(Factor):
    """
    财务健康度因子(Altman-Z 简化版)。

    合成三个公司财务健康维度:
      - 偿债压力(取负):debt_to_equity 越高 → 越不健康 → 因子分量为负
      - 短期流动性  :current_ratio 越高 → 越健康
      - 盈利质量    :ocf_to_profit 越高 → 越健康

    因子值 = z(-debt_to_equity) + z(current_ratio) + z(ocf_to_profit),
            再做总体 z-score。

    解读:
      - z > threshold:财务质量显著高于均值 → 配置加权
      - z < -threshold:财务质量明显恶化 → SELL

    数据来源:
      DataGateway.fundamentals_history()
      - Baostock 提供 debt_to_equity / current_ratio (W1-2)
      - Akshare 或 Baostock 提供 ocf_to_profit

    任意一项缺失时使用其它两项,均缺失时返回全零(降级)。

    Parameters
    ----------
    financial_data : pd.DataFrame, optional
        需含 debt_to_equity / current_ratio / ocf_to_profit 列。
    rolling_window : int
        平滑窗口(默认 60 天)。
    """

    name = 'FinancialHealth'
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
        debt = _align_financial(self.financial_data, data.index, 'debt_to_equity')
        cr = _align_financial(self.financial_data, data.index, 'current_ratio')
        ocf = _align_financial(self.financial_data, data.index, 'ocf_to_profit')

        # 三项均缺失则降级
        if debt.isna().all() and cr.isna().all() and ocf.isna().all():
            return pd.Series(0.0, index=data.index)

        # 各分量先平滑去噪 + 缺失补 0
        def _smooth(s: pd.Series) -> pd.Series:
            return s.rolling(self.rolling_window, min_periods=1).mean().fillna(0.0)

        debt_s = _smooth(debt) if not debt.isna().all() else pd.Series(0.0, index=data.index)
        cr_s = _smooth(cr) if not cr.isna().all() else pd.Series(0.0, index=data.index)
        ocf_s = _smooth(ocf) if not ocf.isna().all() else pd.Series(0.0, index=data.index)

        # 简单 z 拼接:对各分量分别 z-score,避免量纲差异(% vs 倍数)
        def _z(s: pd.Series) -> pd.Series:
            mu = s.mean()
            std = s.std(ddof=0) or 1.0
            return (s - mu) / std

        # debt_to_equity 是越低越好,所以取负
        composite = -_z(debt_s) + _z(cr_s) + _z(ocf_s)
        return self.normalize(composite.fillna(0.0))


# ---------------------------------------------------------------------------
# 8. 股息率因子（W1-6）
# ---------------------------------------------------------------------------

class DividendYieldFactor(Factor):
    """
    股息率因子(价值/防御偏置)。

    因子值 = percentile_rank(dividend_yield, lookback)
    高股息率分位 → 股价相对估值低,资金有"现金回报"安全垫 → BUY

    与 PEPercentileFactor 的关键差异:
      - PE 百分位反映"市场估值低",可能伴随基本面恶化
      - 股息率高百分位反映"分红能力强",更稳健的价值信号

    数据来源:
      Fundamentals.dividend_yield 字段 + Akshare DIVIDENDYIELD 列(W1-1)。

    若数据源不提供 dividend_yield(常见),因子降级为零。

    Parameters
    ----------
    financial_data : 含 dividend_yield 列的 DataFrame(可能由 quote 字段补充)
    lookback_years : 百分位计算窗口(年,默认 3 年)
    """

    name = 'DividendYield'
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
        dy = _align_financial(self.financial_data, data.index, 'dividend_yield')
        if dy.isna().all():
            return pd.Series(0.0, index=data.index)

        # 滚动历史百分位:当前股息率在过去 N 天中的相对位置
        def _pct_rank(arr: np.ndarray) -> float:
            if len(arr) < 2 or np.isnan(arr[-1]):
                return np.nan
            valid = arr[~np.isnan(arr)]
            if len(valid) < 2:
                return np.nan
            return float(np.sum(valid <= arr[-1]) / len(valid))

        pct_rank = dy.rolling(
            self.lookback_days, min_periods=max(10, self.lookback_days // 10),
        ).apply(_pct_rank, raw=True)

        # 高分位 → 高因子值(正向)
        return self.normalize(pct_rank.fillna(0.0))

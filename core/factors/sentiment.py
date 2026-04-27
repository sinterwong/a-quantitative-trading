"""
core/factors/sentiment.py — 情绪因子库

3 个市场情绪因子，基于 A 股特色数据源：

  1. MarginTradingFactor  : 融资余额变化率（融资盘增量 = 看多情绪升温）
  2. NorthboundFlowFactor : 北向资金净流入强度（外资持续买入 = 看多信号）
  3. ShortInterestFactor  : 融券余额变化率（融券增加 = 看空压力上升）

数据来源：
  - 融资融券：AKShare stock_margin_detail()，存入 data/sentiment/ 缓存
  - 北向资金：复用 core/external_signal.py 的 NorthboundStatsAnalyzer 数据接口，
    或直接接受注入的 DataFrame

设计原则：
  - 同样接受 sentiment_data: pd.DataFrame（外部注入，防止因子内部网络调用）
  - 无数据时返回全零（降级不崩溃）
  - 数据频率为日频（每日收盘后更新）

用法：
    # 方式一：直接构造（无数据 → 全零）
    f = MarginTradingFactor()
    z = f.evaluate(price_df)

    # 方式二：注入已获取的数据
    margin_df = pd.DataFrame({'margin_balance': [...]}, index=dates)
    f = MarginTradingFactor(sentiment_data=margin_df)
    z = f.evaluate(price_df)
"""

from __future__ import annotations

from typing import List, Optional
import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal


def _align_sentiment(
    sentiment_data: Optional[pd.DataFrame],
    price_index: pd.Index,
    column: str,
    fill_method: str = 'ffill',
) -> pd.Series:
    """
    将情绪数据对齐到价格数据索引（前向填充）。
    缺失或不存在时返回全 NaN Series。
    """
    if sentiment_data is None or sentiment_data.empty:
        return pd.Series(np.nan, index=price_index)
    if column not in sentiment_data.columns:
        return pd.Series(np.nan, index=price_index)
    series = sentiment_data[column].reindex(price_index, method=fill_method)
    return series


# ---------------------------------------------------------------------------
# 1. 融资余额变化率因子
# ---------------------------------------------------------------------------

class MarginTradingFactor(Factor):
    """
    融资余额变化率因子（A 股融资盘动量）。

    因子值 = 融资余额短期变化率 vs 长期变化率（加速度）
    = (rolling_mean(Δmargin/margin, short)) / (rolling_mean(Δmargin/margin, long))

    解读：
      - z > 0：融资买入加速（看多情绪升温）→ 中期跟随 BUY
      - z < 0：融资买入减速或净偿还（去杠杆）→ 短期谨慎

    注意：融资过热（连续快速增长）本身也是尾部风险信号，
    因此本因子权重建议 ≤ 0.15。

    Parameters
    ----------
    sentiment_data : pd.DataFrame
        需包含列 'margin_balance'（融资余额，元）。
        由调用方从 AKShare stock_margin_detail() 获取并传入。
    short_window   : 短期滚动窗口（默认 5 天）
    long_window    : 长期滚动窗口（默认 20 天）
    """

    name = 'MarginTrading'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        short_window: int = 5,
        long_window: int = 20,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.short_window = short_window
        self.long_window = long_window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        margin = _align_sentiment(self.sentiment_data, data.index, 'margin_balance')

        if margin.isna().all():
            return pd.Series(0.0, index=data.index)

        # 日变化率
        margin_chg = margin.pct_change().fillna(0.0)

        # 短期 vs 长期加速度
        short_ma = margin_chg.rolling(self.short_window, min_periods=1).mean()
        long_ma = margin_chg.rolling(self.long_window, min_periods=1).mean()
        accel = short_ma - long_ma

        return self.normalize(accel)


# ---------------------------------------------------------------------------
# 2. 北向资金净流入因子
# ---------------------------------------------------------------------------

class NorthboundFlowFactor(Factor):
    """
    北向资金净流入强度因子。

    因子值 = 滚动均值(北向净流入, window) / 历史标准差
    = 净流入的 z-score（相对历史波动性）

    解读：
      - z > threshold：北向持续大额净买入 → 外资看多 → BUY
      - z < -threshold：北向持续大额净卖出 → 外资撤离 → SELL

    Parameters
    ----------
    sentiment_data : pd.DataFrame
        需包含列 'north_flow'（北向净流入，亿元/天）。
        可来自 AKShare stock_connect_north_net_flow_in() 或
        DataLayer.get_north_flow()。
    window : 平滑窗口（默认 5 天）
    """

    name = 'NorthboundFlow'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        window: int = 5,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        flow = _align_sentiment(self.sentiment_data, data.index, 'north_flow')

        if flow.isna().all():
            return pd.Series(0.0, index=data.index)

        # 滚动均值平滑（减少单日噪声）
        smoothed = flow.fillna(0.0).rolling(self.window, min_periods=1).mean()

        return self.normalize(smoothed)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: float = 1.0,
    ) -> List[Signal]:
        """北向资金信号：持续净买入 → BUY，持续净卖出 → SELL"""
        latest = factor_values.iloc[-1]
        from datetime import datetime

        if latest > threshold:
            strength = min((latest - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'north_flow_zscore': round(float(latest), 3)},
            )]
        if latest < -threshold:
            strength = min((abs(latest) - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'north_flow_zscore': round(float(latest), 3)},
            )]
        return []


# ---------------------------------------------------------------------------
# 3. 融券余额变化率因子（做空压力）
# ---------------------------------------------------------------------------

class ShortInterestFactor(Factor):
    """
    融券余额变化率因子（做空压力指标）。

    因子值 = -rolling_mean(Δshort_balance/short_balance, window)
    （取负值：融券增加 → 做空压力上升 → 因子值为负 → SELL 方向）

    解读：
      - z < -threshold：融券余额快速增加（做空压力大）→ SELL
      - z > threshold：融券余额快速减少（空头回补 / 看空情绪退潮）→ BUY

    注意：A 股融券规模远小于融资，此因子信号强度通常偏弱，
    建议结合其他因子使用。

    Parameters
    ----------
    sentiment_data : pd.DataFrame
        需包含列 'short_balance'（融券余额，元）。
    window : 变化率平滑窗口（默认 10 天）
    """

    name = 'ShortInterest'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        window: int = 10,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        short_bal = _align_sentiment(self.sentiment_data, data.index, 'short_balance')

        if short_bal.isna().all():
            return pd.Series(0.0, index=data.index)

        # 日变化率（融券增加为正值）
        short_chg = short_bal.pct_change().fillna(0.0)
        smoothed = short_chg.rolling(self.window, min_periods=1).mean()

        # 取负：融券增加 → 因子值为负（SELL 方向）
        return self.normalize(-smoothed)

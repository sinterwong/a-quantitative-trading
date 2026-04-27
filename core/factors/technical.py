"""
core/factors/technical.py — 扩展技术因子库

包含三类因子：
  1. 分钟级代理因子（基于日线 OHLCV 计算）
     - IntraVWAPFactor     : 日内 VWAP 偏离度（典型价 vs 收盘价）
     - OpenGapFactor       : 开盘缺口（今日开盘 vs 昨日收盘）
     - VolAccelerationFactor: 成交量加速度（滚动量能环比）

  2. 市场微观结构代理因子（日线代理，Level2 接入后精度提升）
     - BidAskSpreadFactor  : 买卖价差代理（日内振幅 / 收盘价）
     - BuyingPressureFactor: 买方压力（(Close-Low)/(High-Low) × Volume 加权）

  3. 跨品种关联因子
     - SectorMomentumFactor     : 行业 ETF 相对动量
     - IndexRelativeStrengthFactor: 个股相对指数（沪深300）超额收益动量

设计原则：
  - 全部继承 Factor 基类，evaluate(data) → z-score 归一化 Series
  - 跨品种因子（Sector/Index）需在 __init__ 传入参考数据 DataFrame
  - 对齐 data.index 与参考数据 index（取交集），缺失日期填 0
"""

from __future__ import annotations

from typing import List, Optional
import pandas as pd
import numpy as np

from core.factors.base import Factor, FactorCategory, Signal


# ---------------------------------------------------------------------------
# 1. 分钟级代理因子（日线 OHLCV）
# ---------------------------------------------------------------------------

class IntraVWAPFactor(Factor):
    """
    日内 VWAP 偏离度因子。

    Proxy：典型价 = (High + Low + Close) / 3（OHLC VWAP 近似）
    因子值 = (Close - TypicalPrice) / TypicalPrice，滚动 z-score 归一化。

    解读：
      - z > 0：收盘价显著高于日内均价 → 日内买方强势（短期超买倾向）
      - z < 0：收盘价显著低于日内均价 → 日内卖方强势（短期超卖倾向）

    信号：z < -threshold → BUY（超卖回归），z > threshold → SELL
    """

    name = 'IntraVWAP'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, window: int = 20, threshold: float = 1.0, symbol: str = ''):
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        close = data['close']
        high = data['high']
        low = data['low']

        typical = (high + low + close) / 3.0
        deviation = (close - typical) / (typical + 1e-10)

        return self.normalize(deviation)


class OpenGapFactor(Factor):
    """
    开盘缺口因子。

    因子值 = (Open_t - Close_{t-1}) / Close_{t-1}
    正值：跳空高开（gap up）；负值：跳空低开（gap down）。

    解读：
      - 跳空高开后往往有回补倾向（均值回归），z > threshold → SELL
      - 跳空低开后可能反弹，z < -threshold → BUY
    """

    name = 'OpenGap'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, window: int = 20, threshold: float = 1.0, symbol: str = ''):
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        open_ = data['open']
        prev_close = data['close'].shift(1)

        gap = (open_ - prev_close) / (prev_close + 1e-10)

        return self.normalize(gap.fillna(0.0))


class VolAccelerationFactor(Factor):
    """
    成交量加速度因子。

    因子值 = 短期均量 / 长期均量 - 1
    = (rolling_mean(volume, short) / rolling_mean(volume, long)) - 1

    正值：近期成交量放大（潜在趋势加速）
    负值：近期成交量萎缩（动能衰减）

    解读：
      - z > threshold：量能大幅放大，结合价格方向判断趋势 → 跟随信号
      - z < -threshold：量能萎缩，趋势减速
    """

    name = 'VolAcceleration'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(
        self,
        short_window: int = 5,
        long_window: int = 20,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.short_window = short_window
        self.long_window = long_window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        vol = data['volume'].replace(0, np.nan)

        short_ma = vol.rolling(self.short_window, min_periods=1).mean()
        long_ma = vol.rolling(self.long_window, min_periods=1).mean()

        accel = (short_ma / (long_ma + 1e-10)) - 1.0

        return self.normalize(accel)


# ---------------------------------------------------------------------------
# 2. 市场微观结构代理因子（日线代理）
# ---------------------------------------------------------------------------

class BidAskSpreadFactor(Factor):
    """
    买卖价差代理因子（Garman-Klass 振幅代理）。

    Proxy：相对振幅 = (High - Low) / Close
    振幅越大 → 隐含买卖价差越高 → 流动性越差。

    因子值取负值，使：
      - z < 0（振幅大）→ 流动性差，信号质量低
      - z > 0（振幅小）→ 流动性好，适合交易

    注：此为日线代理，Level2 接入后应替换为真实 Bid-Ask Spread。
    """

    name = 'BidAskSpread'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, window: int = 20, threshold: float = 1.0, symbol: str = ''):
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        high = data['high']
        low = data['low']
        close = data['close']

        # 相对振幅（越大说明流动性越差）
        spread_proxy = (high - low) / (close + 1e-10)

        # 取负值：z > 0 表示流动性好（低振幅）
        return self.normalize(-spread_proxy)

    def signals(self, factor_values: pd.Series, price: float) -> List[Signal]:
        """流动性因子不直接产生方向信号，仅作过滤器使用"""
        return []


class BuyingPressureFactor(Factor):
    """
    买方压力因子（Close Location Value，CLV）。

    CLV = (Close - Low) / (High - Low)，范围 [0, 1]
    - CLV = 1：收盘在最高价（纯买方压力）
    - CLV = 0：收盘在最低价（纯卖方压力）
    - CLV = 0.5：收盘在中间（均衡）

    与成交量加权：买方压力 = CLV × Volume（威廉姆斯量化版）

    解读：
      - z > threshold：持续买方主导 → BUY 动量信号
      - z < -threshold：持续卖方主导 → SELL 动量信号
    """

    name = 'BuyingPressure'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(self, window: int = 10, threshold: float = 1.0, symbol: str = ''):
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume'].replace(0, np.nan)

        rng = (high - low).replace(0, np.nan)
        clv = (close - low) / rng  # [0, 1]，NaN 处理：range=0 时忽略

        # 滚动加权买方压力（CLV × Volume）/ 滚动总成交量
        buy_vol = clv * volume
        roll_buy = buy_vol.rolling(self.window, min_periods=1).sum()
        roll_vol = volume.rolling(self.window, min_periods=1).sum()

        pressure = roll_buy / (roll_vol + 1e-10)

        # 中心化：0.5 为中性
        centered = pressure - 0.5

        return self.normalize(centered)


# ---------------------------------------------------------------------------
# 3. 跨品种关联因子
# ---------------------------------------------------------------------------

class SectorMomentumFactor(Factor):
    """
    行业 ETF 相对动量因子。

    衡量个股所在行业 ETF 的近期动量，作为行业β信号。

    Parameters
    ----------
    sector_data : pd.DataFrame
        行业 ETF 日线 OHLCV 数据（与个股数据对齐，同索引频率）。
        需在构建因子时传入（StrategyRunner 预先获取）。
    momentum_window : int
        动量计算窗口（默认 20 个交易日）。

    若 sector_data 为 None，则因子值恒为 0（退化为无信号）。
    """

    name = 'SectorMomentum'
    category = FactorCategory.EXTERNAL

    def __init__(
        self,
        sector_data: Optional[pd.DataFrame] = None,
        momentum_window: int = 20,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sector_data = sector_data
        self.momentum_window = momentum_window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        if self.sector_data is None or len(self.sector_data) < self.momentum_window:
            return pd.Series(0.0, index=data.index)

        sector_close = self.sector_data['close']

        # 计算行业 ETF 的滚动动量（N日收益率）
        sector_mom = sector_close.pct_change(self.momentum_window)

        # 对齐到 data.index（取交集，缺失日期填 0）
        aligned = sector_mom.reindex(data.index).fillna(0.0)

        return self.normalize(aligned)


class IndexRelativeStrengthFactor(Factor):
    """
    个股相对指数超额收益因子（Relative Strength vs. Index）。

    因子值 = 个股滚动收益率 - 指数（沪深 300）滚动收益率
    = α（超额动量）

    正值：个股跑赢指数 → 强势标的
    负值：个股跑输指数 → 弱势标的

    Parameters
    ----------
    index_data : pd.DataFrame
        沪深 300 指数日线数据（或其他基准）。
        需在构建因子时传入（StrategyRunner 预先获取）。
    window : int
        滚动收益率窗口（默认 20 个交易日）。
    """

    name = 'IndexRelativeStrength'
    category = FactorCategory.EXTERNAL

    def __init__(
        self,
        index_data: Optional[pd.DataFrame] = None,
        window: int = 20,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.index_data = index_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        stock_ret = data['close'].pct_change(self.window)

        if self.index_data is None or len(self.index_data) < self.window:
            # 无基准数据时退化为纯价格动量（NaN 填 0，避免早期缺失值传播）
            return self.normalize(stock_ret.fillna(0.0))

        index_ret = self.index_data['close'].pct_change(self.window)
        index_aligned = index_ret.reindex(data.index).fillna(0.0)

        relative = (stock_ret - index_aligned).fillna(0.0)

        return self.normalize(relative)

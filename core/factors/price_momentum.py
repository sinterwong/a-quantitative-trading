"""
RSI 因子 — 相对强弱指数因子
"""

from __future__ import annotations
from typing import List, Optional
import pandas as pd
import numpy as np
from core.factors.base import Factor, FactorCategory, Signal


class RSIFactor(Factor):
    """
    RSI(14) 因子。
    evaluate() 返回 z-score 归一化的 RSI 偏离度：
      - z > 0：价格接近 RSI 高估区
      - z < 0：价格接近 RSI 低估区
    signals() 生成 RSI 超买超卖信号。
    """

    name = 'RSI'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(
        self,
        period: int = 14,
        buy_threshold: float = 30,
        sell_threshold: float = 70,
        symbol: str = '',
    ):
        self.period = period
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        close = data['close']
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        # Wilder 平滑
        avg_gain = gain.ewm(alpha=1/self.period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/self.period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # 偏离度：RSI 距离 50 的距离（越大说明越极端）
        deviation = rsi - 50
        return self.normalize(deviation)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
    ) -> List[Signal]:
        """从最新 RSI 生成信号"""
        latest_rsi = 50 + factor_values.iloc[-1]
        signals = []

        if latest_rsi < self.buy_threshold:
            strength = (self.buy_threshold - latest_rsi) / self.buy_threshold
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=min(strength, 1.0),
                factor_name=self.name,
                price=price,
                metadata={
                    'rsi': round(latest_rsi, 2),
                    'threshold': self.buy_threshold,
                    'raw_factor_value': round(factor_values.iloc[-1], 4),
                }
            ))
        elif latest_rsi > self.sell_threshold:
            strength = (latest_rsi - self.sell_threshold) / (100 - self.sell_threshold)
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=min(strength, 1.0),
                factor_name=self.name,
                price=price,
                metadata={
                    'rsi': round(latest_rsi, 2),
                    'threshold': self.sell_threshold,
                    'raw_factor_value': round(factor_values.iloc[-1], 4),
                }
            ))
        return signals


class BollingerFactor(Factor):
    """
    布林带因子。
    evaluate() 返回 z-score 归一化的布林带位置：
      - z > 0：价格靠近上轨（高估）
      - z < 0：价格靠近下轨（低估）
    """

    name = 'BollingerBands'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(
        self,
        period: int = 20,
        nb_std: float = 2.0,
        symbol: str = '',
    ):
        self.period = period
        self.nb_std = nb_std
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        close = data['close']
        ma = close.rolling(self.period).mean()
        std = close.rolling(self.period).std()
        upper = ma + self.nb_std * std
        lower = ma - self.nb_std * std

        # 布林带位置：0=下轨，0.5=中轨，1=上轨
        bbp = (close - lower) / (upper - lower + 1e-10)
        # 映射到 [-1, 1]，中轨=0
        normalized = (bbp - 0.5) * 2
        return self.normalize(normalized)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        buy_band: float = -0.8,
        sell_band: float = 0.8,
    ) -> List[Signal]:
        latest = factor_values.iloc[-1]
        signals = []
        if latest < buy_band:
            strength = abs(latest)
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=min(strength, 1.0),
                factor_name=self.name,
                price=price,
                metadata={'bbp_normalized': round(latest, 4)}
            ))
        elif latest > sell_band:
            strength = abs(latest)
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=min(strength, 1.0),
                factor_name=self.name,
                price=price,
                metadata={'bbp_normalized': round(latest, 4)}
            ))
        return signals


class MACDFactor(Factor):
    """
    MACD 因子 (12, 26, 9)。
    evaluate() 返回 z-score 归一化的 MACD 直方图。
    signals() 生成 MACD 金叉死叉信号。
    """

    name = 'MACD'
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        symbol: str = '',
    ):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        close = data['close']
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return self.normalize(histogram)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
    ) -> List[Signal]:
        hist = factor_values
        if len(hist) < 2:
            return []
        latest = hist.iloc[-1]
        prev = hist.iloc[-2]
        signals = []

        # 金叉：hist 从负转正
        if prev < 0 <= latest:
            strength = min(abs(latest) / 0.5, 1.0) if latest != 0 else 0.5
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'macd_hist': round(latest, 4), 'cross': 'golden'}
            ))
        # 死叉：hist 从正转负
        elif prev > 0 >= latest:
            strength = min(abs(latest) / 0.5, 1.0) if latest != 0 else 0.5
            signals.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'macd_hist': round(latest, 4), 'cross': 'death'}
            ))
        return signals


class ATRFactor(Factor):
    """
    ATR 波动率因子。
    evaluate() 返回 z-score 归一化的 ATR ratio（当前 ATR / N 日最高 ATR）。
    用于市场环境检测（高波动 vs 低波动）。
    不直接生成交易信号，而是返回波动率水平。
    """

    name = 'ATR'
    category = FactorCategory.REGIME

    def __init__(self, period: int = 14, lookback: int = 20, symbol: str = ''):
        self.period = period
        self.lookback = lookback   # 计算最高 ATR 的窗口
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        high = data['high']
        low = data['low']
        close = data['close']

        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(self.period).mean()

        # ATR ratio = 当前 ATR / N 日 ATR 最高
        atr_max = atr.rolling(self.lookback).max()
        ratio = atr / atr_max.replace(0, 1e-10)

        # 偏离度：ratio 距离 1.0 的距离（越大说明 ATR 越接近历史高点）
        deviation = ratio - 1.0
        return self.normalize(deviation)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
    ) -> List[Signal]:
        # ATR 因子不直接产生交易信号，返回波动率级别
        return []

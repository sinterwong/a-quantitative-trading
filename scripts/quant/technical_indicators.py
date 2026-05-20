"""
scripts/quant/technical_indicators.py — 纯 Python 技术指标库

从原 scripts/quant/backtest.py 拆出,供 signal_generator.py 及各策略脚本使用。
"""
from __future__ import annotations

from typing import List, Optional, Tuple


class TechnicalIndicators:
    """技术指标库"""

    @staticmethod
    def sma(closes: List[float], period: int) -> List[float]:
        if len(closes) < period:
            return []
        result = []
        for i in range(period - 1, len(closes)):
            result.append(sum(closes[i - period + 1:i + 1]) / period)
        return result

    @staticmethod
    def ema(closes: List[float], period: int) -> List[float]:
        if len(closes) < period:
            return []
        multiplier = 2 / (period + 1)
        result = [sum(closes[:period]) / period]
        for i in range(period, len(closes)):
            result.append((closes[i] - result[-1]) * multiplier + result[-1])
        return result

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> List[float]:
        if len(closes) < period + 1:
            return []
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [c if c > 0 else 0 for c in changes]
        losses = [-c if c < 0 else 0 for c in changes]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        result = [50]
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / (avg_loss if avg_loss > 0 else 0.0001)
            result.append(100 - (100 / (1 + rs)))
        return result

    @staticmethod
    def bollinger_bands(
        closes: List[float], period: int = 20, std_dev: float = 2.0,
    ) -> Tuple[Optional[List[float]], Optional[List[float]], Optional[List[float]]]:
        if len(closes) < period:
            return None, None, None
        mid = TechnicalIndicators.sma(closes, period)
        upper, lower = [], []
        for i in range(period - 1, len(closes)):
            subset = closes[i - period + 1:i + 1]
            mean = sum(subset) / period
            variance = sum((x - mean) ** 2 for x in subset) / period
            std = variance ** 0.5
            upper.append(mean + std_dev * std)
            lower.append(mean - std_dev * std)
        return mid, upper, lower

    @staticmethod
    def atr(
        highs: List[float], lows: List[float], closes: List[float], period: int = 14,
    ) -> List[float]:
        if len(closes) < period + 1:
            return []
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        atr = [sum(trs[:period]) / period]
        for i in range(period, len(trs)):
            atr.append((atr[-1] * (period - 1) + trs[i]) / period)
        return atr

    @staticmethod
    def rsrs(highs: List[float], lows: List[float], period: int = 18) -> List[float]:
        if len(highs) < period or len(lows) < period:
            return []
        result = []
        for i in range(period - 1, len(highs)):
            window_highs = highs[i - period + 1:i + 1]
            window_lows = lows[i - period + 1:i + 1]
            n = period
            x_mean = (n - 1) / 2
            high_mean = sum(window_highs) / n
            low_mean = sum(window_lows) / n
            cov_h = sum((j - x_mean) * (window_highs[j] - high_mean) for j in range(n))
            var_x = sum((j - x_mean) ** 2 for j in range(n))
            slope_h = cov_h / (var_x if var_x > 0 else 1)
            result.append(slope_h)
        return result

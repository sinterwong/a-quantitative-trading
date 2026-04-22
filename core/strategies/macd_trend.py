"""
core/strategies/macd_trend.py — MACD 趋势跟踪因子（Phase 2-A）
=================================================================

MACDTrendFactor 继承自 core.factors.base.Factor，可直接接入：
  - FactorPipeline
  - WalkForwardAnalyzer（factor_class=MACDTrendFactor）
  - BacktestEngine（通过 FactorPipeline）

设计要点：
  1. MACD 金叉（直方图由负转正）→ BUY 信号
  2. MACD 死叉（直方图由正转负）→ SELL 信号
  3. ATR 过滤：当 ATR ratio > atr_threshold 时（高波动），抑制 BUY 信号
     - 趋势策略在低波动期表现更稳定
  4. DIF > 0 时信号强度加权（多头趋势确认）

与 scripts/quant/strategies/macd_strategy.py 的区别：
  - 继承 Factor 接口（evaluate + signals），而非 BaseStrategy（evaluate list）
  - 接受 pd.DataFrame，而非 List[dict]
  - 支持 WFA 网格搜索（fast/slow/signal/atr_threshold 均可参数化）

用法（WFA 验证）：
    from core.walkforward import WalkForwardAnalyzer
    from core.strategies.macd_trend import MACDTrendFactor

    wfa = WalkForwardAnalyzer(df=df, symbol='510300',
                              train_months=18, test_months=6, step_months=6)
    param_grid = {
        'fast': [8, 12],
        'slow': [21, 26],
        'signal': [7, 9],
        'atr_threshold': [0.75, 0.85],
    }
    results = wfa.run(factor_class=MACDTrendFactor, param_grid=param_grid)
    summary = wfa.summarize(results)
    print(f"OOS Sharpe > 0 占比: {summary.positive_sharpe_pct:.1%}")

WFA 合格标准（TODO P2-A）：
  - 滚动窗口 ≥ 5 个
  - OOS Sharpe 均值 > 0.3
  - 正 Sharpe 比例 > 60%
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal


class MACDTrendFactor(Factor):
    """
    MACD 趋势跟踪因子，含 ATR 高波动过滤。

    Parameters
    ----------
    fast : int
        EMA 快线周期（默认 12）
    slow : int
        EMA 慢线周期（默认 26）
    signal : int
        信号线 EMA 周期（默认 9）
    atr_period : int
        ATR 计算周期（默认 14）
    atr_lookback : int
        ATR ratio 分母窗口（默认 30，取该窗口最大 ATR）
    atr_threshold : float
        ATR ratio 过滤阈值，超过时不发出 BUY 信号（默认 0.85）
    symbol : str
        标的代码（写入 Signal.symbol）
    """

    name = "MACDTrend"
    category = FactorCategory.PRICE_MOMENTUM

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        atr_period: int = 14,
        atr_lookback: int = 30,
        atr_threshold: float = 0.85,
        symbol: str = "",
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.signal_period = signal
        self.atr_period = atr_period
        self.atr_lookback = atr_lookback
        self.atr_threshold = atr_threshold
        self.symbol = symbol

    # ------------------------------------------------------------------
    # Factor interface
    # ------------------------------------------------------------------

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        """
        返回 z-score 归一化的 MACD 直方图序列。
        > 0 → 多头动能，< 0 → 空头动能。
        """
        close = data["close"]
        histogram = self._macd_histogram(close)
        return self.normalize(histogram)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        data: Optional[pd.DataFrame] = None,
    ) -> List[Signal]:
        """
        从最新 MACD 直方图生成金叉 / 死叉信号，含 ATR 过滤。

        Parameters
        ----------
        factor_values : pd.Series
            evaluate() 返回的 z-score 序列
        price : float
            当前价格
        data : pd.DataFrame, optional
            原始 OHLCV 数据，用于计算 ATR ratio；None 时跳过 ATR 过滤
        """
        if len(factor_values) < 2:
            return []

        latest = factor_values.iloc[-1]
        prev = factor_values.iloc[-2]
        sigs: List[Signal] = []

        # ATR ratio（高波动过滤）
        atr_ratio = self._atr_ratio(data) if data is not None else 0.0
        high_volatility = atr_ratio > self.atr_threshold

        # 金叉：直方图由负转正
        if prev < 0 <= latest:
            if not high_volatility:
                # DIF > 0 时额外加权（顺势）
                dif_bonus = 0.1 if latest > 0.2 else 0.0
                strength = min(abs(latest) / 0.5 + dif_bonus, 1.0)
                sigs.append(Signal(
                    timestamp=pd.Timestamp.now(),
                    symbol=self.symbol,
                    direction="BUY",
                    strength=strength,
                    factor_name=self.name,
                    price=price,
                    metadata={
                        "macd_hist_z": round(latest, 4),
                        "cross": "golden",
                        "atr_ratio": round(atr_ratio, 3),
                        "atr_filtered": False,
                    },
                ))
            else:
                # 高波动时记录为 metadata，不发信号
                pass

        # 死叉：直方图由正转负
        elif prev > 0 >= latest:
            strength = min(abs(latest) / 0.5, 1.0)
            sigs.append(Signal(
                timestamp=pd.Timestamp.now(),
                symbol=self.symbol,
                direction="SELL",
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={
                    "macd_hist_z": round(latest, 4),
                    "cross": "death",
                    "atr_ratio": round(atr_ratio, 3),
                    "atr_filtered": False,
                },
            ))

        return sigs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _macd_histogram(self, close: pd.Series) -> pd.Series:
        """计算 MACD 直方图（DIF - DEA）。"""
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=self.signal_period, adjust=False).mean()
        return dif - dea

    def _atr_ratio(self, data: pd.DataFrame) -> float:
        """
        计算当前 ATR ratio = 当前 ATR / 最近 atr_lookback 根 ATR 的最大值。
        数据不足时返回 0.0。
        """
        if data is None or len(data) < self.atr_period + 1:
            return 0.0
        high = data["high"]
        low = data["low"]
        close = data["close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        current = atr.iloc[-1]
        if np.isnan(current):
            return 0.0
        window = atr.iloc[-self.atr_lookback:]
        max_atr = window.max()
        if max_atr <= 0 or np.isnan(max_atr):
            return 0.0
        return float(current / max_atr)


# ─── 便捷工厂函数 ──────────────────────────────────────────────────────────

def make_macd_trend_pipeline(
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    atr_threshold: float = 0.85,
    symbol: str = "",
) -> "FactorPipeline":  # type: ignore[name-defined]
    """
    快速创建只含 MACDTrendFactor 的 FactorPipeline（供独立测试 / WFA 对比用）。
    """
    from core.factor_pipeline import FactorPipeline

    pipeline = FactorPipeline()
    pipeline.add(
        MACDTrendFactor,
        weight=1.0,
        params={
            "fast": fast,
            "slow": slow,
            "signal": signal,
            "atr_threshold": atr_threshold,
            "symbol": symbol,
        },
        symbol=symbol,
    )
    return pipeline


# ─── CLI 快速验证 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO)

    # 生成随机 OHLCV 数据做冒烟测试
    rng = np.random.default_rng(42)
    n = 300
    price = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    df = pd.DataFrame({
        "open":  price * (1 + rng.normal(0, 0.002, n)),
        "high":  price * (1 + np.abs(rng.normal(0, 0.005, n))),
        "low":   price * (1 - np.abs(rng.normal(0, 0.005, n))),
        "close": price,
        "volume": rng.integers(1_000_000, 10_000_000, n),
    })

    factor = MACDTrendFactor(fast=12, slow=26, signal=9, atr_threshold=0.85, symbol="TEST")
    fv = factor.evaluate(df)
    sigs = factor.signals(fv, price=float(df["close"].iloc[-1]), data=df)

    print(f"Factor values (last 5): {fv.iloc[-5:].values.round(4)}")
    print(f"Signals: {sigs}")
    print("MACDTrendFactor 冒烟测试通过")

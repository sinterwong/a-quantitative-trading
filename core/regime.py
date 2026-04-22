"""
core/regime.py — 市场环境（Regime）检测模块
============================================

独立实现，无副作用（不修改 os.environ，不写本地文件），
供 StrategyRunner / BacktestEngine 等 core 层组件调用。

四种市场状态：
  BULL     — 上证站上 MA20，且 MA20 > MA60（多头排列）
  BEAR     — 上证跌破 MA20，且 MA20 < MA60（空头排列）
  VOLATILE — ATR ratio > 阈值（高波动，均值回归失效）
  CALM     — 其余情况（趋势不明朗）

StrategyRunner 接入方式：
  from core.regime import get_regime, RegimeConfig

  regime_info = get_regime()   # 返回 RegimeInfo
  if regime_info.is_bear:
      max_pos_pct *= 0.5       # BEAR 时仓位上限降至 50%
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("core.regime")

# ─── 默认参数 ──────────────────────────────────────────────────────────────

INDEX_SYMBOL = "sh000001"   # 上证综指（AkShare 格式）
MA_SHORT = 20
MA_LONG = 60
ATR_PERIOD = 14
ATR_LOOKBACK = 30
ATR_VOLATILE_THRESHOLD = 0.85   # > 0.85 = VOLATILE


# ─── 数据类 ────────────────────────────────────────────────────────────────

@dataclass
class RegimeInfo:
    """市场环境检测结果。"""
    regime: str         # 'BULL' | 'BEAR' | 'VOLATILE' | 'CALM'
    close: float
    ma20: float
    ma60: float
    atr_ratio: float
    atr: float
    reason: str
    date_str: str
    source: str = "akshare"   # 'akshare' | 'fallback'

    @property
    def is_bull(self) -> bool:
        return self.regime == "BULL"

    @property
    def is_bear(self) -> bool:
        return self.regime == "BEAR"

    @property
    def is_volatile(self) -> bool:
        return self.regime == "VOLATILE"

    @property
    def is_calm(self) -> bool:
        return self.regime == "CALM"

    # StrategyRunner 风控参数 ──────────────────────────────────────────────

    @property
    def position_cap(self) -> float:
        """
        持仓上限比例（相对于 config.max_position_pct 的乘数）。
        BEAR 时降至 0.5（即原上限的 50%），其余为 1.0。
        """
        return 0.5 if self.is_bear else 1.0

    @property
    def signal_threshold_multiplier(self) -> float:
        """
        信号阈值乘数。BEAR / VOLATILE 时提高阈值，减少误判。
          BEAR     → ×1.4（更难触发买入）
          VOLATILE → ×1.2（稍微提高门槛）
          其余     → ×1.0
        """
        if self.is_bear:
            return 1.4
        if self.is_volatile:
            return 1.2
        return 1.0

    @property
    def allow_new_buys(self) -> bool:
        """BEAR 状态下禁止新开多仓。"""
        return not self.is_bear

    def __str__(self) -> str:
        return (
            f"Regime[{self.regime}] {self.date_str} | "
            f"close={self.close:.0f} MA20={self.ma20:.0f} MA60={self.ma60:.0f} "
            f"ATR_ratio={self.atr_ratio:.3f}"
        )


# ─── 内部：数据获取 ────────────────────────────────────────────────────────

def _fetch_index_data(lookback: int = 80) -> Optional[dict]:
    """
    通过 AkShare 获取上证综指历史数据。
    返回 dict 含 closes / ma20 / ma60 / atr_ratio / atr，或 None（失败时）。
    """
    try:
        import akshare as ak

        end = date.today().isoformat()
        start = (date.today() - timedelta(days=lookback + 90)).isoformat()

        df = ak.stock_zh_index_daily(symbol=INDEX_SYMBOL)
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = (
            df[(df["date"] >= start) & (df["date"] <= end)]
            .tail(lookback)
            .reset_index(drop=True)
        )

        if len(df) < MA_LONG + 5:
            logger.warning("Insufficient index data: %d bars", len(df))
            return None

        closes = df["close"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)

        ma20 = pd.Series(closes).rolling(MA_SHORT).mean().values
        ma60 = pd.Series(closes).rolling(MA_LONG).mean().values

        # ATR ratio
        trs = np.maximum(
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        )
        atr_arr = pd.Series(trs).rolling(ATR_PERIOD).mean().values
        current_atr = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
        max_atr = float(np.nanmax(atr_arr[-ATR_LOOKBACK:])) if len(atr_arr) >= ATR_LOOKBACK else 1.0
        atr_ratio = current_atr / max_atr if max_atr > 0 else 0.0

        return {
            "closes": closes,
            "ma20": ma20,
            "ma60": ma60,
            "atr_ratio": atr_ratio,
            "atr": current_atr,
        }
    except Exception as exc:
        logger.warning("_fetch_index_data failed: %s", exc)
        return None


# ─── 核心检测逻辑 ──────────────────────────────────────────────────────────

def detect_regime() -> RegimeInfo:
    """
    实时检测市场环境（调用 AkShare 获取上证数据）。
    网络失败时返回 CALM（保守默认值）。
    """
    today_str = date.today().isoformat()
    data = _fetch_index_data(lookback=80)

    if data is None:
        return RegimeInfo(
            regime="CALM",
            close=0.0, ma20=0.0, ma60=0.0,
            atr_ratio=0.0, atr=0.0,
            reason="数据获取失败，默认 CALM",
            date_str=today_str,
            source="fallback",
        )

    closes = data["closes"]
    ma20_arr = data["ma20"]
    ma60_arr = data["ma60"]
    atr_ratio = data["atr_ratio"]
    atr = data["atr"]

    close = float(closes[-1])
    ma20 = float(ma20_arr[-1])
    ma60 = float(ma60_arr[-1])

    above_ma20 = close > ma20
    ma20_above_ma60 = ma20 > ma60
    below_ma20 = close < ma20
    ma20_below_ma60 = ma20 < ma60

    if above_ma20 and ma20_above_ma60:
        regime = "BULL"
        reason = f"上证 {close:.0f} > MA20({ma20:.0f})，均线多头排列"
    elif below_ma20 and ma20_below_ma60:
        regime = "BEAR"
        reason = f"上证 {close:.0f} < MA20({ma20:.0f})，均线空头排列"
    elif atr_ratio > ATR_VOLATILE_THRESHOLD:
        regime = "VOLATILE"
        reason = f"ATR ratio={atr_ratio:.3f} > {ATR_VOLATILE_THRESHOLD}，高波动环境"
    else:
        regime = "CALM"
        reason = f"ATR ratio={atr_ratio:.3f} <= {ATR_VOLATILE_THRESHOLD}，趋势不明朗"

    info = RegimeInfo(
        regime=regime,
        close=round(close, 2),
        ma20=round(ma20, 2),
        ma60=round(ma60, 2),
        atr_ratio=round(atr_ratio, 4),
        atr=round(atr, 4),
        reason=reason,
        date_str=today_str,
        source="akshare",
    )
    logger.info("[Regime] %s", info)
    return info


# ─── 简单日内缓存（进程级，避免重复请求）──────────────────────────────────

_cache: Optional[RegimeInfo] = None
_cache_date: Optional[str] = None


def get_regime(force_refresh: bool = False) -> RegimeInfo:
    """
    获取当日市场环境（进程内缓存，同一天只调用 AkShare 一次）。

    Parameters
    ----------
    force_refresh:
        True → 忽略缓存，立即重新检测
    """
    global _cache, _cache_date
    today_str = date.today().isoformat()

    if not force_refresh and _cache is not None and _cache_date == today_str:
        logger.debug("[Regime] 返回缓存: %s", _cache.regime)
        return _cache

    info = detect_regime()
    _cache = info
    _cache_date = today_str
    return info


def invalidate_cache() -> None:
    """清除进程内缓存（盘中复查时调用）。"""
    global _cache, _cache_date
    _cache = None
    _cache_date = None


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")

    info = get_regime(force_refresh=True)
    print()
    print("=" * 60)
    print(f"  市场环境检测 — {info.date_str}")
    print("=" * 60)
    print(f"  上证收盘:      {info.close}")
    print(f"  MA(20):       {info.ma20}")
    print(f"  MA(60):       {info.ma60}")
    print(f"  ATR ratio:    {info.atr_ratio}  (阈值 {ATR_VOLATILE_THRESHOLD})")
    print(f"  当前 ATR:     {info.atr:.4f}")
    print(f"  环境:         [{info.regime}]")
    print(f"  原因:         {info.reason}")
    print()
    print(f"  持仓上限乘数:  {info.position_cap}")
    print(f"  阈值乘数:      {info.signal_threshold_multiplier}")
    print(f"  允许新多仓:    {info.allow_new_buys}")

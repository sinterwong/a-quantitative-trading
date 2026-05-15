"""
core/regime.py — 市场环境（Regime）检测模块
============================================

独立实现，无副作用（不修改 os.environ，不写本地文件），
供 StrategyRunner / BacktestEngine 等 core 层组件调用。

四种市场状态：
  BULL     — 上证站上 MA20，且 MA20 > MA60，MA60 斜率 ≥ 0（多头排列）
  BEAR     — 上证跌破 MA20，且 MA20 < MA60，MA60 斜率 < 0（空头排列）
                  ↑ P1-13: MA60 斜率维度，避免横盘震荡误判 BEAR
  VOLATILE — ATR 当前值 > 过去 252 日 90 分位数（自适应阈值，P1-13）
  CALM     — 其余情况（趋势不明朗）

P1-13 升级：
  1. ATR 阈值从固定 0.85 改为滚动 252 日的 90 分位数（自适应）
  2. MA60 斜率（30 日变化率）参与 BULL/BEAR 判定
  3. 切换冷却期：5 个交易日内不重复切换，减小抖动
  4. RegimeInfo.position_reduce_target_pct：BEAR 时主动减到原仓位的 75%

StrategyRunner / IntradayMonitor 接入方式：
  from core.regime import get_regime

  regime_info = get_regime()
  if regime_info.should_reduce_positions:
      # 主动减仓到 regime_info.position_reduce_target_pct
      ...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("core.regime")

# ─── 默认参数 ──────────────────────────────────────────────────────────────

INDEX_SYMBOL = "sh000001"   # 上证综指（Gateway 统一格式 sh000001）
MA_SHORT = 20
MA_LONG = 60
ATR_PERIOD = 14
ATR_LOOKBACK = 30
ATR_PERCENTILE_WINDOW = 252   # P1-13: 自适应阈值的窗口（约 1 年交易日）
ATR_PERCENTILE = 90           # P1-13: 90 分位数作为 VOLATILE 触发线
ATR_VOLATILE_THRESHOLD = 0.85   # 后向兼容：固定阈值 fallback

# P1-13: 切换冷却期 + 主动减仓
SWITCH_COOLDOWN_DAYS = 5
BEAR_POSITION_REDUCE_PCT = 0.75   # BEAR 时主动减到原仓位的 75%（即减仓 25%）

# W4-1: VIX 分位强制 VOLATILE 触发线
VIX_SYMBOL = "^VIX"                # yfinance ticker
VIX_LOOKBACK_DAYS = 252            # 1 年历史
VIX_PERCENTILE_THRESHOLD = 80.0    # VIX 处于 1 年 80 分位以上 → 强制 VOLATILE


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
    source: str = "gateway"   # 'gateway' | 'fallback'

    # P1-13: 自适应阈值与斜率
    atr_threshold_dynamic: float = 0.0   # 当前轮使用的 VOLATILE 阈值
    ma60_slope: float = 0.0              # 30 日 MA60 变化率（正=向上）

    # W4-1: VIX 分位(海外恐慌指数,辅助 VOLATILE 判定)
    vix_percentile: float = 0.0          # 当前 VIX 在过去 1 年中的分位(0-100)

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

    @property
    def position_reduce_target_pct(self) -> float:
        """
        P1-13: 应主动减到原仓位的百分比。
          BEAR  → BEAR_POSITION_REDUCE_PCT（默认 0.75，即减仓 25%）
          其余  → 1.0（不减仓）
        """
        return BEAR_POSITION_REDUCE_PCT if self.is_bear else 1.0

    @property
    def should_reduce_positions(self) -> bool:
        """P1-13: BEAR 时是否需要主动减仓（与 position_reduce_target_pct 配套）。"""
        return self.is_bear

    def __str__(self) -> str:
        return (
            f"Regime[{self.regime}] {self.date_str} | "
            f"close={self.close:.0f} MA20={self.ma20:.0f} MA60={self.ma60:.0f} "
            f"slope60={self.ma60_slope:+.4f} "
            f"ATR_ratio={self.atr_ratio:.3f}/dyn_thr={self.atr_threshold_dynamic:.3f}"
        )


# ─── 内部：数据获取 ────────────────────────────────────────────────────────

def _fetch_vix_percentile(lookback_days: int = VIX_LOOKBACK_DAYS) -> float:
    """W4-1: 通过 Gateway 取 VIX 历史,计算当前值的百分位(0-100)。

    返回 0 表示数据获取失败或无意义(不影响 regime 判定)。
    """
    try:
        from core.data_gateway import get_gateway
        df = get_gateway().kline(VIX_SYMBOL, interval="daily", days=lookback_days + 30)
    except Exception as exc:
        logger.debug("_fetch_vix_percentile failed: %s", exc)
        return 0.0

    if df is None or df.empty:
        return 0.0

    # 列名可能为 date 或 timestamp(provider 差异)
    time_col = "date" if "date" in df.columns else (
        "timestamp" if "timestamp" in df.columns else None
    )
    if time_col is not None:
        df = df.sort_values(time_col)

    closes = pd.to_numeric(df.get("close"), errors="coerce").dropna()
    if len(closes) < 30:
        return 0.0

    closes = closes.tail(lookback_days)
    current = float(closes.iloc[-1])
    # 百分位:有多少历史值 <= 当前值
    pct = float((closes <= current).sum()) / len(closes) * 100.0
    return round(pct, 2)


def _fetch_index_data(lookback: int = 320) -> Optional[dict]:
    """
    通过 DataGateway 获取上证综指历史数据。

    P1-13: lookback 从 80 → 320 天，以满足：
      - MA60 + MA60 30 日斜率 → 至少 90 根
      - ATR_PERCENTILE_WINDOW=252 + ATR_PERIOD=14 → 至少 270 根

    数据源走 Gateway 统一出口（享受熔断/健康度路由/字段级合并），
    底层 provider 由 gateway 自动选择（默认腾讯 INDEX KLINE）。

    返回 dict 含 closes / ma20 / ma60 / atr_ratio / atr / atr_arr / ma60_slope，
    或 None（失败时）。
    """
    try:
        from core.data_gateway import get_gateway

        df = get_gateway().kline(
            INDEX_SYMBOL, interval="daily", days=lookback + 90, limit=lookback,
        )
        if df is None or df.empty:
            logger.warning("_fetch_index_data: gateway returned empty kline for %s", INDEX_SYMBOL)
            return None

        # Provider 间列名差异:多数用 'date',Baostock 用 'timestamp'。
        # 统一规整为按时间升序排列的 OHLC 数组。
        time_col = "date" if "date" in df.columns else (
            "timestamp" if "timestamp" in df.columns else None
        )
        if time_col is not None:
            df = df.sort_values(time_col)
        df = df.tail(lookback).reset_index(drop=True)

        if len(df) < MA_LONG + 5:
            logger.warning("Insufficient index data: %d bars", len(df))
            return None

        return _compute_indicators(
            closes=df["close"].values.astype(float),
            highs=df["high"].values.astype(float),
            lows=df["low"].values.astype(float),
        )
    except Exception as exc:
        logger.warning("_fetch_index_data failed: %s", exc)
        return None


def _compute_indicators(closes, highs, lows) -> Optional[dict]:
    """从 OHLC 计算所有 regime 用指标。可单测。"""
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    if len(closes) < MA_LONG + 5:
        return None

    ma20 = pd.Series(closes).rolling(MA_SHORT).mean().values
    ma60 = pd.Series(closes).rolling(MA_LONG).mean().values

    # ATR — True Range = max(H-L, |H-C_prev|, |L-C_prev|)
    tr_a = highs[1:] - lows[1:]
    tr_b = np.abs(highs[1:] - closes[:-1])
    tr_c = np.abs(lows[1:] - closes[:-1])
    trs = np.maximum(np.maximum(tr_a, tr_b), tr_c)
    atr_arr = pd.Series(trs).rolling(ATR_PERIOD).mean().values
    current_atr = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    max_atr = float(np.nanmax(atr_arr[-ATR_LOOKBACK:])) if len(atr_arr) >= ATR_LOOKBACK else 1.0
    atr_ratio = current_atr / max_atr if max_atr > 0 else 0.0

    # P1-13: 自适应 ATR 阈值（过去 252 日的 90 分位数 / max_atr）
    window_size = min(ATR_PERCENTILE_WINDOW, len(atr_arr))
    atr_window = atr_arr[-window_size:]
    atr_window = atr_window[~np.isnan(atr_window)]
    if len(atr_window) >= 30 and max_atr > 0:
        threshold_atr = float(np.percentile(atr_window, ATR_PERCENTILE))
        atr_threshold_dynamic = threshold_atr / max_atr
    else:
        atr_threshold_dynamic = ATR_VOLATILE_THRESHOLD  # fallback

    # P1-13: MA60 30 日斜率 = (MA60_today - MA60_30d_ago) / MA60_30d_ago
    if len(ma60) >= 31 and not np.isnan(ma60[-31]) and ma60[-31] > 0:
        ma60_slope = float((ma60[-1] - ma60[-31]) / ma60[-31])
    else:
        ma60_slope = 0.0

    return {
        "closes": closes,
        "ma20": ma20,
        "ma60": ma60,
        "atr_ratio": atr_ratio,
        "atr": current_atr,
        "atr_threshold_dynamic": atr_threshold_dynamic,
        "ma60_slope": ma60_slope,
    }


# ─── 核心检测逻辑 ──────────────────────────────────────────────────────────

def detect_regime() -> RegimeInfo:
    """
    实时检测市场环境（通过 DataGateway 获取上证数据）。
    网络失败时返回 CALM（保守默认值）。

    P1-13: 三处升级
      1. ATR 阈值用过去 252 日的 90 分位数（_fetch_index_data 内计算）
      2. MA60 30 日斜率参与 BULL/BEAR 判定
      3. 阈值与斜率同时写入 RegimeInfo 用于诊断
    """
    today_str = date.today().isoformat()
    data = _fetch_index_data(lookback=320)

    # W4-1: VIX 分位辅助判定(best-effort,失败不阻塞主链路)
    vix_pct = _fetch_vix_percentile()

    if data is None:
        return RegimeInfo(
            regime="CALM",
            close=0.0, ma20=0.0, ma60=0.0,
            atr_ratio=0.0, atr=0.0,
            reason="数据获取失败，默认 CALM",
            date_str=today_str,
            source="fallback",
            atr_threshold_dynamic=ATR_VOLATILE_THRESHOLD,
            ma60_slope=0.0,
            vix_percentile=vix_pct,
        )

    return _classify_regime(data, today_str, source="gateway", vix_percentile=vix_pct)


def _classify_regime(
    data: dict, date_str: str, source: str = "gateway",
    vix_percentile: float = 0.0,
) -> RegimeInfo:
    """从计算好的 indicators dict 分类 regime。可单测。

    W4-1: VIX 分位 > 80 → 即使 A股 ATR 未到动态阈值,也强制 VOLATILE
    (海外恐慌优先级高于本土平静)。BULL/BEAR 信号仍可压过 VIX 触发。
    """
    closes = data["closes"]
    ma20_arr = data["ma20"]
    ma60_arr = data["ma60"]
    atr_ratio = data["atr_ratio"]
    atr = data["atr"]
    atr_threshold_dynamic = data.get("atr_threshold_dynamic", ATR_VOLATILE_THRESHOLD)
    ma60_slope = data.get("ma60_slope", 0.0)

    close = float(closes[-1])
    ma20 = float(ma20_arr[-1])
    ma60 = float(ma60_arr[-1])

    above_ma20 = close > ma20
    ma20_above_ma60 = ma20 > ma60
    below_ma20 = close < ma20
    ma20_below_ma60 = ma20 < ma60

    # P1-13: BULL/BEAR 加 MA60 斜率确认（避免横盘震荡误判）
    bull_confirmed = above_ma20 and ma20_above_ma60 and ma60_slope >= 0
    bear_confirmed = below_ma20 and ma20_below_ma60 and ma60_slope < 0

    # W4-1: VIX 高分位辅助触发(优先级低于明确的 BULL/BEAR)
    vix_volatile_trigger = vix_percentile >= VIX_PERCENTILE_THRESHOLD

    if bull_confirmed:
        regime = "BULL"
        reason = (
            f"上证 {close:.0f} > MA20({ma20:.0f})，均线多头排列，"
            f"MA60 斜率 {ma60_slope*100:+.2f}%"
        )
    elif bear_confirmed:
        regime = "BEAR"
        reason = (
            f"上证 {close:.0f} < MA20({ma20:.0f})，均线空头排列，"
            f"MA60 斜率 {ma60_slope*100:+.2f}%"
        )
    elif atr_ratio > atr_threshold_dynamic:
        regime = "VOLATILE"
        reason = (
            f"ATR ratio={atr_ratio:.3f} > 动态阈值 {atr_threshold_dynamic:.3f}"
            f"（过去 {ATR_PERCENTILE_WINDOW} 日 P{ATR_PERCENTILE}），高波动环境"
        )
    elif vix_volatile_trigger:
        regime = "VOLATILE"
        reason = (
            f"VIX 分位 {vix_percentile:.1f} ≥ {VIX_PERCENTILE_THRESHOLD:.0f}"
            f"，海外恐慌升温,强制 VOLATILE"
        )
    else:
        regime = "CALM"
        reason = (
            f"ATR ratio={atr_ratio:.3f} ≤ 动态阈值 {atr_threshold_dynamic:.3f}，"
            f"MA60 斜率 {ma60_slope*100:+.2f}%，VIX 分位 {vix_percentile:.1f}，趋势不明朗"
        )

    info = RegimeInfo(
        regime=regime,
        close=round(close, 2),
        ma20=round(ma20, 2),
        ma60=round(ma60, 2),
        atr_ratio=round(atr_ratio, 4),
        atr=round(atr, 4),
        reason=reason,
        date_str=date_str,
        source=source,
        atr_threshold_dynamic=round(atr_threshold_dynamic, 4),
        ma60_slope=round(ma60_slope, 6),
        vix_percentile=round(vix_percentile, 2),
    )
    logger.info("[Regime] %s", info)
    return info


# ─── 简单日内缓存 + 切换冷却期（P1-13）─────────────────────────────────────

_cache: Optional[RegimeInfo] = None
_cache_date: Optional[str] = None
_last_change_date: Optional[str] = None    # 上次状态切换日期
_persistent_regime: Optional[str] = None   # 冷却期内"锁定"的状态


def _trading_days_between(d1: str, d2: str) -> int:
    """两个 ISO 日期间相差的交易日近似数（按周一-周五计）。"""
    try:
        a = date.fromisoformat(d1)
        b = date.fromisoformat(d2)
        if b < a:
            return 0
        # 简单计数：日历天数 × (5/7) 近似
        delta = (b - a).days
        return max(int(delta * 5 // 7), 0)
    except Exception:
        return 0


def get_regime(force_refresh: bool = False) -> RegimeInfo:
    """
    获取当日市场环境（进程内缓存，同一天只通过 Gateway 拉取一次）。

    P1-13: 增加切换冷却期 — 距上次状态切换 < SWITCH_COOLDOWN_DAYS 时
    保持原状态以减少抖动。原始检测结果仍写入 reason 供诊断。

    Parameters
    ----------
    force_refresh:
        True → 忽略缓存，立即重新检测（仍受冷却期约束，除非冷却期已结束）
    """
    global _cache, _cache_date, _last_change_date, _persistent_regime
    today_str = date.today().isoformat()

    if not force_refresh and _cache is not None and _cache_date == today_str:
        logger.debug("[Regime] 返回缓存: %s", _cache.regime)
        return _cache

    info = detect_regime()

    # 切换冷却期：若新检测与上次锁定状态不同，且距上次切换 < SWITCH_COOLDOWN_DAYS，
    # 则保留旧状态（仅在 reason 里追加诊断信息）
    if _persistent_regime is not None and _last_change_date is not None:
        elapsed = _trading_days_between(_last_change_date, today_str)
        if info.regime != _persistent_regime and elapsed < SWITCH_COOLDOWN_DAYS:
            logger.info(
                "[Regime] 冷却期内（已 %d 交易日 < %d），保持 %s（检测为 %s）",
                elapsed, SWITCH_COOLDOWN_DAYS, _persistent_regime, info.regime,
            )
            info = RegimeInfo(
                regime=_persistent_regime,
                close=info.close, ma20=info.ma20, ma60=info.ma60,
                atr_ratio=info.atr_ratio, atr=info.atr,
                reason=(
                    f"冷却期内保持 {_persistent_regime}（{elapsed}/{SWITCH_COOLDOWN_DAYS}天）"
                    f"，原始检测：{info.regime} — {info.reason}"
                ),
                date_str=info.date_str,
                source=info.source,
                atr_threshold_dynamic=info.atr_threshold_dynamic,
                ma60_slope=info.ma60_slope,
            )
        elif info.regime != _persistent_regime:
            # 冷却期已过，正式切换
            _persistent_regime = info.regime
            _last_change_date = today_str
    else:
        # 首次检测
        _persistent_regime = info.regime
        _last_change_date = today_str

    _cache = info
    _cache_date = today_str
    return info


def invalidate_cache() -> None:
    """清除进程内缓存（盘中复查时调用）。冷却期状态保留。"""
    global _cache, _cache_date
    _cache = None
    _cache_date = None


def reset_state() -> None:
    """完全重置状态（包括冷却期）— 仅供测试使用。"""
    global _cache, _cache_date, _last_change_date, _persistent_regime
    _cache = None
    _cache_date = None
    _last_change_date = None
    _persistent_regime = None


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

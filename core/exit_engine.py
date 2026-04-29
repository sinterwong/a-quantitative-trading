"""
core/exit_engine.py — 统一卖出信号引擎
==========================================

设计原则：
  - 单一职责：所有退出信号在此生成，intraday_monitor 仅负责执行
  - 优先级分层：组合级 > 仓位级 > 因子级 > 时间级
  - 分批退出：支持全仓/半仓/自定义比例，避免一刀切式清仓
  - 因子驱动：买入/卖出信号源一致（DynamicWeightPipeline）
  - 无状态：ExitEngine 本身不保存持仓状态，每次接受外部快照

退出优先级（高优先级覆盖低优先级）：
  P0  EMERGENCY_LIQUIDATE   组合回撤 ≥ dd_stop（默认12%），全部清仓
  P1  PORTFOLIO_REDUCE      组合回撤 ≥ dd_warn（默认8%），各仓减半
  P2  HARD_STOP_LOSS        个股亏损 ≥ hard_sl（默认-15%），全仓止损（无条件）
  P3  ATR_TRAILING_STOP     ATR 追踪止损触发，全仓
  P4  FACTOR_REVERSAL       因子流水线综合分 < -factor_exit_z 且持续 N 根 bar，全仓
  P5  SOFT_STOP_LOSS        个股亏损 ≥ soft_sl（默认-8%），半仓减仓
  P6  TAKE_PROFIT_SCALE     个股盈利 ≥ tp_first（默认+15%），半仓止盈
  P7  TAKE_PROFIT_FULL      个股盈利 ≥ tp_full（默认+25%），全仓止盈
  P8  RSI_OVERBOUGHT        日线 RSI ≥ rsi_sell + 分钟确认，半仓
  P9  TIME_STOP             持仓超 max_hold_days 且浮盈 <5%，半仓减仓

用法：
    from core.exit_engine import ExitEngine, ExitSignal

    engine = ExitEngine()
    signals = engine.generate(
        positions=svc.get_positions(),       # 持仓列表
        equity_peak=peak_equity,             # 历史权益峰值
        current_equity=current_equity,       # 当前总权益
        pipeline_scores=pipeline_scores,     # {symbol: combined_score}（可选）
    )
    for sig in signals:
        print(sig.symbol, sig.priority.name, sig.exit_pct, sig.reason)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger('core.exit_engine')


# ── 退出优先级（数字越小优先级越高）────────────────────────────────────────

class ExitPriority(IntEnum):
    EMERGENCY_LIQUIDATE  = 0
    PORTFOLIO_REDUCE     = 1
    HARD_STOP_LOSS       = 2
    ATR_TRAILING_STOP    = 3
    FACTOR_REVERSAL      = 4
    SOFT_STOP_LOSS       = 5
    TAKE_PROFIT_SCALE    = 6
    TAKE_PROFIT_FULL     = 7
    RSI_OVERBOUGHT       = 8
    TIME_STOP            = 9


# ── 退出信号数据类 ────────────────────────────────────────────────────────

@dataclass
class ExitSignal:
    """
    单个标的的退出信号。

    exit_pct: 0.0–1.0，卖出比例
      1.0 = 全仓清仓
      0.5 = 半仓卖出
    """
    symbol: str
    priority: ExitPriority
    exit_pct: float          # 0.0–1.0
    reason: str
    current_price: float
    entry_price: float
    unrealized_pct: float    # 浮盈亏（正=盈利，负=亏损）
    holding_days: int = 0
    metadata: Dict = field(default_factory=dict)

    @property
    def is_emergency(self) -> bool:
        return self.priority.value <= ExitPriority.HARD_STOP_LOSS

    def shares_to_sell(self, total_shares: int) -> int:
        """计算实际卖出股数（向下取整到100股的倍数，保证不为0）"""
        raw = total_shares * self.exit_pct
        rounded = max(100, int(raw // 100) * 100)
        return min(rounded, total_shares)


# ── ATR 计算工具 ─────────────────────────────────────────────────────────

def _compute_atr(prices_df: pd.DataFrame, period: int = 14) -> float:
    """从 OHLCV DataFrame 计算最新 ATR，数据不足时返回 0.0"""
    if prices_df is None or len(prices_df) < period + 1:
        return 0.0
    try:
        h = prices_df['high'].values
        l = prices_df['low'].values
        c = prices_df['close'].values
        tr = np.maximum(
            h[1:] - l[1:],
            np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
        )
        atr = float(tr[-period:].mean())
        return atr if not math.isnan(atr) else 0.0
    except Exception:
        return 0.0


def _compute_rsi(close_arr: np.ndarray, period: int = 14) -> float:
    """Wilder 平滑 RSI，返回最新值（数据不足返回 50.0）"""
    n = len(close_arr)
    if n < period + 2:
        return 50.0
    delta = np.diff(close_arr[-period * 3:])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = gain[:period].mean()
    al = loss[:period].mean()
    for i in range(period, len(delta)):
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
    if al < 1e-10:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


# ── ExitEngine 主类 ──────────────────────────────────────────────────────

class ExitEngine:
    """
    统一卖出信号引擎。

    Parameters
    ----------
    dd_warn         : 组合回撤警告线（默认 0.08 = 8%），触发各仓减半
    dd_stop         : 组合熔断线（默认 0.12 = 12%），全仓清仓
    hard_sl         : 个股硬止损（默认 0.15 = -15%），无条件全仓
    soft_sl         : 个股软止损（默认 0.08 = -8%），半仓减仓
    tp_scale        : 第一止盈线（默认 0.15 = +15%），半仓止盈
    tp_full         : 第二止盈线（默认 0.25 = +25%），全仓止盈
    atr_multiplier  : ATR 追踪止损倍数（默认 2.5）
    rsi_sell        : RSI 卖出阈值（默认 65）
    max_hold_days   : 最大持仓天数（默认 20 个交易日，超时+浮盈不足则减仓）
    factor_exit_z   : 因子综合分绝对值阈值（默认 0.6，score<-0.6 触发因子反转）
    """

    def __init__(
        self,
        dd_warn:        float = 0.08,
        dd_stop:        float = 0.12,
        hard_sl:        float = 0.15,
        soft_sl:        float = 0.08,
        tp_scale:       float = 0.15,
        tp_full:        float = 0.25,
        atr_multiplier: float = 2.5,
        rsi_sell:       int   = 65,
        max_hold_days:  int   = 20,
        factor_exit_z:  float = 0.60,
    ):
        self.dd_warn        = dd_warn
        self.dd_stop        = dd_stop
        self.hard_sl        = hard_sl
        self.soft_sl        = soft_sl
        self.tp_scale       = tp_scale
        self.tp_full        = tp_full
        self.atr_multiplier = atr_multiplier
        self.rsi_sell       = rsi_sell
        self.max_hold_days  = max_hold_days
        self.factor_exit_z  = factor_exit_z

    # ── 主入口 ───────────────────────────────────────────────────────────

    def generate(
        self,
        positions: List[Dict],
        equity_peak: float,
        current_equity: float,
        pipeline_scores: Optional[Dict[str, float]] = None,
        price_bars: Optional[Dict[str, pd.DataFrame]] = None,
        params_map: Optional[Dict[str, Dict]] = None,
    ) -> List[ExitSignal]:
        """
        生成所有持仓的退出信号，按优先级升序排列（P0 在最前）。

        Parameters
        ----------
        positions       : 持仓列表，每项须含 symbol/shares/entry_price/current_price
        equity_peak     : 历史权益峰值（用于组合回撤计算）
        current_equity  : 当前总权益
        pipeline_scores : {symbol: combined_score}，来自 DynamicWeightPipeline
        price_bars      : {symbol: OHLCV DataFrame}，用于 ATR/RSI 计算
        params_map      : {symbol: params_dict}，per-symbol 参数覆盖

        Returns
        -------
        按优先级升序排列的 ExitSignal 列表，每个 symbol 最多出现一次
        （取最高优先级信号）
        """
        if not positions:
            return []

        pipeline_scores = pipeline_scores or {}
        price_bars      = price_bars or {}
        params_map      = params_map or {}

        # ── Step 1: 组合级别检查 ───────────────────────────────────────
        portfolio_signals = self._check_portfolio_level(
            positions, equity_peak, current_equity
        )
        # 若触发 EMERGENCY，直接返回（覆盖所有个股判断）
        if any(s.priority == ExitPriority.EMERGENCY_LIQUIDATE for s in portfolio_signals):
            return sorted(portfolio_signals, key=lambda s: s.priority.value)

        # ── Step 2: 个股级别检查 ──────────────────────────────────────
        position_signals: Dict[str, ExitSignal] = {}

        # 先放入组合级信号（PORTFOLIO_REDUCE）
        for sig in portfolio_signals:
            position_signals[sig.symbol] = sig

        for pos in positions:
            sym = pos.get('symbol', '')
            if not sym:
                continue
            shares = pos.get('shares', 0)
            if shares <= 0:
                continue

            entry_price   = float(pos.get('entry_price', 0) or pos.get('avg_price', 0) or 0)
            current_price = float(pos.get('current_price', 0))
            peak_price    = float(pos.get('peak_price', 0) or current_price)
            entry_date    = pos.get('entry_date', None)
            holding_days  = self._holding_days(entry_date)

            if entry_price <= 0 or current_price <= 0:
                continue

            unrealized_pct = (current_price - entry_price) / entry_price
            bars           = price_bars.get(sym)
            p_params       = params_map.get(sym, {})

            # 运行各规则，收集候选信号
            candidates: List[ExitSignal] = []

            # 若 PORTFOLIO_REDUCE 已存在，该 symbol 的组合信号已处理，跳过
            # 但仍要检查是否有更高优先级的个股信号
            sig = self._check_hard_stop(sym, entry_price, current_price, unrealized_pct,
                                        peak_price, holding_days, bars, p_params)
            if sig:
                candidates.append(sig)

            sig = self._check_atr_trailing_stop(sym, entry_price, current_price, unrealized_pct,
                                                peak_price, holding_days, bars, p_params)
            if sig:
                candidates.append(sig)

            sig = self._check_factor_reversal(sym, entry_price, current_price, unrealized_pct,
                                              holding_days, pipeline_scores)
            if sig:
                candidates.append(sig)

            sig = self._check_soft_stop(sym, entry_price, current_price, unrealized_pct,
                                        peak_price, holding_days, bars, p_params)
            if sig:
                candidates.append(sig)

            sig = self._check_take_profit(sym, entry_price, current_price, unrealized_pct,
                                          peak_price, holding_days, bars, p_params)
            if sig:
                candidates.append(sig)

            sig = self._check_rsi_overbought(sym, entry_price, current_price, unrealized_pct,
                                             holding_days, bars, p_params)
            if sig:
                candidates.append(sig)

            sig = self._check_time_stop(sym, entry_price, current_price, unrealized_pct,
                                        holding_days, p_params)
            if sig:
                candidates.append(sig)

            if candidates:
                # 取最高优先级（数字最小）
                best = min(candidates, key=lambda s: s.priority.value)
                existing = position_signals.get(sym)
                if existing is None or best.priority.value < existing.priority.value:
                    position_signals[sym] = best

        return sorted(position_signals.values(), key=lambda s: s.priority.value)

    # ── 组合级检查 ────────────────────────────────────────────────────────

    def _check_portfolio_level(
        self,
        positions: List[Dict],
        equity_peak: float,
        current_equity: float,
    ) -> List[ExitSignal]:
        """检查组合整体回撤，返回对所有持仓的信号列表。"""
        signals = []
        if equity_peak <= 0 or current_equity <= 0:
            return signals

        drawdown = (equity_peak - current_equity) / equity_peak

        if drawdown >= self.dd_stop:
            for pos in positions:
                sym = pos.get('symbol', '')
                if not sym or pos.get('shares', 0) <= 0:
                    continue
                ep = float(pos.get('entry_price', 0) or pos.get('avg_price', 0) or 0)
                cp = float(pos.get('current_price', 0))
                up = (cp - ep) / ep if ep > 0 else 0.0
                signals.append(ExitSignal(
                    symbol=sym,
                    priority=ExitPriority.EMERGENCY_LIQUIDATE,
                    exit_pct=1.0,
                    reason=f'组合回撤 {drawdown*100:.1f}% ≥ 熔断线 {self.dd_stop*100:.0f}%，全仓清仓',
                    current_price=cp,
                    entry_price=ep,
                    unrealized_pct=up,
                    metadata={'drawdown': round(drawdown, 4)},
                ))
            return signals

        if drawdown >= self.dd_warn:
            for pos in positions:
                sym = pos.get('symbol', '')
                if not sym or pos.get('shares', 0) <= 0:
                    continue
                ep = float(pos.get('entry_price', 0) or pos.get('avg_price', 0) or 0)
                cp = float(pos.get('current_price', 0))
                up = (cp - ep) / ep if ep > 0 else 0.0
                signals.append(ExitSignal(
                    symbol=sym,
                    priority=ExitPriority.PORTFOLIO_REDUCE,
                    exit_pct=0.5,
                    reason=f'组合回撤 {drawdown*100:.1f}% ≥ 警告线 {self.dd_warn*100:.0f}%，各仓减半',
                    current_price=cp,
                    entry_price=ep,
                    unrealized_pct=up,
                    metadata={'drawdown': round(drawdown, 4)},
                ))
            return signals

        return signals

    # ── 个股级检查 ────────────────────────────────────────────────────────

    def _check_hard_stop(self, sym, entry, current, up, peak, days, bars, p_params
                         ) -> Optional[ExitSignal]:
        """硬止损：亏损超 hard_sl（无条件，ATR/冷却无效）。"""
        threshold = p_params.get('hard_sl', self.hard_sl)
        if up <= -threshold:
            return ExitSignal(
                symbol=sym, priority=ExitPriority.HARD_STOP_LOSS,
                exit_pct=1.0,
                reason=f'硬止损：浮亏 {up*100:.1f}% ≤ -{threshold*100:.0f}%，全仓清仓',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
            )
        return None

    def _check_atr_trailing_stop(self, sym, entry, current, up, peak, days, bars, p_params
                                  ) -> Optional[ExitSignal]:
        """ATR 追踪止损：从持仓最高价回撤超 N×ATR。"""
        if bars is None or len(bars) < 20:
            return None
        atr = _compute_atr(bars, period=14)
        if atr <= 0:
            return None
        mult = p_params.get('atr_multiplier', self.atr_multiplier)
        trail_stop = peak - atr * mult
        if current <= trail_stop and up > -self.soft_sl:
            # 只在非亏损/轻微亏损状态下用 ATR 止损（避免和 hard_stop 重叠）
            return ExitSignal(
                symbol=sym, priority=ExitPriority.ATR_TRAILING_STOP,
                exit_pct=1.0,
                reason=(f'ATR追踪止损：现价 {current:.2f} ≤ 追踪止损位 {trail_stop:.2f}'
                        f'（峰值 {peak:.2f} - {mult}×ATR {atr:.2f}）'),
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
                metadata={'atr': round(atr, 4), 'trail_stop': round(trail_stop, 4)},
            )
        return None

    def _check_factor_reversal(self, sym, entry, current, up, days,
                                pipeline_scores) -> Optional[ExitSignal]:
        """
        因子反转：DynamicWeightPipeline 综合分强烈看空（< -factor_exit_z）。
        仅在持仓已有盈利或最低持仓期满足时触发（避免刚建仓即被因子信号冲出）。
        """
        if not pipeline_scores or sym not in pipeline_scores:
            return None
        score = pipeline_scores[sym]
        threshold = -self.factor_exit_z
        # 条件：综合分强烈看空 + （已盈利 OR 持仓超5天）
        if score < threshold and (up > 0.02 or days >= 5):
            return ExitSignal(
                symbol=sym, priority=ExitPriority.FACTOR_REVERSAL,
                exit_pct=1.0,
                reason=f'因子反转：pipeline 综合分 {score:.3f} < {threshold:.2f}，多头信号消失',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
                metadata={'pipeline_score': round(score, 4)},
            )
        return None

    def _check_soft_stop(self, sym, entry, current, up, peak, days, bars, p_params
                          ) -> Optional[ExitSignal]:
        """软止损：亏损超 soft_sl，半仓减仓（给剩余仓位更多时间恢复）。"""
        threshold = p_params.get('soft_sl', self.soft_sl)
        hard_threshold = p_params.get('hard_sl', self.hard_sl)
        # 仅在 soft_sl 到 hard_sl 之间触发（hard_stop 已覆盖更大亏损）
        if -hard_threshold < up <= -threshold:
            return ExitSignal(
                symbol=sym, priority=ExitPriority.SOFT_STOP_LOSS,
                exit_pct=0.5,
                reason=f'软止损：浮亏 {up*100:.1f}% ≤ -{threshold*100:.0f}%，半仓减仓',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
            )
        return None

    def _check_take_profit(self, sym, entry, current, up, peak, days, bars, p_params
                            ) -> Optional[ExitSignal]:
        """分批止盈：+15% 时半仓，+25% 时剩余全出。"""
        tp_scale = p_params.get('tp_scale', self.tp_scale)
        tp_full  = p_params.get('tp_full',  self.tp_full)

        if up >= tp_full:
            return ExitSignal(
                symbol=sym, priority=ExitPriority.TAKE_PROFIT_FULL,
                exit_pct=1.0,
                reason=f'全仓止盈：浮盈 {up*100:.1f}% ≥ {tp_full*100:.0f}%，落袋为安',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
            )
        if up >= tp_scale:
            return ExitSignal(
                symbol=sym, priority=ExitPriority.TAKE_PROFIT_SCALE,
                exit_pct=0.5,
                reason=f'半仓止盈：浮盈 {up*100:.1f}% ≥ {tp_scale*100:.0f}%，锁定部分利润',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
            )
        return None

    def _check_rsi_overbought(self, sym, entry, current, up, days, bars, p_params
                               ) -> Optional[ExitSignal]:
        """RSI 超买：日线 RSI ≥ rsi_sell，半仓减仓（均值回归策略退出）。"""
        if bars is None or len(bars) < 20:
            return None
        rsi_sell = p_params.get('rsi_sell', self.rsi_sell)
        close = bars['close'].values
        rsi = _compute_rsi(close, period=14)
        if rsi >= rsi_sell and up > 0:
            # 仅在有盈利状态下用 RSI 超买减仓（避免超买+亏损时误杀）
            return ExitSignal(
                symbol=sym, priority=ExitPriority.RSI_OVERBOUGHT,
                exit_pct=0.5,
                reason=f'RSI超买：日线 RSI={rsi:.0f} ≥ {rsi_sell}，浮盈 {up*100:.1f}%，半仓兑现',
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
                metadata={'rsi': round(rsi, 1)},
            )
        return None

    def _check_time_stop(self, sym, entry, current, up, days, p_params
                          ) -> Optional[ExitSignal]:
        """
        时间止损：持仓超 max_hold_days 且浮盈不足 tp_scale（机会成本控制）。
        此规则只减仓不清仓，给市场更多时间，但释放资金做更好的机会。
        """
        max_days = p_params.get('max_hold_days', self.max_hold_days)
        tp_scale = p_params.get('tp_scale', self.tp_scale)
        if days >= max_days and up < tp_scale * 0.5:
            return ExitSignal(
                symbol=sym, priority=ExitPriority.TIME_STOP,
                exit_pct=0.5,
                reason=(f'时间止损：持仓 {days} 天 ≥ {max_days} 天，'
                        f'浮盈 {up*100:.1f}% 不达预期，半仓释放资金'),
                current_price=current, entry_price=entry, unrealized_pct=up,
                holding_days=days,
            )
        return None

    # ── 工具方法 ──────────────────────────────────────────────────────────

    @staticmethod
    def _holding_days(entry_date) -> int:
        """计算持仓天数（支持 str/date/datetime 格式）。"""
        if entry_date is None:
            return 0
        try:
            if isinstance(entry_date, str):
                ed = date.fromisoformat(entry_date[:10])
            elif isinstance(entry_date, datetime):
                ed = entry_date.date()
            else:
                ed = entry_date
            return max(0, (date.today() - ed).days)
        except Exception:
            return 0

    @staticmethod
    def _params_with_defaults(custom: Dict, symbol_params: Dict) -> Dict:
        """合并 per-symbol 参数和引擎默认值。"""
        merged = dict(symbol_params)
        merged.update(custom)
        return merged

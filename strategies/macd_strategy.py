"""
strategies/macd_strategy.py — MACD 策略插件
============================================
基于 MACD 金叉死叉的经典趋势跟踪策略。

参数（params）：
    fast_period  : 快线周期（默认 12）
    slow_period  : 慢线周期（默认 26）
    signal_period: 信号线周期（默认 9）
    stop_loss    : 止损比例（默认 0.08）
    take_profit  : 止盈比例（默认 0.25）
    min_hold_days: 最小持仓天数（默认 5）
"""

from typing import Dict, List
from strategies.base import BaseStrategy


class MACDStrategy(BaseStrategy):
    """
    MACD 金叉死叉策略。

    逻辑：
      - DIF 上穿 DEA（金叉）→ 买入信号
      - DIF 下穿 DEA（死叉）→ 卖出信号
      - DIF > 0 且柱状体扩大 → 强势
      - DIF < 0 → 下跌趋势，禁止买入
    """

    name    = 'MACDStrategy'
    version = '1.0'

    def __init__(self, symbol: str, params: Dict = None):
        super().__init__(symbol, params)
        self.fast   = int(self.params.get('fast_period', 12))
        self.slow   = int(self.params.get('slow_period', 26))
        self.signal = int(self.params.get('signal_period', 9))
        self.stop_loss   = float(self.params.get('stop_loss', 0.08))
        self.take_profit = float(self.params.get('take_profit', 0.25))
        self.min_hold    = int(self.params.get('min_hold_days', 5))

        self._prev_dif: float  = 0.0
        self._prev_dea: float  = 0.0
        self._entry_price: float = 0.0
        self._hold_days:   int   = 0
        self._in_position: bool = False

    def evaluate(self, data: List[dict], i: int) -> Dict:
        self._data = data
        if i < self.slow + self.signal + 2:
            return self._hold('data_not_ready')

        closes = self.closes()
        ema_fast = self.compute_ema(closes[:i+1], self.fast)
        ema_slow = self.compute_ema(closes[:i+1], self.slow)

        # 对齐到同一长度
        n = min(len(ema_fast), len(ema_slow))
        dif = [ema_fast[n-1] - ema_slow[n-1]]
        # DEA = EMA(dif, signal_period)
        k = 2.0 / (self.signal + 1)
        dea = [sum(dif[:self.signal]) / self.signal]
        for v in dif[self.signal:]:
            dea.append(v * k + dea[-1] * (1 - k))

        if len(dif) < 2 or len(dea) < 2:
            return self._hold('macd_not_ready')

        dif_prev = dif[-2]
        dea_prev = dea[-2]
        dif_cur  = dif[-1]
        dea_cur  = dea[-1]
        price    = data[i]['close']

        # ── 有持仓 ────────────────────────────────────────
        if self._in_position:
            self._hold_days += 1
            pnl = (price - self._entry_price) / self._entry_price

            if pnl <= -self.stop_loss:
                return self._sell('stop_loss', pnl)
            if pnl >= self.take_profit:
                return self._sell('take_profit', pnl)
            # 死叉
            if dif_prev <= dea_prev and dif_cur > dea_cur and self._hold_days >= self.min_hold:
                return self._sell('macd_death_cross', pnl)
            return self._hold(f'holding_pnl({pnl:.1%})')

        # ── 无持仓 ────────────────────────────────────────
        # 金叉：dif 从负转正或dif上穿dea
        if dif_prev <= dea_prev < dif_cur and dif_cur > dea_cur:
            self._entry_price = price
            self._hold_days = 0
            self._in_position = True
            strength = 0.9 if dif_cur > 0 else 0.7
            return {
                'signal':   'buy',
                'strength': strength,
                'reason':   f'macd_golden_cross(dif={dif_cur:.4f})',
                'meta': {'dif': dif_cur, 'dea': dea_cur, 'pnl': 0.0},
            }

        # dif 仍在零轴下方，趋势不明
        if dif_cur < 0:
            return {
                'signal':   'hold',
                'strength': 0.0,
                'reason':   f'macd_below_zero(dif={dif_cur:.4f})',
                'meta': {'dif': dif_cur, 'dea': dea_cur},
            }

        return {
            'signal':   'hold',
            'strength': 0.0,
            'reason':   f'macd_neutral(dif={dif_cur:.4f})',
            'meta': {'dif': dif_cur, 'dea': dea_cur},
        }

    def _sell(self, reason: str, pnl: float) -> Dict:
        self._in_position = False
        return {
            'signal':   'sell',
            'strength': 0.8,
            'reason':   reason,
            'meta': {'entry': self._entry_price, 'pnl': pnl},
        }

    def _hold(self, reason: str) -> Dict:
        return {
            'signal':   'hold',
            'strength': 0.0,
            'reason':   reason,
            'meta': {},
        }

    def reset(self):
        super().reset()
        self._prev_dif = 0.0
        self._prev_dea = 0.0
        self._entry_price = 0.0
        self._hold_days   = 0
        self._in_position = False

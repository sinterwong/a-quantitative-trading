"""
strategies/rsi_strategy.py — RSI 策略插件
==========================================
基于 RSI 超买超卖指标的经典择时策略。

参数（params）：
    rsi_period    : RSI 周期（默认 14）
    rsi_buy       : 超卖阈值（默认 35，低于此值视为买入信号）
    rsi_sell      : 超买阈值（默认 65，高于此值视为卖出信号）
    stop_loss     : 止损比例（默认 0.08 = 8%）
    take_profit   : 止盈比例（默认 0.25 = 25%）
    min_hold_days : 最小持仓天数（默认 3）

示例：
    from strategies import load_strategy
    strat = load_strategy('RSI', {
        'rsi_buy': 30, 'rsi_sell': 65, 'stop_loss': 0.08
    }, symbol='600519.SH')
    result = strat.evaluate(kline_data, i=-1)
"""

from typing import Dict, List
from strategies.base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """
    RSI 超买超卖策略。

    逻辑：
      - RSI < rsi_buy  → 超卖区域，触发 WATCH_BUY
      - RSI < rsi_buy 且价格已反弹（动量确认） → RSI_BUY（强）
      - RSI > rsi_sell → 超买区域，触发 WATCH_SELL
      - 持仓中 RSI 进入超买 → 触发 SELL
    """

    name    = 'RSIStrategy'
    version = '1.0'

    def __init__(self, symbol: str, params: Dict = None):
        super().__init__(symbol, params)
        self.period      = int(self.params.get('rsi_period', 14))
        self.rsi_buy     = float(self.params.get('rsi_buy', 35))
        self.rsi_sell    = float(self.params.get('rsi_sell', 65))
        self.stop_loss   = float(self.params.get('stop_loss', 0.08))
        self.take_profit = float(self.params.get('take_profit', 0.25))
        self.min_hold    = int(self.params.get('min_hold_days', 3))

        # 持仓状态
        self._entry_price: float  = 0.0
        self._entry_idx:   int    = 0
        self._hold_days:   int    = 0
        self._in_position: bool   = False

    def evaluate(self, data: List[dict], i: int) -> Dict:
        self._data = data
        if i < self.period + 2:
            return self._hold_result('data_not_ready')

        closes  = self.closes()
        rsi_vals = self.compute_rsi(closes[:i+1], self.period)
        if not rsi_vals:
            return self._hold_result('rsi_not_ready')

        rsi     = rsi_vals[-1]
        rsi_prev = rsi_vals[-2] if len(rsi_vals) >= 2 else rsi
        price   = data[i]['close']
        pct_chg = (price - closes[i-1]) / closes[i-1] if i > 0 else 0.0

        # ── 有持仓：检查止损/止盈/卖出 ──────────────────────
        if self._in_position:
            self._hold_days += 1
            pnl = (price - self._entry_price) / self._entry_price

            # 止损
            if pnl <= -self.stop_loss:
                return self._sell_result(f'stop_loss({pnl:.1%})', pnl)
            # 止盈
            if pnl >= self.take_profit:
                return self._sell_result(f'take_profit({pnl:.1%})', pnl)
            # RSI 超买死叉
            if rsi_prev <= self.rsi_sell < rsi and self._hold_days >= self.min_hold:
                return self._sell_result(f'rsi_overbought({rsi:.0f})', pnl)
            # 持有
            return self._hold_result(f'holding_pnl({pnl:.1%})')

        # ── 无持仓：检查买入 ────────────────────────────────
        if rsi_prev < self.rsi_buy <= rsi:
            # 反弹确认：RSI 从超卖区上穿
            strength = 0.9 if pct_chg > 0.01 else 0.6
            self._entry_price = price
            self._entry_idx   = i
            self._hold_days   = 0
            self._in_position = True
            return {
                'signal':   'buy',
                'strength': strength,
                'reason':   f'rsi_oversold_enter({rsi:.0f}≤{self.rsi_buy})',
                'meta': {'rsi': rsi, 'price': price, 'pnl': 0.0},
            }

        if rsi <= self.rsi_buy:
            # 超卖区间（尚未确认反弹）
            return {
                'signal':   'watch_buy',
                'strength': max(0.0, (self.rsi_buy - rsi) / self.rsi_buy),
                'reason':   f'rsi_oversold_watch({rsi:.0f}≤{self.rsi_buy})',
                'meta': {'rsi': rsi, 'price': price},
            }

        # 观望
        return {
            'signal':   'hold',
            'strength': 0.0,
            'reason':   f'rsi_neutral({rsi:.0f})',
            'meta': {'rsi': rsi},
        }

    def _sell_result(self, reason: str, pnl: float) -> Dict:
        self._in_position = False
        return {
            'signal':   'sell',
            'strength': 0.8,
            'reason':   reason,
            'meta': {'entry': self._entry_price, 'pnl': pnl},
        }

    def _hold_result(self, reason: str) -> Dict:
        return {
            'signal':   'hold',
            'strength': 0.0,
            'reason':   reason,
            'meta': {},
        }

    def reset(self):
        super().reset()
        self._entry_price = 0.0
        self._entry_idx   = 0
        self._hold_days   = 0
        self._in_position = False

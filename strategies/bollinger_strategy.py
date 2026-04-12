"""
strategies/bollinger_strategy.py — 布林带策略插件
==================================================
基于布林带（Bollinger Bands）的均值回归策略。

参数（params）：
    period      : 均线周期（默认 20）
    std_mult    : 标准差倍数（默认 2.0，即 ±2σ）
    stop_loss   : 止损比例（默认 0.08）
    take_profit : 止盈比例（默认 0.20）
"""

from typing import Dict, List
import math
from strategies.base import BaseStrategy


class BollingerBandStrategy(BaseStrategy):
    """
    布林带均值回归策略。

    逻辑：
      - 价格下穿下轨 → 超卖，买入信号
      - 价格上穿上轨 → 超买，卖出信号
      - 布林带收窄（Bandwidth < 阈值）→ 即将突破，观望
      - 结合 RSI 增加确认
    """

    name    = 'BollingerBandStrategy'
    version = '1.0'

    def __init__(self, symbol: str, params: Dict = None):
        super().__init__(symbol, params)
        self.period   = int(self.params.get('period', 20))
        self.std_mult = float(self.params.get('std_mult', 2.0))
        self.stop_loss   = float(self.params.get('stop_loss', 0.08))
        self.take_profit = float(self.params.get('take_profit', 0.20))
        self.rsi_period  = int(self.params.get('rsi_period', 14))
        self.rsi_buy      = float(self.params.get('rsi_buy', 35))

        self._entry_price: float = 0.0
        self._in_position: bool  = False

    def _bollinger_bands(self, closes: List[float]) -> tuple:
        """返回 (upper, middle, lower) 列表"""
        if len(closes) < self.period:
            return [], [], []
        n = len(closes)
        mids = []
        uppers, lowers = [], []
        for i in range(self.period - 1, n):
            window = closes[i - self.period + 1:i + 1]
            mid = sum(window) / self.period
            variance = sum((x - mid) ** 2 for x in window) / self.period
            std = math.sqrt(variance)
            mids.append(mid)
            uppers.append(mid + self.std_mult * std)
            lowers.append(mid - self.std_mult * std)
        return uppers, mids, lowers

    def evaluate(self, data: List[dict], i: int) -> Dict:
        self._data = data
        if i < self.period + 2:
            return self._hold('data_not_ready')

        closes = self.closes()
        uppers, mids, lowers = self._bollinger_bands(closes[:i+1])
        if not uppers:
            return self._hold('bollinger_not_ready')

        upper = uppers[-1]
        lower = lowers[-1]
        mid   = mids[-1]
        price = data[i]['close']
        rsi_vals = self.compute_rsi(closes[:i+1], self.rsi_period)
        rsi = rsi_vals[-1] if rsi_vals else 50.0

        # ── 有持仓 ────────────────────────────────────────
        if self._in_position:
            pnl = (price - self._entry_price) / self._entry_price
            if pnl <= -self.stop_loss:
                return self._sell('stop_loss', pnl)
            if pnl >= self.take_profit:
                return self._sell('take_profit', pnl)
            # 价格回到中轨以上，止盈
            if price >= mid:
                return self._sell(f'bollinger_upper_hit({price:.2f}>{mid:.2f})', pnl)
            return self._hold(f'holding_pnl({pnl:.1%})')

        # ── 无持仓 ────────────────────────────────────────
        # 价格触及下轨 + RSI 超卖
        if price <= lower and rsi < self.rsi_buy:
            self._entry_price = price
            self._in_position = True
            return {
                'signal':   'buy',
                'strength': 0.85,
                'reason':   f'bollinger_oversold({price:.2f}<{lower:.2f})_rsi({rsi:.0f})',
                'meta': {'price': price, 'lower': lower, 'rsi': rsi},
            }

        # 价格触及下轨（但 RSI 尚未超卖）
        if price <= lower:
            return {
                'signal':   'watch_buy',
                'strength':  0.5,
                'reason':   f'bollinger_near_lower({price:.2f}<{lower:.2f})',
                'meta': {'price': price, 'lower': lower, 'rsi': rsi},
            }

        # 价格接近上轨（超买）
        if price >= upper:
            return {
                'signal':   'sell',
                'strength':  0.7,
                'reason':   f'bollinger_overbought({price:.2f}>{upper:.2f})',
                'meta': {'price': price, 'upper': upper, 'rsi': rsi},
            }

        bandwidth = (upper - lower) / mid if mid != 0 else 0
        return {
            'signal':   'hold',
            'strength': 0.0,
            'reason':   f'bollinger_neutral(bw={bandwidth:.3f})',
            'meta': {'price': price, 'upper': upper, 'lower': lower, 'bandwidth': bandwidth},
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
        self._entry_price = 0.0
        self._in_position = False

"""
仓位管理模块
- Kelly公式动态计算仓位
- ATR波动率调整
- 最大回撤控制
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import TechnicalIndicators as TI


class PositionSizer:
    """
    动态仓位管理器
    """

    def __init__(self, data, method='fixed'):
        """
        Args:
            data: K线数据
            method: 'fixed' | 'kelly' | 'atr' | 'volatility'
        """
        self.data = data
        self.method = method

    def get_position_size(self, i, signal, capital, params=None):
        """
        计算仓位

        Args:
            i: 当前索引
            signal: 'buy' / 'sell' / 'hold'
            capital: 当前可用资金
            params: dict with keys:
                - kelly_fraction: Kelly公式的比例系数（默认0.5，半Kelly）
                - atr_multiplier: ATR止损倍数（默认2）
                - max_position_pct: 最大仓位比例（默认0.3=30%）

        Returns:
            dict: {'shares': int, 'position_value': float, 'position_pct': float}
        """
        if signal != 'buy':
            return {'shares': 0, 'position_value': 0, 'position_pct': 0}

        p = params or {}
        kelly_fraction = p.get('kelly_fraction', 0.5)
        atr_multiplier = p.get('atr_multiplier', 2.0)
        max_position_pct = p.get('max_position_pct', 0.30)

        closes = [d['close'] for d in self.data]
        highs = [d['high'] for d in self.data]
        lows = [d['low'] for d in self.data]

        current_price = closes[i]
        atr_vals = TI.atr(highs, lows, closes, 14)
        atr = atr_vals[-1] if len(atr_vals) > 0 else current_price * 0.02

        if self.method == 'kelly':
            # Kelly公式: f* = (bp - q) / b
            # 这里用简化版：基于历史胜率和盈亏比估算
            if i >= 60:
                returns = [(closes[j] - closes[j-1]) / closes[j-1]
                          for j in range(max(1, i-60), i)]
                wins = [r for r in returns if r > 0]
                losses = [r for r in returns if r < 0]

                if wins and losses:
                    win_rate = len(wins) / len(returns)
                    avg_win = sum(wins) / len(wins)
                    avg_loss = abs(sum(losses) / len(losses))
                    b = avg_win / avg_loss if avg_loss > 0 else 1
                    q = 1 - win_rate
                    kelly = max(0, min((b * win_rate - q) / b, 0.5))  # 上限50%
                    kelly = kelly * kelly_fraction
                else:
                    kelly = 0.05  # 默认5%
            else:
                kelly = 0.10  # 数据不足默认10%

            position_pct = kelly

        elif self.method == 'atr':
            # ATR波动率调整仓位
            # 波动越大，仓位越小
            atr_pct = atr / current_price
            target_risk = 0.02  # 每笔交易最多亏2%
            risk_per_share = atr * atr_multiplier
            position_pct = min(target_risk * current_price / risk_per_share, max_position_pct)

        elif self.method == 'volatility':
            # 波动率倒数仓位
            if i >= 20:
                recent_vol = sum((closes[j] - closes[j-1]) / closes[j-1] ** 2
                                for j in range(max(1, i-20), i)) ** 0.5
                vol_pct = recent_vol / 20  # 年化波动率估算
                target_vol = 0.15  # 目标波动率15%
                position_pct = min(target_vol / (vol_pct + 0.001), max_position_pct)
            else:
                position_pct = 0.20

        else:
            # 固定仓位
            position_pct = min(0.30, max_position_pct)

        # 最终仓位限制
        position_pct = min(position_pct, max_position_pct)
        position_value = capital * position_pct
        shares = int(position_value / current_price)

        return {
            'shares': shares,
            'position_value': shares * current_price,
            'position_pct': shares * current_price / capital if capital > 0 else 0,
            'atr_stop_distance': atr * atr_multiplier
        }


class ATRStopManager:
    """
    ATR止损管理器
    - Chandelier Exit: 从最高点回撤2ATR止损
    - 动态调整止损线
    """

    def __init__(self, data, atr_period=14, atr_multiplier=2.0):
        self.data = data
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        closes = [d['close'] for d in data]
        highs = [d['high'] for d in data]
        lows = [d['low'] for d in data]
        self.atr_vals = TI.atr(highs, lows, closes, atr_period)

    def get_stop_price(self, entry_idx, entry_price, current_idx):
        """
        获取止损价格

        Args:
            entry_idx: 入场索引
            entry_price: 入场价格
            current_idx: 当前索引

        Returns:
            float: 止损价（价格低于此则止损）
        """
        if current_idx >= len(self.atr_vals):
            current_idx = len(self.atr_vals) - 1

        atr = self.atr_vals[current_idx - self.atr_period] if current_idx - self.atr_period >= 0 else self.atr_vals[0]

        # Chandelier Exit: 最高价 - 2ATR
        window_high = max(d['high'] for d in self.data[entry_idx:current_idx+1])
        stop_price = window_high - self.atr_multiplier * atr

        # 同时不能低于成本价太多（最多回撤15%）
        max_loss = entry_price * 0.15
        stop_price = max(stop_price, entry_price - max_loss)

        return stop_price

    def should_stop(self, entry_idx, entry_price, current_idx):
        """
        判断是否应该止损

        Returns:
            (bool, reason, stop_price)
        """
        current_price = self.data[current_idx]['close']
        stop_price = self.get_stop_price(entry_idx, entry_price, current_idx)

        if current_price <= stop_price:
            return True, 'atr_stop', stop_price

        # 时间止损（持有超过N天）
        hold_days = current_idx - entry_idx
        max_hold_days = self.atr_period * 3
        if hold_days > max_hold_days:
            return True, 'time_stop', stop_price

        return False, None, stop_price

"""
均值回归策略 v2
- RSI超卖买入，超买卖出
- 布林带下轨买入，上轨卖出
- RSRS阻力支撑指标
- 所有策略支持止盈止损参数
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import TechnicalIndicators as TI


def rsi_strategy(data, params=None):
    """RSI均值回归策略"""
    p = params or {}
    period = p.get('period', 14)
    oversold = p.get('oversold', 30)
    overbought = p.get('overbought', 70)

    closes = [d['close'] for d in data]
    rsi_vals = TI.rsi(closes, period)

    def signal(data, i):
        if i < period + 1:
            return 'hold'
        idx = i - period
        if idx >= len(rsi_vals) or idx - 1 < 0:
            return 'hold'
        current_rsi = rsi_vals[idx]
        prev_rsi = rsi_vals[idx - 1]

        # 金叉买入
        if prev_rsi < oversold and current_rsi >= oversold:
            return 'buy'
        # 死叉卖出
        elif prev_rsi > overbought and current_rsi <= overbought:
            return 'sell'
        return 'hold'

    return signal


def bollinger_strategy(data, params=None):
    """布林带均值回归策略"""
    p = params or {}
    period = p.get('period', 20)
    std_dev = p.get('std_dev', 2.0)

    closes = [d['close'] for d in data]
    mid, upper, lower = TI.bollinger_bands(closes, period, std_dev)

    def signal(data, i):
        if i < period:
            return 'hold'
        idx = i - period
        if idx >= len(mid) or idx < 0:
            return 'hold'

        curr_price = data[i]['close']
        prev_price = data[i - 1]['close']

        # 买入：价格下穿下轨
        if prev_price > lower[idx] and curr_price <= lower[idx]:
            return 'buy'
        # 卖出：价格上穿上轨
        elif prev_price < upper[idx] and curr_price >= upper[idx]:
            return 'sell'
        return 'hold'

    return signal


def rsrs_strategy(data, params=None):
    """
    RSRS阻力支撑策略 v2
    当RSRS指标高于阈值且价格站稳均线时买入
    当RSRS指标低于阈值时卖出
    """
    p = params or {}
    period = p.get('period', 18)
    entry_threshold = p.get('entry_threshold', 0.5)  # 入场阈值
    exit_threshold = p.get('exit_threshold', 0.3)   # 出场阈值

    highs = [d['high'] for d in data]
    lows = [d['low'] for d in data]
    closes = [d['close'] for d in data]

    rsrs_vals = TI.rsrs(highs, lows, period)
    ma = TI.sma(closes, 20)

    def signal(data, i):
        if i < period + 20:
            return 'hold'

        rsrs_idx = i - period
        ma_idx = i - 20

        if rsrs_idx >= len(rsrs_vals) or rsrs_idx < 0:
            return 'hold'
        if ma_idx >= len(ma) or ma_idx < 0:
            return 'hold'

        current_rsrs = rsrs_vals[rsrs_idx]
        prev_rsrs = rsrs_vals[rsrs_idx - 1] if rsrs_idx > 0 else current_rsrs
        price = data[i]['close']
        ma_val = ma[ma_idx]

        # 买入：RSRS上穿阈值 + 价格在均线上方
        if prev_rsrs < entry_threshold <= current_rsrs and price > ma_val:
            return 'buy'
        # 卖出：RSRS下穿出场阈值
        elif prev_rsrs > exit_threshold >= current_rsrs:
            return 'sell'
        return 'hold'

    return signal


def dual_combined_strategy(data, params=None):
    """
    RSI + 布林带组合均值回归策略
    """
    p = params or {}
    period = p.get('period', 20)
    rsi_period = p.get('rsi_period', 14)
    oversold = p.get('oversold', 30)
    overbought = p.get('overbought', 70)

    closes = [d['close'] for d in data]
    rsi_vals = TI.rsi(closes, rsi_period)
    mid, upper, lower = TI.bollinger_bands(closes, period)

    def signal(data, i):
        if i < max(period, rsi_period + 1):
            return 'hold'

        bb_idx = i - period
        rsi_idx = i - rsi_period

        if bb_idx >= len(mid) or rsi_idx >= len(rsi_vals) or bb_idx < 0 or rsi_idx < 0:
            return 'hold'

        current_price = data[i]['close']
        current_rsi = rsi_vals[rsi_idx]
        current_lower = lower[bb_idx]
        current_upper = upper[bb_idx]

        # 买入：RSI超卖 + 价格触及布林下轨
        rsi_buy = current_rsi < oversold
        bb_touch_lower = current_price <= current_lower * 1.01  # 1%容差

        # 卖出：RSI超买 + 价格触及布林上轨
        rsi_sell = current_rsi > overbought
        bb_touch_upper = current_price >= current_upper * 0.99

        if rsi_buy and bb_touch_lower:
            return 'buy'
        elif rsi_sell and bb_touch_upper:
            return 'sell'
        return 'hold'

    return signal


def atr_channel_strategy(data, params=None):
    """
    ATR通道策略 - 基于真实波幅的止损/止盈参考
    也可作为一种均值回归信号
    """
    p = params or {}
    period = p.get('period', 20)
    multiplier = p.get('multiplier', 2.0)

    highs = [d['high'] for d in data]
    lows = [d['low'] for d in data]
    closes = [d['close'] for d in data]

    atr_vals = TI.atr(highs, lows, closes, period)
    ma = TI.sma(closes, period)

    def signal(data, i):
        if i < period or len(atr_vals) == 0:
            return 'hold'

        idx = i - period
        if idx >= len(atr_vals) or idx < 0:
            return 'hold'

        current_price = data[i]['close']
        upper = ma[idx] + multiplier * atr_vals[idx]
        lower = ma[idx] - multiplier * atr_vals[idx]

        # 回归均线时买入
        if current_price < lower:
            return 'buy'
        # 触及上轨回归时卖出
        elif current_price > upper:
            return 'sell'
        return 'hold'

    return signal

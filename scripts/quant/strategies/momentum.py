"""
趋势跟踪/动量策略
- 均线金叉死叉
- 动量效应
"""

import sys
sys.path.insert(0, __file__.rsplit('/', 2)[0])

from backtest import TechnicalIndicators as TI


def ma_cross_strategy(data, params=None):
    """
    均线金叉死叉策略
    params: {'short_period': 5, 'long_period': 20}
    """
    p = params or {}
    short_period = p.get('short_period', 5)
    long_period = p.get('long_period', 20)

    closes = [d['close'] for d in data]
    short_ma = TI.sma(closes, short_period)
    long_ma = TI.sma(closes, long_period)

    def signal(data, i):
        if i < long_period:
            return 'hold'

        short_idx = i - short_period
        long_idx = i - long_period

        if short_idx < 0 or long_idx < 0:
            return 'hold'

        prev_short = short_ma[short_idx - 1] if short_idx > 0 else short_ma[short_idx]
        curr_short = short_ma[short_idx]
        prev_long = long_ma[long_idx - 1] if long_idx > 0 else long_ma[long_idx]
        curr_long = long_ma[long_idx]

        # 金叉：短均线上穿长均线
        if prev_short <= prev_long and curr_short > curr_long:
            return 'buy'
        # 死叉：短均线下穿长均线
        elif prev_short >= prev_long and curr_short < curr_long:
            return 'sell'
        return 'hold'

    return signal


def dual_ma_strategy(data, params=None):
    """
    双均线趋势策略（过滤假信号）
    params: {'short_period': 10, 'long_period': 60, 'rsi_threshold': 60}
    """
    p = params or {}
    short_period = p.get('short_period', 10)
    long_period = p.get('long_period', 60)
    rsi_threshold = p.get('rsi_threshold', 60)

    closes = [d['close'] for d in data]
    short_ma = TI.sma(closes, short_period)
    long_ma = TI.sma(closes, long_period)
    rsi = TI.rsi(closes, 14)

    def signal(data, i):
        if i < long_period or i < 14:
            return 'hold'

        short_idx = i - short_period
        long_idx = i - long_period
        rsi_idx = i - 14

        if short_idx < 0 or long_idx < 0 or rsi_idx < 0:
            return 'hold'

        prev_short = short_ma[max(0, short_idx - 1)]
        curr_short = short_ma[short_idx]
        prev_long = long_ma[max(0, long_idx - 1)]
        curr_long = long_ma[long_idx]
        curr_rsi = rsi[rsi_idx]

        # 买入：短均线上穿长均线 + RSI健康（不过热）
        if prev_short <= prev_long and curr_short > curr_long and curr_rsi < rsi_threshold:
            return 'buy'
        # 卖出：短均线下穿长均线 或 RSI过高
        elif (prev_short >= prev_long and curr_short < curr_long) or curr_rsi > 85:
            return 'sell'
        return 'hold'

    return signal


def momentum_strategy(data, params=None):
    """
    动量策略
    近N日涨幅超过阈值买入
    """
    p = params or {}
    lookback = p.get('lookback', 20)
    momentum_threshold = p.get('momentum_threshold', 0.05)

    closes = [d['close'] for d in data]

    def signal(data, i):
        if i < lookback:
            return 'hold'

        momentum = (data[i]['close'] - data[i - lookback]['close']) / data[i - lookback]['close']

        # 强势动量
        if momentum > momentum_threshold:
            # 回踩均线买入
            ma = sum(closes[i - 5:i]) / 5
            if data[i]['close'] < ma and data[i - 1]['close'] >= data[i - 2]['close']:
                return 'buy'

        # 动量衰竭
        if momentum < -momentum_threshold:
            return 'sell'

        return 'hold'

    return signal

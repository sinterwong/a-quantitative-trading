"""
板块轮动策略
- ETF轮动：强势板块切换
- 动量强度择时
"""

import sys
sys.path.insert(0, __file__.rsplit('/', 2)[0])

from backtest import TechnicalIndicators as TI


def etf_rotation_strategy(etf_data_dict, params=None):
    """
    ETF板块轮动策略

    Args:
        etf_data_dict: dict of {etf_name: kline_data}
        e.g. {'酒ETF': [...], '创新药ETF': [...], '长江电力': [...]}

    逻辑：
    1. 计算各ETF过去N日动量
    2. 选取动量最强的ETF持有
    3. 每月再平衡
    """
    p = params or {}
    lookback = p.get('lookback', 20)
    rebalance_days = p.get('rebalance_days', 20)

    def signal(data, i):
        if i < lookback:
            return 'hold'

        # 每月第一个交易日再平衡
        if i % rebalance_days != 0:
            return 'hold'

        # 计算各ETF动量
        momentum_dict = {}
        for name, kline in etf_data_dict.items():
            if len(kline) > i and i >= lookback:
                start_price = kline[i - lookback]['close']
                end_price = kline[i]['close']
                momentum_dict[name] = (end_price - start_price) / start_price

        if not momentum_dict:
            return 'hold'

        # 选取最强板块
        best_etf = max(momentum_dict, key=momentum_dict.get)

        # 如果当前持仓不是最强板块，切换
        # 这里返回signal，实际使用时需要比对持仓标的
        return 'buy'  # 切换信号，实际操作由外部组合管理

    return signal


def relative_strength_strategy(data, params=None):
    """
    相对强弱策略（个股vs指数）
    跑赢指数的股票继续持有，跑输则卖出
    """
    p = params or {}
    index_data = p.get('index_data', None)  # 大盘指数K线
    lookback = p.get('lookback', 20)

    closes = [d['close'] for d in data]

    def signal(data, i):
        if i < lookback:
            return 'hold'

        stock_return = (data[i]['close'] - data[i - lookback]['close']) / data[i - lookback]['close']

        if index_data and len(index_data) > i:
            index_return = (index_data[i]['close'] - index_data[i - lookback]['close']) / index_data[i - lookback]['close']
            relative_strength = stock_return - index_return

            # 相对强弱超过阈值
            if relative_strength > 0.02:
                return 'buy'
            elif relative_strength < -0.02:
                return 'sell'

        # 无指数对比时，用动量
        if stock_return > 0.08:
            return 'buy'
        elif stock_return < -0.05:
            return 'sell'

        return 'hold'

    return signal


def sector_momentum_strategy(sector_etfs, params=None):
    """
    行业动量轮动策略

    Args:
        sector_etfs: {行业名称: ETF_K线数据}
    """
    p = params or {}
    top_n = p.get('top_n', 3)
    lookback = p.get('lookback', 60)

    def signal(data, i):
        if i < lookback:
            return 'hold'

        # 计算各行业动量
        momentum_scores = {}
        for sector, kline in sector_etfs.items():
            if len(kline) > i and i >= lookback:
                momentum = (kline[i]['close'] - kline[i - lookback]['close']) / kline[i - lookback]['close']
                momentum_scores[sector] = momentum

        if not momentum_scores:
            return 'hold'

        # 选取动量最强的前N个行业
        sorted_sectors = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        strong_sectors = [s[0] for s in sorted_sectors[:top_n]]

        return strong_sectors

    return signal

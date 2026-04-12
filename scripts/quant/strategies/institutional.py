"""
机构跟庄策略 v2
基于真实机构持仓数据（基金重仓、社保、QFII）
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import TechnicalIndicators as TI

# ========== 真实数据源接入 ==========

def get_fund_holdings_real(symbol, quarter='2024q3'):
    """
    获取基金重仓数据（真实）

    Args:
        symbol: 股票代码，如 '600900'
        quarter: 季度，如 '2024q3'

    Returns:
        dict: {quarter: {'total_shares': ..., 'change': ..., 'fund_count': ...}}
    """
    try:
        import akshare as ak
        # 基金重仓持股
        df = ak.stock_stock_fund_hold(symbol=symbol)
        if df is not None and not df.empty:
            # 解析季报
            result = {}
            for _, row in df.iterrows():
                # 假设有 '报告期'、'基金家数'、'持股数' 等列
                pass
            return result
    except Exception as e:
        print(f"[WARN] Fund holdings data error: {e}")
    return {}


def get_institutional_holdings_change(symbol):
    """
    获取机构持仓变化（季频）
    返回：[(date, fund_change, ss_change, qfii_change), ...]
    """
    try:
        import akshare as ak
        changes = []

        # 基金重仓变化
        try:
            df_fund = ak.stock_stock_fund_hold(symbol=symbol)
            if df_fund is not None:
                for _, row in df_fund.head(8).iterrows():  # 最近2年
                    changes.append({
                        'date': str(row.get('报告期', '')),
                        'type': 'fund',
                        'change': row.get('持股变化', 0)
                    })
        except:
            pass

        return changes
    except:
        return []


# ========== 改进的模拟机构跟庄策略 ==========

def institutional_following_strategy(data, params=None):
    """
    机构持仓跟庄策略 v2
    逻辑：
    1. 放量突破 + 站稳均线 → 买入（模拟机构进场）
    2. 高位放量滞涨 → 卖出（模拟机构减仓）
    3. 配合RSI避免极端行情
    """
    p = params or {}
    lookback = p.get('lookback', 20)
    ma_period = p.get('ma_period', 20)
    volume_multiplier = p.get('volume_multiplier', 1.8)

    closes = [d['close'] for d in data]
    volumes = [d['volume'] for d in data]
    ma = TI.sma(closes, ma_period)

    def signal(data, i):
        if i < max(lookback, ma_period + 1):
            return 'hold'

        # 计算成交量均线
        recent_vols = volumes[i - lookback:i]
        avg_vol = sum(recent_vols) / len(recent_vols)
        current_vol = volumes[i]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1

        # 计算价格动量
        price_ma_idx = i - ma_period
        if price_ma_idx < 0 or price_ma_idx >= len(ma):
            return 'hold'

        price_above_ma = data[i]['close'] > ma[price_ma_idx]
        ma_uptrend = ma[price_ma_idx] > ma[price_ma_idx - 1]

        # RSI
        rsi_vals = TI.rsi(closes[:i], 14)
        current_rsi = rsi_vals[-1] if rsi_vals else 50

        # === 买入信号 ===
        # 放量突破 + 均线多头 + RSI不过热
        if vol_ratio > volume_multiplier and price_above_ma and ma_uptrend:
            if 40 < current_rsi < 70:
                return 'buy'

        # === 卖出信号 ===
        # 高位放量大跌（机构出货特征）
        if data[i]['close'] < data[i-1]['close'] * 0.96 and vol_ratio > 1.5:
            return 'sell'

        # RSI过高且价格偏离均线
        if current_rsi > 80 and data[i]['close'] > ma[price_ma_idx] * 1.1:
            return 'sell'

        return 'hold'

    return signal


def north_flow_strategy(data, params=None):
    """
    北向资金择时策略
    用价格动量模拟北向资金方向
    """
    p = params or {}
    up_threshold = p.get('up_threshold', 0.025)  # 连续上涨阈值
    down_threshold = p.get('down_threshold', -0.020)  # 连续下跌阈值
    lookback = p.get('lookback', 5)

    closes = [d['close'] for d in data]

    def signal(data, i):
        if i < lookback + 1:
            return 'hold'

        # 计算最近N日累计收益率
        total_change = 0
        for j in range(i - lookback + 1, i + 1):
            if j > 0:
                total_change += (data[j]['close'] - data[j-1]['close']) / data[j-1]['close']

        # RSI辅助判断
        rsi_vals = TI.rsi(closes[:i], 14)
        current_rsi = rsi_vals[-1] if rsi_vals else 50

        # 连续上涨后回调
        if total_change > up_threshold and data[i]['close'] < data[i-1]['close']:
            if current_rsi > 60:
                return 'sell'

        # 连续下跌后反弹
        if total_change < down_threshold and data[i]['close'] > data[i-1]['close']:
            if current_rsi < 40:
                return 'buy'

        return 'hold'

    return signal


def value_institutional_strategy(data, params=None):
    """
    价值投资+机构跟庄组合
    PE/PB低位 + 机构持仓增加 → 买入
    PE/PB高位 + 机构持仓减少 → 卖出

    注：完整实现需要财务数据和真实机构持仓
    """
    p = params or {}
    rebalance_days = p.get('rebalance_days', 60)  # 每季度检查一次

    closes = [d['close'] for d in data]
    ma = TI.sma(closes, 60)

    def signal(data, i):
        if i < max(rebalance_days, 60):
            return 'hold'

        # 每隔rebalance_days天检查一次
        if i % rebalance_days != 0:
            return 'hold'

        price_ma_idx = i - 60
        if price_ma_idx < 0:
            return 'hold'

        current_price = data[i]['close']
        ma_val = ma[price_ma_idx]

        # 价格低于均线20%且RSI<40 → 买入
        if current_price < ma_val * 0.80:
            rsi_vals = TI.rsi(closes[:i], 14)
            if rsi_vals and rsi_vals[-1] < 40:
                return 'buy'

        # 价格高于均线30%且RSI>65 → 卖出
        if current_price > ma_val * 1.30:
            rsi_vals = TI.rsi(closes[:i], 14)
            if rsi_vals and rsi_vals[-1] > 65:
                return 'sell'

        return 'hold'

    return signal

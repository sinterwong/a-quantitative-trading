"""
改进的轮动策略: 趋势确认后持有

之前的简单动量轮动：每月切换至动量最强标的 → 频繁切换，追高被套
改进后：趋势确认才入场，回调确认才出场

逻辑:
1. 趋势确认: MA20 > MA60 + RSI > 50 → 上升趋势
2. 入场: RSI超卖金叉(35) + 上升趋势中
3. 出场: RSI超买死叉(65) OR 趋势破坏(MA20 < MA60)
4. 空仓等待: 趋势破坏后不追，等下一个入场信号
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import DataLoader
from backtest import TechnicalIndicators as TI


def run_trend_confirmed_rotation(symbols, start, end, initial_capital=3000000):
    """
    运行趋势确认轮动策略

    vs 之前的简单动量轮动
    """
    loader = DataLoader()
    all_data = {}
    all_results = {}

    print("\nLoading data...")
    for sym in symbols:
        data = loader.get_kline(sym, start, end)
        if data:
            all_data[sym] = data
            print(f"  {sym}: {len(data)} records OK")

    if not all_data:
        print("[ERROR] No data loaded")
        return None

    # ========== 策略A: 趋势确认轮动 ==========
    print("\n" + "=" * 50)
    print("Strategy A: Trend-Confirmed Rotation")
    print("=" * 50)

    result_a = _run_trend_rotation(all_data, initial_capital)
    all_results['trend_confirmed'] = result_a

    print(f"\n  Total Return: {result_a['total_return']:+.1f}%")
    print(f"  Sharpe: {result_a['sharpe']:.2f}")
    print(f"  MaxDD: {result_a['max_dd']:.1f}%")
    print(f"  Trades: {result_a['total_trades']}")

    # ========== 策略B: 简单动量轮动(对照) ==========
    print("\n" + "=" * 50)
    print("Strategy B: Simple Momentum Rotation (control)")
    print("=" * 50)

    result_b = _run_momentum_rotation(all_data, initial_capital)
    all_results['simple_momentum'] = result_b

    print(f"\n  Total Return: {result_b['total_return']:+.1f}%")
    print(f"  Sharpe: {result_b['sharpe']:.2f}")
    print(f"  MaxDD: {result_b['max_dd']:.1f}%")
    print(f"  Trades: {result_b['total_trades']}")

    # ========== 对比 ==========
    print("\n" + "=" * 50)
    print("COMPARISON")
    print("=" * 50)
    print(f"\n  Trend-Confirmed: Return={result_a['total_return']:+.1f}%, Sharpe={result_a['sharpe']:.2f}, MaxDD={result_a['max_dd']:.1f}%")
    print(f"  Simple Momentum: Return={result_b['total_return']:+.1f}%, Sharpe={result_b['sharpe']:.2f}, MaxDD={result_b['max_dd']:.1f}%")

    if result_a['sharpe'] > result_b['sharpe']:
        print(f"\n  => Trend-Confirmed has BETTER Sharpe ratio (+{result_a['sharpe']-result_b['sharpe']:.2f})")
    if result_a['max_dd'] < result_b['max_dd']:
        print(f"  => Trend-Confirmed has LOWER max drawdown ({result_a['max_dd']:.1f}% vs {result_b['max_dd']:.1f}%)")

    return all_results


def _run_trend_rotation(all_data, initial_capital):
    """趋势确认轮动策略"""
    capital = initial_capital
    cash = capital
    position = 0
    holding_symbol = None
    entry_price = 0
    trades = []
    equity_curve = []

    # 对齐时间线
    all_dates = sorted(set(d['date'] for data in all_data.values() for d in data))

    per_symbol_capital = initial_capital / len(all_data)

    for date in all_dates:
        # 获取各标的当日数据
        symbol_data = {}
        for sym, data in all_data.items():
            for d in data:
                if d['date'] == date:
                    symbol_data[sym] = d
                    break

        if len(symbol_data) < len(all_data) * 0.5:
            continue  # 至少一半标的有数据

        # ========== 计算各标的趋势状态 ==========
        trend_scores = {}
        for sym, data in all_data.items():
            if len(data) < 60:
                continue

            # 找到日期对应的索引
            idx = None
            for i, d in enumerate(data):
                if d['date'] == date:
                    idx = i
                    break
            if idx is None or idx < 60:
                continue

            closes = [d['close'] for d in data[:idx+1]]
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60
            rsi_vals = TI.rsi(closes, 21)

            if len(rsi_vals) < 2:
                continue

            rsi = rsi_vals[-1]
            rsi_prev = rsi_vals[-2]
            price = closes[-1]

            # 趋势评分
            trend_score = 0
            if price > ma20:
                trend_score += 1
            if price > ma60:
                trend_score += 1
            if ma20 > ma60:
                trend_score += 1
            if rsi > 50:
                trend_score += 1

            # 入场信号: RSI金叉 + 上升趋势
            buy_signal = rsi_prev < 35 <= rsi and trend_score >= 3
            # 出场信号: RSI死叉 OR 趋势破坏
            sell_signal = (rsi_prev > 65 >= rsi) or trend_score <= 1

            trend_scores[sym] = {
                'trend_score': trend_score,
                'rsi': rsi,
                'buy_signal': buy_signal,
                'sell_signal': sell_signal,
                'price': price,
                'in_trend': trend_score >= 3
            }

        # ========== 持仓处理 ==========
        if holding_symbol and holding_symbol in trend_scores:
            ts = trend_scores[holding_symbol]

            # 检查是否应该出场
            if ts['sell_signal'] or ts['trend_score'] <= 1:
                # 出场
                revenue = position * ts['price'] * 0.999
                pnl_pct = (revenue - position * entry_price) / (position * entry_price)
                trades.append({
                    'action': 'sell',
                    'symbol': holding_symbol,
                    'date': date,
                    'price': ts['price'],
                    'pnl_pct': pnl_pct * 100
                })
                cash += revenue
                position = 0
                holding_symbol = None
                entry_price = 0

        # ========== 入场处理 ==========
        if not holding_symbol:
            # 选择趋势最强且有买入信号的标的
            candidates = [(sym, ts) for sym, ts in trend_scores.items()
                        if ts['buy_signal'] and ts['in_trend']]

            if candidates:
                # 选趋势最强的
                best = max(candidates, key=lambda x: x[1]['trend_score'])
                sym, ts = best

                # 买入
                price = ts['price']
                shares = int(per_symbol_capital / (price * 1.003))
                if shares > 0:
                    cost = shares * price * 1.003
                    position = shares
                    cash -= cost
                    holding_symbol = sym
                    entry_price = price
                    trades.append({
                        'action': 'buy',
                        'symbol': sym,
                        'date': date,
                        'price': price,
                        'trend_score': ts['trend_score']
                    })

        # ========== 更新权益 ==========
        if holding_symbol and holding_symbol in trend_scores:
            total_value = cash + position * trend_scores[holding_symbol]['price']
        else:
            total_value = cash

        equity_curve.append({'date': date, 'value': total_value})

    # 最终平仓
    if position > 0 and holding_symbol:
        last_data = all_data[holding_symbol]
        final_price = last_data[-1]['close']
        cash += position * final_price * 0.999
        position = 0

    final_value = cash

    # 计算指标
    total_return = (final_value - initial_capital) / initial_capital * 100

    returns = [(equity_curve[j]['value'] - equity_curve[j-1]['value']) / equity_curve[j-1]['value']
              for j in range(1, len(equity_curve))]
    if returns and len(returns) > 1:
        mean_ret = sum(returns) / len(returns)
        std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
    else:
        sharpe = 0

    # 最大回撤
    peak = initial_capital
    max_dd = 0
    for eq in equity_curve:
        if eq['value'] > peak:
            peak = eq['value']
        dd = (peak - eq['value']) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        'total_return': total_return,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'total_trades': len(trades),
        'equity_curve': equity_curve,
        'trades': trades
    }


def _run_momentum_rotation(all_data, initial_capital):
    """简单动量轮动策略(对照) - 每月切换至动量最强"""
    capital = initial_capital
    cash = capital
    position = 0
    holding_symbol = None
    entry_price = 0
    trades = []
    equity_curve = []

    all_dates = sorted(set(d['date'] for data in all_data.values() for d in data))
    per_symbol_capital = initial_capital / len(all_data)

    rebalance_days = 20  # 每月再平衡
    last_rebalance = 0

    for date in all_dates:
        idx = all_dates.index(date)

        # 获取各标的当日数据
        symbol_data = {}
        for sym, data in all_data.items():
            for i, d in enumerate(data):
                if d['date'] == date:
                    symbol_data[sym] = (i, d)
                    break

        if len(symbol_data) < len(all_data) * 0.5:
            continue

        # ========== 每月再平衡 ==========
        if idx - last_rebalance >= rebalance_days:
            last_rebalance = idx

            # 计算过去20日动量
            momentum_scores = {}
            for sym, (i, d) in symbol_data.items():
                if i >= 20:
                    data = all_data[sym]
                    start_price = data[i - 20]['close']
                    end_price = data[i]['close']
                    momentum = (end_price - start_price) / start_price
                    momentum_scores[sym] = momentum

            if momentum_scores and holding_symbol:
                # 卖出当前持仓
                if holding_symbol in symbol_data:
                    price = symbol_data[holding_symbol][1]['close']
                    revenue = position * price * 0.999
                    cash += revenue
                    position = 0
                    trades.append({
                        'action': 'sell',
                        'symbol': holding_symbol,
                        'date': date,
                        'price': price
                    })
                    holding_symbol = None

            # 买入动量最强
            if momentum_scores:
                best_sym = max(momentum_scores, key=momentum_scores.get)
                if best_sym in symbol_data:
                    price = symbol_data[best_sym][1]['close']
                    shares = int(per_symbol_capital / (price * 1.003))
                    if shares > 0:
                        cost = shares * price * 1.003
                        position = shares
                        cash -= cost
                        holding_symbol = best_sym
                        entry_price = price
                        trades.append({
                            'action': 'buy',
                            'symbol': best_sym,
                            'date': date,
                            'price': price,
                            'momentum': momentum_scores[best_sym]
                        })

        # 更新权益
        if holding_symbol and holding_symbol in symbol_data:
            total_value = cash + position * symbol_data[holding_symbol][1]['close']
        else:
            total_value = cash

        equity_curve.append({'date': date, 'value': total_value})

    # 平仓
    if position > 0:
        last_data = all_data[holding_symbol]
        cash += position * last_data[-1]['close'] * 0.999

    final_value = cash
    total_return = (final_value - initial_capital) / initial_capital * 100

    returns = [(equity_curve[j]['value'] - equity_curve[j-1]['value']) / equity_curve[j-1]['value']
              for j in range(1, len(equity_curve))]
    if returns and len(returns) > 1:
        mean_ret = sum(returns) / len(returns)
        std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
    else:
        sharpe = 0

    peak = initial_capital
    max_dd = 0
    for eq in equity_curve:
        if eq['value'] > peak:
            peak = eq['value']
        dd = (peak - eq['value']) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        'total_return': total_return,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'total_trades': len(trades),
        'equity_curve': equity_curve,
        'trades': trades
    }


if __name__ == '__main__':
    symbols = [
        '688981.SH',  # 中芯国际
        '600276.SH',  # 恒瑞医药
        '600519.SH',  # 贵州茅台
        '000858.SZ',  # 五粮液
        '601318.SH',  # 中国平安
        '300750.SZ',  # 宁德时代
        '600309.SH',  # 万华化学
        '601919.SH',  # 中远海控
    ]

    results = run_trend_confirmed_rotation(symbols, '20200101', '20251231')

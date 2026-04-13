"""
atr_sweep.py — ATR Multiplier 参数扫描
=========================================
对比不同 ATR multiplier 值对策略表现的影响。

扫描范围: 0.5 ~ 5.0，步长 0.5
固定参数:
  - RSI: buy=35, sell=65
  - 固定止盈: 25%
  - ATR period: 14
对比指标:
  - 年化收益率 (Annual Return %)
  - 夏普比率 (Sharpe)
  - 最大回撤 (Max Drawdown %)
  - 胜率 (Win Rate %)
  - 交易次数 (#Trades)
  - ATR止损触发次数

用法:
  python scripts/quant/atr_sweep.py [--symbol 600519.SH] [--multipliers 1.0,2.0,3.0]
"""

import sys
import os
import argparse
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.data_loader import DataLoader
from quant.backtest import BacktestEngine


def run_atr_sweep(symbol: str,
                  multipliers: list[float],
                  train_years: int = 2,
                  test_years: int = 1) -> dict:
    """
    对单个股票扫描不同 ATR multiplier。
    使用最近 3 年数据：前 train_years 年训练，后 test_years 年测试。
    """
    print(f"\n{'='*60}")
    print(f"  ATR Sweep — {symbol}")
    print(f"  Multipliers: {multipliers}")
    print(f"{'='*60}")

    # 计算日期范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * (train_years + test_years + 1))
    start_str = start_date.strftime('%Y%m%d')
    end_str   = end_date.strftime('%Y%m%d')

    # 加载数据
    loader = DataLoader()
    all_data = loader.get_kline(symbol, start_str, end_str, adjust='qfq')
    if not all_data or len(all_data) < 250 * (train_years + test_years):
        print(f"  [WARN] Insufficient data for {symbol}, skipping")
        return {}

    # Split train / test
    n_test = min(250 * test_years, len(all_data) // 3)
    test_data = all_data[-n_test:]
    train_data = all_data[:-n_test] if len(all_data) > n_test else all_data

    print(f"  Train: {train_data[0]['date']} → {train_data[-1]['date']} ({len(train_data)} bars)")
    print(f"  Test:  {test_data[0]['date']} → {test_data[-1]['date']} ({len(test_data)} bars)")

    # 构建 RSI 信号函数（直接返回 'buy'/'sell'/'hold'，兼容 BacktestEngine）
    rsi_buy, rsi_sell = 35, 65

    def rsi_signal_func(data: list, i: int) -> str:
        if i < 14:
            return 'hold'
        closes = [d['close'] for d in data[:i+1]]
        # compute RSI
        gains, losses = [], []
        for j in range(1, len(closes)):
            delta = closes[j] - closes[j-1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        if len(gains) < 14:
            return 'hold'
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        if rsi <= rsi_buy:
            return 'buy'
        elif rsi >= rsi_sell:
            return 'sell'
        return 'hold'

    results = []

    for mult in multipliers:
        bt = BacktestEngine(
            initial_capital=20000,
            commission=0.0003,
            use_atr_stop=True,
            atr_period=14,
            atr_multiplier=mult,
            take_profit=0.25,
            position_method='fixed',
            max_position_pct=0.20,
        )

        bt.run(test_data, rsi_signal_func, strategy_name=f'RSI_ATR_{mult}x')

        trades = bt.trades
        equity = bt.equity_curve

        # 计算指标
        if not equity:
            print(f"  ATR×{mult:.1f}: No equity curve, skipping")
            continue

        final_value = equity[-1]['value']
        total_return = (final_value - bt.initial_capital) / bt.initial_capital
        n_days = len(equity)
        annual_return = total_return * (250 / n_days)

        # Max drawdown
        peak = bt.initial_capital
        max_dd = 0.0
        for eq in equity:
            v = eq['value']
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

        # Sharpe (simplified: daily returns vs risk-free)
        daily_returns = []
        for i in range(1, len(equity)):
            r = (equity[i]['value'] - equity[i-1]['value']) / equity[i-1]['value']
            daily_returns.append(r)
        if daily_returns:
            mean_r = sum(daily_returns) / len(daily_returns)
            std_r = (sum((r - mean_r)**2 for r in daily_returns) / len(daily_returns)) ** 0.5
            sharpe = (mean_r * 250) / (std_r * (250 ** 0.5)) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        # Win rate
        wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
        win_rate = wins / len(trades) * 100 if trades else 0

        # ATR stop triggers
        atr_triggers = sum(
            1 for t in trades
            if t.get('reason') == 'atr_stop'
        )

        n_trades = len(trades)

        results.append({
            'multiplier': mult,
            'annual_return': annual_return * 100,
            'total_return': total_return * 100,
            'sharpe': sharpe,
            'max_dd': max_dd * 100,
            'win_rate': win_rate,
            'n_trades': n_trades,
            'atr_triggers': atr_triggers,
        })

        print(
            f"  ATR×{mult:.1f} | "
            f"Return={annual_return*100:+.1f}% | "
            f"Sharpe={sharpe:+.2f} | "
            f"MaxDD={max_dd*100:.1f}% | "
            f"Win={win_rate:.0f}% | "
            f"Trades={n_trades} | "
            f"ATRstop={atr_triggers}"
        )

    return results


def print_summary(results: list[dict]):
    """打印汇总表，找出最优 multiplier。"""
    if not results:
        return

    print(f"\n{'='*70}")
    print(f"  ATR Multiplier Sweep Summary")
    print(f"{'='*70}")
    header = f"  {'Mult':>6} | {'Ann.Return':>10} | {'Sharpe':>7} | {'MaxDD':>7} | {'WinRate':>7} | {'Trades':>6} | {'ATRstop':>7}"
    print(header)
    print("  " + "-" * 68)

    for r in results:
        print(
            f"  {r['multiplier']:>5.1f}x | "
            f"{r['annual_return']:>+9.1f}% | "
            f"{r['sharpe']:>+6.2f} | "
            f"{r['max_dd']:>6.1f}% | "
            f"{r['win_rate']:>6.0f}% | "
            f"{r['n_trades']:>5d} | "
            f"{r['atr_triggers']:>6d}"
        )

    # 找最优（Sharpe 优先，其次 MaxDD）
    best = max(results, key=lambda x: (x['sharpe'], -x['max_dd']))
    print(f"\n  ★ Best by Sharpe: ATR×{best['multiplier']:.1f}  (Sharpe={best['sharpe']:+.2f}, MaxDD={best['max_dd']:.1f}%)")

    # 按 multiplier 排序
    results_sorted = sorted(results, key=lambda x: x['multiplier'])
    return results_sorted


def main():
    parser = argparse.ArgumentParser(description='ATR Multiplier Sweep')
    parser.add_argument('--symbol', default='600519.SH',
                        help='Stock symbol (default: 600519.SH)')
    parser.add_argument('--multipliers', default='0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0',
                        help='Comma-separated multipliers (default: 0.5-5.0 step 0.5)')
    parser.add_argument('--train-years', type=int, default=2)
    parser.add_argument('--test-years', type=int, default=1)
    parser.add_argument('--symbols-file', default=None,
                        help='JSON file with list of symbols to sweep')
    args = parser.parse_args()

    multipliers = [float(m.strip()) for m in args.multipliers.split(',')]
    print(f"[ATR Sweep] Multipliers: {multipliers}")

    all_symbol_results = {}

    if args.symbols_file and os.path.exists(args.symbols_file):
        with open(args.symbols_file, 'r', encoding='utf-8') as f:
            symbols = json.load(f)
        print(f"[ATR Sweep] Sweeping {len(symbols)} symbols from {args.symbols_file}")
    else:
        symbols = [args.symbol]

    for sym in symbols:
        results = run_atr_sweep(
            symbol=sym,
            multipliers=multipliers,
            train_years=args.train_years,
            test_years=args.test_years,
        )
        if results:
            all_symbol_results[sym] = results

    # 全局汇总
    if len(all_symbol_results) > 1:
        print(f"\n{'#'*70}")
        print(f"  ALL SYMBOLS AGGREGATED")
        print(f"{'#'*70}")
        # Average across symbols for each multiplier
        by_mult = {}
        for sym, ress in all_symbol_results.items():
            for r in ress:
                m = r['multiplier']
                if m not in by_mult:
                    by_mult[m] = []
                by_mult[m].append(r)

        avg_results = []
        for m, ress in sorted(by_mult.items()):
            n = len(ress)
            avg_results.append({
                'multiplier': m,
                'annual_return': sum(r['annual_return'] for r in ress) / n,
                'sharpe': sum(r['sharpe'] for r in ress) / n,
                'max_dd': sum(r['max_dd'] for r in ress) / n,
                'win_rate': sum(r['win_rate'] for r in ress) / n,
                'n_trades': sum(r['n_trades'] for r in ress) / n,
                'atr_triggers': sum(r['atr_triggers'] for r in ress) / n,
            })

        print(f"\n  Averaged across {len(all_symbol_results)} symbols:")
        for r in avg_results:
            print(
                f"  ATR×{r['multiplier']:>4.1f} | "
                f"Return={r['annual_return']:>+8.1f}% | "
                f"Sharpe={r['sharpe']:>+6.2f} | "
                f"MaxDD={r['max_dd']:>6.1f}% | "
                f"Win={r['win_rate']:>5.0f}% | "
                f"Trades={r['n_trades']:>5.0f} | "
                f"ATRstop={r['atr_triggers']:>5.1f}"
            )
        best_avg = max(avg_results, key=lambda x: (x['sharpe'], -x['max_dd']))
        print(f"\n  ★ Best (avg): ATR×{best_avg['multiplier']:.1f}  (Sharpe={best_avg['sharpe']:+.2f}, MaxDD={best_avg['max_dd']:.1f}%)")

    elif len(all_symbol_results) == 1:
        sym, ress = list(all_symbol_results.items())[0]
        print_summary(ress)
        best = max(ress, key=lambda x: (x['sharpe'], -x['max_dd']))
        print(f"\n  → Recommended ATR multiplier for {sym}: {best['multiplier']:.1f}")
        print(f"     (Sharpe={best['sharpe']:+.2f}, MaxDD={best['max_dd']:.1f}%, WinRate={best['win_rate']:.0f}%)")

    print(f"\n[ATR Sweep] Done at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()

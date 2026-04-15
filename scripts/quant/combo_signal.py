#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSI + 放量组合信号 vs RSI单独 对比回测
======================================
测试：放量作为二次确认是否能减少假信号、提升夏普

信号逻辑：
  RSI单独:  RSI从超卖上穿 -> buy
  RSI+放量: RSI从超卖上穿 AND 今日成交量 > 5日均量*1.5 -> buy
            否则 hold

运行:
  python combo_signal.py [symbol] [--start YYYYMMDD] [--capital N]
"""

import os, sys, argparse
from datetime import datetime, timedelta

for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

QUANT_DIR = r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\quant'
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader
from backtest import BacktestEngine


# ─── 信号函数 ───────────────────────────────────────────

class RSISignal:
    """RSI 单独信号"""
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'rsi_vals')

    def __init__(self, rsi_buy=25, rsi_sell=65, rsi_period=14):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.rsi_vals = None

    def setup(self, data):
        n = len(data)
        period = self.rsi_period
        closes = [d['close'] for d in data]
        rsi = [None] * n
        for i in range(period, n):
            g, l = 0.0, 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: g += d
                else:     l -= d
            avg_gain = g / period
            avg_loss = l / period
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.rsi_vals = rsi

    def __call__(self, data, idx):
        if self.rsi_vals is None:
            self.setup(data)
        period = self.rsi_period
        rv = self.rsi_vals
        if idx < period or rv[idx] is None or rv[idx-1] is None:
            return 'hold'
        rsi = rv[idx]
        rsi_prev = rv[idx-1]
        if rsi_prev < self.rsi_buy <= rsi:
            return 'buy'
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        return 'hold'


class RSI_Volume_ComboSignal:
    """
    RSI + 放量组合信号

    买入条件：RSI从超卖上穿 AND 今日成交量 > 5日均量 * volume_mult
    卖出条件：RSI超买死叉
    """
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'volume_mult',
                 'rsi_vals', 'vol_ma5')

    def __init__(self, rsi_buy=25, rsi_sell=65, rsi_period=14, volume_mult=1.5):
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period
        self.volume_mult = volume_mult
        self.rsi_vals = None
        self.vol_ma5 = None

    def setup(self, data):
        n = len(data)
        period = self.rsi_period
        closes = [d['close'] for d in data]
        volumes = [d.get('volume', 0) for d in data]

        # RSI
        rsi = [None] * n
        for i in range(period, n):
            g, l = 0.0, 0.0
            for j in range(i - period + 1, i + 1):
                d = closes[j] - closes[j - 1]
                if d > 0: g += d
                else:     l -= d
            avg_gain = g / period
            avg_loss = l / period
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

        # 5日均量
        ma5 = [None] * n
        for i in range(4, n):
            ma5[i] = sum(volumes[i-4:i+1]) / 5.0

        self.rsi_vals = rsi
        self.vol_ma5 = ma5

    def __call__(self, data, idx):
        if self.rsi_vals is None:
            self.setup(data)
        period = self.rsi_period
        rv = self.rsi_vals
        ma5 = self.vol_ma5
        volumes = [d.get('volume', 0) for d in data]

        if idx < period + 4 or rv[idx] is None or rv[idx-1] is None:
            return 'hold'

        rsi = rv[idx]
        rsi_prev = rv[idx-1]

        # 卖出：RSI超买死叉
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'

        # 买入：RSI上穿超卖 + 放量确认
        if rsi_prev < self.rsi_buy <= rsi:
            vol = volumes[idx] if idx < len(volumes) else 0
            avg_vol = ma5[idx] if idx < len(ma5) and ma5[idx] is not None else 0
            if avg_vol > 0 and vol >= avg_vol * self.volume_mult:
                return 'buy'
            # 放量不满足，改为 watch（不强买）
            return 'hold'

        return 'hold'


# ─── 回测对比 ───────────────────────────────────────────

def run_comparison(symbol, start_date=None, end_date=None, capital=200000):
    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')

    print(f"\n{'='*60}")
    print(f"  RSI vs RSI+Volume 组合对比: {symbol}")
    print(f"  周期: {start_str} ~ {end_str}")
    print(f"  资金: {capital:,.0f}")
    print(f"{'='*60}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 60:
        print(f"  [FAIL] 数据不足: {len(kline) if kline else 0} 天")
        return

    print(f"  [OK] 数据: {len(kline)} 天 ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})\n")

    # 最优参数（来自 WFA）
    rsi_buy, rsi_sell = 25, 65
    stop_loss, take_profit = 0.05, 0.20

    volume_mults = [1.2, 1.5, 2.0]
    results = []

    # ── RSI 单独 ──
    sig_rsi = RSISignal(rsi_buy, rsi_sell, 14)
    sig_rsi.setup(kline)
    engine = BacktestEngine(initial_capital=capital, commission=0.0003,
                             stop_loss=stop_loss, take_profit=take_profit,
                             max_position_pct=0.20)
    r_rsi = engine.run(kline, sig_rsi, 'RSI_Only')
    r_rsi['_name'] = f'RSI_Only'
    r_rsi['_trades'] = len([t for t in engine.trades if t['action']=='buy'])
    results.append(r_rsi)
    print(f"  RSI_Only:       Sharpe={r_rsi['sharpe_ratio']:+.3f}  "
          f"Return={r_rsi['total_return_pct']:+.1f}%  "
          f"MaxDD={r_rsi['max_drawdown_pct']:.1f}%  "
          f"WinRate={r_rsi['win_rate_pct']:.0f}%  Trades={r_rsi['_trades']}")

    # ── RSI + 放量 ──
    best_combo = None
    best_sharpe = r_rsi['sharpe_ratio']

    for vm in volume_mults:
        sig = RSI_Volume_ComboSignal(rsi_buy, rsi_sell, 14, vm)
        sig.setup(kline)
        engine = BacktestEngine(initial_capital=capital, commission=0.0003,
                                 stop_loss=stop_loss, take_profit=take_profit,
                                 max_position_pct=0.20)
        r = engine.run(kline, sig, f'RSI+Vol({vm}x)')
        n_trades = len([t for t in engine.trades if t['action']=='buy'])
        r['_name'] = f'RSI+Vol({vm}x)'
        r['_trades'] = n_trades
        results.append(r)
        impr = (r['sharpe_ratio'] - r_rsi['sharpe_ratio']) / abs(r_rsi['sharpe_ratio'] or 1) * 100
        flag = ' *BEST*' if r['sharpe_ratio'] > best_sharpe else ''
        print(f"  RSI+Vol({vm}x):   Sharpe={r['sharpe_ratio']:+.3f}  "
              f"Return={r['total_return_pct']:+.1f}%  "
              f"MaxDD={r['max_drawdown_pct']:.1f}%  "
              f"WinRate={r['win_rate_pct']:.0f}%  Trades={n_trades}  (sharpe改善: {impr:+.0f}%){flag}")
        if r['sharpe_ratio'] > best_sharpe:
            best_sharpe = r['sharpe_ratio']
            best_combo = vm

    # ── 汇总表 ──
    print(f"\n{'='*60}")
    print(f"  汇总对比")
    print(f"{'='*60}")
    print(f"  {'Signal':<15} {'Sharpe':>8} {'Return':>9} {'Annual':>9} {'MaxDD':>8} {'WinRate':>7} {'Trades':>6}")
    print(f"  {'-'*60}")
    for r in results:
        print(f"  {r['_name']:<15} {r['sharpe_ratio']:>+8.3f} "
              f"{r['total_return_pct']:>+8.1f}% {r['annualized_return_pct']:>+8.1f}% "
              f"{r['max_drawdown_pct']:>7.1f}% {r['win_rate_pct']:>6.0f}% {r['_trades']:>5d}")

    # ── 结论 ──
    print(f"\n  结论:")
    rsi_only = results[0]
    rsi_vol = results[1]

    if best_combo:
        best = [r for r in results if r['_name'] == f'RSI+Vol({best_combo}x)'][0]
        if best['sharpe_ratio'] > rsi_only['sharpe_ratio']:
            print(f"    放量确认有效！RSI+Vol({best_combo}x) 优于 RSI单独")
            print(f"    夏普提升: {rsi_only['sharpe_ratio']:+.3f} -> {best['sharpe_ratio']:+.3f}")
            print(f"    交易次数减少: {rsi_only['_trades']} -> {best['_trades']} (减少假信号)")
        else:
            print(f"    放量确认无效。RSI 单独信号更优。")
    else:
        print(f"    所有 RSI+Vol 组合均不如 RSI 单独。")

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', nargs='?', default='510310.SH')
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--capital', type=float, default=200000)
    args = parser.parse_args()

    run_comparison(args.symbol, args.start, args.end, args.capital)

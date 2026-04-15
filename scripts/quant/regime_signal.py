#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场状态过滤器对比测试
======================
对比:
  1. MA200 牛熊过滤 (binary)
  2. MA50 快线方向 (趋势确认)
  3. ATR 波动率过滤 (高波动期不开仓)
  4. 无过滤 (基准)

运行:
  python regime_signal.py [symbol] --start YYYYMMDD
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


# ─── 基础 RSI ──────────────────────────────────────────

class RSISignal:
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
        rsi, rsi_prev = rv[idx], rv[idx-1]
        if rsi_prev < self.rsi_buy <= rsi:
            return 'buy'
        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        return 'hold'


# ─── MA200 Regime 过滤 ──────────────────────────────────

class MA200RegimeSignal(RSISignal):
    """价格 > MA200 时才允许买入"""
    def setup(self, data):
        super().setup(data)
        closes = [d['close'] for d in data]
        n = len(closes)
        ma = [None] * n
        for i in range(199, n):
            ma[i] = sum(closes[i-199:i+1]) / 200.0
        self._ma = ma

    def __call__(self, data, idx):
        if self.rsi_vals is None:
            self.setup(data)
        closes = [d['close'] for d in data]
        price = closes[idx]
        ma_val = self._ma[idx]

        rv = self.rsi_vals
        if idx < 200 or rv[idx] is None or rv[idx-1] is None:
            return 'hold'

        rsi, rsi_prev = rv[idx], rv[idx-1]

        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        if rsi_prev < self.rsi_buy <= rsi:
            if ma_val and price > ma_val:
                return 'buy'
            return 'hold'
        return 'hold'


# ─── MA50 快线趋势过滤 ─────────────────────────────────

class MA50TrendSignal(RSISignal):
    """MA50 > MA200 时（多头趋势）允许买入"""
    def setup(self, data):
        super().setup(data)
        closes = [d['close'] for d in data]
        n = len(closes)
        ma50 = [None] * n
        ma200 = [None] * n
        for i in range(49, n):
            ma50[i] = sum(closes[i-49:i+1]) / 50.0
        for i in range(199, n):
            ma200[i] = sum(closes[i-199:i+1]) / 200.0
        self._ma50 = ma50
        self._ma200 = ma200

    def __call__(self, data, idx):
        if self.rsi_vals is None:
            self.setup(data)
        closes = [d['close'] for d in data]
        ma50 = self._ma50[idx]
        ma200 = self._ma200[idx]
        rv = self.rsi_vals

        if idx < 200 or rv[idx] is None or rv[idx-1] is None:
            return 'hold'

        rsi, rsi_prev = rv[idx], rv[idx-1]
        trend_bull = (ma50 is not None and ma200 is not None and ma50 > ma200)

        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        if rsi_prev < self.rsi_buy <= rsi:
            if trend_bull:
                return 'buy'
            return 'hold'
        return 'hold'


# ─── ATR 波动率过滤 ─────────────────────────────────────

class ATRVolatilitySignal(RSISignal):
    """
    ATR 历史分位数 > P90 时不开新仓（极端波动期）
    ATR 位置 = 当前 ATR / 过去 N 日 ATR 最高值
    """
    def setup(self, data):
        super().setup(data)
        closes = [d['close'] for d in data]
        highs = [d.get('high', c) for d, c in zip(data, closes)]
        lows  = [d.get('low',  c) for d, c in zip(data, closes)]
        n = len(closes)

        # 计算 ATR(14)
        atr = [None] * n
        for i in range(1, n):
            tr = max(highs[i]-lows[i],
                     abs(highs[i]-closes[i-1]),
                     abs(lows[i]-closes[i-1]))
            if i >= 14:
                prev = atr[i-1] if atr[i-1] is not None else tr
                atr[i] = (prev * 13 + tr) / 14
            else:
                atr[i] = None

        # ATR 的 20 日滚动最高值（代表近期波动极值）
        atr_high = [None] * n
        for i in range(33, n):  # need at least 14 (atr) + 20 (rolling max)
            window = [v for v in atr[i-19:i+1] if v is not None]
            atr_high[i] = max(window) if window else None

        self._atr_ratio = [None] * n
        for i in range(33, n):
            if atr[i] is not None and atr_high[i] is not None and atr_high[i] > 0:
                self._atr_ratio[i] = atr[i] / atr_high[i]

    def __call__(self, data, idx):
        if self.rsi_vals is None:
            self.setup(data)
        closes = [d['close'] for d in data]
        atr_r = self._atr_ratio

        if idx < 50 or self.rsi_vals[idx] is None or self.rsi_vals[idx-1] is None:
            return 'hold'

        rsi = self.rsi_vals[idx]
        rsi_prev = self.rsi_vals[idx-1]

        # 高波动（ATR ratio > 0.8 = 最近20日波动的80%以上）不开仓
        vol_high = (atr_r[idx] is not None) and (atr_r[idx] > 0.80)

        if rsi_prev < self.rsi_sell <= rsi:
            return 'sell'
        if rsi_prev < self.rsi_buy <= rsi:
            if not vol_high:
                return 'buy'
            return 'hold'
        return 'hold'


# ─── 主测试 ────────────────────────────────────────────

def run_tests(symbol, start_date=None, end_date=None, capital=200000):
    end_str = end_date or datetime.now().strftime('%Y%m%d')
    start_str = start_date or (datetime.now() - timedelta(days=900)).strftime('%Y%m%d')

    print(f"\n{'='*60}")
    print(f"  市场状态过滤器对比: {symbol}")
    print(f"  周期: {start_str} ~ {end_str}")
    print(f"{'='*60}")

    loader = DataLoader()
    kline = loader.get_kline(symbol, start_str, end_str)
    if not kline or len(kline) < 300:
        print(f"  [FAIL] 数据不足: {len(kline) if kline else 0} 天")
        return

    print(f"  [OK] 数据: {len(kline)} 天 ({kline[0]['date'][:10]} ~ {kline[-1]['date'][:10]})\n")

    rsi_buy, rsi_sell = 25, 65
    stop_loss, take_profit = 0.05, 0.20

    configs = [
        ('RSI_Only',          RSISignal,             {}),
        ('MA200_Regime',       MA200RegimeSignal,     {}),
        ('MA50_Trend',        MA50TrendSignal,       {}),
        ('ATR_VolFilter',     ATRVolatilitySignal,   {}),
    ]

    results = []

    for name, cls, extra_params in configs:
        sig = cls(rsi_buy, rsi_sell, 14, **extra_params)
        sig.setup(kline)
        engine = BacktestEngine(initial_capital=capital, commission=0.0003,
                                 stop_loss=stop_loss, take_profit=take_profit,
                                 max_position_pct=0.20)
        r = engine.run(kline, sig, name)
        n_trades = len([t for t in engine.trades if t['action'] == 'buy'])
        results.append({
            'name': name,
            'sharpe': r['sharpe_ratio'],
            'ret': r['total_return_pct'],
            'ann': r['annualized_return_pct'],
            'maxdd': r['max_drawdown_pct'],
            'wr': r['win_rate_pct'],
            'trades': n_trades,
            'engine': engine,
        })
        print(f"  {name:20s}: Sharpe={r['sharpe_ratio']:+.3f}  "
              f"Return={r['total_return_pct']:+.1f}%  "
              f"MaxDD={r['max_drawdown_pct']:.1f}%  "
              f"WinRate={r['win_rate_pct']:.0f}%  Trades={n_trades}")

    # 排序
    results.sort(key=lambda x: x['sharpe'], reverse=True)

    print(f"\n{'='*60}")
    print(f"  排名 (按夏普比率)")
    print(f"{'='*60}")
    print(f"  {'#':<4} {'Signal':<20} {'Sharpe':>8} {'Return':>9} {'MaxDD':>8} {'WinRate':>7} {'Trades':>6}")
    print(f"  {'-'*60}")
    for i, r in enumerate(results, 1):
        flag = ' <-BEST' if i == 1 else ''
        print(f"  {i:<4} {r['name']:<20} {r['sharpe']:>+8.3f} "
              f"{r['ret']:>+8.1f}% {r['maxdd']:>7.1f}% {r['wr']:>6.0f}% {r['trades']:>5d}{flag}")

    r_best = results[0]
    r_base = [r for r in results if r['name'] == 'RSI_Only'][0]
    print(f"\n  最佳: {r_best['name']}  Sharpe={r_best['sharpe']:+.3f}")
    if r_best['name'] != 'RSI_Only':
        delta = r_best['sharpe'] - r_base['sharpe']
        impr = delta / abs(r_base['sharpe']) * 100 if r_base['sharpe'] != 0 else 0
        print(f"  vs RSI_Only: Sharpe {r_base['sharpe']:+.3f} -> {r_best['sharpe']:+.3f} "
              f"({delta:+.3f}, {impr:+.0f}%)")

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', nargs='?', default='510310.SH')
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--capital', type=float, default=200000)
    args = parser.parse_args()
    run_tests(args.symbol, args.start, args.end, args.capital)

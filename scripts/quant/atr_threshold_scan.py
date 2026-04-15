#!/usr/bin/env python3
# ATR阈值扫描 - 找最优阈值
import os, sys, argparse
from datetime import datetime, timedelta
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]
QUANT_DIR = r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\quant'
sys.path.insert(0, QUANT_DIR)
from data_loader import DataLoader
from backtest import BacktestEngine

class RSISignal:
    __slots__ = ('rsi_buy', 'rsi_sell', 'rsi_period', 'rsi_vals')
    def __init__(self, rsi_buy=25, rsi_sell=65, rsi_period=14):
        self.rsi_buy = rsi_buy; self.rsi_sell = rsi_sell
        self.rsi_period = rsi_period; self.rsi_vals = None
    def setup(self, data):
        n = len(data); closes = [d['close'] for d in data]
        rsi = [None] * n
        for i in range(self.rsi_period, n):
            g, l = 0.0, 0.0
            for j in range(i-self.rsi_period+1, i+1):
                d = closes[j]-closes[j-1]
                if d > 0: g += d
                else: l -= d
            ag = g/self.rsi_period; al = l/self.rsi_period
            rsi[i] = 100.0 if al==0 else 100.0-(100.0/(1.0+ag/al))
        self.rsi_vals = rsi
    def __call__(self, data, idx):
        if self.rsi_vals is None: self.setup(data)
        rv = self.rsi_vals
        if idx < self.rsi_period or rv[idx] is None or rv[idx-1] is None: return 'hold'
        rsi, rp = rv[idx], rv[idx-1]
        if rp < self.rsi_buy <= rsi: return 'buy'
        if rp < self.rsi_sell <= rsi: return 'sell'
        return 'hold'

class RSI_ATR:
    __slots__ = ('rsi_buy','rsi_sell','rsi_period','atr_t','rsi_vals','atr_r')
    def __init__(self, rsi_buy, rsi_sell, rsi_period, atr_threshold):
        self.rsi_buy=rsi_buy; self.rsi_sell=rsi_sell
        self.rsi_period=rsi_period; self.atr_t=atr_threshold
        self.rsi_vals=None; self.atr_r=None
    def setup(self, data):
        n=len(data); closes=[d['close'] for d in data]
        highs=[d.get('high',c) for d,c in zip(data,closes)]
        lows=[d.get('low',c) for d,c in zip(data,closes)]
        rsi=[None]*n
        for i in range(self.rsi_period, n):
            g,l=0.0,0.0
            for j in range(i-self.rsi_period+1,i+1):
                d=closes[j]-closes[j-1]
                if d>0: g+=d
                else: l-=d
            ag=g/self.rsi_period; al=l/self.rsi_period
            rsi[i]=100.0 if al==0 else 100.0-(100.0/(1.0+ag/al))
        self.rsi_vals=rsi
        atr=[None]*n
        for i in range(1,n):
            tr=max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1]))
            if i>=14 and atr[i-1] is not None: atr[i]=(atr[i-1]*13+tr)/14
            elif i==14:
                atr[i]=sum(max(highs[j]-lows[j],abs(highs[j]-closes[j-1]),abs(lows[j]-closes[j-1])) for j in range(1,15))/14
        self.atr_r=[None]*n
        for i in range(33,n):
            w=[atr[j] for j in range(i-19,i+1) if atr[j] is not None]
            if w: self.atr_r[i]=atr[i]/max(w) if max(w)>0 else None
    def __call__(self, data, idx):
        if self.rsi_vals is None: self.setup(data)
        rv=self.rsi_vals; atr_r=self.atr_r
        if idx<50 or rv[idx] is None or rv[idx-1] is None: return 'hold'
        rsi,rp=rv[idx],rv[idx-1]
        vol_high=(atr_r[idx] is not None) and (atr_r[idx]>self.atr_t)
        if rp<self.rsi_sell<=rsi: return 'sell'
        if rp<self.rsi_buy<=rsi and not vol_high: return 'buy'
        return 'hold'

def scan_thresholds(symbol, start, end, capital):
    loader = DataLoader()
    kline = loader.get_kline(symbol, start, end)
    if not kline: return
    closes=[d['close'] for d in kline]
    print(f'\n[DATA] {len(kline)} days\n')
    thresholds = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    rsi_buy, rsi_sell = 25, 65
    stop_loss, take_profit = 0.05, 0.20
    results = []
    for t in thresholds:
        sig = RSI_ATR(rsi_buy, rsi_sell, 14, t)
        sig.setup(kline)
        engine = BacktestEngine(initial_capital=capital, commission=0.0003,
            stop_loss=stop_loss, take_profit=take_profit, max_position_pct=0.20)
        r = engine.run(kline, sig, f'ATR_{t}')
        n = len([x for x in engine.trades if x['action']=='buy'])
        results.append({'th': t, 'sharpe': r['sharpe_ratio'],
            'ret': r['total_return_pct'], 'ann': r['annualized_return_pct'],
            'maxdd': r['max_drawdown_pct'], 'wr': r['win_rate_pct'], 'trades': n})
        print(f'  ATR>{t:.0%}: Sharpe={r["sharpe_ratio"]:+.3f}  Ret={r["total_return_pct"]:+.1f}%  '
              f'MaxDD={r["max_drawdown_pct"]:.1f}%  WR={r["win_rate_pct"]:.0f}%  Trades={n}')
    results.sort(key=lambda x: x['sharpe'], reverse=True)
    print(f'\n  BEST: {results[0]["th"]:.0%}  Sharpe={results[0]["sharpe"]:+.3f}')
    return results

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('symbol', nargs='?', default='510310.SH')
    p.add_argument('--start', default=None)
    p.add_argument('--capital', type=float, default=200000)
    a = p.parse_args()
    end = datetime.now().strftime('%Y%m%d')
    start = a.start or (datetime.now()-timedelta(days=900)).strftime('%Y%m%d')
    scan_thresholds(a.symbol, start, end, a.capital)

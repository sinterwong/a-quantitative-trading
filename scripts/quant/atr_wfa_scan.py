#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S1-T1: ATR 阈值 WFA 精确化
============================
对多个 ATR ratio 阈值跑完整 Walk-Forward，选最优阈值。

阈值范围: 0.80 / 0.85 / 0.88 / 0.90 / 0.92
验收: Sharpe > 0.5 且正收益窗口 >= 60%

运行:
  python atr_wfa_scan.py [symbol] [--train-years N] [--test-years N]
"""

import os, sys, json, argparse
from datetime import datetime, timedelta

for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

QUANT_DIR = r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\quant'
sys.path.insert(0, QUANT_DIR)
BK_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from data_loader import DataLoader
from backtest import BacktestEngine

LIVE_PARAMS_PATH = os.path.join(BK_DIR, 'backend', 'services', 'live_params.json')


def save_best_threshold(symbol: str, best_threshold: float, best_sharpe: float,
                        rsi_buy: int = 25, rsi_sell: int = 65):
    """将最优 ATR 阈值回写到 live_params.json。"""
    live_params = {}
    if os.path.exists(LIVE_PARAMS_PATH):
        try:
            with open(LIVE_PARAMS_PATH, encoding='utf-8') as f:
                live_params = json.load(f)
        except Exception:
            pass

    key = f'{symbol}_RSI'
    entry = live_params.get(key, {'symbol': symbol, 'strategy': 'RSI'})
    entry['params'] = {
        'rsi_buy': rsi_buy,
        'rsi_sell': rsi_sell,
        'stop_loss': 0.05,
        'take_profit': 0.20,
        'atr_threshold': best_threshold,
    }
    entry['test_sharpe'] = round(best_sharpe, 4)
    entry['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    live_params[key] = entry

    os.makedirs(os.path.dirname(LIVE_PARAMS_PATH), exist_ok=True)
    with open(LIVE_PARAMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(live_params, f, indent=2, ensure_ascii=False)
    print(f'  [WRITE] {LIVE_PARAMS_PATH} updated: atr_threshold={best_threshold}')

# ─── RSI + ATR Filter 信号 ────────────────────────────────

class RSI_ATR_Filter:
    __slots__ = ('rsi_buy','rsi_sell','rsi_period','atr_t','rsi_vals','atr_r')
    def __init__(self, rsi_buy, rsi_sell, rsi_period, atr_threshold):
        self.rsi_buy=rsi_buy; self.rsi_sell=rsi_sell
        self.rsi_period=rsi_period; self.atr_t=atr_threshold
        self.rsi_vals=None; self.atr_r=None

    def setup(self, data):
        n=len(data)
        closes=[d['close'] for d in data]
        highs=[d.get('high',c) for d,c in zip(data,closes)]
        lows=[d.get('low',c) for d,c in zip(data,closes)]

        # RSI
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

        # ATR ratio
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


class RSISignalOnly:
    __slots__ = ('rsi_buy','rsi_sell','rsi_period','rsi_vals')
    def __init__(self, rsi_buy, rsi_sell, rsi_period):
        self.rsi_buy=rsi_buy; self.rsi_sell=rsi_sell; self.rsi_period=rsi_period
        self.rsi_vals=None
    def setup(self, data):
        n=len(data); closes=[d['close'] for d in data]
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
    def __call__(self, data, idx):
        if self.rsi_vals is None: self.setup(data)
        rv=self.rsi_vals
        if idx<self.rsi_period or rv[idx] is None or rv[idx-1] is None: return 'hold'
        rsi,rp=rv[idx],rv[idx-1]
        if rp<self.rsi_sell<=rsi: return 'sell'
        if rp<self.rsi_buy<=rsi: return 'buy'
        return 'hold'


# ─── WFA 单次 ──────────────────────────────────────────

def run_wfa_for_threshold(kline, threshold, rsi_buy, rsi_sell, train_years, test_years, capital):
    total_days=len(kline)
    train_days=train_years*252
    test_days=test_years*252
    n_windows=int((total_days/252 - train_years)/test_years)
    if n_windows<1: n_windows=1

    param_grid={
        'rsi_buy': [25,30,35],
        'rsi_sell': [60,65,70],
        'stop_loss': [0.05],
        'take_profit': [0.20],
    }

    import itertools
    combinations=list(itertools.product(
        param_grid['rsi_buy'], param_grid['rsi_sell'],
        param_grid['stop_loss'], param_grid['take_profit']
    ))

    window_results=[]
    for w in range(n_windows):
        ts_i=w*test_days
        te_i=ts_i+train_days
        tt_i=te_i
        tf_i=min(tt_i+test_days,total_days)
        if te_i>=total_days: break
        if tt_i>=total_days: break

        train=kline[ts_i:te_i]
        test=kline[tt_i:tf_i]

        if len(train)<train_days*0.8 or len(test)<test_days*0.8: continue

        # Train: grid search
        best_sharpe=-999; best_params=None
        for rb,rs,sl,tp in combinations:
            sig=RSI_ATR_Filter(rb,rs,14,threshold) if threshold<1.0 else RSISignalOnly(rb,rs,14)
            sig.setup(train)
            eng=BacktestEngine(initial_capital=capital,commission=0.0003,
                              stop_loss=sl,take_profit=tp,max_position_pct=0.20)
            r=eng.run(train,sig,f'Train')
            if r['sharpe_ratio']>best_sharpe and r['total_trades']>=4:
                best_sharpe=r['sharpe_ratio']; best_params=(rb,rs,sl,tp)

        if best_params is None: continue
        rb,rs,sl,tp=best_params

        # Test
        sig2=RSI_ATR_Filter(rb,rs,14,threshold) if threshold<1.0 else RSISignalOnly(rb,rs,14)
        sig2.setup(test)
        eng2=BacktestEngine(initial_capital=capital,commission=0.0003,
                            stop_loss=sl,take_profit=tp,max_position_pct=0.20)
        r2=eng2.run(test,sig2,f'Test')
        window_results.append({
            'sharpe': r2['sharpe_ratio'],
            'ret': r2['total_return_pct'],
            'maxdd': r2['max_drawdown_pct'],
            'winrate': r2['win_rate_pct'],
            'trades': r2['total_trades'],
            'params': {'rsi_buy':rb,'rsi_sell':rs,'atr_threshold':threshold}
        })

    if not window_results: return None
    n=len(window_results)
    sharpes=[w['sharpe'] for w in window_results]
    rets=[w['ret'] for w in window_results]
    maxdds=[w['maxdd'] for w in window_results]
    winrates=[w['winrate'] for w in window_results]
    pos_w=sum(1 for s in sharpes if s>0)
    return {
        'threshold': threshold,
        'n_windows': n,
        'avg_sharpe': sum(sharpes)/n,
        'avg_return': sum(rets)/n,
        'avg_maxdd': sum(maxdds)/n,
        'avg_winrate': sum(winrates)/n,
        'positive_windows': pos_w,
        'pos_rate': pos_w/n*100,
        'min_sharpe': min(sharpes),
        'max_sharpe': max(sharpes),
        'windows': window_results,
    }


def scan_thresholds(symbol, thresholds, train_years, test_years, capital):
    end=datetime.now().strftime('%Y%m%d')
    start=(datetime.now()-timedelta(days=train_years*365+test_years*365+90)).strftime('%Y%m%d')

    loader=DataLoader()
    kline=loader.get_kline(symbol,start,end)
    if not kline or len(kline)<300:
        print(f'[FAIL] Data insufficient: {len(kline) if kline else 0} days'); return

    print(f'[DATA] {len(kline)} days | {symbol} | train={train_years}y test={test_years}y\n')

    results=[]
    for th in thresholds:
        label=f'RSI+ATR({th:.0%})' if th<1.0 else 'RSI_Only'
        print(f'  Running {label}...', end='',flush=True)
        r=run_wfa_for_threshold(kline,th,25,65,train_years,test_years,capital)
        if r:
            results.append(r)
            print(f' done | Sharpe={r["avg_sharpe"]:+.3f} | PosWin={r["positive_windows"]}/{r["n_windows"]} | MaxDD={r["avg_maxdd"]:.1f}%')
        else:
            print(f' no valid windows')

    if not results: print('[FAIL] No results'); return

    # Sort by Sharpe desc
    results.sort(key=lambda x:x['avg_sharpe'],reverse=True)

    print(f'\n{"="*70}')
    print(f'  ATR Threshold WFA Scan Results ({symbol})')
    print(f'{"="*70}')
    print(f'  {"Th":>8} {"Sharpe":>8} {"Return":>9} {"MaxDD":>8} {"WinRate":>8} {"PosWin":>8} {"Pass":>6}')
    print(f'  {"-"*55}')
    for r in results:
        th=r['threshold']
        label=f'ATR({th:.0%})' if th<1.0 else 'RSI_Only'
        sharpe_ok=r['avg_sharpe']>0.5
        pos_ok=r['pos_rate']>=60
        flag='[PASS]' if (sharpe_ok and pos_ok) else '[----]'
        print(f'  {label:>8} {r["avg_sharpe"]:>+8.3f} {r["avg_return"]:>+8.1f}% {r["avg_maxdd"]:>7.1f}% {r["avg_winrate"]:>7.0f}% {r["positive_windows"]:>5d}/{r["n_windows"]:<3} {flag:>6}')

    best=results[0]
    print(f'\n  BEST: ATR({best["threshold"]:.0%})  Sharpe={best["avg_sharpe"]:+.3f}  MaxDD={best["avg_maxdd"]:.1f}%')
    return results


def save_best_threshold_to_live_params(symbol: str, results: list, rsi_buy: int = 25, rsi_sell: int = 65):
    """将最优阈值结果写入 live_params.json（供盘中信号引擎使用）。"""
    if not results:
        return
    results.sort(key=lambda x: x['avg_sharpe'], reverse=True)
    best = results[0]
    save_best_threshold(symbol, best['threshold'], best['avg_sharpe'], rsi_buy, rsi_sell)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('symbol',nargs='?',default='510310.SH')
    p.add_argument('--thresholds',default='0.80,0.85,0.88,0.90,0.92')
    p.add_argument('--train-years',type=int,default=2)
    p.add_argument('--test-years',type=int,default=1)
    p.add_argument('--capital',type=float,default=200000)
    p.add_argument('--write',action='store_true',help='将最优阈值写入 live_params.json')
    a=p.parse_args()
    ths=[float(x) for x in a.thresholds.split(',')]
    results = scan_thresholds(a.symbol,ths,a.train_years,a.test_years,a.capital)
    if results and a.write:
        save_best_threshold_to_live_params(a.symbol, results)

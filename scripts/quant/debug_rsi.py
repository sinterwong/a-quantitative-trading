import sys, os
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]
sys.path.insert(0, r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\quant')

from data_loader import DataLoader
from backtest import TechnicalIndicators as TI

loader = DataLoader()
kline = loader.get_kline('600900.SH', '20240101', '20260415')
closes = [d['close'] for d in kline]
print('Kline days:', len(kline))

rsi = TI.rsi(closes, 14)
print('RSI len:', len(rsi))
print('RSI[13:20] =', [round(x,2) for x in rsi[13:20]])

rb, rs = 30, 65
period = 14

# Precompute same way as signal_func
rsi_vals = [None] * len(closes)
for i in range(period, len(closes)):
    seg = closes[i-period:i+1]
    g, l = [], []
    for j in range(1, len(seg)):
        d = seg[j] - seg[j-1]
        g.append(d if d > 0 else 0.0)
        l.append(-d if d < 0 else 0.0)
    ag = sum(g)/period
    al = sum(l)/period
    rsi_vals[i] = 100 if al == 0 else 100 - (100/(1 + ag/al))

buys = 0
for i in range(period+1, len(kline)):
    if rsi_vals[i] is None or rsi_vals[i-1] is None:
        continue
    if rsi_vals[i-1] < rb <= rsi_vals[i]:
        buys += 1
        if buys <= 5:
            print('  BUY at i=%d date=%s rsi=%.2f prev=%.2f' % (i, kline[i]['date'], rsi_vals[i], rsi_vals[i-1]))

print('Total BUY signals:', buys)

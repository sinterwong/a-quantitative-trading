import sys, os
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]
sys.path.insert(0, r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\quant')

from data_loader import DataLoader
from backtest import BacktestEngine

loader = DataLoader()
kline = loader.get_kline('600900.SH', '20220101', '20260415')
closes = [d['close'] for d in kline]
print('Days:', len(kline))

# Patch signal_func with debug
rb, rs = 30, 65
period = 14

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

debug_count = 0
def debug_signal_func(data, i):
    global debug_count
    if i < period:
        return 'hold'
    rv = rsi_vals[i]
    rv_prev = rsi_vals[i-1]
    if rv is None or rv_prev is None:
        return 'hold'
    if rv_prev < rb <= rv:
        debug_count += 1
        if debug_count <= 5:
            print('BUY at i=%d date=%s rsi=%.2f prev=%.2f' % (i, kline[i]['date'], rv, rv_prev))
        return 'buy'
    if rv_prev < rs <= rv:
        return 'sell'
    return 'hold'

# Run engine
engine = BacktestEngine(initial_capital=200000, commission=0.0003,
                         stop_loss=0.08, take_profit=0.25, max_position_pct=0.20)
result = engine.run(kline, debug_signal_func, 'DEBUG')

print('Total buy signals seen:', debug_count)
print('Trades:', len([t for t in engine.trades if t['action']=='buy']))
print('Result:', result.get('total_trades'), 'sharpe', result.get('sharpe_ratio'))

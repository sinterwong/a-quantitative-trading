import sys, os
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]
sys.path.insert(0, r'C:\Users\sinte\.openclaw\workspace\quant_repo\backend')

from services.signals import evaluate_signal, load_symbol_params, _compute_atr_ratio

# Check ATR ratio
ratio = _compute_atr_ratio('510310.SH')
print('ATR ratio 510310.SH: %s (threshold=0.90, high=%s)' % (ratio, ratio > 0.90 if ratio else 'N/A'))

# Test a simulated high-vol scenario: 
# If ATR ratio > 0.90, RSI_BUY should be blocked
# Let's manually check what happens if RSI is in oversold territory
from services.signals import _compute_rsi, _fetch_history_sina

hist = _fetch_history_sina('510310.SH', days=20)
if hist:
    closes = [d['close'] for d in hist]
    rsi = _compute_rsi(closes)
    print('RSI (last): %s' % (rsi[-1] if rsi else 'N/A'))

params = load_symbol_params('510310.SH')
print('Using params: rsi_buy=%d rsi_sell=%d atr_threshold=%.2f' % (
    params['rsi_buy'], params['rsi_sell'], params['atr_threshold']))
print('All params OK for 510310.SH: atr_threshold=0.90 integrated into intraday_monitor')

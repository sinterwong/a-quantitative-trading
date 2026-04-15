path = r'C:\Users\sinte\.openclaw\workspace\quant_repo\backend\services\intraday_monitor.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix first evaluate_signal call (in run method - watchlist check)
old1 = "                    rsi_buy=int(params.get('rsi_buy', 35)),\n                    rsi_sell=int(params.get('rsi_sell', 70)),\n                )\n                if not alert:\n                    continue\n                if alert.signal not in ('RSI_BUY', 'WATCH_BUY'):"
new1 = "                    rsi_buy=int(params.get('rsi_buy', 25)),\n                    rsi_sell=int(params.get('rsi_sell', 65)),\n                    atr_threshold=float(params.get('atr_threshold', 0.90)),\n                )\n                if not alert:\n                    continue\n                if alert.signal not in ('RSI_BUY', 'WATCH_BUY', 'HOLD'):"

if old1 in content:
    content = content.replace(old1, new1, 1)
    print('Fixed first call')
else:
    idx = content.find("rsi_buy', 35")
    print('First pattern NOT found')
    print(repr(content[max(0,idx-100):idx+300]))

# Fix second evaluate_signal call (in _check_positions)
old2 = "                rsi_buy=int(params.get('rsi_buy', 35)),\n                rsi_sell=int(params.get('rsi_sell', 70)),\n            )\n            if alert:\n                alerts.append(alert)"
new2 = "                rsi_buy=int(params.get('rsi_buy', 25)),\n                rsi_sell=int(params.get('rsi_sell', 65)),\n                atr_threshold=float(params.get('atr_threshold', 0.90)),\n            )\n            if alert:\n                alerts.append(alert)"

if old2 in content:
    content = content.replace(old2, new2, 1)
    print('Fixed second call')
else:
    idx = content.find("rsi_buy', 35")
    idx2 = content.find("rsi_buy', 35", idx+10)
    print('Second pattern NOT found')
    print(repr(content[idx2-50:idx2+300]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Saved')

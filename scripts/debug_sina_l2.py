"""调试新浪行情字段结构"""

import requests

url = 'https://hq.sinajs.cn/rn=1&list=sh600900'
r = requests.get(url, headers={
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://finance.sina.com.cn',
}, timeout=8)
text = r.content.decode('gbk', errors='replace')
print("Raw:", text)

content = text.split('"')[1]
fields = content.split(',')
print(f"\nTotal fields: {len(fields)}")
for i, f in enumerate(fields):
    print(f"  [{i:2d}]: {f}")

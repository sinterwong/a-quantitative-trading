"""调研港股数据格式"""

import requests

# 新浪港股格式
def fetch_sina_hk(sym):
    url = f'https://hq.sinajs.cn/rn=1&list={sym}'
    r = requests.get(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.sina.com.cn',
    }, timeout=8)
    text = r.content.decode('gbk', errors='replace')
    content = text.split('"')[1]
    fields = content.split(',')
    return fields

# 小米集团
fields = fetch_sina_hk('hk01810')
print(f"H01810 (Xiaomi) - {len(fields)} fields:")
for i, f in enumerate(fields):
    print(f"  [{i:2d}]: {f}")

print()

# 腾讯
fields = fetch_sina_hk('hk00700')
print(f"00700 (Tencent) - {len(fields)} fields:")
for i, f in enumerate(fields):
    print(f"  [{i:2d}]: {f}")

print()

# 恒生指数
fields = fetch_sina_hk('hkHSI')
print(f"HSI (Hang Seng) - {len(fields)} fields:")
for i, f in enumerate(fields):
    print(f"  [{i:2d}]: {f}")

print()

# 恒生科技
fields = fetch_sina_hk('hkHSTECH')
print(f"HSTECH - {len(fields)} fields:")
for i, f in enumerate(fields):
    print(f"  [{i:2d}]: {f}")

# 新浪港股分钟K线
print("\n=== Sina HK Minute K-line ===")
url = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=hk00700&scale=240&ma=no&datalen=3'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
print("HK 240minK:", r.text[:300])

url = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=hk01810&scale=60&ma=no&datalen=5'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
print("HK 60minK:", r.text[:300])

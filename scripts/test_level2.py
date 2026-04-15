"""Phase 4 Level2 数据源调研"""

import requests
import json
import os

# 东方财富 Level2 实时行情（免费，无需认证）
# 格式：ut=fa461dd8f8a04fd29c4fd7d7647ce59a, cb=jQuery, 重口...
# 需要用 JSONP 格式

def test_em_l2_realtime():
    """东方财富 Level2 实时盘口（免费）"""
    try:
        # 东方财富 Level2 实时行情
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            "?ut=fa461dd8f8a04fd29c4fd7d7647ce59a"
            "&secid=1.600900"
            "&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f107,f116,f117,f152"
            "&cb=jQuery"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, headers=headers, timeout=8)
        raw = resp.text
        # JSONP 回调提取
        if raw.startswith('jQuery'):
            raw = raw[raw.index('(')+1 : raw.rindex(')')]
        data = json.loads(raw)
        s = data.get('data', {})
        print("EM Level2 data keys:", list(s.keys()) if s else 'empty')
        print("Sample:", {k: s.get(k) for k in list(s.keys())[:10]})
        return s
    except Exception as e:
        print(f"EM L2 failed: {e}")
        return None

def test_em_tick_history():
    """东方财富 分时历史（含逐笔）"""
    try:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
            "?secid=1.600900"
            "&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            "&iscr=0"
            "&ndays=1"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        s = data.get('data', {})
        if s:
            print("Trends data keys:", list(s.keys()))
            # s['data'] 是分时数组，s['preData'] 是昨日收盘
            print("Today trends count:", len(s.get('data', [])))
        return s
    except Exception as e:
        print(f"EM trends failed: {e}")
        return None

def test_sse_deal():
    """上交所 Level2 逐笔成交"""
    try:
        url = "http://query.sse.com.cn/sseQuery/commonQuery.do"
        params = {
            'sqlId': 'COMMON_SSE_CP_GPJCTPZJ_GPLB_GP_L2_CJML',
            'stockCode': '600900',
            'date': '',
            'beginDate': '',
            'endDate': '',
        }
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'http://query.sse.com.cn/',
        }
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        print("SSE deal response:", resp.text[:200])
        return resp.json()
    except Exception as e:
        print(f"SSE deal failed: {e}")
        return None

def test_tx_l2():
    """腾讯 Level2 盘口（已测试：域名解析失败）"""
    try:
        sym = 'sh600900'
        url = f'https://qt.gtimg.cn/q=l2_{sym}'
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=8)
        print("TX L2:", resp.text[:300])
        return resp.text
    except Exception as e:
        print(f"TX L2 failed: {e}")
        return None

if __name__ == '__main__':
    print("=== 1. EM Level2 Realtime ===")
    test_em_l2_realtime()
    print("\n=== 2. EM Trends History ===")
    test_em_tick_history()
    print("\n=== 3. TX L2 ===")
    test_tx_l2()

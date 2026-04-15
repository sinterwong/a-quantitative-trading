"""调研东方财富 Level2 盘口深度接口"""

import requests
import json

def test_em_depth():
    """东方财富 Level2 盘口（买卖队列）"""
    try:
        # 东方财富 Level2 盘口行情
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            "?ut=fa461dd8f8a04fd29c4fd7d7647ce59a"
            "&secid=1.600900"
            "&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,"
            "f107,f116,f117,f152,f168,f170,f171"
            "&cb=jQuery"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, headers=headers, timeout=8)
        raw = resp.text
        if raw.startswith('jQuery'):
            raw = raw[raw.index('(')+1 : raw.rindex(')')]
        data = json.loads(raw)
        s = data.get('data', {})
        print("L2 keys:", list(s.keys()))
        print("All values:", json.dumps(s, ensure_ascii=False, indent=2)[:500])
    except Exception as e:
        print(f"EM depth failed: {e}")

def test_em_stock_detail():
    """东方财富 股票详情（包含逐笔）"""
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            "?ut=fa461dd8f8a04fd29c4fd7d7647ce59a"
            "&secid=1.600900"
            "&fields="
            "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f53,"
            "f54,f55,f56,f57,f58,f59,f60,f107,f108,f109,f110,f111,f112,"
            "f116,f117,f118,f119,f120,f121,f122,f123,f124,f125,f126,f127,f128,"
            "f129,f130,f131,f132,f133,f134,f135,f136,f137,f138,f139,f140,"
            "f141,f142,f143,f144,f145,f146,f147,f148,f149,f150,"
            "f151,f152,f153,f154,f155,f156,f157,f158,f159,f160,f161,f162,f163,"
            "f164,f165,f166,f167,f168,f169,f170,f171,f172"
            "&cb=jQuery"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, headers=headers, timeout=8)
        raw = resp.text
        if raw.startswith('jQuery'):
            raw = raw[raw.index('(')+1 : raw.rindex(')')]
        data = json.loads(raw)
        s = data.get('data', {})
        print("Full L2 keys:", list(s.keys()))
        # 找盘口数据字段
        for k in sorted(s.keys()):
            if '1' in k or '2' in k:
                print(f"  {k}: {s[k]}")
    except Exception as e:
        print(f"EM detail failed: {e}")

def test_em_depth_2():
    """东方财富 Level2 委托队列（买卖队列）"""
    try:
        # 这个接口获取逐笔委托（而不是逐笔成交）
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            "?ut=fa461dd8f8a04fd29c4fd7d7647ce59a"
            "&secid=1.600900"
            "&fields=allasks,allbids"
            "&cb=jQuery"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/',
        }
        resp = requests.get(url, headers=headers, timeout=8)
        raw = resp.text
        print("Depth response:", raw[:300])
    except Exception as e:
        print(f"EM depth2 failed: {e}")

if __name__ == '__main__':
    print("=== EM Depth ===")
    test_em_depth()
    print("\n=== EM Full Detail ===")
    test_em_stock_detail()
    print("\n=== EM Depth 2 ===")
    test_em_depth_2()

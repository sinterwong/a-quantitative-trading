"""
基本面数据获取模块 (S2-T4)
从 data_gateway 统一行情中提取 PE、PB、股息率等指标。
"""

from typing import Optional


def fetch_fundamentals(symbol: str) -> Optional[dict]:
    """
    获取单只股票的基本面指标。

    Returns:
        dict with keys:
            pe (float): 市盈率 PE (TTM)
            pb (float): 市净率 PB
            dividend_yield (float): 股息率 (%)
            market_cap (float): 总市值（亿元）
            price (float): 当前价格
            symbol (str): 标的代码
            name (str): 股票名称
        None if data unavailable.
    """
    try:
        from core.data_gateway import get_gateway
        q = get_gateway().quote(symbol)
        if q is None or not q.is_valid:
            return None

        return {
            'symbol': symbol,
            'name': q.name,
            'pe': q.pe_ttm,
            'pb': q.pb,
            'dividend_yield': q.dividend_yield,
            'market_cap': q.market_cap,  # 亿元
            'price': q.price,
        }
    except Exception:
        return None


def check_fundamentals_filter(symbol: str,
                               max_pe: float = 80.0,
                               max_pb: float = 15.0) -> tuple[bool, str]:
    """
    检查基本面是否满足筛选条件。

    Returns:
        (pass, reason) — pass=True 表示通过，False 表示不通过
    """
    data = fetch_fundamentals(symbol)
    if data is None:
        return True, '基本面数据获取失败，跳过过滤'


    pe = data.get('pe', 0)
    pb = data.get('pb', 0)
    mc = data.get('market_cap', 0)

    # ETF 类品种（价格>0 但 PE/PB 为 0）直接通过
    if pe <= 0 and pb <= 0:
        # 可能是 ETF 或特殊品种，跳过基本面过滤
        return True, f'PE/PB 均无效（可能是 ETF），跳过过滤'

    if pe <= 0:
        return False, f'PE={pe:.1f}无效或亏损'
    if pe > max_pe:
        return False, f'PE={pe:.1f}>{max_pe}过高'

    if pb > max_pb:
        return False, f'PB={pb:.2f}>{max_pb}过高'

    reason = f'PE={pe:.1f} PB={pb:.2f}'
    if data.get('dividend_yield', 0) > 0:
        reason += f' 股息率={data["dividend_yield"]:.2f}%'
    return True, reason


# ── 主测试入口 ────────────────────────────────────────
if __name__ == '__main__':
    import sys, os
    for k in list(os.environ.keys()):
        if 'proxy' in k.lower(): del os.environ[k]

    test_symbols = ['600036.SH', '000001.SZ', '600900.SH', '510310.SH']
    print('=' * 60)
    print('基本面数据测试')
    print('=' * 60)
    for sym in test_symbols:
        data = fetch_fundamentals(sym)
        if data:
            print('\n[%s %s]' % (sym, data.get('name', '')))
            print('  PE=%.2f  PB=%.2f  股息率=%.2f%%  市值=%.0f亿' % (
                data['pe'], data['pb'], data['dividend_yield'], data['market_cap']))
            ok, reason = check_fundamentals_filter(sym)
            print('  filter: pass=%s  reason=%s' % (ok, reason))
        else:
            print('\n[%s] 数据获取失败' % sym)

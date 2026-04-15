"""
基本面数据获取模块 (S2-T4)
从腾讯财经实时行情中提取 PE、PB、股息率等指标。
"""

from typing import Optional
import urllib.request
import ssl


def _fetch_tencent_fields(symbol: str) -> Optional[list]:
    """
    获取腾讯财经实时行情字段列表。
    """
    # Normalize: 600036.SH -> sh600036, 000001.SZ -> sz000001
    sym = symbol.upper().replace('.SH', '').replace('.SZ', '')
    if symbol.upper().endswith('.SH'):
        sym = 'sh' + symbol.replace('.SH', '')
    elif symbol.upper().endswith('.SZ'):
        sym = 'sz' + symbol.replace('.SZ', '')
    else:
        sym = 'sh' + symbol  # default SH
    url = 'http://qt.gtimg.cn/q=%s' % sym
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode('gbk', errors='replace')
        fields = raw.split('~')
        return fields if len(fields) > 45 else None
    except Exception:
        return None


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
    fields = _fetch_tencent_fields(symbol)
    if not fields:
        return None

    try:
        pe = float(fields[39]) if fields[39].strip() else 0.0
        pb = float(fields[46]) if fields[46].strip() else 0.0
        dy = float(fields[38]) if fields[38].strip() else 0.0
        mc = float(fields[44]) if fields[44].strip() else 0.0
        price = float(fields[3]) if fields[3].strip() else 0.0
        name = fields[1]
        return {
            'symbol': symbol,
            'name': name,
            'pe': pe,
            'pb': pb,
            'dividend_yield': dy,
            'market_cap': mc,  # 亿元
            'price': price,
        }
    except (ValueError, IndexError):
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

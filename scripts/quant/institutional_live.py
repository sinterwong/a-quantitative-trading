"""
真实机构持仓数据加载器 v2
使用akshare.stock_institute_hold获取季度机构持仓数据
"""

import os
import sys
import json
import warnings
warnings.filterwarnings('ignore')
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 禁用代理
for key in list(os.environ.keys()):
    if 'proxy' in key.lower():
        del os.environ[key]


# ETF成分股映射
ETF_COMPONENTS = {
    '159992.SZ': [  # 创新药ETF
        ('600276', '恒瑞医药'),
        ('603259', '药明康德'),
        ('300122', '智飞生物'),
        ('000661', '长春高新'),
    ],
    '512690.SH': [  # 酒ETF
        ('600519', '贵州茅台'),
        ('000858', '五粮液'),
        ('000568', '泸州老窖'),
        ('600809', '山西汾酒'),
    ],
    '510300.SH': [  # 沪深300ETF
        ('600519', '贵州茅台'),
        ('600036', '招商银行'),
        ('601318', '中国平安'),
        ('000858', '五粮液'),
    ],
    '600900.SH': [  # 长江电力
        ('600900', '长江电力'),
    ]
}


def get_quarterly_institutional_holdings(quarter: str = '20241') -> dict:
    """
    获取指定季度所有股票的机构持仓数据

    Args:
        quarter: 季度，如 '20241' (2024年Q1), '20243' (2024年Q3)

    Returns:
        dict: {stock_code: {institution_count, change, hold_ratio, ...}}
    """
    cache_file = os.path.join(os.path.dirname(__file__), 'cache', f'inst_hold_{quarter}.json')

    # 尝试读取缓存
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass

    try:
        import akshare as ak
        df = ak.stock_institute_hold(symbol=quarter)

        if df is None or df.empty:
            return {}

        # 列名映射（根据实际数据结构调整）
        # 假设列顺序: 证券代码, 证券简称, 机构总数, 机构变化, 持股数量, 持股变化, 占流通股, 占流通股变化
        result = {}
        for idx, row in df.iterrows():
            try:
                code = str(row.iloc[0])
                name = str(row.iloc[1]) if len(row) > 1 else ''
                inst_count = float(row.iloc[2]) if pd.notna(row.iloc[2]) else 0
                inst_change = float(row.iloc[3]) if pd.notna(row.iloc[3]) else 0
                hold_ratio = float(row.iloc[6]) if pd.notna(row.iloc[6]) else 0

                result[code] = {
                    'name': name,
                    'institution_count': inst_count,
                    'institution_change': inst_change,
                    'hold_ratio': hold_ratio
                }
            except Exception as e:
                continue

        # 缓存
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)

        print(f"[OK] Loaded institutional holdings for quarter {quarter}: {len(result)} stocks")
        return result

    except Exception as e:
        print(f"[ERROR] Failed to load institutional holdings: {e}")
        return {}




def get_company_announcements(symbol: str, count: int = 10) -> list:
    """
    获取指定股票的最近公告（东方财富接口）

    覆盖以下公告类型：
    - 年报/季报摘要
    - 业绩预告/快报
    - 重大资产重组
    - 监管问询
    - 分红派息

    Args:
        symbol: 股票代码，如 '600900.SH'
        count: 返回最新N条公告

    Returns:
        list of dict: [{
            'title': str,
            'notice_date': str,
            'ann_type': str,
            'art_code': str,
        }]
    """
    import urllib.request, ssl, json

    # 转换代码格式
    if symbol.endswith('.SH'):
        em_type = 'SHA'
        code = symbol.replace('.SH', '')
    else:
        em_type = 'SZA'
        code = symbol.replace('.SZ', '')

    url = (
        f'https://np-anotice-stock.eastmoney.com/api/security/ann'
        f'?sr=-1&page_size={count}&page_index=1'
        f'&ann_type={em_type}&client_source=web&stock={code}'
    )

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}
        )
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            data = json.loads(resp.read())
            items = data.get('data', {}).get('list', [])
            return [
                {
                    'title': item.get('title', ''),
                    'notice_date': item.get('notice_date', ''),
                    'ann_type': item.get('ann_type', ''),
                    'art_code': item.get('art_code', ''),
                }
                for item in items
            ]
    except Exception:
        return []


def get_important_announcements(symbol: str) -> dict:
    """
    筛选重要公告，返回结构化摘要

    Returns:
        dict: {
            'earnings': list,      # 业绩相关
            'dividend': list,      # 分红派息
            'regulatory': list,    # 监管问询/重组
        }
    """
    announcements = get_company_announcements(symbol, count=20)
    result = {'earnings': [], 'dividend': [], 'regulatory': []}

    earnings_keywords = ['业绩', '快报', '预告', '年报', '季报', '半年报', '营收', '净利润', '每股收益']
    dividend_keywords = ['分红', '派息', '股息', '送股', '转增']
    regulatory_keywords = ['问询', '监管', '重组', '停牌', '复牌', '发行', '增发', '回购']

    for ann in announcements:
        title = ann.get('title', '')
        if any(k in title for k in earnings_keywords):
            result['earnings'].append(ann)
        elif any(k in title for k in dividend_keywords):
            result['dividend'].append(ann)
        elif any(k in title for k in regulatory_keywords):
            result['regulatory'].append(ann)

    return result


if __name__ == '__main__':
    import pandas as pd  # noqa


def get_etf_institutional_score(etf_symbol: str, quarter: str = '20241') -> dict:
    """
    计算ETF成分股或单个股票的机构持仓评分
    """
    components = ETF_COMPONENTS.get(etf_symbol, [])
    all_holdings = get_quarterly_institutional_holdings(quarter)

    if not components:
        # 可能是单个股票代码
        pure = etf_symbol.replace('.SH', '').replace('.SZ', '')
        if pure in all_holdings:
            data = all_holdings[pure]
            fund_count = data.get('institution_count', 0)
            hold_ratio = data.get('hold_ratio', 0)
            total_score = fund_count * hold_ratio
            signal = 'buy' if total_score > 5 else ('sell' if total_score < 2 else 'hold')
            return {
                'total_score': total_score,
                'fund_count': int(fund_count),
                'avg_hold_ratio': hold_ratio,
                'avg_fund_count': fund_count,
                'signal': signal,
                'quarter': quarter
            }
        return {'total_score': 0, 'signal': 'hold'}

    total_fund_count = 0
    total_hold_ratio = 0
    valid_stocks = 0
    stock_scores = []

    for code, name in components:
        if code in all_holdings:
            data = all_holdings[code]
            fund_count = data.get('institution_count', 0)
            hold_ratio = data.get('hold_ratio', 0)

            total_fund_count += fund_count
            total_hold_ratio += hold_ratio
            valid_stocks += 1
            stock_scores.append((code, name, fund_count, hold_ratio))

    if valid_stocks == 0:
        return {'total_score': 0, 'signal': 'hold'}

    avg_hold_ratio = total_hold_ratio / valid_stocks
    avg_fund_count = total_fund_count / valid_stocks
    total_score = avg_fund_count * avg_hold_ratio

    if total_score > 5:
        signal = 'buy'
    elif total_score < 2:
        signal = 'sell'
    else:
        signal = 'hold'

    stock_scores.sort(key=lambda x: x[2], reverse=True)

    return {
        'total_score': total_score,
        'fund_count': int(total_fund_count),
        'avg_hold_ratio': avg_hold_ratio,
        'avg_fund_count': avg_fund_count,
        'top_stocks': stock_scores[:5],
        'signal': signal,
        'quarter': quarter
    }


def get_institutional_signal_for_date(etf_symbol: str, date) -> str:
    """
    根据日期获取对应季度的机构信号

    Args:
        etf_symbol: ETF代码
        date: datetime对象或日期字符串

    Returns:
        'buy' / 'sell' / 'hold'
    """
    if isinstance(date, str):
        from datetime import datetime
        if ' ' in date:
            date = datetime.strptime(date.split()[0], '%Y-%m-%d')
        elif '-' in date:
            date = datetime.strptime(date, '%Y-%m-%d')

    year = date.year
    month = date.month

    # 确定季度
    if month <= 3:
        quarter = f"{year-1}q4"  # 去年Q4的年报
        quarter_code = f"{year-1}4"
    elif month <= 6:
        quarter = f"{year}q1"
        quarter_code = f"{year}1"
    elif month <= 9:
        quarter = f"{year}q2"
        quarter_code = f"{year}2"
    else:
        quarter = f"{year}q3"
        quarter_code = f"{year}3"

    try:
        result = get_etf_institutional_score(etf_symbol, quarter_code)
        return result.get('signal', 'hold')
    except Exception as e:
        print(f"[WARN] Institutional signal error: {e}")
        return 'hold'


# 测试
if __name__ == '__main__':
    print("=" * 50)
    print("Testing Institutional Data Loading")
    print("=" * 50)

    # 测试获取最新季度数据
    result = get_quarterly_institutional_holdings('20243')
    print(f"\nTotal stocks with data: {len(result)}")

    if result:
        # 测试计算创新药ETF评分
        score = get_etf_institutional_score('159992.SZ', '20243')
        print(f"\n159992.SZ (创新药ETF):")
        print(f"  Total Score: {score['total_score']:.2f}")
        print(f"  Avg Fund Count: {score['avg_fund_count']:.1f}")
        print(f"  Avg Hold Ratio: {score['avg_hold_ratio']:.2f}%")
        print(f"  Signal: {score['signal']}")
        if score['top_stocks']:
            print(f"  Top Holdings:")
            for code, name, count, ratio in score['top_stocks'][:3]:
                print(f"    {code} {name}: {int(count)} funds, {ratio:.2f}%")

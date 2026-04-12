#!/usr/bin/env python3
"""
股市日报生成脚本 - 动态选股版本
根据东方财富资讯自动选择热门标的
"""
import datetime
import urllib.request
import ssl
import os
import sys

# 强制禁用所有代理
for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    if key in os.environ:
        del os.environ[key]

# 创建 SSL 上下文
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# 添加 scripts 目录到 path
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from dynamic_selector import DynamicStockSelectorV2 as DynamicStockSelector


def get_stocks_tencent(codes):
    """批量获取股票数据"""
    try:
        url = f"https://qt.gtimg.cn/q={','.join(codes)}"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, context=ssl_context, timeout=10) as response:
            data = response.read().decode('gbk', errors='ignore')
        
        results = []
        for line in data.strip().split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            try:
                parts = line.split('=')
                if len(parts) < 2:
                    continue
                content = parts[1].strip().strip('"')
                if not content:
                    continue
                fields = content.split('~')
                if len(fields) < 45:
                    continue
                
                results.append({
                    'name': fields[1],
                    'code': fields[2],
                    'price': fields[3],
                    'pre_close': fields[4],
                    'open': fields[5],
                    'volume': fields[6],
                    'amount': fields[7],
                    'high': fields[9],
                    'low': fields[10],
                    'change_pct': fields[32],
                    'change': fields[33]
                })
            except Exception:
                continue
        return results
    except Exception as e:
        return [{'error': str(e)}]


def format_volume(num_str):
    try:
        num = float(num_str)
        if num >= 10000:
            return f"{num/10000:.2f}万手"
        return f"{num:.0f}手"
    except:
        return num_str


def format_amount(num_str):
    try:
        num = float(num_str)
        if num >= 10000:
            return f"{num/10000:.2f}亿"
        return f"{num:.2f}万"
    except:
        return num_str


def main():
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    weekday = datetime.datetime.now().weekday()
    weekdays_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    print("=" * 60)
    print(f"  股市日报 - {today} {weekdays_cn[weekday]}")
    print("=" * 60)
    print()
    
    # ===== 动态选股 =====
    print("[选股] 正在从东方财富资讯获取热门板块...")
    selector = DynamicStockSelector()
    
    # 获取新闻
    news = selector.fetch_market_news(limit=20)
    print(f"[选股] 获取到 {len(news)} 条资讯")
    
    # 计算多维度评分
    selector.calc_all_scores()
    top_bks = selector.get_top_bk_sectors(5)
    top_sector_names = [(info.get('name', bk), info) for bk, info in top_bks]
    print(f"[选股] 热门板块: {[n for n, _ in top_sector_names]}")
    
    # 选股
    stock_codes = selector.select_stocks(top_n=5)
    print(f"[选股] 选中标的: {stock_codes}")
    
    # 检测是否降级到宽基ETF（说明板块数据获取失败）
    top_bks_check = selector.get_top_bk_sectors(5)
    is_fallback = (
        not top_bks_check or
        all(selector.sector_scores.get(bk, {}).get('total', 0) < 20 for bk, _ in top_bks_check)
    )
    if is_fallback:
        print()
        print("!!! ================================================")
        print("!!!  板块数据暂时无法获取，本报告使用宽基ETF替代")
        print("!!!  评分仅供参考，不构成投资建议")
        print("!!!  建议稍后手动确认市场板块情况")
        print("!!! ================================================")
    print()
    
    # ===== 获取行情数据 =====
    # 转换代码格式: 512480.SH -> sh512480, 002371.SZ -> sz002371
    qt_codes = []
    for code in stock_codes:
        if '.' in code:
            num, market = code.split('.', 1)
            qt_codes.append(market.lower() + num)
        else:
            qt_codes.append(code)
    stocks = get_stocks_tencent(qt_codes)
    
    # 名称由腾讯接口直接返回，无需额外获取
    
    # ===== 生成报告 =====
    print("【一、关注标的行情】")
    print("-" * 60)
    
    if not stocks or all('error' in s for s in stocks):
        print("[错误] 获取数据失败")
        stocks = []
    
    for stock in stocks:
        if 'error' in stock:
            print(f"[错误] {stock['code']}: {stock['error']}")
            continue
        
        name = stock.get('name', '未知')
        code = stock.get('code', '')
        price = stock.get('price', '-')
        change_pct = stock.get('change_pct', '-')
        change = stock.get('change', '-')
        volume = format_volume(stock.get('volume', '0'))
        amount = format_amount(stock.get('amount', '0'))
        high = stock.get('high', '-')
        low = stock.get('low', '-')
        open_price = stock.get('open', '-')
        pre_close = stock.get('pre_close', '-')
        
        try:
            change_val = float(change) if change not in ['-', ''] else 0
            trend = "↑上涨" if change_val > 0 else ("↓下跌" if change_val < 0 else "平盘")
        except:
            trend = "未知"
        
        market = "SH" if code.startswith('6') or code.startswith('5') else "SZ"
        
        print()
        print(f"[{trend}] {name} ({code}.{market})")
        print(f"  最新价: {price} 元")
        print(f"  涨跌幅: {change_pct}%")
        print(f"  涨跌额: {change} 元")
        print(f"  成交量: {volume}")
        print(f"  成交额: {amount}")
        print(f"  最高: {high} | 最低: {low}")
        print(f"  今开: {open_price} | 昨收: {pre_close}")
    
    # ===== 新闻板块部分 =====
    print()
    print("【二、今日要闻】")
    print("-" * 60)
    print("(来源: 东方财富)")
    print()
    
    # 热门板块汇总
    if top_bks and not is_fallback:
        print("[热门板块]")
        for bk, info in top_bks:
            total = info.get('total', 0)
            chg = info.get('change_pct', 0)
            print(f"  - {info.get('name','?')} [{bk}]: 涨幅{chg:+.2f}% 综合:{total:.1f}分")
        print()
    
    # 新闻列表
    news_summary = selector.get_news_summary(limit=10)
    print(news_summary)
    
    # 选股逻辑说明
    print()
    print("【选股逻辑】")
    print("-" * 60)
    print("根据东方财富市场资讯，识别热门板块，")
    print("综合新闻(15%) + 行情(35%) + 资金(25%) + 技术(15%) + 一致性(10%)自动选股。")
    print()
    for info in selector.get_stock_with_context(5):
        chg = info.get('change_pct', '-')
        sector = info.get('sector_name', '宽基')
        total = info.get('total_score', 0)
        if is_fallback:
            print(f"  - {info['name']} ({info['code']}) [{chg}] - 宽基ETF")
        else:
            print(f"  - {info['name']} ({info['code']}) [{chg}] - {sector} 综合:{total:.0f}分")
    
    print()
    print("=" * 60)
    # 数据来源状态
    src_map = {'cache': '文件缓存', 'eastmoney': '东方财富API', 'ths': '同花顺', 'failed': '全部失败(WARN)', 'not_tried': '未尝试'}
    news_src = src_map.get(selector._last_news_source, selector._last_news_source)
    sector_src = src_map.get(selector._last_source, selector._last_source)
    print(f"[数据状态] 资讯: {news_src} | 板块: {sector_src}")
    if is_fallback:
        print("[WARN] 板块数据获取失败 -> 已fallback宽基ETF(沪深300/创业板/酒ETF)，评分仅供参考")
    print("数据来源: 腾讯财经/东方财富/同花顺 | 仅供参考，不构成投资建议")
    
    # 返回选中的标的代码（供外部调用）
    return [s['code'] for s in stocks if 'error' not in s]


if __name__ == "__main__":
    main()

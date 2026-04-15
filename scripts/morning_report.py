# -*- coding: utf-8 -*-
"""
morning_report.py — A股早报生成模块
===================================
从 dynamic_selector 获取市场数据 + news_scorer 获取情绪，生成结构化早报。

纯函数，无状态，可直接 import 后调用 build_report()。
"""

import os
import sys
import json
import ssl
import logging
from datetime import datetime
from typing import Dict, List, Optional

# 禁用代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUANT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

_logger = logging.getLogger('morning_report')


# ─────────────────────────────────────────────────────────
# 新闻情绪（带缓存单例）
# ─────────────────────────────────────────────────────────

_sentiment_scorer = None


def _get_sentiment_scorer():
    global _sentiment_scorer
    if _sentiment_scorer is None:
        try:
            from quant.news_scorer import NewsSentimentScorer
            _sentiment_scorer = NewsSentimentScorer(cache_minutes=15)
        except Exception:
            _logger.warning('NewsSentimentScorer not available')
    return _sentiment_scorer


def _fetch_market_sentiment() -> Dict:
    """获取市场综合情绪"""
    scorer = _get_sentiment_scorer()
    if scorer is None:
        return {}
    try:
        return scorer.get_market_sentiment()
    except Exception as e:
        _logger.warning('get_market_sentiment failed: %s', e)
        return {}


# ─────────────────────────────────────────────────────────
# 大盘指数
# ─────────────────────────────────────────────────────────

def _fetch_index_prices() -> List[Dict]:
    """获取上证/深证/创业板/科创50/沪深300指数实时价格"""
    indices = [
        ('sh000001', '上证指数'),
        ('sz399001', '深证成指'),
        ('sz399006', '创业板指'),
        ('sh000688', '科创50'),
        ('sh000300', '沪深300'),
    ]
    result = []
    try:
        import ssl, urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        codes = ','.join(k for k, _ in indices)
        url = f'https://qt.gtimg.cn/q={codes}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
        lines = raw.strip().split('\n')
        for line in lines:
            parts = line.split('~')
            if len(parts) < 35:
                continue
            code = parts[0].split('_')[1] if '_' in parts[0] else ''
            for ticker, name in indices:
                if ticker in parts[0]:
                    try:
                        price = float(parts[3])   # 当前价
                        prev  = float(parts[4])   # 昨收
                        chg   = (price - prev) / prev * 100 if prev else 0
                        result.append({
                            'name':  name,
                            'price': price,
                            'pct':   chg,
                        })
                    except (ValueError, IndexError):
                        pass
                    break
    except Exception as e:
        _logger.warning('Index fetch failed: %s', e)
    return result


# ─────────────────────────────────────────────────────────
# 北向资金
# ─────────────────────────────────────────────────────────

def _fetch_northbound() -> Dict:
    """获取北向资金数据"""
    try:
        sys.path.insert(0, os.path.join(QUANT_DIR, 'backend'))
        from services.northbound import fetch_kamt
        data = fetch_kamt()
        return data or {}
    except Exception as e:
        _logger.warning('Northbound fetch failed: %s', e)
        return {}


# ─────────────────────────────────────────────────────────
# 选股结果
# ─────────────────────────────────────────────────────────

def _fetch_selected_stocks(n: int = 5) -> List[Dict]:
    """通过 DynamicStockSelectorV2 获取选股结果"""
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import dynamic_selector
        sel = dynamic_selector.DynamicStockSelectorV2()
        sel.fetch_market_news()
        sel.fetch_sectors()
        result = sel.get_stock_with_context(top_n=n)
        return result
    except Exception as e:
        _logger.warning('DynamicStockSelector failed: %s', e)
        return []


# ─────────────────────────────────────────────────────────
# 报告构建
# ─────────────────────────────────────────────────────────

def _format_sentiment_block(sentiment: Dict) -> str:
    """格式化情绪区块"""
    lines = []
    score = sentiment.get('composite_score', 0)
    label = sentiment.get('label', '中性')
    total = sentiment.get('total_news', 0)

    # 标签映射
    emoji = {'利好': '🟢', '利空': '🔴', '中性': '⚪'}.get(label, '⚪')
    lines.append(f'  市场情绪：{emoji} {label} ({score:>+3}) | {total}条新闻')

    # 板块情绪
    sector_scores = sentiment.get('sector_scores', {})
    if sector_scores:
        sorted_sectors = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_sectors[:3]
        if top3:
            sector_str = ' / '.join(f'{s}:{v:+.0f}' for s, v in top3)
            lines.append(f'  热门板块情绪：{sector_str}')
    return '\n'.join(lines)


def _format_index_block(indices: List[Dict]) -> str:
    """格式化大盘指数区块"""
    lines = []
    for idx in indices:
        name = idx.get('name', '')
        pct  = idx.get('pct', 0)
        try:
            pct_val = float(str(pct).rstrip('%'))
        except (ValueError, TypeError):
            pct_val = 0.0
        arrow = '🔺' if pct_val >= 0 else '🔻'
        lines.append(f'  {arrow} {name}: {pct:+.2f}%')
    return '\n'.join(lines)


def _format_stock_block(stocks: List[Dict]) -> str:
    """格式化选股结果区块"""
    lines = []
    for i, s in enumerate(stocks, 1):
        name = s.get('name', '?')
        code = s.get('code', '?')
        chg  = s.get('change_pct', 0)
        sector = s.get('sector_name', '')
        score = s.get('total_score', 0)
        try:
            chg_val = float(str(chg).rstrip('%'))
        except (ValueError, TypeError):
            chg_val = 0.0
        arrow = '🔺' if chg_val >= 0 else '🔻'
        lines.append(
            f'  {i}. {name} [{code}] {arrow}{abs(chg_val):.2f}%'
            f' | {sector} | 综合:{score:.0f}分'
        )
    return '\n'.join(lines)


def _format_news_block(top_positive, top_negative, limit: int = 5) -> str:
    """格式化新闻区块"""
    lines = []
    if top_positive:
        lines.append('  🟢 利好：')
        for n in top_positive[:limit]:
            title = n.get('title', '')[:40]
            lines.append(f'    · {title}')
    if top_negative:
        lines.append('  🔴 利空：')
        for n in top_negative[:limit]:
            title = n.get('title', '')[:40]
            lines.append(f'    · {title}')
    return '\n'.join(lines) if lines else '  暂无重要新闻'


def build_report(include_sentiment: bool = True,
                 include_indices: bool = True,
                 include_stocks: bool = True,
                 include_news: bool = True) -> str:
    """
    生成完整早报文本。

    调用示例：
        report = build_report()
        print(report)
    """
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d %A')
    time_str = now.strftime('%H:%M')

    sections = []

    # ── 标题 ──────────────────────────────────────────────
    sections.append(f"📊 A股早报 | {date_str} {time_str}")
    sections.append('=' * 50)

    # ── 市场情绪 ─────────────────────────────────────────
    if include_sentiment:
        sentiment = _fetch_market_sentiment()
        if sentiment:
            sections.append('【一、市场情绪】')
            sections.append('-' * 50)
            sections.append(_format_sentiment_block(sentiment))
            sections.append('')

    # ── 大盘指数 ─────────────────────────────────────────
    if include_indices:
        indices = _fetch_index_prices()
        if indices:
            sections.append('【二、大盘指数】')
            sections.append('-' * 50)
            sections.append(_format_index_block(indices))
            sections.append('')

    # ── 选股结果 ─────────────────────────────────────────
    if include_stocks:
        stocks = _fetch_selected_stocks(n=5)
        if stocks:
            sections.append('【三、关注标的】(动态选股)')
            sections.append('-' * 50)
            sections.append(_format_stock_block(stocks))
            sections.append('')

    # ── 重要新闻 ─────────────────────────────────────────
    if include_news:
        sentiment = _fetch_market_sentiment()
        top_pos = sentiment.get('top_positive', []) if sentiment else []
        top_neg = sentiment.get('top_negative', []) if sentiment else []
        if top_pos or top_neg:
            sections.append('【四、精选资讯】')
            sections.append('-' * 50)
            sections.append(_format_news_block(top_pos, top_neg))
            sections.append('')

    sections.append('=' * 50)
    sections.append('数据来源：东方财富 / 腾讯财经 | 仅供参考，不构成投资建议')
    sections.append(f'生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}')

    return '\n'.join(sections)


if __name__ == '__main__':
    print('Building morning report...')
    report = build_report()
    print(report)

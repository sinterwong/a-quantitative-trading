# -*- coding: utf-8 -*-
"""
morning_report.py — A股早报生成模块
===================================
从 dynamic_selector 获取市场数据 + news_scorer 获取情绪，生成结构化早报。

纯函数，无状态，可直接 import 后调用 build_report()。

设计原则：
  1. build_report() 接受预计算数据，避免 morning_runner 重复抓取
  2. 所有区块获取失败时显示占位符，不静默消失
  3. _fetch_market_sentiment() 在单次 build_report() 调用中只执行一次
  4. _fetch_selected_stocks() 包含完整的 calc_all_scores() 流程
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
_llm_service     = None


def _get_llm_service():
    """懒加载 LLMService（仅当配置了 API key 时才初始化）"""
    global _llm_service
    if _llm_service is not None:
        return _llm_service
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(QUANT_DIR, 'backend'))
        from services.llm.factory import create_llm_service
        svc = create_llm_service()
        if svc and svc.is_available:
            _llm_service = svc
            _logger.info('LLM service loaded for morning report')
        else:
            _logger.warning('LLM provider not available, morning report LLM summary disabled')
    except Exception as e:
        _logger.warning('LLM service init failed in morning report: %s', e)
    return _llm_service


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
    """获取市场综合情绪（含 top_positive/top_negative/sector_scores）"""
    scorer = _get_sentiment_scorer()
    if scorer is None:
        return {}
    try:
        return scorer.get_market_sentiment()
    except Exception as e:
        _logger.warning('get_market_sentiment failed: %s', e)
        return {}


def _fetch_llm_market_narrative(sentiment: Dict) -> str:
    """
    用 LLM 批量分析当日热门新闻，生成市场情绪总结。
    失败时返回空字符串，不阻塞早报其他内容。
    """
    llm = _get_llm_service()
    if llm is None:
        return ''

    top_pos = sentiment.get('top_positive', [])
    top_neg = sentiment.get('top_negative', [])
    if not top_pos and not top_neg:
        return ''

    # 拼装新闻文本（取前8条，每条限80字）
    news_lines = []
    for n in (top_pos + top_neg)[:8]:
        title = n.get('title', '')[:80]
        score = n.get('score', 0)
        label = '🟢' if score >= 10 else '🔴'
        news_lines.append(f'{label} {title}')
    news_text = '\n'.join(news_lines)

    prompt = (
        f"你是一位专业的A股量化分析师。以下是今日最重要的财经新闻：\n"
        f"{news_text}\n\n"
        f"请用2-3句话总结：1）今日市场整体情绪和主要驱动力；"
        f"2）哪些板块/概念最值得关注及原因。"
        f"回复直接是正文，不要加标题，不要加JSON，直接给总结段落。"
    )

    try:
        resp = llm.provider.chat(
            [{'role': 'user', 'content': prompt}],
            max_tokens=512,
            temperature=0.3,
        )
        summary = resp.content.strip() if resp.content else ''
        if summary:
            _logger.info('LLM market narrative generated (%d chars)', len(summary))
        return summary
    except Exception as e:
        _logger.warning('LLM market narrative failed: %s', e)
        return ''


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
        import urllib.request
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
            for ticker, name in indices:
                if ticker in parts[0]:
                    try:
                        price = float(parts[3])   # 当前价
                        prev  = float(parts[4])   # 昨收
                        pct   = (price - prev) / prev * 100 if prev else 0.0
                        result.append({
                            'name':  name,
                            'price': price,
                            'pct':   pct,           # 统一为 float，避免格式化混用
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
# 选股结果（完整流程，含 calc_all_scores）
# ─────────────────────────────────────────────────────────

def _fetch_selected_stocks(n: int = 5) -> List[Dict]:
    """
    通过 DynamicStockSelectorV2 获取选股结果。
    注意：必须调用 calc_all_scores() 后再 get_stock_with_context()，否则评分为空。
    """
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import dynamic_selector
        sel = dynamic_selector.DynamicStockSelectorV2()
        sel.fetch_market_news(30)
        sel.fetch_sectors()
        sel.calc_all_scores()                        # ← 关键步骤，不可省略
        result = sel.get_stock_with_context(top_n=n)
        return result
    except Exception as e:
        _logger.warning('DynamicStockSelector failed: %s', e)
        return []


# ─────────────────────────────────────────────────────────
# 格式化区块
# ─────────────────────────────────────────────────────────

def _format_sentiment_block(sentiment: Dict, llm_narrative: str = '') -> str:
    """格式化情绪区块"""
    lines = []
    score = sentiment.get('composite_score', 0)
    label = sentiment.get('label', '中性')
    total = sentiment.get('total_news', 0)

    emoji = {'利好': '🟢', '利空': '🔴', '中性': '⚪'}.get(label, '⚪')
    lines.append(f'  市场情绪：{emoji} {label} ({score:>+3}) | {total}条新闻')

    sector_scores = sentiment.get('sector_scores', {})
    if sector_scores:
        sorted_sectors = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_sectors[:3]
        if top3:
            sector_str = ' / '.join(f'{s}:{v:+.0f}' for s, v in top3)
            lines.append(f'  热门板块情绪：{sector_str}')

    if llm_narrative:
        lines.append(f'  📝 市场解读：{llm_narrative}')

    return '\n'.join(lines)


def _format_index_block(indices: List[Dict]) -> str:
    """格式化大盘指数区块（pct 统一为 float）"""
    lines = []
    for idx in indices:
        name = idx.get('name', '')
        pct  = float(idx.get('pct', 0) or 0)   # 统一转 float
        arrow = '🔺' if pct >= 0 else '🔻'
        lines.append(f'  {arrow} {name}: {pct:+.2f}%')
    return '\n'.join(lines)


def _format_stock_block(stocks: List[Dict]) -> str:
    """格式化选股结果区块"""
    lines = []
    for i, s in enumerate(stocks, 1):
        name   = s.get('name', '?')
        code   = s.get('code', s.get('symbol', '?'))
        chg_raw = s.get('change_pct', s.get('pct', 0)) or 0
        if isinstance(chg_raw, str):
            chg_raw = chg_raw.rstrip('%').strip()
        try:
            chg = float(chg_raw)
        except (ValueError, TypeError):
            chg = 0.0
        sector = s.get('sector_name', s.get('sector', ''))
        score  = s.get('total_score', s.get('score', s.get('total', 0)))
        arrow  = '🔺' if chg >= 0 else '🔻'
        lines.append(
            f'  {i}. {name} [{code}] {arrow}{abs(chg):.2f}%'
            f' | {sector} | 综合:{score:.0f}分'
        )
    return '\n'.join(lines)


def _format_news_block(top_positive: list, top_negative: list, limit: int = 5) -> str:
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


def _format_regime_block(regime_info: Dict) -> str:
    """格式化市场环境区块"""
    regime = regime_info.get('regime', 'UNKNOWN')
    reason = regime_info.get('regime_reason', regime_info.get('reason', ''))
    atr    = regime_info.get('atr_ratio', 0)
    rsi_b  = regime_info.get('rsi_buy', '--')
    rsi_s  = regime_info.get('rsi_sell', '--')
    label  = {'BULL': '🟢 牛市', 'BEAR': '🔴 熊市',
               'VOLATILE': '🟡 震荡', 'CALM': '⚪ 平静'}.get(regime, f'❓ {regime}')
    lines  = [f'  环境：{label}  | ATR比率：{atr:.3f}']
    if reason:
        lines.append(f'  原因：{reason}')
    lines.append(f'  RSI参数：买入<{rsi_b}  卖出>{rsi_s}')
    return '\n'.join(lines)


def _format_orders_block(buy_results: list) -> str:
    """格式化今日执行订单区块"""
    executed = [r for r in buy_results if r.get('filled_shares', 0) > 0]
    rejected = [r for r in buy_results if r.get('filled_shares', 0) == 0]

    lines = []
    if executed:
        for r in executed:
            sym    = r.get('symbol', '?')
            shares = r.get('filled_shares', 0)
            price  = r.get('avg_price', 0)
            lines.append(f'  ✅ 买入 {sym}  {shares}股 @ {price:.2f}')
    if rejected:
        for r in rejected:
            sym    = r.get('symbol', '?')
            reason = r.get('reason', r.get('status', ''))
            lines.append(f'  ❌ 跳过 {sym}  原因：{reason}')
    if not lines:
        lines.append('  今日无开盘订单')
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────
# 报告构建主函数
# ─────────────────────────────────────────────────────────

def build_report(
    include_sentiment: bool = True,
    include_indices:   bool = True,
    include_stocks:    bool = True,
    include_news:      bool = True,
    # 预计算数据（由 morning_runner 传入，避免重复抓取）
    prefetched_stocks:  Optional[List[Dict]] = None,
    prefetched_regime:  Optional[Dict]       = None,
    prefetched_orders:  Optional[List[Dict]] = None,
) -> str:
    """
    生成完整早报文本。

    Parameters
    ----------
    prefetched_stocks : 由 morning_runner 传入的选股结果（跳过内部再次抓取）
    prefetched_regime : 由 morning_runner 传入的市场环境参数
    prefetched_orders : 由 morning_runner 传入的开盘订单结果

    调用示例（独立运行）：
        report = build_report()

    调用示例（morning_runner 集成）：
        report = build_report(
            prefetched_stocks=candidates,
            prefetched_regime=regime_info,
            prefetched_orders=buy_results,
        )
    """
    now      = datetime.now()
    date_str = now.strftime('%Y-%m-%d %A')
    time_str = now.strftime('%H:%M')

    sections = []

    # ── 标题 ──────────────────────────────────────────────
    sections.append(f"📊 A股早报 | {date_str} {time_str}")
    sections.append('=' * 50)

    # ── 市场情绪（单次 fetch，供情绪+新闻两个区块复用）────
    sentiment: Dict = {}
    llm_narrative = ''
    if include_sentiment or include_news:
        sentiment = _fetch_market_sentiment()
        if sentiment:
            llm_narrative = _fetch_llm_market_narrative(sentiment)

    # ── 一、市场环境（来自 regime，优先于情绪）────────────
    if prefetched_regime:
        sections.append('【一、市场环境】')
        sections.append('-' * 50)
        sections.append(_format_regime_block(prefetched_regime))
        sections.append('')

    # ── 二、市场情绪 ─────────────────────────────────────
    if include_sentiment:
        sections.append('【二、市场情绪】')
        sections.append('-' * 50)
        if sentiment:
            sections.append(_format_sentiment_block(sentiment, llm_narrative))
        else:
            sections.append('  ⚠️ 情绪数据获取失败（网络或 NewsSentimentScorer 异常）')
        sections.append('')

    # ── 三、大盘指数 ─────────────────────────────────────
    if include_indices:
        indices = _fetch_index_prices()
        sections.append('【三、大盘指数】')
        sections.append('-' * 50)
        if indices:
            sections.append(_format_index_block(indices))
        else:
            sections.append('  ⚠️ 指数数据获取失败（腾讯财经接口超时）')
        sections.append('')

    # ── 四、关注标的 ─────────────────────────────────────
    if include_stocks:
        # 优先使用预计算结果，否则独立抓取
        stocks = prefetched_stocks if prefetched_stocks is not None else _fetch_selected_stocks(n=5)
        sections.append('【四、关注标的】(动态选股)')
        sections.append('-' * 50)
        if stocks:
            sections.append(_format_stock_block(stocks))
        else:
            sections.append('  ⚠️ 选股数据获取失败（DynamicStockSelectorV2 异常或无符合标的）')
        sections.append('')

    # ── 五、开盘订单（仅当 morning_runner 传入时显示）─────
    if prefetched_orders is not None:
        sections.append('【五、开盘订单】')
        sections.append('-' * 50)
        sections.append(_format_orders_block(prefetched_orders))
        sections.append('')

    # ── 六、精选资讯 ─────────────────────────────────────
    if include_news:
        top_pos = sentiment.get('top_positive', [])
        top_neg = sentiment.get('top_negative', [])
        num = 6 if prefetched_orders is not None else 5  # 有订单时用 6，否则用 5
        sections.append(f'【{num}、精选资讯】')
        sections.append('-' * 50)
        if top_pos or top_neg:
            sections.append(_format_news_block(top_pos, top_neg))
        elif not sentiment:
            sections.append('  ⚠️ 资讯数据获取失败')
        else:
            sections.append('  暂无重要新闻')
        sections.append('')

    sections.append('=' * 50)
    sections.append('数据来源：东方财富 / 腾讯财经 | 仅供参考，不构成投资建议')
    sections.append(f'生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}')

    return '\n'.join(sections)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    print('Building morning report...')
    report = build_report()
    print(report)

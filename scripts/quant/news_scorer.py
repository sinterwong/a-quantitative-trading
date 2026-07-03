# -*- coding: utf-8 -*-
"""
News Sentiment Scorer - 新闻情绪打分
P2 - 新闻情绪打分模块

默认使用 LLM（DeepSeek）分析情感，保留关键词兜底当 LLM 不可用时。
"""

import os, sys, time, random, json, re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

POSITIVE_KEYWORDS = [
    '降准', '降息', '放水', '宽松', '量化宽松', '财政刺激', '基建投资',
    '并购重组', '资产注入', '业绩超预期', '营收增长', '净利润增长',
    '订单大增', '市场份额提升', '技术突破', '新产品发布', '产能扩张',
    '获批', '获许可', '通过审批', '中标', '签约',
    '回购', '增持', '战略合作', '引入战投',
    '牛市', '反弹', '企稳', '回暖', '复苏',
    '开放', '改革', '试点', '示范',
    '北向资金大举流入', '外资抄底', '机构看好',
    '电力改革', '电费上调', '电价上涨',
    '银行信贷增长', '不良率下降',
    '芯片国产替代', '技术封锁突破',
    '新能源政策利好', '汽车以旧换新补贴',
    '涨停', '连续涨停', '股价创新高',
]

NEGATIVE_KEYWORDS = [
    '加息', '缩表', '收紧', '去杠杆', '监管收紧',
    '减持', '限售股解禁', '扩容', 'IPO加速',
    '贸易战', '关税', '出口管制', '制裁',
    '业绩下滑', '亏损', '营收下降', '商誉减值', '资产减值',
    '被立案调查', '监管措施', '警示函', '整改',
    '产品安全事故', '召回', '造假', '虚增利润',
    '债务违约', '评级下调', '破产风险',
    '股东减持', '高管离职', '核心人员流失',
    '诉讼', '仲裁', '处罚',
    '跌停', '连续跌停', '股价创新低',
    '北向资金大举流出', '外资抛售',
    '恐慌', '踩踏', '抛售潮',
    '股灾', '暴跌', '大幅回落',
]

NEUTRAL_KEYWORDS = [
    '维持评级', '符合预期', '观望', '中性',
    '等待突破', '震荡', '整理',
]

SECTOR_KEYWORDS = {
    '银行': ['银行', '信贷', '存款', '巴塞尔', '净息差'],
    '电力': ['电力', '电价', '发电', '电网', '煤价', '光伏', '风电', '水电', '核电'],
    '电子': ['电子', '半导体', '芯片', '集成电路', 'PCB', '消费电子', '苹果产业链'],
    '新能源': ['新能源', '锂电池', '电动汽车', '充电桩', '储能', '氢能'],
    '医药': ['医药', '中药', '医疗器械', '创新药', '疫苗', '仿制药', '集采'],
    '消费': ['消费', '白酒', '食品饮料', '家电', '纺织服装', '零售'],
    '房地产': ['房地产', '地产', '楼市', '房价', '限购', '限贷', '万科', '碧桂园'],
    '基建': ['基建', '建筑', '水泥', '钢铁', '工程机械', 'PPP', '城投'],
    '科技': ['科技', '人工智能', 'AI', '云计算', '大数据', '5G', '网络安全'],
    '军工': ['军工', '国防', '航天', '航空', '船舶', '导弹'],
    '化工': ['化工', '石化', '石油', '天然气', '化肥', '农药'],
    '交通运输': ['航空', '机场', '港口', '航运', '物流', '快递'],
    '保险': ['保险', '寿险', '财险', '险资'],
    '证券': ['证券', '券商', '经纪', '投行', '资管'],
}


def _clean_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _detect_sectors(text: str) -> List[str]:
    """基于关键词匹配板块分类（LLM 不参与板块识别，仍用规则）。"""
    text_clean = _clean_text(text)
    found = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in text_clean:
                if sector not in found:
                    found.append(sector)
                break
    return found


def _score_text(text: str) -> Tuple[int, str]:
    """关键词兜底打分（LLM 不可用时使用）。"""
    text_clean = _clean_text(text)
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_clean)
    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_clean)
    neu_count = sum(1 for kw in NEUTRAL_KEYWORDS if kw in text_clean)
    raw_score = pos_count * 12 - neg_count * 15 + neu_count * 2
    for kw in ['股灾', '暴跌', '债务违约', '破产风险', '被立案调查']:
        if kw in text_clean:
            raw_score -= 20
    for kw in ['业绩超预期', '并购重组', '技术突破', '降准', '降息']:
        if kw in text_clean:
            raw_score += 15
    score = max(-100, min(100, raw_score))
    if score >= 10:
        label = '利好'
    elif score <= -10:
        label = '利空'
    else:
        label = '中性'
    return score, label


def _get_llm_service():
    """懒加载 LLMService，失败时返回 None。"""
    try:
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scripts/
        _backend_path = os.path.join(_repo_root, 'backend')
        for _p in [_repo_root, _backend_path]:
            if _p not in sys.path:
                sys.path.insert(0, _p)
        from backend.services.llm.factory import create_llm_service
        svc = create_llm_service()
        if svc and svc.is_available:
            return svc
    except Exception:
        pass
    return None


def fetch_latest_news(max_news: int = 20) -> List[Dict]:
    news_list = []
    try:
        import ssl, urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = (f'https://newsapi.eastmoney.com/kuaixun/v1/getlist_101_ajaxResult_{max_news}_1_.html')
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Referer': 'https://www.eastmoney.com',
        })
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            content = resp.read().decode('utf-8', errors='replace')
        import re
        m = re.search(r'ajaxResult\s*=\s*(\{.+})', content)
        if m:
            data = json.loads(m.group(1))
            lives = data.get('LivesList', [])
            for item in lives[:max_news]:
                news_list.append({
                    'title': item.get('title', ''),
                    'url': item.get('url_w', ''),
                    'date': item.get('showtime', ''),
                    'source': '东方财富',
                })
    except Exception as e:
        print(f'[WARN] Eastmoney news API failed: {e}')
    return news_list


class NewsSentimentScorer:
    def __init__(self, cache_minutes: int = 10, use_llm: bool = True):
        """
        Args:
            cache_minutes: 缓存有效期（分钟）
            use_llm: 是否优先使用 LLM 情感分析（默认 True，失败时自动降级关键词）
        """
        self.cache_minutes = cache_minutes
        self.use_llm = use_llm
        self._cache = None
        self._cache_time = None
        self._llm_service = None
        self._llm_checked = False

    def _is_cache_valid(self) -> bool:
        if self._cache is None or self._cache_time is None:
            return False
        elapsed = (datetime.now() - self._cache_time).total_seconds() / 60
        return elapsed < self.cache_minutes

    def _get_llm(self):
        """获取 LLMService，只检查一次。"""
        if not self.use_llm:
            return None
        if self._llm_checked:
            return self._llm_service
        self._llm_checked = True
        self._llm_service = _get_llm_service()
        if self._llm_service is None:
            print('[WARN] NewsSentimentScorer: LLM not available, using keyword fallback')
        return self._llm_service

    def fetch_news(self, max_news: int = 20) -> List[Dict]:
        if self._is_cache_valid():
            return self._cache
        news = fetch_latest_news(max_news=max_news)
        self._cache = news
        self._cache_time = datetime.now()
        return news

    def score_one(self, title: str, use_llm: bool = None) -> Tuple[int, str, List[str]]:
        """
        对单条新闻打分。

        Args:
            title: 新闻标题
            use_llm: 是否用 LLM（None=使用实例默认值 self.use_llm）

        Returns:
            (score: int -100~100, label: str, sectors: List[str])
        """
        if use_llm is None:
            use_llm = self.use_llm

        # 板块分类始终用关键词（不需要 LLM）
        sectors = _detect_sectors(title)

        # 情感分析
        if use_llm and self._get_llm() is not None:
            try:
                result = self._llm_service.analyze_news(title.strip(), timeout=15)
                sentiment = getattr(result, 'sentiment', 'neutral')
                confidence = getattr(result, 'confidence', 0.0)
                # 映射 LLM 结果到 -100~100
                if sentiment == 'bullish':
                    raw_score = int(confidence * 100)
                elif sentiment == 'bearish':
                    raw_score = int(-confidence * 100)
                else:
                    raw_score = 0
                raw_score = max(-100, min(100, raw_score))
                label = '利好' if raw_score >= 10 else ('利空' if raw_score <= -10 else '中性')
                return raw_score, label, sectors
            except Exception as e:
                print(f'[WARN] LLM analyze_news failed: {e}, falling back to keywords')

        # 关键词兜底
        score, label = _score_text(title)
        return score, label, sectors

    def score_all(self, max_news: int = 20,
                  sector_filter: Optional[str] = None,
                  use_llm: bool = None) -> List[Dict]:
        if use_llm is None:
            use_llm = self.use_llm
        news = self.fetch_news(max_news)
        results = []

        # 板块分类始终用关键词（快）
        for item in news:
            title = item.get('title', '')
            item['_sectors'] = _detect_sectors(title)

        # LLM 批量情感分析
        llm = self._get_llm() if use_llm else None
        if llm is not None and news:
            try:
                # batch_news 返回带 sentiment_result 的原列表（顺序不变）
                enriched = llm.batch_news(news, text_field='title',
                                          timeout_per_item=20, max_concurrency=3)
                for item in enriched:
                    title = item.get('title', '')
                    sr = item.get('sentiment_result')
                    sectors = item.get('_sectors', [])
                    if sr is not None:
                        sentiment = getattr(sr, 'sentiment', 'neutral')
                        confidence = getattr(sr, 'confidence', 0.0)
                        if sentiment == 'bullish':
                            raw_score = int(confidence * 100)
                        elif sentiment == 'bearish':
                            raw_score = int(-confidence * 100)
                        else:
                            raw_score = 0
                        raw_score = max(-100, min(100, raw_score))
                        label = '利好' if raw_score >= 10 else ('利空' if raw_score <= -10 else '中性')
                    else:
                        raw_score, label = _score_text(title)
                    if sector_filter and sector_filter not in sectors:
                        continue
                    results.append({
                        'date': item.get('date', ''),
                        'title': title,
                        'score': raw_score,
                        'label': label,
                        'sectors': sectors,
                        'url': item.get('url', ''),
                        'source': item.get('source', ''),
                    })
                results.sort(key=lambda x: x['score'], reverse=True)
                return results
            except Exception as e:
                print(f'[WARN] batch_news failed: {e}, falling back to keywords')
                # Fall through to keyword scoring

        # 关键词兜底
        for item in news:
            title = item.get('title', '')
            sectors = item.get('_sectors', [])
            score, label = _score_text(title)
            if sector_filter and sector_filter not in sectors:
                continue
            results.append({
                'date': item.get('date', ''),
                'title': title,
                'score': score,
                'label': label,
                'sectors': sectors,
                'url': item.get('url', ''),
                'source': item.get('source', ''),
            })
        results.sort(key=lambda x: x['score'], reverse=True)
        return results

    def get_composite_score(self, scored_news: List[Dict]) -> Tuple[int, str]:
        if not scored_news:
            return 0, '中性'
        weighted_sum = 0
        total_weight = 0
        for i, item in enumerate(scored_news):
            weight = len(scored_news) - i
            weighted_sum += item['score'] * weight
            total_weight += weight
        composite = int(weighted_sum / total_weight) if total_weight > 0 else 0
        composite = max(-100, min(100, composite))
        if composite >= 10:
            label = '利好'
        elif composite <= -10:
            label = '利空'
        else:
            label = '中性'
        return composite, label

    def get_market_sentiment(self, use_llm: bool = None) -> Dict:
        """获取市场综合情绪（含板块情绪）。默认使用 LLM。"""
        if use_llm is None:
            use_llm = self.use_llm
        all_news = self.score_all(max_news=30, use_llm=use_llm)
        composite, label = self.get_composite_score(all_news)
        sector_scores: Dict[str, List[int]] = {}
        for item in all_news:
            for sector in item['sectors']:
                if sector not in sector_scores:
                    sector_scores[sector] = []
                sector_scores[sector].append(item['score'])
        sector_avg = {s: sum(v) / len(v) for s, v in sector_scores.items() if v}
        return {
            'composite_score': composite,
            'label': label,
            'total_news': len(all_news),
            'top_positive': [n for n in all_news if n['label'] == '利好'][:3],
            'top_negative': [n for n in all_news if n['label'] == '利空'][:3],
            'sector_scores': sector_avg,
            'timestamp': datetime.now().isoformat(),
        }


if __name__ == '__main__':
    print('\n============================================================')
    print('  NewsSentimentScorer Test (LLM Mode)')
    print('============================================================\n')
    scorer = NewsSentimentScorer(use_llm=True)
    test_titles = [
        '央行宣布降准0.25个百分点，释放长期资金约5000亿元',
        '多家银行信贷增长超预期，不良率持续下降',
        '芯片板块技术突破，国产替代进程加速',
        '某上市公司被证监会立案调查',
        'A股今日窄幅震荡，市场观望情绪浓厚',
        '电力改革重磅文件出台，电价机制迎来重大调整',
        '锂价暴跌30%，新能源板块集体重挫',
    ]
    print('[UNIT TEST] Scoring')
    for title in test_titles:
        score, label, sectors = scorer.score_one(title)
        print(f'  [{score:>+4},{label}] {title[:40]}... sectors={sectors}')
    print('\n[LIVE TEST] Fetch real news from Eastmoney + LLM sentiment')
    try:
        sentiment = scorer.get_market_sentiment()
        print(f'  Composite: {sentiment["composite_score"]:>+4} ({sentiment["label"]})')
        print(f'  Total news: {sentiment["total_news"]}')
        if sentiment.get('sector_scores'):
            print('  Sector scores:')
            for s, sc in sorted(sentiment['sector_scores'].items(),
                                key=lambda x: x[1], reverse=True)[:5]:
                print(f'    {s}: {sc:>+5.1f}')
    except Exception as e:
        print(f'  [WARN] News fetch failed: {e}')
    print('\n============================================================')

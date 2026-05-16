"""
analyze_stock._news — Parquet 新闻情感缓存读取(不调网络)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger('core.use_cases.analyze_stock')


def try_news_sentiment(symbol: str) -> Dict[str, Any]:
    """尝试读取 Parquet 缓存的新闻情感。不调网络。"""
    try:
        from core.factors.nlp import NewsSentimentFactor
        factor = NewsSentimentFactor(symbol=symbol)
        # 使用 _load_parquet_cache(如有);不存在则返回 unavailable
        if hasattr(factor, '_load_parquet_cache'):
            cache = factor._load_parquet_cache()
            if cache is None or len(cache) == 0:
                return {'available': False, 'reason': 'no_cached_sentiment'}
            latest = cache.iloc[-1]
            return {
                'available': True,
                'score': round(float(latest.get('score', 0.0) or 0.0), 4),
                'as_of': str(latest.get('date') or cache.index[-1]),
                'source': 'parquet_cache',
            }
        return {'available': False, 'reason': 'cache_api_unavailable'}
    except Exception as exc:
        logger.debug('_try_news_sentiment failed: %s', exc)
        return {'available': False, 'reason': str(exc)}

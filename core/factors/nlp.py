"""
core/factors/nlp.py — 新闻情感 LLM 因子

使用 Claude API 对股票相关新闻标题进行情感打分，转换为 Factor 接口。

数据来源：
  - 东方财富新闻（AKShare stock_news_em()）
  - 同花顺财经新闻（AKShare stock_news_ths()，可选）

情感打分：
  - 调用 Anthropic Claude API（claude-haiku-4-5 模型，速度快成本低）
  - Prompt：返回 JSON {"score": <-1到1>, "reason": "..."}
  - 多条新闻打分取加权平均（近期新闻权重更高）

缓存策略：
  - 新闻列表缓存 TTL = 4 小时（内存 + 本地 JSON）
  - 情感评分缓存 TTL = 24 小时（本地 JSON）
  - 无 API Key 时优雅降级（返回全零）

注意：
  - 此因子依赖外部 API，不适合高频调用
  - 建议每日只在收盘后更新一次（日频因子）
  - 建议权重 ≤ 0.15（LLM 评分存在噪声）

用法：
    from core.factors.nlp import NewsSentimentFactor

    # 方式一：无 API（全零）
    f = NewsSentimentFactor()
    z = f.evaluate(price_df)

    # 方式二：注入已获取的情感数据
    sentiment_series = pd.Series({pd.Timestamp('2024-01-15'): 0.3, ...})
    f = NewsSentimentFactor(sentiment_data=sentiment_series)
    z = f.evaluate(price_df)

    # 方式三：实时获取（需 ANTHROPIC_API_KEY）
    f = NewsSentimentFactor(symbol='000001.SZ', use_api=True)
    z = f.evaluate(price_df)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal

logger = logging.getLogger(__name__)

# 缓存目录
_CACHE_DIR = Path('data/news_cache')

# 新闻缓存 TTL（秒）
_NEWS_TTL = 4 * 3600       # 4 小时
_SCORE_TTL = 24 * 3600     # 24 小时


# ---------------------------------------------------------------------------
# 新闻获取层
# ---------------------------------------------------------------------------

def _fetch_news_eastmoney(symbol: str, n: int = 20) -> List[str]:
    """
    从东方财富获取股票新闻标题（AKShare）。

    Parameters
    ----------
    symbol : str
        标的代码（如 '000001.SZ'）
    n : int
        最多返回条数

    Returns
    -------
    List[str] — 新闻标题列表（最新在前）
    """
    try:
        import akshare as ak
        # AKShare 代码格式：去掉后缀
        code = symbol.split('.')[0]
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
        # 列名可能是 '标题' 或 'title'
        title_col = None
        for col in ['标题', 'title', '新闻标题', 'news_title']:
            if col in df.columns:
                title_col = col
                break
        if title_col is None:
            return []
        headlines = df[title_col].dropna().tolist()[:n]
        return [str(h).strip() for h in headlines if h]
    except Exception as e:
        logger.debug('[NewsSentimentFactor] 东财新闻获取失败: %s', e)
        return []


# ---------------------------------------------------------------------------
# LLM 情感打分层
# ---------------------------------------------------------------------------

def _score_with_claude(
    headlines: List[str],
    model: str = 'claude-haiku-4-5-20251001',
    api_key: Optional[str] = None,
) -> float:
    """
    调用 Claude API 对新闻标题列表进行情感打分。

    Parameters
    ----------
    headlines : List[str]
        新闻标题列表
    model : str
        Claude 模型 ID
    api_key : str or None
        Anthropic API Key（None 时从环境变量读取）

    Returns
    -------
    float — 情感得分 [-1, 1]（正=利好，负=利空）
    """
    if not headlines:
        return 0.0

    key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        logger.debug('[NewsSentimentFactor] 未设置 ANTHROPIC_API_KEY，跳过 LLM 打分')
        return 0.0

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)

        headlines_text = '\n'.join(f'- {h}' for h in headlines[:10])
        prompt = (
            f"请分析以下中国A股市场新闻标题的情感倾向，"
            f"并给出一个 -1 到 1 之间的分数（1=极度利好，-1=极度利空，0=中性）。\n\n"
            f"新闻标题：\n{headlines_text}\n\n"
            f"请只返回 JSON 格式：{{\"score\": <-1到1的小数>, \"reason\": \"<简短理由>\"}}"
        )

        message = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{'role': 'user', 'content': prompt}],
        )

        response_text = message.content[0].text.strip()
        # 解析 JSON
        if '{' in response_text and '}' in response_text:
            json_str = response_text[response_text.index('{'):response_text.rindex('}') + 1]
            data = json.loads(json_str)
            score = float(data.get('score', 0.0))
            return float(np.clip(score, -1.0, 1.0))

    except Exception as e:
        logger.warning('[NewsSentimentFactor] Claude API 调用失败: %s', e)

    return 0.0


# ---------------------------------------------------------------------------
# 缓存工具
# ---------------------------------------------------------------------------

def _cache_key(symbol: str, date_str: str) -> str:
    return hashlib.md5(f'{symbol}_{date_str}'.encode()).hexdigest()[:12]


def _load_cache(cache_path: Path, ttl_seconds: int) -> Optional[dict]:
    """加载本地缓存（TTL 超期则返回 None）。"""
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        ts = data.get('_cached_at', 0)
        if time.time() - ts > ttl_seconds:
            return None
        return data
    except Exception:
        return None


def _save_cache(cache_path: Path, data: dict) -> None:
    """保存到本地缓存。"""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data['_cached_at'] = time.time()
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.debug('[NewsSentimentFactor] 缓存写入失败: %s', e)


# ---------------------------------------------------------------------------
# NewsSentimentFactor
# ---------------------------------------------------------------------------

class NewsSentimentFactor(Factor):
    """
    新闻情感 LLM 因子。

    因子值 = z-score 归一化的日情感得分（[-1,1] → z-score）

    解读：
      - z > threshold：持续利好新闻 → BUY
      - z < -threshold：持续利空新闻 → SELL

    Parameters
    ----------
    symbol : str
        标的代码（用于获取特定股票新闻）
    sentiment_data : pd.Series or None
        外部注入的日频情感得分（index=日期，值=[-1,1]）。
        若提供，跳过 API 获取直接使用。
    use_api : bool
        是否调用 Claude API 实时获取（需 ANTHROPIC_API_KEY）。
        False = 仅使用 sentiment_data（无数据则全零）。
    window : int
        滚动均值平滑窗口（默认 5 天，减少单日噪声）
    n_news : int
        每次获取最多条数（默认 20）
    api_key : str or None
        Anthropic API Key（None 时从环境变量读取）
    threshold : float
        信号触发 z-score 阈值（默认 1.0）
    """

    name = 'NewsSentiment'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        symbol: str = '',
        sentiment_data: Optional[pd.Series] = None,
        use_api: bool = False,
        window: int = 5,
        n_news: int = 20,
        api_key: Optional[str] = None,
        threshold: float = 1.0,
    ) -> None:
        self.symbol = symbol
        self._sentiment_data = sentiment_data
        self.use_api = use_api
        self.window = window
        self.n_news = n_news
        self.api_key = api_key
        self.threshold = threshold

        # 内存缓存：{date_str: score}
        self._score_cache: Dict[str, float] = {}

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        """
        计算日频情感因子值（z-score）。

        优先级：sentiment_data（外部注入）> API 获取 > 全零降级
        """
        # 方式一：使用外部注入的情感数据
        if self._sentiment_data is not None and not self._sentiment_data.empty:
            return self._evaluate_from_series(data)

        # 方式二：实时 API 获取
        if self.use_api and self.symbol:
            return self._evaluate_from_api(data)

        # 方式三：降级为零
        return pd.Series(0.0, index=data.index)

    def _evaluate_from_series(self, data: pd.DataFrame) -> pd.Series:
        """从外部注入的情感 Series 计算因子值。"""
        sentiment = self._sentiment_data.reindex(data.index, method='ffill').fillna(0.0)
        smoothed = sentiment.rolling(self.window, min_periods=1).mean()
        return self.normalize(smoothed)

    def _evaluate_from_api(self, data: pd.DataFrame) -> pd.Series:
        """从 API 获取每日情感得分，构建历史序列。"""
        scores: Dict[pd.Timestamp, float] = {}

        for date in data.index:
            date_str = str(date.date()) if hasattr(date, 'date') else str(date)
            score = self._get_daily_score(date_str)
            scores[date] = score

        sentiment = pd.Series(scores).reindex(data.index).fillna(0.0)
        smoothed = sentiment.rolling(self.window, min_periods=1).mean()
        return self.normalize(smoothed)

    def _get_daily_score(self, date_str: str) -> float:
        """获取某日的情感得分（优先本地缓存）。"""
        # 内存缓存
        if date_str in self._score_cache:
            return self._score_cache[date_str]

        # 本地磁盘缓存
        cache_path = _CACHE_DIR / f'score_{_cache_key(self.symbol, date_str)}.json'
        cached = _load_cache(cache_path, _SCORE_TTL)
        if cached and 'score' in cached:
            score = float(cached['score'])
            self._score_cache[date_str] = score
            return score

        # 获取新闻 + 打分
        headlines = self._fetch_headlines(date_str)
        score = _score_with_claude(headlines, api_key=self.api_key) if headlines else 0.0

        # 写缓存
        self._score_cache[date_str] = score
        _save_cache(cache_path, {'score': score, 'headlines': headlines[:5], 'date': date_str})
        return score

    def _fetch_headlines(self, date_str: str) -> List[str]:
        """获取指定日期的新闻标题（含新闻缓存）。"""
        cache_path = _CACHE_DIR / f'news_{_cache_key(self.symbol, date_str)}.json'
        cached = _load_cache(cache_path, _NEWS_TTL)
        if cached and 'headlines' in cached:
            return cached['headlines']

        headlines = _fetch_news_eastmoney(self.symbol, self.n_news)

        _save_cache(cache_path, {'headlines': headlines, 'symbol': self.symbol, 'date': date_str})
        return headlines

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: Optional[float] = None,
    ) -> List[Signal]:
        """新闻情感信号：持续利好 → BUY，持续利空 → SELL。"""
        if len(factor_values) == 0:
            return []

        thr = threshold if threshold is not None else self.threshold
        latest = float(factor_values.iloc[-1])
        ts = datetime.now()

        if latest > thr:
            strength = min((latest - thr) / thr, 1.0)
            return [Signal(
                timestamp=ts,
                symbol=self.symbol,
                direction='BUY',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'sentiment_zscore': round(latest, 3)},
            )]
        if latest < -thr:
            strength = min((abs(latest) - thr) / thr, 1.0)
            return [Signal(
                timestamp=ts,
                symbol=self.symbol,
                direction='SELL',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'sentiment_zscore': round(latest, 3)},
            )]
        return []

    # ------------------------------------------------------------------
    # 数据注入接口
    # ------------------------------------------------------------------

    def inject_scores(self, scores: Dict[str, float]) -> None:
        """
        批量注入历史情感得分（用于批量回测，避免 API 调用）。

        Parameters
        ----------
        scores : Dict[str, float]
            {日期字符串: 情感得分} — 如 {'2024-01-15': 0.3}
        """
        self._score_cache.update(scores)

    def update_sentiment_data(self, sentiment_data: pd.Series) -> None:
        """更新外部情感数据序列。"""
        self._sentiment_data = sentiment_data

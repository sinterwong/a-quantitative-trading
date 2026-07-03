"""
core/factors/nlp.py — 新闻情感 LLM 因子

使用 DeepSeek API 对股票相关新闻标题进行情感打分，转换为 Factor 接口。

数据来源：
  - 东方财富新闻（AKShare stock_news_em()）
  - 同花顺财经新闻（AKShare stock_news_ths()，可选）

情感打分：
  - 调用 DeepSeek API（deepseek-flash 模型）
  - Prompt：返回 JSON {"score": <-1到1>, "reason": "..."}
  - 多条新闻打分取加权平均（近期新闻权重更高）

缓存策略：
  - LLMService 内部缓存（单条新闻 TTL）
  - 新闻情感评分缓存 TTL = 24 小时（本地 JSON）
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

    # 方式三：实时获取（需 DEEPSEEK_API_KEY）
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
    通过 DataGateway 获取股票新闻标题(底层东方财富/AkShare,享受熔断保护)。

    Parameters
    ----------
    symbol : str
        标的代码(如 '000001.SZ' / 'sh600519')
    n : int
        最多返回条数

    Returns
    -------
    List[str] — 新闻标题列表(最新在前)
    """
    try:
        from core.data_gateway import get_gateway
        return get_gateway().news_headlines(symbol, n=n)
    except Exception as e:
        logger.debug('[NewsSentimentFactor] 新闻获取失败: %s', e)
        return []


# ---------------------------------------------------------------------------
# LLM 情感打分层
# ---------------------------------------------------------------------------

def _score_with_deepseek(
    headlines: List[str],
    api_key: Optional[str] = None,
) -> float:
    """
    调用 LLM API 对新闻标题列表进行情感打分。

    Parameters
    ----------
    headlines : List[str]
        新闻标题列表
    api_key : str or None
        API Key（None 时从环境变量 DEEPSEEK_API_KEY 读取）

    Returns
    -------
    float — 情感得分 [-1, 1]（正=利好，负=利空）
    """
    if not headlines:
        return 0.0

    key = api_key or os.environ.get('DEEPSEEK_API_KEY', '')
    if not key:
        logger.debug('[NewsSentimentFactor] 未设置 DEEPSEEK_API_KEY，跳过 LLM 打分')
        return 0.0

    try:
        import sys as _sys
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/
        _backend_path = os.path.join(_repo_root, 'backend')
        if _backend_path not in _sys.path:
            _sys.path.insert(0, _backend_path)
        from services.llm.factory import create_llm_service

        llm = create_llm_service()

        scores: List[float] = []
        for i, headline in enumerate(headlines[:10]):
            try:
                # 近期新闻权重更高（指数衰减）
                recency_weight = 0.5 ** (i * 0.3)
                result = llm.analyze_news(headline.strip(), timeout=10)
                sentiment = getattr(result, 'sentiment', 'neutral')
                confidence = getattr(result, 'confidence', 0.5)
                # 映射: bullish→+1, bearish→-1, neutral→0，乘以 confidence
                if sentiment == 'bullish':
                    raw_score = confidence
                elif sentiment == 'bearish':
                    raw_score = -confidence
                else:
                    raw_score = 0.0
                scores.append(raw_score * recency_weight)
            except Exception as e:
                logger.debug('[NewsSentimentFactor] 单条新闻分析失败: %s', e)
                continue

        if not scores:
            return 0.0

        # 加权平均
        avg = float(np.mean(scores))
        return float(np.clip(avg, -1.0, 1.0))

    except Exception as e:
        logger.warning('[NewsSentimentFactor] LLMService 调用失败: %s', e)

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
        是否调用 DeepSeek API 实时获取（需 DEEPSEEK_API_KEY）。
        False = 仅使用 sentiment_data（无数据则全零）。
    window : int
        滚动均值平滑窗口（默认 5 天，减少单日噪声）
    n_news : int
        每次获取最多条数（默认 20）
    api_key : str or None
        DeepSeek API Key（None 时从环境变量读取）
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

        优先级（P1-9 升级）：
          sentiment_data（外部注入）
          → outputs/nlp_sentiment/{symbol}.parquet（批处理生成）
          → API 实时获取（需 use_api=True）
          → 全零降级
        """
        # 方式一：使用外部注入的情感数据
        if self._sentiment_data is not None and not self._sentiment_data.empty:
            return self._evaluate_from_series(data)

        # 方式二（P1-9）：从批处理 Parquet 缓存读取
        cached = self._load_parquet_cache()
        if cached is not None and not cached.empty:
            self._sentiment_data = cached
            return self._evaluate_from_series(data)

        # 方式三：实时 API 获取（不推荐生产用，延迟 + 成本）
        if self.use_api and self.symbol:
            return self._evaluate_from_api(data)

        # 方式四：降级为零
        return pd.Series(0.0, index=data.index)

    def _load_parquet_cache(self) -> Optional[pd.Series]:
        """
        从 outputs/nlp_sentiment/{symbol}.parquet 加载预计算的日频情感序列。

        返回 None 表示文件不存在或读取失败。
        """
        if not self.symbol:
            return None
        try:
            from pathlib import Path as _P
            cache_path = (
                _P(__file__).resolve().parent.parent.parent
                / 'outputs' / 'nlp_sentiment' / f'{self.symbol}.parquet'
            )
            if not cache_path.exists():
                return None
            df = pd.read_parquet(cache_path)
            if df.empty or 'score' not in df.columns:
                return None
            # date 列可能是 index 或 column
            if 'date' in df.columns:
                df = df.set_index('date')
            df.index = pd.to_datetime(df.index)
            return df['score'].astype(float)
        except Exception as exc:
            logger.warning('parquet cache load failed for %s: %s', self.symbol, exc)
            return None

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
        score = _score_with_deepseek(headlines, api_key=self.api_key) if headlines else 0.0

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

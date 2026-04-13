"""
service.py — LLM 统一服务入口
==============================

用法示例：

    from backend.services.llm import LLMService
    from backend.services.llm.providers import DeepSeekProvider

    # 初始化（自动从环境变量读取 API key）
    provider = DeepSeekProvider()
    llm = LLMService(provider)

    # 新闻情感分析（同步，3秒超时）
    result = llm.analyze_news(
        "央行宣布下调存款准备金率0.5个百分点"
    )
    print(result.sentiment, result.confidence, result.impact_sectors)

    # 异步模式（用于盘中，不阻塞主循环）
    future = llm.analyze_news_async("某行业利好政策发布...")
    # ... 主循环继续 ...
    result = future.result(timeout=5)

    # 批量分析（用于每日报告）
    results = llm.batch_news(news_list, timeout_per_item=8, max_concurrency=3)
"""

import os
import re
import json
import time
import logging
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Optional

from backend.services.llm.providers import LLMProvider
from backend.services.llm.cache import CacheManager
from backend.services.llm.prompts import SYSTEM_PROMPTS, USER_TEMPLATES

logger = logging.getLogger(__name__)

# ─── 数据模型 ────────────────────────────────


@dataclass
class NewsSentiment:
    """新闻情感分析结果"""
    sentiment: str           # "bullish" | "bearish" | "neutral"
    confidence: float        # 0.0-1.0
    impact_sectors: list[str] = field(default_factory=list)
    price_already_moved: Optional[bool] = None
    price_already_moved_reason: Optional[str] = None
    summary: str = ""
    raw_json: str = ""       # 原始 LLM 输出（用于调试）
    from_cache: bool = False


@dataclass
class PolicyAnalysis:
    """政策解读结果（v2 — 意图穿透 + 超预期 + Price-In）"""
    # 基础分类
    policy_type: str = ""        # 刺激型|托底型|收紧型|制度型|口号型
    real_intent: str = ""        # 底层真实意图（一句话）
    mandatory_vs_rhetoric: str = ""  # "不得不做X%|口头平衡Y%"
    # 超预期分析
    surprise_degree: float = 0.0  # 0-100
    surprise_reason: str = ""
    # 资源约束
    resource_constraint: list[str] = field(default_factory=list)
    # 影响板块
    affected_sectors: list[str] = field(default_factory=list)
    # Price-In 判断
    price_in_judgment: Optional[bool] = None  # True/False/null
    price_in_reason: str = ""
    # 落地节奏
    implementation_timeline: str = ""  # 立即执行|3个月内|3-6个月|6个月以上|规划中
    new_vs_continuation: str = ""  # new|continuation|unknown
    # 情绪结论
    market_sentiment: str = "neutral"  # bullish|bearish|neutral
    confidence: float = 0.0          # 0.0-1.0
    key_takeaway: str = ""           # 普通投资者最需要知道的1件事
    # 兜底
    raw_json: str = ""
    from_cache: bool = False


@dataclass
class MarketNarrative:
    """市场叙事生成结果"""
    market_theme: str = ""         # 一句话市场主线
    sentiment_temperature: str = "中性"  # 亢奋|偏热|中性|偏冷|恐慌
    temperature_score: float = 50.0  # 0-100
    main_line_sectors: list[str] = field(default_factory=list)
    secondary_line_sectors: list[str] = field(default_factory=list)
    key_events: list[str] = field(default_factory=list)
    volume_signal: str = ""        # 放量突破|缩量整理|量能萎缩|异常放大|null
    north_flow: str = ""            # 净流入X亿|null
    narrative: str = ""             # 完整叙事段落
    next_day_outlook: str = ""      # 次日展望
    risk_alert: str = ""            # 风险提示（可为空）
    opportunity_alert: str = ""     # 逆向机会（可为空）
    confidence: float = 0.0
    raw_json: str = ""
    from_cache: bool = False


# ─── 异常定义 ────────────────────────────────


class LLMError(RuntimeError):
    """LLM 服务异常（网络、超时、解析失败等）"""
    pass


class LLMParseError(LLMError):
    """LLM 返回内容无法解析为目标 JSON 格式"""
    pass


# ─── 核心服务 ────────────────────────────────


class LLMService:
    """
    统一 LLM 服务入口。

    支持：
    - 新闻情感分析（同步/异步/批量）
    - 政策文档解读
    - Provider 插拔（DeepSeek / Kimi / 其他 OpenAI 兼容接口）
    - 两级缓存（内存 LRU + 磁盘持久化）
    - 调用失败自动重试（指数退避）
    """

    def __init__(
        self,
        provider: LLMProvider,
        cache_dir: str = ".llm_cache",
        news_cache_ttl: int = 300,
        policy_cache_ttl: int = 3600,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
    ):
        """
        Args:
            provider: LLM Provider 实例
            cache_dir: 缓存目录路径
            news_cache_ttl: 新闻分析结果缓存 TTL（秒）
            policy_cache_ttl: 政策分析结果缓存 TTL（秒）
            max_retries: 调用失败最大重试次数
            retry_base_delay: 重试基础延迟（指数退避）
        """
        self.provider = provider
        self.cache = CacheManager(
            cache_dir=cache_dir,
            memory_capacity=200,
            default_ttl=news_cache_ttl,
        )
        self.news_cache_ttl = news_cache_ttl
        self.policy_cache_ttl = policy_cache_ttl
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

        # 从环境变量读取配置（覆盖默认值）
        if os.environ.get('NEWS_CACHE_TTL'):
            self.news_cache_ttl = int(os.environ['NEWS_CACHE_TTL'])
        if os.environ.get('POLICY_CACHE_TTL'):
            self.policy_cache_ttl = int(os.environ['POLICY_CACHE_TTL'])

        logger.info(
            "LLMService initialized: provider=%s cache=%s news_ttl=%ds policy_ttl=%ds",
            provider.name, cache_dir, self.news_cache_ttl, self.policy_cache_ttl,
        )

    @property
    def is_available(self) -> bool:
        """检查 Provider 是否可用"""
        return self.provider.is_available

    # ─── 新闻情感分析 ───────────────────────────

    def analyze_news(self, text: str, timeout: int = 10) -> NewsSentiment:
        """
        同步分析单条新闻情感。

        Args:
            text: 新闻文本（标题+正文内容）
            timeout: 超时时间（秒）

        Returns:
            NewsSentiment 结果对象
        """
        if not text or not text.strip():
            return NewsSentiment(sentiment='neutral', confidence=0.0, summary='Empty input')

        text = text.strip()

        # 缓存查询
        cached = self.cache.get(text, task='news_sentiment')
        if cached:
            try:
                parsed = json.loads(cached)
                result = self._parse_news_result(parsed, cached)
                result.from_cache = True
                logger.debug("News sentiment (cached): %s", result.sentiment)
                return result
            except Exception:
                # 缓存内容损坏，当作 miss 处理
                logger.warning("Cache corrupted for news, refetching")

        # 调用 LLM
        try:
            raw = self._call_llm(
                task='news_sentiment',
                content=text,
                timeout=timeout,
            )
            if not raw:
                logger.warning("LLM returned empty response for news sentiment")
                return NewsSentiment(sentiment='neutral', confidence=0.0, summary='Empty response from LLM')
            raw = raw.strip()
            if not raw:
                logger.warning("LLM returned whitespace-only response")
                return NewsSentiment(sentiment='neutral', confidence=0.0, summary='Whitespace-only response from LLM')
            parsed = self._parse_json(raw)
            result = self._parse_news_result(parsed, raw)

            # 写入缓存（只有置信度 > 0.3 才缓存，避免垃圾数据）
            # 存入时去掉 markdown 包装，确保缓存可直接 json.loads
            cache_value = raw.strip()
            if cache_value.startswith('```'):
                lines = cache_value.split('\n')
                cache_value = '\n'.join(lines[1:-1])  # 去掉首尾 ``` 行
                cache_value = cache_value.strip()
            if result.confidence > 0.3:
                self.cache.set(text, cache_value, task='news_sentiment', ttl=self.news_cache_ttl)

            logger.info(
                "News sentiment: sentiment=%s conf=%.2f sectors=%s [cache=%s]",
                result.sentiment, result.confidence, result.impact_sectors, result.from_cache,
            )
            return result

        except LLMError:
            raise
        except Exception as e:
            raise LLMParseError(f"Failed to parse LLM response: {e}") from e

    def analyze_news_async(self, text: str) -> concurrent.futures.Future:
        """
        异步分析新闻（不阻塞调用方）。
        返回 Future，可用 future.result(timeout=N) 获取结果。
        """
        if not text or not text.strip():
            # 空输入直接返回
            f = concurrent.futures.Future()
            f.set_result(NewsSentiment(sentiment='neutral', confidence=0.0))
            return f

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        return executor.submit(self.analyze_news, text.strip())

    def batch_news(
        self,
        news_list: list[dict],
        text_field: str = 'title',
        timeout_per_item: int = 8,
        max_concurrency: int = 3,
    ) -> list[dict]:
        """
        批量分析新闻列表（在每日报告等场景使用）。

        Args:
            news_list: [{"title": "...", "summary": "...", ...}, ...]
            text_field: 取哪一字段作为分析文本
            timeout_per_item: 每条新闻的超时时间
            max_concurrency: 最大并发数

        Returns:
            同顺序的 news_list，但每条 dict 增加了 "sentiment_result": NewsSentiment
        """
        if not news_list:
            return []

        texts = [item.get(text_field, '') or '' for item in news_list]

        # 缓存命中检查 + 缺失列表
        results: list[Optional[NewsSentiment]] = [None] * len(texts)
        missed_indices = []

        for i, text in enumerate(texts):
            if not text.strip():
                results[i] = NewsSentiment(sentiment='neutral', confidence=0.0, summary='Empty')
                continue
            cached = self.cache.get(text, task='news_sentiment')
            if cached:
                try:
                    parsed = json.loads(cached)
                    r = self._parse_news_result(parsed, cached)
                    r.from_cache = True
                    results[i] = r
                    continue
                except Exception:
                    pass
            missed_indices.append(i)

        logger.info("Batch news: total=%d cache_hit=%d miss=%d",
                     len(texts), len(texts) - len(missed_indices), len(missed_indices))

        # 并发获取缺失项（限流）
        if missed_indices:
            semaphore = threading.Semaphore(max_concurrency)

            def fetch_with_semaphore(idx: int) -> tuple[int, NewsSentiment]:
                text = texts[idx]
                semaphore.acquire()
                try:
                    return idx, self.analyze_news(text, timeout=timeout_per_item)
                finally:
                    semaphore.release()

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
                futures = {executor.submit(fetch_with_semaphore, i): i for i in missed_indices}
                for future in concurrent.futures.as_completed(futures, timeout=timeout_per_item * len(missed_indices) + 5):
                    try:
                        idx, sentiment = future.result()
                        results[idx] = sentiment
                    except Exception as e:
                        idx = futures[future]
                        logger.warning("batch item %d failed: %s", idx, e)
                        results[idx] = NewsSentiment(sentiment='neutral', confidence=0.0, summary=f'Error: {e}')

        # 将结果写回原始 dict
        output = []
        for i, item in enumerate(news_list):
            item = dict(item)  # 复制，不修改原始
            item['sentiment_result'] = results[i]
            output.append(item)

        return output

    # ─── 政策解读 ───────────────────────────────

    def analyze_policy(self, text: str, timeout: int = 15) -> PolicyAnalysis:
        """
        分析政策文档。

        Args:
            text: 政策文件全文或摘要
            timeout: 超时时间（秒）

        Returns:
            PolicyAnalysis 结果对象
        """
        if not text or not text.strip():
            return PolicyAnalysis(sentiment='neutral', policy_type='', key_signal='Empty input')

        text = text.strip()

        # 缓存
        cached = self.cache.get(text, task='policy_analysis')
        if cached:
            try:
                parsed = json.loads(cached)
                result = self._parse_policy_result(parsed, cached)
                result.from_cache = True
                return result
            except Exception:
                pass

        # LLM 调用
        raw = self._call_llm(task='policy_analysis', content=text, timeout=timeout)
        parsed = self._parse_json(raw)
        result = self._parse_policy_result(parsed, raw)

        if result.confidence >= 0.5:
            self.cache.set(text, raw, task='policy_analysis', ttl=self.policy_cache_ttl)

        logger.info(
            "Policy analysis: sentiment=%s type=%s surprise=%.0f%% sectors=%s confidence=%.2f",
            result.market_sentiment, result.policy_type,
            result.surprise_degree or 0.0, result.affected_sectors,
            result.confidence,
        )
        return result

    # ─── 内部方法 ───────────────────────────────

    def _call_llm(self, task: str, content: str, timeout: int) -> str:
        """
        调用 LLM，包含重试逻辑。
        """
        system_prompt = SYSTEM_PROMPTS.get(task, '')
        user_prompt = USER_TEMPLATES.get(task, '{content}').format(content=content)

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ]

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.provider.chat(
                    messages,
                    temperature=0.1,
                    max_tokens=1024,
                )
                return response.content

            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    # 过载时等更久（指数退避 + 额外等待）
                    base_delay = self.retry_base_delay * (2 ** attempt)
                    is_overload = 'overload' in str(e).lower() or '529' in str(e)
                    delay = base_delay * 3 if is_overload else base_delay
                    logger.warning(
                        "LLM call attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                        attempt + 1, self.max_retries + 1, task, e, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error("LLM call failed after %d attempts: %s", self.max_retries + 1, e)

        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")

    def _parse_news_result(self, parsed: dict, raw: str) -> NewsSentiment:
        """将 LLM JSON 解析为 NewsSentiment，带默认值保护"""
        sentiment_raw = parsed.get('sentiment', 'neutral')
        sentiment = sentiment_raw.lower() if sentiment_raw else 'neutral'
        if sentiment not in ('bullish', 'bearish', 'neutral'):
            sentiment = 'neutral'

        return NewsSentiment(
            sentiment=sentiment,
            confidence=float(parsed.get('confidence', 0.5) or 0.5),
            impact_sectors=parsed.get('impact_sectors') or [],
            price_already_moved=parsed.get('price_already_moved'),
            price_already_moved_reason=parsed.get('price_already_moved_reason'),
            summary=parsed.get('summary') or '',
            raw_json=raw,
            from_cache=False,
        )

    def _parse_json(self, raw: str) -> dict:
        """
        解析 LLM 返回的 JSON 字符串。
        LLM 有时会返回 markdown 包裹的 JSON（```json ... ```），
        此函数自动处理这种情况。
        """
        # 去掉 markdown code fence
        text = raw.strip()
        if text.startswith('```'):
            # 去掉 ```json 或 ```
            text = text.split('\n', 1)[-1]  # 去掉第一行（```json 或 ```）
            text = text.rsplit('```', 1)[0]  # 去掉最后一行的 ```
            text = text.strip()

        # 如果还有 ``` 在中间，当作普通字符处理后尝试解析
        # 去掉可能的行内 ``` 
        import re
        text = re.sub(r'^```[a-z]*\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)

        return json.loads(text)
        """将 LLM JSON 解析为 NewsSentiment，带默认值保护"""
        sentiment_raw = parsed.get('sentiment', 'neutral')
        sentiment = sentiment_raw.lower() if sentiment_raw else 'neutral'
        if sentiment not in ('bullish', 'bearish', 'neutral'):
            sentiment = 'neutral'

        return NewsSentiment(
            sentiment=sentiment,
            confidence=float(parsed.get('confidence', 0.5) or 0.5),
            impact_sectors=parsed.get('impact_sectors') or [],
            price_already_moved=parsed.get('price_already_moved'),
            price_already_moved_reason=parsed.get('price_already_moved_reason'),
            summary=parsed.get('summary') or '',
            raw_json=raw,
            from_cache=False,
        )

    def _parse_policy_result(self, parsed: dict, raw: str) -> PolicyAnalysis:
        """将 LLM JSON 解析为 PolicyAnalysis（v2）"""
        sentiment_raw = parsed.get('market_sentiment', parsed.get('sentiment', 'neutral'))
        sentiment = sentiment_raw.lower() if sentiment_raw else 'neutral'
        if sentiment not in ('bullish', 'bearish', 'neutral'):
            sentiment = 'neutral'

        return PolicyAnalysis(
            policy_type=parsed.get('policy_type') or '',
            real_intent=parsed.get('real_intent') or '',
            mandatory_vs_rhetoric=parsed.get('mandatory_vs_rhetoric') or '',
            surprise_degree=_safe_float(parsed.get('surprise_degree', 0)),
            surprise_reason=parsed.get('surprise_reason') or '',
            resource_constraint=parsed.get('resource_constraint') or [],
            affected_sectors=parsed.get('affected_sectors') or [],
            price_in_judgment=parsed.get('price_in_judgment'),
            price_in_reason=parsed.get('price_in_reason') or '',
            implementation_timeline=parsed.get('implementation_timeline') or '',
            new_vs_continuation=parsed.get('new_vs_continuation') or 'unknown',
            market_sentiment=sentiment,
            confidence=_safe_float(parsed.get('confidence', 0.5)),
            key_takeaway=parsed.get('key_takeaway') or '',
            raw_json=raw,
            from_cache=False,
        )

    def analyze_market_narrative(self, market_data: str, timeout: int = 15) -> MarketNarrative:
        """
        生成市场叙事（每日报告用）。

        Args:
            market_data: 市场行情摘要文本（可以是板块涨跌、资金流向、北向数据等）
            timeout: 超时时间（秒）

        Returns:
            MarketNarrative 结果
        """
        if not market_data or not market_data.strip():
            return MarketNarrative(
                market_theme='无数据',
                narrative='市场数据不足，无法生成叙事',
                confidence=0.0,
            )

        text = market_data.strip()

        # 缓存查询
        cached = self.cache.get(text, task='market_narrative')
        if cached:
            try:
                parsed = json.loads(cached)
                result = self._parse_narrative_result(parsed, cached)
                result.from_cache = True
                return result
            except Exception:
                pass

        # LLM 调用
        raw = self._call_llm(task='market_narrative', content=text, timeout=timeout)
        parsed = self._parse_json(raw)
        result = self._parse_narrative_result(parsed, raw)

        # 缓存
        if result.confidence >= 0.4:
            self.cache.set(text, raw, task='market_narrative', ttl=self.policy_cache_ttl)

        logger.info(
            "Market narrative: theme=%s temp=%s score=%.0f confidence=%.2f",
            result.market_theme, result.sentiment_temperature,
            result.temperature_score, result.confidence,
        )
        return result

    def _parse_narrative_result(self, parsed: dict, raw: str) -> MarketNarrative:
        """将 LLM JSON 解析为 MarketNarrative"""
        return MarketNarrative(
            market_theme=parsed.get('market_theme') or '市场主题不明确',
            sentiment_temperature=parsed.get('sentiment_temperature', '中性'),
            temperature_score=_safe_float(parsed.get('temperature_score', 50.0)),
            main_line_sectors=parsed.get('main_line_sectors') or [],
            secondary_line_sectors=parsed.get('secondary_line_sectors') or [],
            key_events=parsed.get('key_events') or [],
            volume_signal=parsed.get('volume_signal') or '',
            north_flow=parsed.get('north_flow') or '',
            narrative=parsed.get('narrative') or '',
            next_day_outlook=parsed.get('next_day_outlook') or '',
            risk_alert=parsed.get('risk_alert') or '',
            opportunity_alert=parsed.get('opportunity_alert') or '',
            confidence=_safe_float(parsed.get('confidence', 0.5)),
            raw_json=raw,
            from_cache=False,
        )


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None

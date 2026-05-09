# -*- coding: utf-8 -*-
"""
quote_source_manager.py — 行情数据源组合管理器
================================================

按 (数据类型, 市场) 自动路由到最佳数据源：
  - 腾讯: A 股/港股/美股/指数（主源）
  - 新浪: A 股行情 + 分钟 K 线（互补源）

路由规则：
  实时行情: 腾讯 → 新浪
  日 K 线: 腾讯 → 新浪
  分钟 K 线 A 股: 新浪（腾讯不支持）
  分钟 K 线 港股: 腾讯（新浪不可靠）

Usage:
  from core.quote_source_manager import get_quote_manager
  mgr = get_quote_manager()
  quote = mgr.fetch_quote('sh600519')
  bars = mgr.fetch_daily_kline('hk00700', days=30)
  minutes = mgr.fetch_minute_kline('sh600519', period='15m')
"""

import logging
import threading
from typing import Dict, List, Optional

import pandas as pd

from .quote_data_source import QuoteData, QuoteDataSource, detect_market, tencent_quote_to_quote_data

logger = logging.getLogger('quote_source_manager')


# ─── 路由表 ──────────────────────────────────────────────────────────────────

# (数据类型, 市场) → (主源, 备源)
# 数据类型: 'realtime' | 'daily' | 'minute'
# 市场: 'A' | 'INDEX' | 'HK' | 'US'
# 源名: 'tencent' | 'sina' | None
_ROUTE = {
    ('realtime', 'A'):     ('tencent', 'sina'),
    ('realtime', 'INDEX'): ('tencent', 'sina'),
    ('realtime', 'HK'):    ('tencent', 'sina'),
    ('realtime', 'US'):    ('tencent', None),
    ('daily', 'A'):        ('tencent', 'sina'),
    ('daily', 'INDEX'):    ('tencent', 'sina'),
    ('daily', 'HK'):       ('tencent', None),
    ('daily', 'US'):       ('tencent', None),
    ('minute', 'A'):       ('sina', None),      # 新浪主源（腾讯不支持 A 股分钟 K）
    ('minute', 'INDEX'):   ('sina', None),
    ('minute', 'HK'):      ('tencent', None),    # 腾讯港股分钟 K
    ('minute', 'US'):      (None, None),          # 不支持
}


# ─── 腾讯源适配器 ────────────────────────────────────────────────────────────


class _TencentAdapter(QuoteDataSource):
    """将 TencentQuoteDataSource 适配为 QuoteDataSource ABC（转换返回类型）"""

    name = "TencentQuoteDataSource"

    def __init__(self, inner):
        self._inner = inner

    def fetch_quote(self, symbol: str) -> Optional[QuoteData]:
        tq = self._inner.fetch_quote(symbol)
        if tq is None:
            return None
        return tencent_quote_to_quote_data(tq)

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteData]:
        tqs = self._inner.fetch_quotes(symbols)
        return {sym: tencent_quote_to_quote_data(q) for sym, q in tqs.items()}

    def fetch_daily_kline(self, symbol: str, days: int = 120, adjust: str = "qfq") -> pd.DataFrame:
        return self._inner.fetch_daily_kline(symbol, days=days, adjust=adjust)

    def fetch_minute_kline(self, symbol: str, period: str = "15m", limit: int = 100) -> pd.DataFrame:
        return self._inner.fetch_minute_kline(symbol, period=period, limit=limit)

    def supported_markets(self) -> List[str]:
        return self._inner.supported_markets()


# ─── 管理器 ──────────────────────────────────────────────────────────────────


class QuoteSourceManager:
    """
    行情数据源组合管理器。

    按路由表自动选择最佳数据源，主源不可用时切换备源。
    """

    def __init__(
        self,
        tencent: Optional[QuoteDataSource] = None,
        sina: Optional[QuoteDataSource] = None,
    ):
        self._sources: Dict[str, QuoteDataSource] = {}
        self._init_lock = threading.Lock()

        if tencent is not None:
            self._sources['tencent'] = tencent
        if sina is not None:
            self._sources['sina'] = sina

    def _get_source(self, name: str) -> Optional[QuoteDataSource]:
        """延迟获取数据源实例"""
        if name in self._sources:
            return self._sources[name]

        with self._init_lock:
            if name in self._sources:
                return self._sources[name]

            if name == 'tencent':
                from .tencent_quote_source import TencentQuoteDataSource
                self._sources['tencent'] = _TencentAdapter(TencentQuoteDataSource())
            elif name == 'sina':
                from .sina_quote_source import SinaQuoteDataSource
                self._sources['sina'] = SinaQuoteDataSource()
            else:
                return None

            return self._sources[name]

    def _is_available(self, source_name: str) -> bool:
        """检查数据源熔断器状态"""
        from .circuit_breaker import get_breaker
        cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
        return cb.allow()

    def _record_success(self, source_name: str) -> None:
        from .circuit_breaker import get_breaker
        cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
        cb.on_success()

    def _record_failure(self, source_name: str) -> None:
        from .circuit_breaker import get_breaker
        cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
        cb.on_failure()

    def _try_route(
        self,
        data_type: str,
        market: str,
        method_name: str,
        *args,
        **kwargs,
    ):
        """
        按路由表尝试数据源。

        Returns:
            方法返回值，或 None/空 DataFrame（全部失败时）
        """
        route = _ROUTE.get((data_type, market))
        if route is None:
            logger.warning("无路由: data_type=%s market=%s", data_type, market)
            return None

        primary, fallback = route
        empty_result = pd.DataFrame() if 'kline' in method_name else None

        for source_name in [primary, fallback]:
            if source_name is None:
                continue
            if not self._is_available(source_name):
                logger.info("[QuoteManager] %s 熔断中，跳过", source_name)
                continue

            source = self._get_source(source_name)
            if source is None:
                continue

            try:
                method = getattr(source, method_name)
                result = method(*args, **kwargs)

                # 判断是否成功
                if isinstance(result, pd.DataFrame):
                    if not result.empty:
                        self._record_success(source_name)
                        return result
                elif isinstance(result, dict):
                    if result:
                        self._record_success(source_name)
                        return result
                elif result is not None:
                    self._record_success(source_name)
                    return result

                # 结果为空，尝试下一个源
                logger.info("[QuoteManager] %s 返回空结果", source_name)

            except Exception as e:
                self._record_failure(source_name)
                logger.warning("[QuoteManager] %s 调用失败: %s", source_name, e)

        return empty_result

    # ── 公开 API ──────────────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[QuoteData]:
        """获取单只标的实时行情"""
        market = detect_market(symbol)
        return self._try_route('realtime', market, 'fetch_quote', symbol)

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteData]:
        """
        批量获取实时行情（按市场分组路由）。

        Returns:
            {原始symbol: QuoteData}
        """
        if not symbols:
            return {}

        # 按市场分组
        groups: Dict[str, List[str]] = {}
        for sym in symbols:
            market = detect_market(sym)
            groups.setdefault(market, []).append(sym)

        result: Dict[str, QuoteData] = {}

        for market, market_symbols in groups.items():
            route = _ROUTE.get(('realtime', market))
            if route is None:
                continue

            primary, fallback = route

            for source_name in [primary, fallback]:
                if source_name is None:
                    continue
                if not self._is_available(source_name):
                    continue

                source = self._get_source(source_name)
                if source is None:
                    continue

                try:
                    quotes = source.fetch_quotes(market_symbols)
                    if quotes:
                        self._record_success(source_name)
                        result.update(quotes)
                        break
                except Exception as e:
                    self._record_failure(source_name)
                    logger.warning("[QuoteManager] 批量获取失败 %s: %s", source_name, e)

        return result

    def fetch_daily_kline(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取日 K 线数据"""
        market = detect_market(symbol)
        return self._try_route('daily', market, 'fetch_daily_kline', symbol, days, adjust)

    def fetch_minute_kline(
        self,
        symbol: str,
        period: str = "15m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """获取分钟 K 线数据"""
        market = detect_market(symbol)
        return self._try_route('minute', market, 'fetch_minute_kline', symbol, period, limit)


# ─── 单例 ────────────────────────────────────────────────────────────────────

_manager: Optional[QuoteSourceManager] = None
_manager_lock = threading.Lock()


def get_quote_manager() -> QuoteSourceManager:
    """获取全局 QuoteSourceManager 单例"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = QuoteSourceManager()
    return _manager


def reset_quote_manager() -> None:
    """重置全局单例（用于测试）"""
    global _manager
    with _manager_lock:
        _manager = None

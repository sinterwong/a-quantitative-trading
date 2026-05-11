# -*- coding: utf-8 -*-
"""
quote_source_manager.py — 行情数据源组合管理器
================================================

按 (数据类型, 市场) 自动路由到最佳数据源，并对多个来源的 QuoteData 进行字段级合并。

路由规则：
  实时行情(A/INDEX/HK/US): 腾讯主 → 新浪备 → 字段级合并（取两家之长）
  日 K 线(A/INDEX/HK/US): 腾讯主 → 新浪备 → 字段级合并
  分钟 K 线(A/INDEX):     新浪主（腾讯不支持 A 股分钟 K）
  分钟 K 线(港股):        腾讯主（新浪港股分钟 K 不可靠）
  板块排名:               东方财富主（唯一来源）
  板块成分股:             东方财富主（唯一来源）

Usage:
  from core.quote_source_manager import get_quote_manager

  mgr = get_quote_manager()

  # 单只股票：自动合并腾讯+Sina 字段
  quote = mgr.fetch_quote('sh600519')
  print(quote.amount, quote.field_source('amount'))  # amount 来自腾讯

  # 批量股票
  quotes = mgr.fetch_quotes(['sh600519', 'sz000001'])

  # 板块排名
  sectors = mgr.fetch_sector_rankings(limit=50)

  # 板块成分股
  constituents = mgr.fetch_sector_constituents('BK0716', limit=10)
"""

import logging
import threading
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .quote_data_source import (
    QuoteData, QuoteDataSource,
    SectorData, SectorConstituentData,
    detect_market,
)

logger = logging.getLogger('quote_source_manager')


# ─── 路由表 ──────────────────────────────────────────────────────────────────

# (数据类型, 市场) → (主源, 备源)
# 数据类型: 'realtime' | 'daily' | 'minute' | 'sector' | 'constituents'
# 市场: 'A' | 'INDEX' | 'HK' | 'US' | '' (用于 sector/constituents)
# 源名: 'tencent' | 'sina' | 'eastmoney' | None
_ROUTE: Dict[Tuple[str, str], Tuple[str, Optional[str]]] = {
    # ── 个股实时行情 ──────────────────────────────────────────────────────────
    ('realtime', 'A'):     ('tencent', 'sina'),
    ('realtime', 'INDEX'): ('tencent', 'sina'),
    ('realtime', 'HK'):    ('tencent', 'sina'),
    ('realtime', 'US'):    ('tencent', None),

    # ── 日 K 线 ─────────────────────────────────────────────────────────────
    ('daily', 'A'):        ('tencent', 'sina'),
    ('daily', 'INDEX'):    ('tencent', 'sina'),
    ('daily', 'HK'):       ('tencent', None),
    ('daily', 'US'):       ('tencent', None),

    # ── 分钟 K 线 ────────────────────────────────────────────────────────────
    ('minute', 'A'):       ('sina', None),       # 腾讯不支持 A 股分钟 K
    ('minute', 'INDEX'):   ('sina', None),
    ('minute', 'HK'):      ('tencent', None),    # 腾讯港股分钟 K 唯一可用
    ('minute', 'US'):      (None, None),

    # ── 板块数据 ─────────────────────────────────────────────────────────────
    ('sector', ''):        ('eastmoney', None),  # 板块排名（无市场概念）
    ('constituents', ''):  ('eastmoney', None),  # 板块成分股
}


# ─── 管理器 ──────────────────────────────────────────────────────────────────


class QuoteSourceManager:
    """
    行情数据源组合管理器。

    核心能力：
      1. 按路由表自动选择数据源（主 → 备）
      2. 对多个来源的 QuoteData 进行字段级合并（取两家之长）
      3. 统一封装板块排名、成分股等区块数据接口
      4. 内置熔断器，自动跳过不可用源

    所有 HTTP 请求均通过本管理器，调用方不直接访问数据源实例。
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
                self._sources['tencent'] = TencentQuoteDataSource()
            elif name == 'sina':
                from .sina_quote_source import SinaQuoteDataSource
                self._sources['sina'] = SinaQuoteDataSource()
            elif name == 'eastmoney':
                from .eastmoney_sector_source import EastmoneySectorSource
                self._sources['eastmoney'] = EastmoneySectorSource()
            else:
                return None

            return self._sources[name]

    def _is_available(self, source_name: str) -> bool:
        """检查数据源熔断器状态"""
        try:
            from .circuit_breaker import get_breaker
            cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
            return cb.allow()
        except Exception:
            return True  # 熔断器未初始化时默认可用

    def _record_success(self, source_name: str) -> None:
        try:
            from .circuit_breaker import get_breaker
            cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
            cb.on_success()
        except Exception:
            pass

    def _record_failure(self, source_name: str) -> None:
        try:
            from .circuit_breaker import get_breaker
            cb = get_breaker(f'{source_name}_quote', failure_threshold=3, cooldown_seconds=120.0)
            cb.on_failure()
        except Exception:
            pass

    def _try_sources_for_quote(
        self,
        symbol: str,
    ) -> Optional[QuoteData]:
        """
        尝试多个来源获取个股行情，并进行字段级合并。

        Returns:
            合并后的 QuoteData，或 None（全部失败）
        """
        market = detect_market(symbol)
        route = _ROUTE.get(('realtime', market))

        if route is None:
            return None

        primary, fallback = route
        primary_q: Optional[QuoteData] = None
        fallback_q: Optional[QuoteData] = None

        # 1. 尝试主源
        if primary and self._is_available(primary):
            try:
                src = self._get_source(primary)
                if src:
                    q = src.fetch_quote(symbol)
                    if q and q.is_valid:
                        primary_q = q
                        self._record_success(primary)
            except Exception as e:
                self._record_failure(primary)
                logger.warning("[QuoteManager] %s.fetch_quote(%s) 失败: %s", primary, symbol, e)

        # 2. 尝试备源
        if fallback and self._is_available(fallback):
            try:
                src = self._get_source(fallback)
                if src:
                    q = src.fetch_quote(symbol)
                    if q and q.is_valid:
                        fallback_q = q
                        self._record_success(fallback)
            except Exception as e:
                self._record_failure(fallback)
                logger.warning("[QuoteManager] %s.fetch_quote(%s) 失败: %s", fallback, symbol, e)

        # 3. 字段级合并
        if primary_q and fallback_q:
            # 两个来源都有 → 字段级合并（腾讯优先）
            return primary_q.merge(fallback_q, priority=primary)
        elif primary_q:
            return primary_q
        elif fallback_q:
            return fallback_q
        else:
            return None

    def _try_sources_for_quotes(
        self,
        symbols: List[str],
    ) -> Dict[str, QuoteData]:
        """
        批量获取个股行情，同股多源时字段级合并。

        Returns:
            {symbol: QuoteData}
        """
        if not symbols:
            return {}

        # 按市场分组，同市场一起路由
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
            primary_results: Dict[str, QuoteData] = {}
            fallback_results: Dict[str, QuoteData] = {}

            # 批量请求主源
            if primary and self._is_available(primary):
                try:
                    src = self._get_source(primary)
                    if src:
                        batch = src.fetch_quotes(market_symbols)
                        if batch:
                            primary_results = {k: v for k, v in batch.items() if v.is_valid}
                            self._record_success(primary)
                except Exception as e:
                    self._record_failure(primary)
                    logger.warning("[QuoteManager] %s.fetch_quotes 失败: %s", primary, e)

            # 批量请求备源
            if fallback and self._is_available(fallback):
                try:
                    src = self._get_source(fallback)
                    if src:
                        batch = src.fetch_quotes(market_symbols)
                        if batch:
                            fallback_results = {k: v for k, v in batch.items() if v.is_valid}
                            self._record_success(fallback)
                except Exception as e:
                    self._record_failure(fallback)
                    logger.warning("[QuoteManager] %s.fetch_quotes 失败: %s", fallback, e)

            # 字段级合并
            all_symbols = set(primary_results) | set(fallback_results)
            for sym in all_symbols:
                p = primary_results.get(sym)
                f = fallback_results.get(sym)
                if p and f:
                    result[sym] = p.merge(f, priority=primary)
                elif p:
                    result[sym] = p
                elif f:
                    result[sym] = f

        return result

    # ── 公开 API: 个股行情 ────────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[QuoteData]:
        """
        获取单只标的实时行情（字段级合并）。

        腾讯和新浪同时尝试，合并后返回字段最全的 QuoteData。
        通过 field_source('xxx') 可查询每个字段的数据来源。
        """
        return self._try_sources_for_quote(symbol)

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteData]:
        """
        批量获取实时行情（字段级合并）。

        Returns:
            {原始symbol: QuoteData}
        """
        return self._try_sources_for_quotes(symbols)

    # ── 公开 API: K 线 ────────────────────────────────────────────────────────

    def fetch_daily_kline(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取日 K 线数据（主 → 备 failover，不做字段合并）。

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        market = detect_market(symbol)
        route = _ROUTE.get(('daily', market))
        if route is None:
            return pd.DataFrame()

        primary, fallback = route
        empty = pd.DataFrame()

        for src_name in [primary, fallback]:
            if src_name is None:
                continue
            if not self._is_available(src_name):
                continue
            try:
                src = self._get_source(src_name)
                if src is None:
                    continue
                df = src.fetch_daily_kline(symbol, days, adjust)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    self._record_success(src_name)
                    return df
            except Exception as e:
                self._record_failure(src_name)
                logger.warning("[QuoteManager] %s.fetch_daily_kline(%s) 失败: %s", src_name, symbol, e)

        return empty

    def fetch_minute_kline(
        self,
        symbol: str,
        period: str = "15m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        获取分钟 K 线数据。

        Returns:
            DataFrame with columns: datetime, open, high, low, close, volume
        """
        market = detect_market(symbol)
        route = _ROUTE.get(('minute', market))
        if route is None:
            return pd.DataFrame()

        primary, fallback = route
        empty = pd.DataFrame()

        for src_name in [primary, fallback]:
            if src_name is None:
                continue
            if not self._is_available(src_name):
                continue
            try:
                src = self._get_source(src_name)
                if src is None:
                    continue
                df = src.fetch_minute_kline(symbol, period, limit)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    self._record_success(src_name)
                    return df
            except Exception as e:
                self._record_failure(src_name)
                logger.warning("[QuoteManager] %s.fetch_minute_kline(%s) 失败: %s", src_name, symbol, e)

        return empty

    # ── 公开 API: 板块数据 ────────────────────────────────────────────────────

    def fetch_sector_rankings(self, limit: int = 100) -> List[SectorData]:
        """
        获取板块排名（涨跌幅 + 资金流）。

        数据来源：东方财富（唯一来源）
        缓存：内部文件缓存（1小时有效）

        Returns:
            List[SectorData]，按涨跌幅排名
        """
        try:
            src = self._get_source('eastmoney')
            if src is None:
                return []
            data = src.fetch_sector_rankings(limit)
            if data:
                self._record_success('eastmoney')
                return data
        except Exception as e:
            self._record_failure('eastmoney')
            logger.warning("[QuoteManager] fetch_sector_rankings 失败: %s", e)

        return []

    def fetch_sector_constituents(
        self,
        bk_code: str,
        limit: int = 20,
    ) -> List[SectorConstituentData]:
        """
        获取指定板块的成分股列表（含实时行情）。

        数据来源：东方财富（唯一来源）

        Args:
            bk_code: 板块代码，如 'BK0716'（东方财富格式）或 'SINA_GNhwqc'（新浪格式）
            limit: 返回数量（按涨幅排序取 top N）

        Returns:
            List[SectorConstituentData]
        """
        try:
            src = self._get_source('eastmoney')
            if src is None:
                return []
            data = src.fetch_sector_constituents(bk_code, limit)
            if data:
                self._record_success('eastmoney')
                return data
        except Exception as e:
            self._record_failure('eastmoney')
            logger.warning("[QuoteManager] fetch_sector_constituents(%s) 失败: %s", bk_code, e)

        return []


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

# -*- coding: utf-8 -*-
"""
data_gateway.gateway — 统一数据网关

整个系统对外网数据的唯一出口。所有 provider 平级,通过 capability 矩阵 +
健康度评分动态路由,可合并数据(Quote/Fundamentals)做字段级互补合并。

公开 API:
    gw = get_gateway()
    gw.quote(symbol) / gw.quotes([symbol, ...])
    gw.kline(symbol, interval='daily', days=120)
    gw.fundamentals(symbol)
    gw.sectors(limit=50)
    gw.sector_constituents(code, limit=20)
    gw.north_flow()
    gw.market_index(code)
    gw.macro(indicator)

横切关注点:
  - 熔断: 接入 core.circuit_breaker,失败累计触发硬开关
  - 健康度: HealthTracker 滑窗评分,做软排序
  - 缓存: MemoryCache(Quote 30s, Fundamentals/Sector 60s, MarketIndex 60s)
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .cache import MemoryCache
from .capabilities import Capability, MacroIndicator, Market
from .health import HealthTracker, get_health_tracker
from .merge import Candidate, merge_field_level
from .providers.base import Provider, ProviderError
from .schemas import (
    Fundamentals, MarketIndexSnapshot, NorthFlow,
    Quote, SectorConstituent, SectorRanking,
)
from .symbols import detect_market

logger = logging.getLogger("data_gateway.gateway")


# ─── 缓存 TTL 默认值 ──────────────────────────────────────────────────────────

_DEFAULT_TTL = {
    Capability.QUOTE: 30.0,
    Capability.FUNDAMENTALS: 60.0,
    Capability.FUNDAMENTALS_HISTORY: 86400.0,  # 季度数据，24h 缓存足够
    Capability.SECTOR_RANKING: 3600.0,
    Capability.SECTOR_CONSTITUENTS: 60.0,
    Capability.NORTH_FLOW: 60.0,
    Capability.MARKET_INDEX: 60.0,
    Capability.MACRO: 86400.0,
}


# ─── 熔断器辅助 ────────────────────────────────────────────────────────────────


def _breaker_for(provider_name: str, capability: Capability):
    """获取/创建 (provider × capability) 熔断器。"""
    try:
        from core.circuit_breaker import get_breaker
        name = f"gw_{provider_name}_{capability.value}"
        return get_breaker(name, failure_threshold=3, cooldown_seconds=120.0)
    except Exception:
        return None


# ─── Gateway ──────────────────────────────────────────────────────────────────


class DataGateway:
    """统一数据网关。

    Provider 注册后,gateway 根据请求的 capability + market 自动:
      1. 筛选 supports() 返回 True 的 provider
      2. 过滤熔断 open 的 provider
      3. 按健康度 + 字段权威(可合并数据)/单分数(其他)排序
      4. 可合并 → 并发问 top-K 家,字段级 merge
         不可合并 → 顺序问,第一个成功即返回
    """

    def __init__(
        self,
        *,
        health: Optional[HealthTracker] = None,
        cache: Optional[MemoryCache] = None,
        max_parallel: int = 4,
    ):
        self._providers: Dict[str, Provider] = {}
        self._health = health or get_health_tracker()
        self._cache = cache or MemoryCache(default_ttl=30.0)
        self._max_parallel = max_parallel
        self._lock = threading.Lock()
        self._last_provenance: Dict[str, Dict[str, str]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallel, thread_name_prefix="gw"
        )

    # ── 注册 ─────────────────────────────────────────────────────────────────

    def register_provider(self, provider: Provider) -> None:
        with self._lock:
            self._providers[provider.name] = provider

    def unregister_provider(self, name: str) -> None:
        with self._lock:
            self._providers.pop(name, None)

    def providers(self) -> List[Provider]:
        with self._lock:
            return list(self._providers.values())

    # ── 选源 ─────────────────────────────────────────────────────────────────

    def _candidates_for(
        self,
        capability: Capability,
        market: Optional[Market],
    ) -> List[Tuple[Provider, float]]:
        """返回 [(provider, health_score)] 按分数降序,已过滤熔断 open。"""
        out: List[Tuple[Provider, float]] = []
        for p in self.providers():
            decl = p.declare()
            if capability not in decl.capabilities:
                continue
            if market is not None and not p.supports(capability, market):
                continue
            # 熔断硬开关
            cb = _breaker_for(p.name, capability)
            if cb is not None and not cb.allow():
                continue
            score = self._health.score(
                p.name, capability, priority_hint=decl.priority_hint,
            )
            out.append((p, score))
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out

    # ── 调用单 provider 并记录健康度 ──────────────────────────────────────

    def _invoke(
        self,
        provider: Provider,
        capability: Capability,
        fn_name: str,
        *args,
        **kwargs,
    ) -> Any:
        """调用 provider 的 fetch_* 方法,自动记录健康度 + 触发熔断。

        约定:
          - fn 返回有效值(None/empty 之外)→ success
          - fn 抛 ProviderError 或其他异常 → failure
          - empty 视为"本源无此数据",不计为失败也不计为成功
        """
        fn = getattr(provider, fn_name)
        cb = _breaker_for(provider.name, capability)
        t0 = perf_counter()
        try:
            result = fn(*args, **kwargs)
        except ProviderError as exc:
            elapsed = (perf_counter() - t0) * 1000
            self._health.record(provider.name, capability, success=False, latency_ms=elapsed)
            if cb is not None:
                cb.on_failure()
            logger.debug("provider %s.%s 失败: %s", provider.name, fn_name, exc)
            return None
        except Exception as exc:
            elapsed = (perf_counter() - t0) * 1000
            self._health.record(provider.name, capability, success=False, latency_ms=elapsed)
            if cb is not None:
                cb.on_failure()
            logger.warning("provider %s.%s 异常: %s", provider.name, fn_name, exc)
            return None

        elapsed = (perf_counter() - t0) * 1000
        is_empty = (
            result is None
            or (isinstance(result, dict) and not result)
            or (isinstance(result, list) and not result)
            or (isinstance(result, pd.DataFrame) and result.empty)
        )
        # 空结果不计为失败(可能本源就没数据),也不计为成功
        if not is_empty:
            self._health.record(provider.name, capability, success=True, latency_ms=elapsed)
            if cb is not None:
                cb.on_success()
        return result

    # ── 并发问多家 + 字段级 merge ──────────────────────────────────────────

    def _merged_fetch(
        self,
        capability: Capability,
        market: Optional[Market],
        fn_name: str,
        skip_fields: tuple[str, ...] = (),
        *args,
        **kwargs,
    ) -> Tuple[Any, Dict[str, str]]:
        """并发调用 top-K provider,字段级合并。

        Returns:
            (merged_obj_or_None, provenance_dict)
        """
        candidates = self._candidates_for(capability, market)
        if not candidates:
            return None, {}

        top = candidates[: self._max_parallel]
        futures = {
            self._executor.submit(self._invoke, p, capability, fn_name, *args, **kwargs): (p, score)
            for p, score in top
        }

        results: List[Candidate] = []
        for fut in as_completed(futures):
            provider, score = futures[fut]
            obj = fut.result()
            if obj is None:
                continue
            authority = provider.field_authority().get(capability, {})
            results.append(Candidate(provider.name, obj, health=score, authority=authority))

        if not results:
            return None, {}
        merged, prov = merge_field_level(results, skip_fields=skip_fields)
        return merged, prov

    # ── 顺序 failover ────────────────────────────────────────────────────────

    def _sequential_fetch(
        self,
        capability: Capability,
        market: Optional[Market],
        fn_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """按健康度降序逐个尝试,第一个非空返回 (result, provider_name)。"""
        for provider, _score in self._candidates_for(capability, market):
            result = self._invoke(provider, capability, fn_name, *args, **kwargs)
            if result is None:
                continue
            if isinstance(result, pd.DataFrame) and result.empty:
                continue
            if isinstance(result, (list, dict)) and not result:
                continue
            return result, provider.name
        return None, None

    # ──────────────────────────────────────────────────────────────────────────
    # 公开 API
    # ──────────────────────────────────────────────────────────────────────────

    def quote(self, symbol: str) -> Optional[Quote]:
        """获取单只标的实时行情(字段级合并多源)。"""
        cache_key = f"quote:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        market = detect_market(symbol)
        merged, prov = self._merged_fetch(
            Capability.QUOTE, market, "fetch_quote",
            ("symbol", "code", "market", "name", "currency"),
            symbol,
        )
        if merged is not None:
            self._cache.set(cache_key, merged, _DEFAULT_TTL[Capability.QUOTE])
            self._last_provenance[cache_key] = prov
        return merged

    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """批量行情。按 provider 批量能力调用,然后逐个字段级合并。"""
        if not symbols:
            return {}

        # 缓存命中
        result: Dict[str, Quote] = {}
        missing: List[str] = []
        for s in symbols:
            cached = self._cache.get(f"quote:{s}")
            if cached is not None:
                result[s] = cached
            else:
                missing.append(s)
        if not missing:
            return result

        # 按 market 分组(不同 provider 对市场覆盖不一致)
        by_market: Dict[Market, List[str]] = {}
        for s in missing:
            by_market.setdefault(detect_market(s), []).append(s)

        for market, syms in by_market.items():
            # 找所有支持(QUOTE, market)的 provider,并发批量请求
            candidates = self._candidates_for(Capability.QUOTE, market)
            if not candidates:
                continue
            top = candidates[: self._max_parallel]

            futures = {
                self._executor.submit(
                    self._invoke, p, Capability.QUOTE, "fetch_quotes", syms,
                ): (p, score)
                for p, score in top
            }

            # {symbol: [Candidate, ...]}
            buckets: Dict[str, List[Candidate]] = {s: [] for s in syms}
            for fut in as_completed(futures):
                provider, score = futures[fut]
                batch = fut.result() or {}
                authority = provider.field_authority().get(Capability.QUOTE, {})
                for s, q in batch.items():
                    buckets.setdefault(s, []).append(
                        Candidate(provider.name, q, health=score, authority=authority)
                    )

            for s, cands in buckets.items():
                if not cands:
                    continue
                merged, prov = merge_field_level(
                    cands, skip_fields=("symbol", "code", "market", "name", "currency"),
                )
                if merged is None:
                    continue
                result[s] = merged
                self._cache.set(f"quote:{s}", merged, _DEFAULT_TTL[Capability.QUOTE])
                self._last_provenance[f"quote:{s}"] = prov

        return result

    def kline(
        self,
        symbol: str,
        interval: str = "daily",
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        """K 线数据（failover，不合并）。

        interval 参数决定路由到 KLINE_DAILY 还是 KLINE_MINUTE Capability，
        进而触发对应的 fetch_kline_daily / fetch_kline_minute 方法。"""
        market = detect_market(symbol)
        is_minute = interval in ("1m", "5m", "15m", "30m", "60m")
        cap = Capability.KLINE_MINUTE if is_minute else Capability.KLINE_DAILY
        cache_key = f"kline:{symbol}:{interval}:{days}:{adjust}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # 按 Capability 选择对应方法，避免在一个方法内同时处理日K和分钟K
        if is_minute:
            result, _ = self._sequential_fetch(
                cap, market, "fetch_kline_minute",
                symbol, interval=interval, limit=limit,
            )
        else:
            result, _ = self._sequential_fetch(
                cap, market, "fetch_kline_daily",
                symbol, days=days, adjust=adjust, limit=limit,
            )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        if not df.empty:
            ttl = 60.0 if is_minute else 300.0
            self._cache.set(cache_key, df, ttl)
        return df

    def fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        """基本面(字段级合并)。"""
        cache_key = f"fundamentals:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        market = Market.GLOBAL  # 基本面数据跨市场统一，用 GLOBAL 查所有 provider
        merged, prov = self._merged_fetch(
            Capability.FUNDAMENTALS, market, "fetch_fundamentals",
            ("symbol", "name", "industry", "sector"),
            symbol,
        )
        if merged is not None:
            # PE/PB 由腾讯实时行情补充（akshare 财报接口不含此字段）
            if merged.pe_ttm <= 0 or merged.pb <= 0:
                quote = self.quote(symbol)
                if quote is not None:
                    if merged.pe_ttm <= 0 and quote.pe_ttm > 0:
                        merged.pe_ttm = quote.pe_ttm
                    if merged.pb <= 0 and quote.pb > 0:
                        merged.pb = quote.pb
            self._cache.set(cache_key, merged, _DEFAULT_TTL[Capability.FUNDAMENTALS])
            self._last_provenance[cache_key] = prov
        return merged

    def sectors(self, limit: int = 100) -> List[SectorRanking]:
        cache_key = f"sectors:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result, _ = self._sequential_fetch(
            Capability.SECTOR_RANKING, Market.GLOBAL, "fetch_sectors", limit,
        )
        out = result or []
        if out:
            self._cache.set(cache_key, out, _DEFAULT_TTL[Capability.SECTOR_RANKING])
        return out

    def sector_constituents(
        self,
        code: str,
        limit: int = 20,
    ) -> List[SectorConstituent]:
        cache_key = f"constituents:{code}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result, _ = self._sequential_fetch(
            Capability.SECTOR_CONSTITUENTS, Market.GLOBAL,
            "fetch_sector_constituents", code, limit,
        )
        out = result or []
        if out:
            self._cache.set(cache_key, out, _DEFAULT_TTL[Capability.SECTOR_CONSTITUENTS])
        return out

    def north_flow(self) -> Optional[NorthFlow]:
        cache_key = "north_flow"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result, _ = self._sequential_fetch(
            Capability.NORTH_FLOW, Market.GLOBAL, "fetch_north_flow",
        )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.NORTH_FLOW])
        return result

    def market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        cache_key = f"market_index:{code}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # 指数市场:腾讯支持 usSPY/hkHSI(对应 US/HK),其他归 GLOBAL
        market = detect_market(code)
        result, _ = self._sequential_fetch(
            Capability.MARKET_INDEX, market, "fetch_market_index", code,
        )
        if result is None:
            # 兜底:用 GLOBAL 路由(yfinance)
            result, _ = self._sequential_fetch(
                Capability.MARKET_INDEX, Market.GLOBAL, "fetch_market_index", code,
            )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.MARKET_INDEX])
        return result

    def macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        """indicator: MacroIndicator enum (PMI / M2 / CREDIT)。"""
        cache_key = f"macro:{indicator.value}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result, _ = self._sequential_fetch(
            Capability.MACRO, Market.GLOBAL, "fetch_macro", indicator,
        )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        if not df.empty:
            self._cache.set(cache_key, df, _DEFAULT_TTL[Capability.MACRO])
        return df

    def fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """基本面历史时序（日频，前向填充季报）。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        """
        cache_key = f"fundamentals_history:{symbol}:{start}:{end}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result, _ = self._sequential_fetch(
            Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
            "fetch_fundamentals_history", symbol, start, end,
        )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        if not df.empty:
            self._cache.set(
                cache_key, df,
                _DEFAULT_TTL.get(Capability.FUNDAMENTALS_HISTORY, 86400.0),
            )
        return df

    # ── 监控 / 调试 ──────────────────────────────────────────────────────────

    def provenance(self, key: str) -> Dict[str, str]:
        """查询某次合并的字段来源记录。

        key 形如 'quote:sh600519' / 'fundamentals:sh600519'。
        """
        return dict(self._last_provenance.get(key, {}))

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def invalidate_fundamentals_history(self, symbol: str) -> None:
        """清除指定标的的基本面历史缓存（精确清除，不影响其他标的缓存）。"""
        # fundamentals_history 缓存键格式：fundamentals_history:{symbol}:{start}:{end}
        # 用 prefix 匹配清除该标的所有变体缓存键
        prefix = f"fundamentals_history:{symbol}:"
        self._cache._store.pop(prefix, None)  # exact match (no start/end)
        # 清除所有以该 prefix 开头的键（不同 start/end 组合）
        with self._cache._lock:
            to_remove = [k for k in self._cache._store if k.startswith(prefix)]
            for k in to_remove:
                self._cache._store.pop(k, None)


# ─── 默认注册 + 单例 ──────────────────────────────────────────────────────────


_gateway: Optional[DataGateway] = None
_gateway_lock = threading.Lock()


def _build_default_gateway() -> DataGateway:
    from .providers.akshare import AkshareProvider
    from .providers.eastmoney import EastmoneyProvider
    from .providers.sina import SinaProvider
    from .providers.tencent import TencentProvider
    from .providers.yfinance import YfinanceProvider

    gw = DataGateway()
    for cls in (TencentProvider, SinaProvider, EastmoneyProvider,
                YfinanceProvider, AkshareProvider):
        try:
            gw.register_provider(cls())
        except Exception as exc:
            logger.warning("注册 %s 失败: %s", cls.__name__, exc)
    return gw


def get_gateway() -> DataGateway:
    """获取全局 DataGateway 单例(默认注册全部 provider)。"""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = _build_default_gateway()
    return _gateway


def reset_gateway(gw: Optional[DataGateway] = None) -> None:
    """重置/替换全局单例(测试用)。"""
    global _gateway
    with _gateway_lock:
        _gateway = gw


__all__ = ["DataGateway", "get_gateway", "reset_gateway"]

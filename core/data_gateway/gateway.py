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
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .cache import MemoryCache, ParquetDiskCache, TieredCache
from .capabilities import (
    Capability, MacroIndicator, Market, RoutingStrategy, get_policy,
)
from .health import HealthTracker, get_health_tracker
from .merge import (
    Candidate, DIVERGENCE_SUFFIX, _field_divergence, merge_field_level,
)
from .providers.base import Provider, ProviderError
from .schemas import (
    BalanceSheet, Fundamentals, MarketIndexSnapshot, NorthFlow,
    Quote, SectorConstituent, SectorRanking, StockProfile,
)
from .symbols import detect_market

logger = logging.getLogger("data_gateway.gateway")


# ─── 缓存 TTL 默认值 ──────────────────────────────────────────────────────────
# 此表值担两个角色：
#   1) _cache_set 写 L1 时调用方未显式传 ttl 的回退值
#   2) _cache_get 读 L2 时的 disk_ttl 阈值（仅 _PERSISTENT_CAPS 中 cap 生效）
# 对 KLINE 这类 L1 ttl 由调用方 hardcode（60/300s）但 L2 阈值需要更宽松（盘后
# 24h/1h 都算新鲜）的 cap，此处的值以 L2 阈值为准，L1 端调用方显式覆盖即可。

_DEFAULT_TTL = {
    Capability.QUOTE: 30.0,
    Capability.FUNDAMENTALS: 60.0,
    Capability.FUNDAMENTALS_HISTORY: 86400.0,  # 季度数据，24h 缓存足够
    Capability.BALANCE_SHEET: 86400.0,         # 季报数据，24h 缓存足够
    Capability.KLINE_DAILY: 86400.0,           # L2 disk_ttl；L1 ttl 由 kline() hardcode 300s
    Capability.SECTOR_RANKING: 3600.0,
    Capability.SECTOR_CONSTITUENTS: 60.0,
    Capability.NORTH_FLOW: 60.0,
    Capability.MARKET_INDEX: 60.0,
    Capability.MACRO: 86400.0,
    Capability.MARGIN_FLOW: 14400.0,           # 融资融券日频，4h 缓存(收盘后更新)
    Capability.FUND_FLOW: 14400.0,              # 资金流日频，4h 缓存(收盘后更新)
    Capability.NEWS_HEADLINES: 1800.0,         # 新闻标题，30min 缓存
    Capability.DUPONT: 86400.0,                 # 杜邦分析，季报数据，24h 缓存
    Capability.OPERATION: 86400.0,              # 运营能力，季报数据，24h 缓存
    Capability.DIVIDEND: 86400.0,              # 分红记录，季报数据，24h 缓存
}


# ─── L2 持久化白名单 ──────────────────────────────────────────────────────────
# 仅历史 / 慢变 DataFrame 落 Parquet 盘，实时 Quote / 列表数据只走 L1。
# 进程重启后 L2 可避免重拉昂贵的多源历史时序。
#
# 不变量：本集合 ⊆ _DEFAULT_TTL.keys()——_cache_get 用 _DEFAULT_TTL[cap] 作
# L2 disk_ttl，缺键会让 L2 fallback 静默失效（KLINE 在 review 中被发现过此 bug）。
# 文件尾部有 assert 锁住。

_PERSISTENT_CAPS: set = {
    Capability.KLINE_DAILY,
    # 注：KLINE_MINUTE 故意不在白名单——分钟 K 数据量大、变化快、L2 价值低；
    # test_kline_daily_persists_kline_minute_does_not 锁定此设计。
    Capability.FUNDAMENTALS_HISTORY,
    Capability.MARGIN_FLOW,
    Capability.FUND_FLOW,
    Capability.NORTH_FLOW,      # 仅 north_flow_history 走 L2,实时 snapshot 不会
    Capability.MACRO,
    # 注：BALANCE_SHEET 是 dataclass 不是 DataFrame，TieredCache.set 的
    # isinstance 守卫会跳过，加入白名单也不会落盘——故不在此处登记。
}


# 模块加载期自检：若新增 cap 入 _PERSISTENT_CAPS 但忘了 _DEFAULT_TTL，直接报错。
assert _PERSISTENT_CAPS <= set(_DEFAULT_TTL.keys()), (
    "_PERSISTENT_CAPS 中以下 cap 缺 _DEFAULT_TTL 登记，"
    "会导致 L2 disk_ttl=None / 回读失效: "
    f"{_PERSISTENT_CAPS - set(_DEFAULT_TTL.keys())}"
)


# ─── 时序切片辅助 ─────────────────────────────────────────────────────────────
# G3: 缓存"全量已知时序"，出口处按用户参数切片。这样同一 symbol 的
#     不同时间窗口请求共享一份缓存，大幅降低对外网压力。

# 各能力的"宽抓取"默认值——首次 miss 时拉这个量，覆盖未来其它窗口请求
_WIDE_FETCH = {
    Capability.KLINE_DAILY: {"days": 730, "limit": 730},          # 2 年
    Capability.KLINE_MINUTE: {"limit": 500},                       # 500 根
    Capability.NORTH_FLOW: {"days": 1825},                         # 5 年
    # G3: news 也走"全量缓存 + 出口切片"——同 symbol 不同 n 共享同一缓存。
    # EM kuaixun 单页上限 20，AkShare 财联社可给更多，50 已足够日常显示需求。
    Capability.NEWS_HEADLINES: {"n": 50},
}


def _slice_by_range(
    df: pd.DataFrame, start: Optional[str], end: Optional[str],
) -> pd.DataFrame:
    """按 start/end 字符串切 DatetimeIndex DataFrame。空表/无索引时安全返回。"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    out = df
    if start is not None:
        try:
            out = out[out.index >= pd.Timestamp(start)]
        except (ValueError, TypeError):
            pass
    if end is not None:
        try:
            out = out[out.index <= pd.Timestamp(end)]
        except (ValueError, TypeError):
            pass
    return out


def _tail_by_n(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """取末尾 n 行。空表 / n<=0 时安全返回。"""
    if df is None or df.empty or n <= 0:
        return df if df is not None else pd.DataFrame()
    return df.tail(n)


def _default_cache_dir() -> str:
    """L2 落盘路径：环境变量优先 → 项目 data/cache/data_gateway/。"""
    import os as _os
    env = _os.environ.get("TRADING_DATA_GATEWAY_CACHE_DIR")
    if env:
        return env
    # 项目根目录的 data/cache/data_gateway
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    return _os.path.join(repo_root, "data", "cache", "data_gateway")


# ─── G5: NewsItem 归一与时间排序辅助 ─────────────────────────────────────────

# 常见标题前缀（去除后做 dedupe key）。所有形式 "【XXX】" 都已统一剥掉，
# 这里仅列具体业务前缀以兜底正则之外的纯文本前缀。
_NEWS_TITLE_PREFIX_PATTERN = re.compile(r"^[【\[][^】\]]{1,12}[】\]]\s*")


def _news_dedupe_key(item: Any) -> str:
    """把一条 NewsItem 的标题归一为 dedupe key。

    归一化：strip → 去 "【...】"/"[...]" 前缀 → 全角空格转半角 →
    多空白折叠 → 末尾的"。"/"."统一去掉。两源对同事件的常见写法
    如 "【快讯】央行降准" 与 "央行降准。" 会归到同一 key。
    """
    if not hasattr(item, "title"):
        return ""
    title = str(item.title or "").strip()
    if not title:
        return ""
    title = _NEWS_TITLE_PREFIX_PATTERN.sub("", title)
    title = title.replace("　", " ")    # 全角空格
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip("。.")
    return title


def _news_has_ts(item: Any) -> bool:
    ts = getattr(item, "timestamp", None)
    return isinstance(ts, datetime)


def _news_ts_epoch(item: Any) -> float:
    """timestamp epoch（秒）；缺失返回 0.0，排序时配合 has_ts 一起用。"""
    ts = getattr(item, "timestamp", None)
    if not isinstance(ts, datetime):
        return 0.0
    try:
        return ts.timestamp()
    except (OSError, ValueError, OverflowError):
        return 0.0


# ─── 熔断器辅助 ────────────────────────────────────────────────────────────────


def _breaker_for(provider_name: str, capability: Capability):
    """获取/创建 (provider × capability) 熔断器。"""
    try:
        from core.circuit_breaker import get_breaker
        name = f"gw_{provider_name}_{capability.value}"
        return get_breaker(name, failure_threshold=3, cooldown_seconds=120.0)
    except Exception:
        return None


def _stale_seconds(ts: datetime) -> int:
    """从 dataclass.timestamp 计算缓存陈旧度（秒）。

    时区无关：用本地 datetime.now() 与 ts 直接相减，假设 provider 写入时也
    用本地时间（schemas.py 默认值就是 datetime.now()）。负值（时钟漂移）
    归零，避免下游策略误判。
    """
    try:
        delta = (datetime.now() - ts).total_seconds()
    except (TypeError, ValueError):
        return 0
    return max(0, int(delta))


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
        cache: Optional[Any] = None,
        cache_dir: Optional[str] = None,
        enable_disk_cache: bool = True,
        max_parallel: int = 4,
    ):
        """
        Args:
            cache: 可注入自定义缓存(MemoryCache 或 TieredCache)。
                None 时按 enable_disk_cache 自动构建。
            cache_dir: L2 落盘目录，None 时取 _default_cache_dir()。
            enable_disk_cache: 是否启用 L2 落盘(测试可关闭)。
        """
        self._providers: Dict[str, Provider] = {}
        self._health = health or get_health_tracker()
        if cache is not None:
            self._cache = cache
        elif enable_disk_cache:
            disk = ParquetDiskCache(cache_dir or _default_cache_dir())
            self._cache = TieredCache(memory=MemoryCache(default_ttl=30.0), disk=disk)
        else:
            self._cache = MemoryCache(default_ttl=30.0)
        self._max_parallel = max_parallel
        self._lock = threading.Lock()
        self._last_provenance: Dict[str, Dict[str, str]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_parallel, thread_name_prefix="gw"
        )
        # profile() 用的独立池：与 self._executor 分离以避免嵌套提交死锁
        # （外层切片任务又会向 _executor 提交内层 fan-out 调用）。
        # 懒创建，避免不调 profile() 的场景白白占线程。
        self._profile_executor: Optional[ThreadPoolExecutor] = None
        self._profile_executor_lock = threading.Lock()

    # ── 缓存读写辅助(自动应用 L2 落盘白名单) ──────────────────────────────

    def _cache_get(self, cap: Capability, key: str) -> Optional[Any]:
        """对持久化 capability 自动走 L2 fallback。"""
        if isinstance(self._cache, TieredCache) and cap in _PERSISTENT_CAPS:
            return self._cache.get(key, disk_ttl=_DEFAULT_TTL.get(cap))
        return self._cache.get(key)

    def _cache_set(
        self,
        cap: Capability,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
    ) -> None:
        """对持久化 capability 自动写 L2(仅 DataFrame)。"""
        ttl = ttl if ttl is not None else _DEFAULT_TTL.get(cap, 60.0)
        if isinstance(self._cache, TieredCache):
            self._cache.set(key, value, ttl=ttl, persistent=cap in _PERSISTENT_CAPS)
        else:
            self._cache.set(key, value, ttl=ttl)

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
                # Prometheus 旁路：记录跳过原因，便于运维定位降级链路。
                try:
                    from core.metrics import get_registry
                    get_registry().observe_provider(
                        provider=p.name,
                        capability=capability.value,
                        status='circuit_open',
                        latency_ms=0.0,
                    )
                except Exception:
                    pass
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

    # ── 字段级矛盾检测 ────────────────────────────────────────────────────

    @staticmethod
    def _divergence_threshold() -> float:
        """读取 TRADING_DIVERGENCE_THRESHOLD（默认 0.05）。

        非法值（无法 float() 解析）回退到默认，避免环境配置错误把整个数据
        流污染。每次调用都读环境，便于测试用 monkeypatch 改值。
        """
        raw = os.environ.get("TRADING_DIVERGENCE_THRESHOLD", "0.05")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.05

    def _warn_divergences(
        self,
        capability: Capability,
        fn_name: str,
        provenance: Dict[str, str],
        identifier: str,
    ) -> None:
        """扫描 provenance 中的 `<field>__divergence` 元数据，超阈值写 WARNING。"""
        threshold = self._divergence_threshold()
        for key, val in provenance.items():
            if not key.endswith(DIVERGENCE_SUFFIX):
                continue
            try:
                pct = float(val)
            except (TypeError, ValueError):
                continue
            if pct > threshold:
                field = key[: -len(DIVERGENCE_SUFFIX)]
                logger.warning(
                    "字段差异超阈值 capability=%s fn=%s id=%s field=%s pct=%.4f threshold=%.4f",
                    capability.name, fn_name, identifier, field, pct, threshold,
                )

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
        # capability + 调用方上下文留在 provenance 之外的层。为日志可读性，
        # 这里用 `*args` 第一个元素（通常是 symbol / code）做 identifier。
        ident = str(args[0]) if args else "-"
        self._warn_divergences(capability, fn_name, prov, ident)
        return merged, prov

    # ── 时序数据多源列级合并 (G1) ──────────────────────────────────────────

    def _merged_history_fetch(
        self,
        capability: Capability,
        market: Optional[Market],
        fn_name: str,
        *args,
        ffill: bool = True,
        **kwargs,
    ) -> Tuple[pd.DataFrame, Dict[str, str]]:
        """并发拉 top-K 源，按 (索引并集 × 列级 score 胜出) 合并时序 DataFrame。

        合并规则：
          - 行索引：所有源 DatetimeIndex 的并集，升序
          - 列：所有源出现过的列的并集
          - 同一(行, 列)单元格：按 score 降序，首个非 NaN 值胜出
            (即 score 高的源覆盖低的，但低源能填高源缺失的行/列)
          - ffill=True 时整体前向填充（适合季报这类稀疏时序的日频回填）

        Args:
            capability / market / fn_name / *args / **kwargs: 与 _sequential_fetch 一致
            ffill: 是否对合并结果做前向填充。
                季报类数据(fundamentals_history) → True
                K 线 / 资金流 / 北向 → False（缺失日期通常是真缺失）

        Returns:
            (merged_df, provenance):
              merged_df: 合并后的 DataFrame，空 DataFrame 表示无可用源
              provenance: {column_name: provider_name} 记录每列首贡献源
        """
        candidates = self._candidates_for(capability, market)
        if not candidates:
            return pd.DataFrame(), {}

        top = candidates[: self._max_parallel]
        futures = {
            self._executor.submit(
                self._invoke, p, capability, fn_name, *args, **kwargs,
            ): (p, score)
            for p, score in top
        }

        results: List[Tuple[str, float, pd.DataFrame]] = []
        for fut in as_completed(futures):
            provider, score = futures[fut]
            obj = fut.result()
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                results.append((provider.name, score, obj))

        if not results:
            return pd.DataFrame(), {}

        results.sort(key=lambda x: x[1], reverse=True)

        # 索引并集
        union_idx = results[0][2].index
        for _, _, df in results[1:]:
            union_idx = union_idx.union(df.index)
        union_idx = union_idx.sort_values()

        # 列并集（保持首次出现顺序，让高分源的列在前）
        all_cols: List[str] = []
        seen: set = set()
        for _, _, df in results:
            for c in df.columns:
                if c not in seen:
                    all_cols.append(c)
                    seen.add(c)

        merged = pd.DataFrame(index=union_idx)
        provenance: Dict[str, str] = {}

        for col in all_cols:
            sources = [(name, df[col]) for name, _, df in results if col in df.columns]
            if not sources:
                continue
            provenance[col] = sources[0][0]    # score 最高的贡献源
            top_name, top_series = sources[0]
            top_aligned = top_series.reindex(union_idx)
            col_series = top_aligned
            # combine_first：self 非 NaN 留 self，self NaN 用 other —— 正符合
            # 「score 高的胜，低的补缺」语义
            for _name, s in sources[1:]:
                col_series = col_series.combine_first(s.reindex(union_idx))
            merged[col] = col_series

            # 字段级矛盾检测：同 (row, col) 多源都给值时取 top vs 其他源里
            # 与 top 最大差异的那个，记为该列的 divergence_pct。
            # 非数值列降级为"是否相等"的二元判定，避免 .abs() 在 StringDtype 上抛错。
            if len(sources) >= 2:
                top_is_numeric = pd.api.types.is_numeric_dtype(top_aligned)
                max_div = 0.0
                for _name, other in sources[1:]:
                    other_aligned = other.reindex(union_idx)
                    overlap_idx = top_aligned.notna() & other_aligned.notna()
                    if not overlap_idx.any():
                        continue
                    a_vals = top_aligned[overlap_idx]
                    b_vals = other_aligned[overlap_idx]
                    if top_is_numeric and pd.api.types.is_numeric_dtype(b_vals):
                        try:
                            denom = pd.concat(
                                [a_vals.abs(), b_vals.abs()], axis=1,
                            ).max(axis=1)
                            diff = (a_vals - b_vals).abs() / denom.where(denom > 0)
                            col_max = float(diff.max(skipna=True))
                        except (TypeError, ValueError):
                            continue
                        if col_max != col_max:  # NaN
                            continue
                    else:
                        col_max = 0.0 if bool((a_vals == b_vals).all()) else 1.0
                    if col_max > max_div:
                        max_div = col_max
                if max_div > 0.0:
                    provenance[f"{col}{DIVERGENCE_SUFFIX}"] = f"{max_div:.4f}"

        if ffill:
            merged = merged.ffill()

        # 尽量保持数值列的 numeric dtype（reindex/combine_first 可能引入 object）。
        # pandas ≥ 2.2 起 errors='ignore' 已移除（3.0 直接抛 ValueError），
        # 改用 try/except 保留"能转就转、转不了就保留 object"的旧语义。
        for col in merged.columns:
            if merged[col].dtype == object:
                try:
                    merged[col] = pd.to_numeric(merged[col], errors="raise")
                except (ValueError, TypeError):
                    pass

        ident = str(args[0]) if args else "-"
        self._warn_divergences(capability, fn_name, provenance, ident)
        return merged, provenance

    # ── 多源 list 归一去重 (G5) ─────────────────────────────────────────────

    def _merged_list_fetch(
        self,
        capability: Capability,
        market: Optional[Market],
        fn_name: str,
        *args,
        **kwargs,
    ) -> Tuple[List[Any], Dict[str, str]]:
        """并发拉 top-K 源的 list，归一去重 + 时间倒序合并。

        当前唯一消费者：`news_headlines`，元素类型 NewsItem。

        规则：
          - 并发问全部候选源（list 是有限规模，不必限 top-K：失败的源被
            _invoke 静默忽略；多 1-2 源额外成本可控）
          - 元素若有 `title` 属性 → 归一化标题做 dedupe key，保留首次出现
            的条目（首次按 source health 高→低）
          - 元素若有 `timestamp` 属性且为 datetime → 按 ts 倒序排在前；
            缺 ts 的条目按 source health 顺序紧随其后
          - 不在此截断 n，由 gateway 出口处 tail(n)
          - prov_dict: {source_name: 该源贡献条数}

        Returns:
            (merged_list, {source: n_contributed_unique})
            无可用源 → ([], {})
        """
        candidates = self._candidates_for(capability, market)
        if not candidates:
            return [], {}

        futures = {
            self._executor.submit(
                self._invoke, p, capability, fn_name, *args, **kwargs,
            ): (p, score)
            for p, score in candidates
        }

        # 收集 (score, provider_name, list)，按 score 降序排
        results: List[Tuple[float, str, List[Any]]] = []
        for fut in as_completed(futures):
            provider, score = futures[fut]
            obj = fut.result()
            if isinstance(obj, list) and obj:
                results.append((score, provider.name, obj))

        if not results:
            return [], {}
        results.sort(key=lambda x: x[0], reverse=True)

        merged: List[Any] = []
        provenance: Dict[str, int] = {}
        seen_keys: set = set()

        for _score, prov_name, items in results:
            contrib = 0
            for item in items:
                key = _news_dedupe_key(item)
                if not key:
                    # 没有标题等可去重信号 → 直接加入（罕见兜底）
                    merged.append(item)
                    contrib += 1
                    continue
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(item)
                contrib += 1
            if contrib:
                provenance[prov_name] = contrib

        # 排序：有 ts 的按 ts 倒序在前；无 ts 的紧随其后（保留原顺序作 stable
        # tiebreaker，即按 source health 高→低 + 该源内原顺序）
        merged.sort(
            key=lambda it: (
                0 if _news_has_ts(it) else 1,            # 有 ts 的在前
                -_news_ts_epoch(it),                      # ts 越大越靠前
            )
        )
        # provenance dict 值 int → str 便于与其它策略统一类型
        return merged, {k: str(v) for k, v in provenance.items()}

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

    # ── G4: 策略声明驱动的统一分派器 ───────────────────────────────────────
    def _route(
        self,
        capability: Capability,
        market: Optional[Market],
        fn_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Any, Dict[str, str]]:
        """根据 ROUTING_POLICY 把 (capability, fn_name) 分派到对应聚合原语。

        统一返回 (value, prov_dict)：
          - FAILOVER: value 来自首个非空源；prov_dict = {"_provider": name}
            或 {} (无可用源)。比直接调用 _sequential_fetch 多保留了源名，便于
            profile / 调试时复盘单源选源。
          - MERGE_FIELDS: 走 _merged_fetch，prov_dict 为字段→源映射。
          - MERGE_FRAMES: 走 _merged_history_fetch，prov_dict 为列→源映射。
          - MERGE_LISTS: G5 实现；当前 raise NotImplementedError 防误用。

        强制查表：未登记 → KeyError，避免新加 capability 时静默走默认分支
        造成行为不一致。
        """
        policy = get_policy(capability, fn_name)
        strat = policy.strategy
        if strat is RoutingStrategy.FAILOVER:
            result, provider_name = self._sequential_fetch(
                capability, market, fn_name, *args, **kwargs,
            )
            prov: Dict[str, str] = {"_provider": provider_name} if provider_name else {}
            return result, prov
        if strat is RoutingStrategy.MERGE_FIELDS:
            return self._merged_fetch(
                capability, market, fn_name, policy.skip_fields,
                *args, **kwargs,
            )
        if strat is RoutingStrategy.MERGE_FRAMES:
            return self._merged_history_fetch(
                capability, market, fn_name, *args,
                ffill=policy.ffill, **kwargs,
            )
        if strat is RoutingStrategy.MERGE_LISTS:
            return self._merged_list_fetch(
                capability, market, fn_name, *args, **kwargs,
            )
        raise ValueError(f"未知 RoutingStrategy: {strat!r}")

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
        merged, prov = self._route(
            Capability.QUOTE, market, "fetch_quote", symbol,
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

            quotes_skip = get_policy(
                Capability.QUOTE, "fetch_quotes",
            ).skip_fields
            for s, cands in buckets.items():
                if not cands:
                    continue
                merged, prov = merge_field_level(cands, skip_fields=quotes_skip)
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
        # G3: cache key 仅含结构性参数(symbol/interval/adjust)，不含时间窗口
        cache_key = f"kline:{symbol}:{interval}:{adjust}"
        cached = self._cache_get(cap, cache_key)
        if cached is not None and not cached.empty:
            n = limit if is_minute else days
            return _tail_by_n(cached, n)

        # G1: K 线走多源列级合并(腾讯/新浪/Baostock OHLCV 互补 + 高分胜出)
        # G3: 首次拉取时用"宽窗口"，覆盖未来其他窗口请求
        # G4: 策略 (MERGE_FRAMES, ffill=False) 由 ROUTING_POLICY 声明
        wide = _WIDE_FETCH.get(cap, {})
        if is_minute:
            fetch_limit = max(limit, wide.get("limit", limit))
            merged, prov = self._route(
                cap, market, "fetch_kline_minute",
                symbol, interval=interval, limit=fetch_limit,
            )
        else:
            fetch_days = max(days, wide.get("days", days))
            fetch_limit = max(limit, wide.get("limit", limit))
            merged, prov = self._route(
                cap, market, "fetch_kline_daily",
                symbol, days=fetch_days, adjust=adjust, limit=fetch_limit,
            )
        if not merged.empty:
            ttl = 60.0 if is_minute else 300.0
            self._cache_set(cap, cache_key, merged, ttl=ttl)
            self._last_provenance[cache_key] = prov
        return _tail_by_n(merged, limit if is_minute else days)

    def fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        """基本面(字段级合并)。"""
        cache_key = f"fundamentals:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached.stale_seconds = _stale_seconds(cached.timestamp)
            return cached

        market = Market.GLOBAL  # 基本面数据跨市场统一，用 GLOBAL 查所有 provider
        merged, prov = self._route(
            Capability.FUNDAMENTALS, market, "fetch_fundamentals", symbol,
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
        result, prov = self._route(
            Capability.SECTOR_RANKING, Market.A, "fetch_sectors", limit,
        )
        out = result or []
        if out:
            self._cache.set(cache_key, out, _DEFAULT_TTL[Capability.SECTOR_RANKING])
            self._last_provenance[cache_key] = prov
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
        result, prov = self._route(
            Capability.SECTOR_CONSTITUENTS, Market.GLOBAL,
            "fetch_sector_constituents", code, limit,
        )
        out = result or []
        if out:
            self._cache.set(cache_key, out, _DEFAULT_TTL[Capability.SECTOR_CONSTITUENTS])
            self._last_provenance[cache_key] = prov
        return out

    def north_flow(self) -> Optional[NorthFlow]:
        cache_key = "north_flow"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result, prov = self._route(
            Capability.NORTH_FLOW, Market.GLOBAL, "fetch_north_flow",
        )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.NORTH_FLOW])
            self._last_provenance[cache_key] = prov
        return result

    def north_flow_history(self, days: int = 252) -> pd.DataFrame:
        """北向资金日频历史(顺序 failover)。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex,列 north_flow(亿元/天)。
            空 DataFrame 表示无可用源。
        """
        # G3: cache key 不含 days，存最长可得序列
        cache_key = "north_flow_history"
        cached = self._cache_get(Capability.NORTH_FLOW, cache_key)
        if cached is not None and not cached.empty:
            return _tail_by_n(cached, days)

        # G1: 走列级合并(north / south 两列可能来自不同源)
        # 拉取时用最宽窗口，覆盖未来 days 请求
        # G4: 策略 (MERGE_FRAMES, ffill=False) 由 ROUTING_POLICY 声明
        wide_days = max(days, _WIDE_FETCH.get(Capability.NORTH_FLOW, {}).get("days", days))
        merged, prov = self._route(
            Capability.NORTH_FLOW, Market.GLOBAL,
            "fetch_north_flow_history", wide_days,
        )
        if not merged.empty:
            # 历史数据 4h 缓存(每日收盘后更新)
            self._cache_set(Capability.NORTH_FLOW, cache_key, merged, ttl=14400.0)
            self._last_provenance[cache_key] = prov
        return _tail_by_n(merged, days)

    def market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        cache_key = f"market_index:{code}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # 指数市场:腾讯支持 usSPY/hkHSI(对应 US/HK),其他归 GLOBAL
        market = detect_market(code)
        result, prov = self._route(
            Capability.MARKET_INDEX, market, "fetch_market_index", code,
        )
        if result is None:
            # 兜底:用 GLOBAL 路由(yfinance)
            result, prov = self._route(
                Capability.MARKET_INDEX, Market.GLOBAL, "fetch_market_index", code,
            )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.MARKET_INDEX])
            self._last_provenance[cache_key] = prov
        return result

    def macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        """indicator: MacroIndicator enum (PMI / M2 / CREDIT / CPI / PPI)。"""
        cache_key = f"macro:{indicator.value}"
        cached = self._cache_get(Capability.MACRO, cache_key)
        if cached is not None:
            return cached
        result, prov = self._route(
            Capability.MACRO, Market.GLOBAL, "fetch_macro", indicator,
        )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        if not df.empty:
            self._cache_set(Capability.MACRO, cache_key, df)
            self._last_provenance[cache_key] = prov
        return df

    def balance_sheet(self, symbol: str) -> Optional[BalanceSheet]:
        """资产负债表快照(字段级合并多源)。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        当前实现源:BaostockProvider(A股)。
        """
        cache_key = f"balance_sheet:{symbol}"
        # BalanceSheet 是 dataclass 不是 DataFrame，L2 落盘对它无意义，只用 L1
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached.stale_seconds = _stale_seconds(cached.timestamp)
            return cached

        market = detect_market(symbol)
        merged, prov = self._route(
            Capability.BALANCE_SHEET, market, "fetch_balance_sheet", symbol,
        )
        if merged is not None:
            self._cache.set(cache_key, merged, _DEFAULT_TTL[Capability.BALANCE_SHEET])
            self._last_provenance[cache_key] = prov
        return merged

    def dupont_metrics(self, symbol: str) -> Optional["DupontMetrics"]:
        """杜邦分析指标快照（ROE 拆解：净利率 × 资产周转率 × 权益乘数）。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        当前实现源:BaostockProvider(A股)。

        Returns
        -------
        DupontMetrics | None
        """
        from .schemas import DupontMetrics
        cache_key = f"dupont_metrics:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        market = detect_market(symbol)
        result, prov = self._route(
            Capability.DUPONT, market, "fetch_dupont_metrics", symbol,
        )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.DUPONT])
            self._last_provenance[cache_key] = prov
        return result

    def operation_metrics(self, symbol: str) -> Optional["OperationMetrics"]:
        """运营能力指标快照（存货周转天数 / 应收账款周转天数等）。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        当前实现源:BaostockProvider(A股)。

        Returns
        -------
        OperationMetrics | None
        """
        from .schemas import OperationMetrics
        cache_key = f"operation_metrics:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        market = detect_market(symbol)
        result, prov = self._route(
            Capability.OPERATION, market, "fetch_operation_metrics", symbol,
        )
        if result is not None:
            self._cache.set(cache_key, result, _DEFAULT_TTL[Capability.OPERATION])
            self._last_provenance[cache_key] = prov
        return result

    def dividend(self, symbol: str, year: int | None = None) -> List["DividendRecord"]:
        """股票分红记录列表（按除权除息日倒序）。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        当前实现源:BaostockProvider(A股)。

        Args:
            symbol: 标准化代码，如 'sh600519'
            year: 指定年份，None 表示最近4年。

        Returns
        -------
        List[DividendRecord]，空列表表示无分红记录或查询失败。
        """
        from .schemas import DividendRecord
        cache_key = f"dividend:{symbol}:{year}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        market = detect_market(symbol)
        result, prov = self._route(
            Capability.DIVIDEND, market, "fetch_dividend", symbol, year,
        )
        records = result if isinstance(result, list) else []
        if records:
            self._cache.set(cache_key, records, _DEFAULT_TTL[Capability.DIVIDEND])
            self._last_provenance[cache_key] = prov
        return records

    def margin_flow(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """个股融资融券日频时序（顺序 failover，不合并）。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列 margin_balance / short_balance。
            空 DataFrame 表示无数据。
        """
        cache_key = f"margin_flow:{symbol}:{start}:{end}"
        cached = self._cache_get(Capability.MARGIN_FLOW, cache_key)
        if cached is not None:
            return cached

        result, prov = self._route(
            Capability.MARGIN_FLOW, Market.GLOBAL,
            "fetch_margin_flow", symbol, start, end,
        )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        if not df.empty:
            self._cache_set(Capability.MARGIN_FLOW, cache_key, df)
            self._last_provenance[cache_key] = prov
        return df

    def news_headlines(self, symbol: str, n: int = 20) -> List[str]:
        """新闻标题列表（MERGE_LISTS：EM kuaixun + AkShare 财联社电报多源
        去重 + 时间倒序）。

        ⚠️ 当前两个 provider（EM kuaixun、AkShare 财联社电报）都是**全市场
        快讯**接口，symbol 参数被它们忽略——任何 symbol 都会拿到相同结果。
        保留 symbol 入参是为未来接入个股粒度新闻源（如腾讯个股资讯）留口子，
        缓存键仍按 symbol 分桶。

        Returns
        -------
        List[str]
            最多 n 条标题（最新在前），空列表表示无数据。

        Provider 内部约定返回 List[NewsItem]（G5-1）；gateway 在出口
        投影为 title 字符串列表，保持调用方签名不变。
        """
        from .schemas import NewsItem
        # G3: cache key 不含 n，缓存最大量列表，出口处 [:n] 切片
        cache_key = f"news_headlines:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached[:n]

        # G3: 首次 miss 时按"宽 n"拉，让后续不同 n 共享同一缓存
        wide_n = max(n, _WIDE_FETCH.get(Capability.NEWS_HEADLINES, {}).get("n", n))
        result, prov = self._route(
            Capability.NEWS_HEADLINES, Market.GLOBAL,
            "fetch_news_headlines", symbol, wide_n,
        )
        items = result if isinstance(result, list) else []
        # 兜底：旧 fixture / 第三方 mock 可能还返回 List[str]
        headlines: List[str] = [
            it.title if isinstance(it, NewsItem) else str(it) for it in items
        ]
        if headlines:
            self._cache.set(cache_key, headlines, _DEFAULT_TTL[Capability.NEWS_HEADLINES])
            self._last_provenance[cache_key] = prov
        return headlines[:n]

    def fund_flow(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """个股资金流日频时序（主力/超大/大单净流入）。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列 main_net_inflow / super_net_inflow / large_net_inflow
            及其净占比（%），含 close / change_pct。
        """
        # G3: cache key 不含 start/end，存全量(AkShare 默认提供 ~120 个交易日)
        cache_key = f"fund_flow:{symbol}"
        cached = self._cache_get(Capability.FUND_FLOW, cache_key)
        if cached is not None and not cached.empty:
            return _slice_by_range(cached, start, end)

        # G1: 走列级合并(为未来接入第二个资金流源做准备)
        # 拉取时不传 start/end，让 provider 给最长可得序列
        # G4: 策略 (MERGE_FRAMES, ffill=False) 由 ROUTING_POLICY 声明
        merged, prov = self._route(
            Capability.FUND_FLOW, Market.GLOBAL,
            "fetch_fund_flow", symbol, None, None,
        )
        if not merged.empty:
            self._cache_set(Capability.FUND_FLOW, cache_key, merged)
            self._last_provenance[cache_key] = prov
        return _slice_by_range(merged, start, end)

    def fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """基本面历史时序（日频，前向填充季报）。

        通过 DataGateway 统一路由，享受熔断 + 健康度 + 缓存保护。
        多 provider 列级合并:不同源贡献不同字段时取并集,重叠列由健康度
        + priority_hint 更高者胜出(类似 Quote 的字段级合并)。

        G3 缓存策略：缓存键不含 start/end，内部存储"全量已知时序"，
        本方法出口按 [start, end] 切片。同一 symbol 任何时间窗口请求
        都命中同一份缓存。
        """
        # G3: cache key 去掉 start/end，存全量
        cache_key = f"fundamentals_history:{symbol}"
        cached = self._cache_get(Capability.FUNDAMENTALS_HISTORY, cache_key)
        if cached is not None and not cached.empty:
            return _slice_by_range(cached, start, end)

        # G1: 用 _merged_history_fetch 统一处理多源列级合并
        # G3: 拉取时不传 start/end，让 provider 给出可得的最长序列
        # G4: 策略 (MERGE_FRAMES, ffill=True) 由 ROUTING_POLICY 声明
        merged, prov = self._route(
            Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
            "fetch_fundamentals_history", symbol, None, None,
        )
        if merged.empty:
            return pd.DataFrame()

        # G3: 缓存全量，出口处切片
        self._cache_set(Capability.FUNDAMENTALS_HISTORY, cache_key, merged)
        self._last_provenance[cache_key] = prov
        return _slice_by_range(merged, start, end)

    # ── G2: 聚合视图 profile() ──────────────────────────────────────────────

    def _get_profile_executor(self) -> ThreadPoolExecutor:
        """懒创建并复用 profile 专用线程池。

        与 self._executor 物理隔离避免嵌套提交死锁；workers=9 对应当前
        profile() 切片数（保持每次调用都能完全并发，不串行任何切片）。
        """
        if self._profile_executor is None:
            with self._profile_executor_lock:
                if self._profile_executor is None:
                    self._profile_executor = ThreadPoolExecutor(
                        max_workers=9, thread_name_prefix="gw_profile",
                    )
        return self._profile_executor

    def profile(self, symbol: str, *, headlines_n: int = 10) -> StockProfile:
        """聚合所有 capability 的"信息包"。

        本方法实现在 core/data_gateway/profile.py:build_profile,本类只负责
        提供执行所需的上下文(executor / provenance store / 公开数据方法)。
        详细行为见 profile.build_profile 的 docstring。
        """
        from .profile import build_profile
        return build_profile(self, symbol, headlines_n=headlines_n)

    # ── 监控 / 调试 ──────────────────────────────────────────────────────────

    def provenance(self, key: str) -> Dict[str, str]:
        """查询某次合并的字段来源记录。

        key 形如 'quote:sh600519' / 'fundamentals:sh600519'。
        """
        return dict(self._last_provenance.get(key, {}))

    def invalidate_cache(self) -> None:
        self._cache.clear()

    def invalidate_fundamentals_history(self, symbol: str) -> None:
        """清除指定标的的基本面历史缓存（精确清除，不影响其他标的缓存）。

        G3 后缓存键为 'fundamentals_history:{symbol}'（不再含 start/end），
        因此精确 invalidate 即可，无需 prefix 扫描。
        """
        cache_key = f"fundamentals_history:{symbol}"
        self._cache.invalidate(cache_key)


# ─── 默认注册 + 单例 ──────────────────────────────────────────────────────────


def _build_default_gateway() -> DataGateway:
    from .providers.akshare import AkshareProvider
    from .providers.baostock import BaostockProvider
    from .providers.eastmoney import EastmoneyProvider
    from .providers.sina import SinaProvider
    from .providers.tencent import TencentProvider
    from .providers.yfinance import YfinanceProvider

    gw = DataGateway()
    for cls in (TencentProvider, SinaProvider, EastmoneyProvider,
                YfinanceProvider, BaostockProvider, AkshareProvider):
        try:
            gw.register_provider(cls())
        except Exception as exc:
            logger.warning("注册 %s 失败: %s", cls.__name__, exc)
    return gw


from core.singleton import LockedSingleton

_gateway_singleton: LockedSingleton[DataGateway] = LockedSingleton(
    _build_default_gateway, name="data_gateway"
)


def get_gateway() -> DataGateway:
    """获取全局 DataGateway 单例(默认注册全部 provider，线程安全)。"""
    return _gateway_singleton.get()


def reset_gateway(gw: Optional[DataGateway] = None) -> None:
    """重置/替换全局单例(测试用)。"""
    _gateway_singleton.reset(gw)


__all__ = ["DataGateway", "get_gateway", "reset_gateway"]

"""
core/data_layer.py — 统一数据层
================================
回测和实盘共用同一套数据接口，内置 TTL 缓存与降级。

接口设计原则：
  1. get_bars()       — 日K线，返回 pd.DataFrame（open/high/low/close/volume）
  2. get_realtime()   — 单只实时行情，返回 Quote
  3. get_realtime_bulk() — 批量实时行情，单次请求
  4. get_north_flow() — 北向资金快照

两种运行模式：
  DataLayer         — 实盘模式，调用真实 API，带 TTL 缓存
  BacktestDataLayer — 回测模式，读注入的 DataFrame，防止前视偏差

依赖：
  仅标准库 + pandas（不依赖 backend/）
  底层 HTTP 调用参考 signals.py 但完全独立实现
"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("core.data_layer")

# ─── 数据类 ──────────────────────────────────────────────────────────────────


@dataclass
class Quote:
    """实时行情快照"""
    symbol: str
    price: float
    prev_close: float
    pct_change: float          # 涨跌幅（%）
    high: float
    low: float
    vol_ratio: Optional[float] = None   # 量比
    # ── 腾讯 88 字段补充 ──
    pe_ttm: Optional[float] = None      # 市盈率（TTM）
    pb: Optional[float] = None           # 市净率
    turnover_rate: Optional[float] = None  # 换手率（%）
    market_cap: Optional[float] = None   # 总市值（亿元）
    float_cap: Optional[float] = None   # 流通市值（亿元）
    high_52w: Optional[float] = None    # 52W 高
    low_52w: Optional[float] = None     # 52W 低
    limit_up: Optional[float] = None    # 涨停价
    limit_down: Optional[float] = None  # 跌停价
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def day_change(self) -> float:
        """当日涨跌额"""
        return self.price - self.prev_close

    @property
    def is_limit_up(self) -> bool:
        """是否涨停（默认普通 A 股 9.9%，ST/创业板/科创板需调用 check_limit_status）"""
        return self.pct_change >= 9.9

    @property
    def is_limit_down(self) -> bool:
        return self.pct_change <= -9.9


@dataclass
class NorthFlowSnapshot:
    """北向资金快照"""
    net_north_yi: float = 0.0       # 北向净流入（亿元，正=净买入）
    net_south_yi: float = 0.0       # 南向净流入（亿元）
    direction: str = "NEUTRAL"      # BUY / SELL / NEUTRAL
    source: str = "unknown"
    stale: bool = False             # True=数据可能陈旧
    timestamp: datetime = field(default_factory=datetime.now)

    def is_strong_inflow(self, threshold_yi: float = 50.0) -> bool:
        return self.net_north_yi >= threshold_yi


# ─── TTL 缓存 ────────────────────────────────────────────────────────────────


class _TTLCache:
    """线程安全的 TTL 内存缓存"""

    def __init__(self):
        self._store: Dict[str, tuple] = {}   # key → (value, expire_at)
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expire_at = entry
            if time.monotonic() > expire_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value, ttl: float):
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ─── 转换工具 ────────────────────────────────────────────────────────────────


def _tencent_quote_to_quote(symbol: str, tq) -> Optional[Quote]:
    """将 TencentQuote 转换为 data_layer.Quote"""
    if not tq or not tq.is_valid:
        return None
    return Quote(
        symbol=symbol,
        price=tq.price,
        prev_close=tq.prev_close,
        pct_change=tq.pct_change,
        high=tq.high,
        low=tq.low,
        vol_ratio=tq.volume_ratio if tq.volume_ratio > 0 else None,
        # ── 腾讯 88 字段映射 ──
        pe_ttm=tq.pe_ttm if tq.pe_ttm and tq.pe_ttm > 0 else None,
        pb=tq.pb if tq.pb and tq.pb > 0 else None,
        turnover_rate=tq.turnover_rate if tq.turnover_rate and tq.turnover_rate > 0 else None,
        market_cap=tq.market_cap if tq.market_cap and tq.market_cap > 0 else None,
        float_cap=tq.float_cap if tq.float_cap and tq.float_cap > 0 else None,
        high_52w=tq.high_52w if tq.high_52w and tq.high_52w > 0 else None,
        low_52w=tq.low_52w if tq.low_52w and tq.low_52w > 0 else None,
        limit_up=tq.limit_up if tq.limit_up and tq.limit_up > 0 else None,
        limit_down=tq.limit_down if tq.limit_down and tq.limit_down > 0 else None,
    )


# ─── 分钟 K 线获取（AKShare）─────────────────────────────────────────────────

def _fetch_minute_bars_akshare(
    symbol: str,
    period: str = '1',
    adjust: str = 'qfq',
) -> Optional[pd.DataFrame]:
    """
    通过 AKShare 获取 A 股分钟 K 线（免费，限约 1 年历史）。

    Args:
        symbol: 标的代码，如 '510300' 或 '510300.SH'
        period: '1' | '5' | '15' | '30' | '60'（分钟）
        adjust: 'qfq'=前复权, 'hfq'=后复权, ''=不复权

    Returns:
        DataFrame，列：open, high, low, close, volume，DatetimeIndex
    """
    # P2-16: 熔断检查 — 连续 3 次失败后冷却 5 分钟，避免限流加剧
    try:
        from core.circuit_breaker import get_breaker
        cb = get_breaker('akshare_minute', failure_threshold=3,
                         cooldown_seconds=300.0)
        if not cb.allow():
            logger.warning("AKShare 分钟 K 线熔断中（state=%s），跳过 %s", cb.state(), symbol)
            return None
    except Exception:
        cb = None

    try:
        import akshare as ak  # type: ignore
    except ImportError:
        logger.warning("akshare 未安装，无法获取分钟 K 线。请: pip install akshare")
        return None

    # 标准化代码（去掉市场后缀）
    code = symbol.upper()
    if code.endswith('.SH') or code.endswith('.SZ'):
        code = code[:-3]

    try:
        df = ak.stock_zh_a_minute(symbol=code, period=period, adjust=adjust)
        if cb:
            cb.on_success()
    except Exception as exc:
        logger.debug("AKShare minute bars failed for %s: %s", symbol, exc)
        if cb:
            cb.on_failure()
        try:
            from core.metrics import get_registry
            get_registry().record_data_source_failure('akshare')
        except Exception:
            pass
        return None

    if df is None or df.empty:
        return None

    # AKShare 返回列名：日期/时间, 开盘, 收盘, 最高, 最低, 成交量
    col_map = {
        '时间': 'datetime', '日期': 'datetime',
        '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low',
        '成交量': 'volume', '成交额': 'amount',
    }
    df = df.rename(columns={c: col_map.get(c, c) for c in df.columns})

    # 标准化时间索引
    time_col = next((c for c in ['datetime', 'date', 'time'] if c in df.columns), None)
    if time_col:
        df[time_col] = pd.to_datetime(df[time_col])
        df = df.set_index(time_col)

    # 保留标准列
    needed = ['open', 'high', 'low', 'close', 'volume']
    existing = [c for c in needed if c in df.columns]
    if not existing:
        return None
    df = df[existing].copy()
    for col in needed:
        if col not in df.columns:
            df[col] = 0.0
    df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
    df = df.sort_index()
    return df


# ─── Parquet 本地缓存 ─────────────────────────────────────────────────────────

_PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'bars')


class ParquetCache:
    """
    日线数据本地 Parquet 缓存（Phase 1-C）。

    - 首次下载后写入 data/bars/{symbol}.parquet
    - 后续调用：加载本地缓存，若最新数据不足则增量更新
    - 格式：DatetimeIndex + [open, high, low, close, volume]

    用法：
        cache = ParquetCache()
        df = cache.load('510300')           # 加载缓存（无则 None）
        cache.save('510300', df)            # 保存/追加
        df = cache.upsert('510300', df_new) # 合并去重后保存
    """

    def __init__(self, cache_dir: str = _PARQUET_DIR):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, symbol: str) -> str:
        safe = symbol.replace('.', '_').replace('/', '_')
        return os.path.join(self.cache_dir, f"{safe}.parquet")

    def exists(self, symbol: str) -> bool:
        return os.path.isfile(self._path(symbol))

    def load(self, symbol: str) -> Optional[pd.DataFrame]:
        """加载本地缓存。不存在返回 None。"""
        path = self._path(symbol)
        if not os.path.isfile(path):
            return None
        try:
            df = pd.read_parquet(path)
            if not pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index)
            return df.sort_index()
        except Exception as exc:
            logger.warning("Parquet load failed for %s: %s", symbol, exc)
            return None

    def save(self, symbol: str, df: pd.DataFrame) -> bool:
        """覆盖保存（DatetimeIndex）。"""
        if df is None or df.empty:
            return False
        try:
            path = self._path(symbol)
            out = df.copy()
            if not pd.api.types.is_datetime64_any_dtype(out.index):
                out.index = pd.to_datetime(out.index)
            out.to_parquet(path, engine='pyarrow', compression='snappy')
            return True
        except Exception as exc:
            logger.warning("Parquet save failed for %s: %s", symbol, exc)
            return False

    def upsert(self, symbol: str, df_new: pd.DataFrame) -> pd.DataFrame:
        """
        合并新数据与本地缓存：去重、排序后保存。
        返回合并后的完整 DataFrame。
        """
        df_old = self.load(symbol)
        if df_old is not None and not df_old.empty:
            combined = pd.concat([df_old, df_new])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
        else:
            combined = df_new.sort_index()
        self.save(symbol, combined)
        return combined

    def latest_date(self, symbol: str) -> Optional[pd.Timestamp]:
        """返回缓存中最新的日期，无缓存返回 None。"""
        df = self.load(symbol)
        if df is None or df.empty:
            return None
        return df.index[-1]

    def delete(self, symbol: str) -> bool:
        """删除缓存文件。"""
        path = self._path(symbol)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False


# ─── DataLayer（实盘模式）────────────────────────────────────────────────────


class DataLayer:
    """
    A股统一数据接口 — 实盘模式。
    带 TTL 缓存，降级链：腾讯 → 新浪。
    支持本地 Parquet 缓存（首次下载后存入 data/bars/）。
    """

    # 默认 TTL（秒）
    QUOTE_TTL   = 30      # 实时行情缓存 30 秒
    BAR_TTL     = 3600    # 日K线缓存 1 小时
    NORTH_TTL   = 60      # 北向资金缓存 60 秒

    def __init__(self, use_parquet_cache: bool = True):
        """
        Args:
            use_parquet_cache: 是否启用本地 Parquet 缓存（默认开启）
        """
        self._cache = _TTLCache()
        self._parquet = ParquetCache() if use_parquet_cache else None
        self._tencent_src = None  # 延迟初始化
        self._quote_manager = None  # 延迟初始化

    def _get_tencent_source(self):
        """延迟初始化 TencentQuoteDataSource"""
        if self._tencent_src is None:
            from core.tencent_quote_source import TencentQuoteDataSource
            self._tencent_src = TencentQuoteDataSource(cache_ttl=self.QUOTE_TTL)
        return self._tencent_src

    def _get_quote_manager(self):
        """延迟初始化 QuoteSourceManager"""
        if self._quote_manager is None:
            from core.quote_source_manager import QuoteSourceManager
            self._quote_manager = QuoteSourceManager()
        return self._quote_manager

    # ── 日K线 ────────────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        """
        获取日K线数据（前复权）。
        返回 DataFrame，列：date(datetime64), open, high, low, close, volume

        缓存策略：
          1. 内存 TTL 缓存（30 min）
          2. 本地 Parquet 缓存（首次下载后增量更新）
          3. 网络抓取：腾讯 → 新浪（降级）
        """
        cache_key = f"bars:{symbol}:{days}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        df = None

        # 尝试 Parquet 缓存
        if self._parquet is not None:
            cached_df = self._parquet.load(symbol)
            if cached_df is not None and not cached_df.empty:
                latest = self._parquet.latest_date(symbol)
                today = pd.Timestamp.now().normalize()
                # 缓存今日或昨日数据视为新鲜（非交易日不更新）
                if latest is not None and (today - latest).days <= 3:
                    df = cached_df
                    logger.debug("Parquet cache hit for %s (latest=%s)", symbol, latest.date())

        # 网络抓取（缓存过旧或不存在时）
        if df is None:
            mgr = self._get_quote_manager()
            df_net = mgr.fetch_daily_kline(symbol, days=max(days, 365))

            if df_net is not None and not df_net.empty:
                # 转换为 DatetimeIndex
                df_net_idx = df_net.copy()
                if 'date' in df_net_idx.columns:
                    df_net_idx = df_net_idx.set_index('date')
                if not pd.api.types.is_datetime64_any_dtype(df_net_idx.index):
                    df_net_idx.index = pd.to_datetime(df_net_idx.index)
                # 写入 Parquet（增量合并）
                if self._parquet is not None:
                    df = self._parquet.upsert(symbol, df_net_idx)
                else:
                    df = df_net_idx

        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        # 取最近 days 条，恢复 date 列风格（保持向后兼容）
        result = df.tail(days).reset_index()
        if 'index' in result.columns and 'date' not in result.columns:
            result = result.rename(columns={'index': 'date'})

        self._cache.set(cache_key, result, self.BAR_TTL)
        return result

    def get_minute_bars(
        self,
        symbol: str,
        period: str = '1',
        adjust: str = 'qfq',
    ) -> pd.DataFrame:
        """
        获取分钟 K 线（新浪 A 股 / 腾讯港股，AKShare 兜底）。

        Args:
            symbol: 标的代码，如 '510300' 或 '510300.SH'
            period: '1' | '5' | '15' | '30' | '60'（分钟）
            adjust: 复权方式 'qfq'|'hfq'|''

        Returns:
            DataFrame，列：datetime, open, high, low, close, volume
        """
        cache_key = f"minute:{symbol}:{period}:{adjust}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        mgr = self._get_quote_manager()
        df = mgr.fetch_minute_kline(symbol, period=f'{period}m', limit=200)

        if df is None or df.empty:
            # 最后兜底：AKShare
            df = _fetch_minute_bars_akshare(symbol, period=period, adjust=adjust)
            if df is None or df.empty:
                logger.warning("分钟 K 线获取失败: %s", symbol)
                df = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

        # 分钟数据缓存 5 分钟（交易时间内需要较频繁刷新）
        self._cache.set(cache_key, df, 300)
        return df

    # ── 实时行情 ─────────────────────────────────────────────────────────────

    def get_realtime(self, symbol: str) -> Optional[Quote]:
        """获取单只股票实时行情"""
        result = self.get_realtime_bulk([symbol])
        return result.get(symbol)

    def get_realtime_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        """
        批量实时行情（腾讯 qt.gtimg.cn）。
        支持 A 股 / 港股 / 美股 / 指数，对缓存命中的标的不重复请求。
        """
        if not symbols:
            return {}

        cached_result: Dict[str, Quote] = {}
        missing: List[str] = []

        for sym in symbols:
            cached = self._cache.get(f"quote:{sym}")
            if cached is not None:
                cached_result[sym] = cached
            else:
                missing.append(sym)

        if missing:
            # 使用统一的 TencentQuoteDataSource（支持全市场）
            src = self._get_tencent_source()
            fresh_quotes = src.fetch_quotes(missing)
            for sym, tq in fresh_quotes.items():
                q = _tencent_quote_to_quote(sym, tq)
                if q:
                    self._cache.set(f"quote:{sym}", q, self.QUOTE_TTL)
                    cached_result[sym] = q

        return cached_result

    # ── 北向资金 ─────────────────────────────────────────────────────────────

    def get_north_flow(self) -> NorthFlowSnapshot:
        """获取北向资金快照（复用 backend/services/data_cache.py 中的 cached_kamt）"""
        cached = self._cache.get("north_flow")
        if cached is not None:
            return cached

        snap = NorthFlowSnapshot()
        try:
            import sys as _sys
            _backend = os.path.join(os.path.dirname(__file__), "..", "backend")
            if _backend not in _sys.path:
                _sys.path.insert(0, _backend)
            from services.data_cache import cached_kamt  # type: ignore
            kamt = cached_kamt()
            net_yi = kamt.get("net_north_yi", kamt.get("net_north_cny", 0))
            if abs(net_yi) > 1e6:            # 单位是元，转亿
                net_yi = net_yi / 1e8
            snap = NorthFlowSnapshot(
                net_north_yi=float(net_yi),
                net_south_yi=float(kamt.get("net_south_yi", 0)),
                direction="BUY" if net_yi > 0 else ("SELL" if net_yi < 0 else "NEUTRAL"),
                source=kamt.get("source", "kamt"),
                stale=kamt.get("stale", False),
            )
        except Exception as exc:
            logger.warning("get_north_flow failed: %s", exc)

        self._cache.set("north_flow", snap, self.NORTH_TTL)
        return snap

    # ── 宏观数据 ─────────────────────────────────────────────────────────────

    MACRO_TTL = 86400   # 宏观月度数据缓存 24 小时

    def get_macro_data(self, indicator: str) -> pd.DataFrame:
        """
        获取月度宏观经济数据，供 PMIFactor / M2GrowthFactor / CreditImpulseFactor 使用。

        Parameters
        ----------
        indicator : str
            'PMI'    — 制造业 PMI（列：pmi）
            'M2'     — M2 货币供应量同比增速（列：m2_yoy）
            'CREDIT' — 社融同比增速（列：credit_yoy）

        Returns
        -------
        pd.DataFrame
            index 为 DatetimeIndex，失败时返回空 DataFrame（因子自动降级为全零）
        """
        cache_key = f'macro:{indicator}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        df = pd.DataFrame()
        try:
            import akshare as ak
            if indicator == 'PMI':
                raw = ak.macro_china_pmi_monthly()
                # 列名：'月份' + '制造业PMI' 等，取第一列（日期）和第二列（PMI）
                raw.columns = [c.strip() for c in raw.columns]
                date_col = raw.columns[0]
                pmi_col  = next((c for c in raw.columns if 'PMI' in c or 'pmi' in c.lower()), raw.columns[1])
                raw = raw[[date_col, pmi_col]].copy()
                raw.columns = ['date', 'pmi']
                raw['date'] = pd.to_datetime(raw['date'], errors='coerce')
                raw = raw.dropna(subset=['date']).set_index('date')
                raw['pmi'] = pd.to_numeric(raw['pmi'], errors='coerce')
                df = raw.sort_index()

            elif indicator == 'M2':
                raw = ak.macro_china_money_supply_bal()
                raw.columns = [c.strip() for c in raw.columns]
                date_col  = raw.columns[0]
                m2_col    = next((c for c in raw.columns if 'm2' in c.lower() and 'yoy' in c.lower()), None)
                if m2_col is None:
                    m2_col = next((c for c in raw.columns if 'm2' in c.lower()), raw.columns[1])
                raw = raw[[date_col, m2_col]].copy()
                raw.columns = ['date', 'm2_yoy']
                raw['date'] = pd.to_datetime(raw['date'], errors='coerce')
                raw = raw.dropna(subset=['date']).set_index('date')
                raw['m2_yoy'] = pd.to_numeric(raw['m2_yoy'], errors='coerce')
                df = raw.sort_index()

            elif indicator == 'CREDIT':
                raw = ak.macro_china_shrzgm()
                raw.columns = [c.strip() for c in raw.columns]
                date_col   = raw.columns[0]
                val_col    = next((c for c in raw.columns if 'yoy' in c.lower()), None)
                if val_col is None:
                    val_col = next((c for c in raw.columns if '同比' in c), raw.columns[1])
                col_out = 'credit_yoy' if val_col else 'value'
                raw = raw[[date_col, val_col]].copy()
                raw.columns = ['date', col_out]
                raw['date'] = pd.to_datetime(raw['date'], errors='coerce')
                raw = raw.dropna(subset=['date']).set_index('date')
                raw[col_out] = pd.to_numeric(raw[col_out], errors='coerce')
                df = raw.sort_index()

        except Exception as exc:
            logger.warning('get_macro_data(%s) failed: %s', indicator, exc)

        self._cache.set(cache_key, df, self.MACRO_TTL)
        return df

    # ── 缓存管理 ─────────────────────────────────────────────────────────────

    def invalidate(self, symbol: str = None):
        """清除缓存。symbol=None 时清全部"""
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.delete(f"quote:{symbol}")
            for d in [30, 60, 90, 120, 252]:
                self._cache.delete(f"bars:{symbol}:{d}")


# ─── BacktestDataLayer（回测模式）────────────────────────────────────────────


class BacktestDataLayer(DataLayer):
    """
    回测专用数据层。
    从注入的 DataFrame 读取数据，严格禁止前视偏差：
    get_bars(symbol, days) 只返回 current_date（含）之前的数据。

    Usage:
        data = {'510310.SH': df_ohlcv}
        layer = BacktestDataLayer(data)
        layer.set_date('2024-06-01')
        bars = layer.get_bars('510310.SH', days=30)  # 只有 2024-06-01 及之前
    """

    def __init__(self, data: Dict[str, pd.DataFrame]):
        """
        data: {symbol: DataFrame}，DataFrame 必须含列：
              date(datetime64 or str), open, high, low, close, volume
        """
        super().__init__()
        self._data: Dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            df = df.copy()
            if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            self._data[sym] = df

        self._current_date: Optional[pd.Timestamp] = None

    def set_date(self, dt):
        """设置回测当前日期（字符串 '2024-01-01' 或 datetime/Timestamp）"""
        self._current_date = pd.Timestamp(dt)

    @property
    def current_date(self) -> Optional[pd.Timestamp]:
        return self._current_date

    def get_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        """
        返回截止 current_date（含）的最近 days 条日K线。
        若未设置 current_date，返回全量数据的最后 days 条。
        """
        df = self._data.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        if self._current_date is not None:
            df = df[df["date"] <= self._current_date]

        return df.tail(days).reset_index(drop=True)

    def get_realtime(self, symbol: str) -> Optional[Quote]:
        """
        回测实时行情：返回 current_date 当日的收盘价模拟 Quote。
        """
        df = self.get_bars(symbol, days=1)
        if df.empty:
            return None
        row = df.iloc[-1]
        return Quote(
            symbol=symbol,
            price=float(row["close"]),
            prev_close=float(row["close"]),   # 回测中 prev_close 用同值近似
            pct_change=0.0,
            high=float(row["high"]),
            low=float(row["low"]),
            vol_ratio=None,
            timestamp=row["date"].to_pydatetime() if hasattr(row["date"], "to_pydatetime") else datetime.now(),
        )

    def get_realtime_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for sym in symbols:
            q = self.get_realtime(sym)
            if q:
                result[sym] = q
        return result

    def get_north_flow(self) -> NorthFlowSnapshot:
        """回测中北向资金固定返回中性（无历史数据）"""
        return NorthFlowSnapshot(direction="NEUTRAL", stale=True)

    def available_dates(self, symbol: str) -> List[pd.Timestamp]:
        """返回该标的所有可用交易日（用于回测驱动循环）"""
        df = self._data.get(symbol)
        if df is None:
            return []
        return df["date"].tolist()


# ─── 全局单例（实盘用）────────────────────────────────────────────────────────

_global_layer: Optional[DataLayer] = None
_global_lock = threading.Lock()


def get_data_layer() -> DataLayer:
    """获取全局 DataLayer 单例（实盘模式）"""
    global _global_layer
    with _global_lock:
        if _global_layer is None:
            _global_layer = DataLayer()
    return _global_layer


def reset_data_layer():
    """测试用：重置全局单例"""
    global _global_layer
    with _global_lock:
        _global_layer = None

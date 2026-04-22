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

import json
import logging
import os
import ssl
import time
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("core.data_layer")

# 清除代理（A 股接口不走代理）
import os as _os
for _k in list(_os.environ.keys()):
    if "proxy" in _k.lower():
        del _os.environ[_k]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.qq.com",
}


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
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def day_change(self) -> float:
        """当日涨跌额"""
        return self.price - self.prev_close

    @property
    def is_limit_up(self) -> bool:
        """是否涨停（普通 A 股阈值 9.9%）"""
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


# ─── 底层 HTTP 工具 ──────────────────────────────────────────────────────────


def _symbol_to_tencent(symbol: str) -> str:
    """'600519.SH' → 'sh600519'"""
    s = symbol.upper()
    if s.endswith(".SH"):
        return "sh" + s[:-3]
    if s.endswith(".SZ"):
        return "sz" + s[:-3]
    return symbol.lower()


def _http_get(url: str, timeout: int = 8, encoding: str = "gbk") -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.read().decode(encoding, errors="replace")
    except Exception as exc:
        logger.debug("HTTP GET failed %s: %s", url, exc)
        return None


def _parse_tencent_quote(symbol: str, raw_line: str) -> Optional[Quote]:
    """解析腾讯实时行情单行（去掉 v_XXXX=" 前缀后的内容）"""
    eq = raw_line.find('="')
    if eq >= 0:
        raw_line = raw_line[eq + 2:]
    fields = raw_line.rstrip('";').split("~")
    if len(fields) < 40:
        return None
    try:
        price     = float(fields[3])  if fields[3]  not in ("", "-") else 0.0
        prev_cls  = float(fields[4])  if fields[4]  not in ("", "-") else 0.0
        pct       = float(fields[32]) if fields[32] not in ("", "-") else 0.0
        high      = float(fields[33]) if len(fields) > 33 and fields[33] not in ("", "-") else price
        low       = float(fields[34]) if len(fields) > 34 and fields[34] not in ("", "-") else price
        vol_ratio = (float(fields[38])
                     if len(fields) > 38 and fields[38] not in ("", "-", "0")
                     else None)
        return Quote(
            symbol=symbol,
            price=price,
            prev_close=prev_cls,
            pct_change=pct,
            high=high,
            low=low,
            vol_ratio=vol_ratio,
        )
    except (ValueError, IndexError) as exc:
        logger.debug("parse quote %s failed: %s", symbol, exc)
        return None


def _fetch_realtime_bulk_raw(symbols: List[str]) -> Dict[str, Quote]:
    """腾讯批量实时行情（单次 HTTP 请求）"""
    if not symbols:
        return {}
    tc_syms = [_symbol_to_tencent(s) for s in symbols]
    url = "https://qt.gtimg.cn/q=" + ",".join(tc_syms)
    raw = _http_get(url)
    if not raw:
        return {}
    result: Dict[str, Quote] = {}
    lines = raw.strip().split("\n")
    for i, line in enumerate(lines):
        if i >= len(symbols):
            break
        sym = symbols[i]
        q = _parse_tencent_quote(sym, line)
        if q:
            result[sym] = q
    return result


def _fetch_daily_bars_tencent(symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
    """腾讯前复权日K线 → DataFrame(date, open, high, low, close, volume)"""
    qt = _symbol_to_tencent(symbol)
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?_var=kline_dayqfq&param={qt},day,,,{days},qfq"
    )
    raw = _http_get(url, encoding="utf-8")
    if not raw:
        return None
    eq = raw.find("=")
    if eq >= 0:
        raw = raw[eq + 1:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    qfq = data.get("data", {}).get(qt, {})
    bars = qfq.get("qfqday") or qfq.get("day") or []
    if not bars:
        return None
    rows = []
    for bar in bars:
        if len(bar) < 6:
            continue
        try:
            rows.append({
                "date":   bar[0],
                "open":   float(bar[1]),
                "close":  float(bar[2]),
                "high":   float(bar[3]),
                "low":    float(bar[4]),
                "volume": float(bar[5]),
            })
        except (ValueError, IndexError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _fetch_daily_bars_sina(symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
    """新浪日K线（降级用）→ DataFrame(date, open, high, low, close, volume)"""
    s = symbol.upper()
    code = ("sh" + s[:-3]) if s.endswith(".SH") else ("sz" + s[:-3])
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
        f"/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={days}"
    )
    raw = _http_get(url, encoding="utf-8")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    rows = []
    for item in data:
        try:
            rows.append({
                "date":   item.get("day", ""),
                "open":   float(item.get("open", 0)),
                "high":   float(item.get("high", 0)),
                "low":    float(item.get("low", 0)),
                "close":  float(item.get("close", 0)),
                "volume": float(item.get("volume", 0)),
            })
        except (ValueError, TypeError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


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
    except Exception as exc:
        logger.debug("AKShare minute bars failed for %s: %s", symbol, exc)
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
            df_net = _fetch_daily_bars_tencent(symbol, max(days, 365))
            if df_net is None or df_net.empty:
                logger.info("Tencent bars failed for %s, trying Sina", symbol)
                df_net = _fetch_daily_bars_sina(symbol, max(days, 365))

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
        获取分钟 K 线（通过 AKShare，免费，约 1 年历史）。

        Args:
            symbol: 标的代码，如 '510300' 或 '510300.SH'
            period: '1' | '5' | '15' | '30' | '60'（分钟）
            adjust: 复权方式 'qfq'|'hfq'|''

        Returns:
            DataFrame，DatetimeIndex，列：open, high, low, close, volume
        """
        cache_key = f"minute:{symbol}:{period}:{adjust}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        df = _fetch_minute_bars_akshare(symbol, period=period, adjust=adjust)
        if df is None or df.empty:
            logger.warning("分钟 K 线获取失败: %s", symbol)
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

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
        批量实时行情（腾讯单次 HTTP 请求）。
        对缓存命中的标的不重复请求。
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
            fresh = _fetch_realtime_bulk_raw(missing)
            for sym, q in fresh.items():
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
            _backend = _os.path.join(_os.path.dirname(__file__), "..", "backend")
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

"""
core/tencent_quote_source.py — 腾讯 qt.gtimg.cn 统一行情数据源
================================================================

覆盖 A 股 / 指数 / 港股 / 美股的实时行情 + K 线数据。

能力矩阵：
  ┌────────────┬──────────┬──────────┬──────────┬──────────┐
  │ 数据类型    │ A 股/指数 │ 港股      │ 美股      │ 批量      │
  ├────────────┼──────────┼──────────┼──────────┼──────────┤
  │ 实时行情    │ ✓        │ ✓        │ ✓        │ ✓ (≤50) │
  │ 日 K 线    │ ✓        │ ✓        │ ✓        │ 单只      │
  │ 周/月 K 线 │ ✓        │ ✓        │ ✓        │ 单只      │
  │ 分钟 K 线  │ ✗ 不可用  │ ✓        │ ✗ 不可用  │ 单只      │
  └────────────┴──────────┴──────────┴──────────┴──────────┘

⚠️ 已确认失效的接口（勿尝试）：
  A 股/指数分钟 K 线（5m/15m/30m/60m）、季 K、资金流、F10 等

用法：
    from core.tencent_quote_source import TencentQuoteDataSource
    src = TencentQuoteDataSource()

    # 实时行情
    q = src.fetch_quote('hk00700')
    quotes = src.fetch_quotes(['sh600519', 'hk00700', 'usAAPL'])

    # 日 K 线
    bars = src.fetch_kline('sh600519', period='day', limit=120)

    # 港股分钟 K 线（仅港股可用）
    bars = src.fetch_minute_kline('hk00700', period='5m', limit=100)
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Literal

import pandas as pd

logger = logging.getLogger("core.tencent_quote")

# 清除代理
for _k in list(os.environ.keys()):
    if "proxy" in _k.lower():
        del os.environ[_k]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.qq.com",
}

# 批量请求上限
BATCH_LIMIT = 50


# ─── 数据类 ──────────────────────────────────────────────────────────────────


@dataclass
class TencentQuote:
    """腾讯行情快照（全市场统一格式）"""

    symbol: str           # 原始传入的 symbol，如 'sh600519', 'hk00700', 'usAAPL'
    name: str = ""        # 中文名
    code: str = ""        # 纯代码，如 '600519', '00700', 'AAPL'
    market: str = ""      # A / INDEX / HK / US

    # 价格
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    avg_price: float = 0.0

    # 涨跌
    change: float = 0.0       # 涨跌额
    pct_change: float = 0.0   # 涨跌幅 (%)

    # 成交
    volume: float = 0.0       # 成交量（股/份）
    amount: float = 0.0       # 成交额（元/港元/美元）
    turnover_rate: float = 0.0  # 换手率 (%)

    # 盘口
    bid1_price: float = 0.0
    bid1_vol: float = 0.0
    ask1_price: float = 0.0
    ask1_vol: float = 0.0

    # 基本面
    pe_ttm: float = 0.0
    pb: float = 0.0
    dividend_yield: float = 0.0
    market_cap: float = 0.0    # 总市值（亿元/亿港元/亿美元）
    float_cap: float = 0.0     # 流通市值

    # 限制
    limit_up: float = 0.0
    limit_down: float = 0.0
    amplitude: float = 0.0     # 振幅 (%)

    # 52 周
    high_52w: float = 0.0
    low_52w: float = 0.0

    # 元数据
    volume_ratio: float = 0.0  # 量比
    currency: str = ""         # CNY / HKD / USD
    timestamp: str = ""        # 原始时间戳字符串
    raw_field_count: int = 0

    @property
    def is_valid(self) -> bool:
        return self.price > 0


# ─── 字段映射 ────────────────────────────────────────────────────────────────

# 所有市场共享的核心字段位置（经实测 2026-05-08 验证）
# A 股 88 字段 / 指数 88 字段 / 港股 78 字段 / 美股 71 字段
# 核心字段位置一致，差异在扩展字段

_COMMON_FIELDS = {
    "name": 1,
    "code": 2,
    "price": 3,
    "prev_close": 4,
    "open": 5,
    "volume": 6,
    "bid1_price": 9,
    "bid1_vol": 10,
    "ask1_price": 19,
    "ask1_vol": 20,
    "timestamp": 30,
    "change": 31,
    "pct_change": 32,
    "high": 33,
    "low": 34,
}

_A_SHARE_EXTRA = {
    "volume_lots": 36,     # 成交量（手）
    "amount_wan": 37,      # 成交额（万元）
    "turnover_rate": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_cap": 44,       # 流通市值（亿）
    "market_cap": 45,      # 总市值（亿）
    "pb": 46,
    "limit_up": 47,
    "limit_down": 48,
    "volume_ratio": 49,
    "avg_price": 51,
    "dividend_yield": 56,
    "amount_wan2": 57,     # 成交额（万元，更精确）
    "high_52w": 67,
    "low_52w": 68,
    "currency": 82,
}

_HK_EXTRA = {
    "volume_real": 29,     # 港股成交量（股）
    "pe_ttm": 39,
    "amplitude": 43,
    "market_cap": 44,      # 总市值（亿港元）
    "float_cap": 45,
    "name_en": 46,
    "turnover_rate": 47,
    "high_52w": 48,
    "low_52w": 49,
    "volume_ratio": 50,
    "pb": 57,
    "avg_price": 73,
    "currency": 75,
}

_US_EXTRA = {
    "volume_real": 36,     # 美股成交量（股）
    "amount": 37,          # 成交额
    "turnover_rate": 38,
    "pe_ttm": 39,
    "name_en": 46,
    "high_52w": 48,
    "low_52w": 49,
    "market_cap": 44,      # 总市值（亿美元）
    "float_cap": 45,
    "pb": 57,
    "avg_price": 67,
}


# ─── 工具函数 ────────────────────────────────────────────────────────────────


def _detect_market(symbol: str) -> str:
    """
    检测市场类型。

    Returns: 'A' | 'INDEX' | 'HK' | 'US'
    """
    s = symbol.strip().lower()
    if s.startswith("hk"):
        return "HK"
    if s.startswith("us"):
        return "US"
    # 指数：sh000xxx / sz399xxx
    if s.startswith(("sh000", "sz399")):
        return "INDEX"
    return "A"


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换为 float"""
    if val is None:
        return default
    try:
        s = str(val).strip()
        if s in ("", "-", "--"):
            return default
        f = float(s)
        return f if f == f else default  # NaN check
    except (ValueError, TypeError):
        return default


def _get_field(fields: list, idx: int, default: float = 0.0) -> float:
    """安全获取字段值"""
    if idx < 0 or idx >= len(fields):
        return default
    return _safe_float(fields[idx], default)


def _get_field_str(fields: list, idx: int, default: str = "") -> str:
    """安全获取字符串字段"""
    if idx < 0 or idx >= len(fields):
        return default
    try:
        return str(fields[idx]).strip()
    except Exception:
        return default


# ─── 解析器 ──────────────────────────────────────────────────────────────────


def parse_quote(symbol: str, raw_line: str) -> Optional[TencentQuote]:
    """
    解析单行腾讯行情数据。

    Args:
        symbol: 原始 symbol，如 'sh600519', 'hk00700'
        raw_line: qt.gtimg.cn 返回的单行文本

    Returns:
        TencentQuote 或 None（解析失败时）
    """
    # 去掉 v_xxx=" 前缀
    eq = raw_line.find('="')
    if eq >= 0:
        raw_line = raw_line[eq + 2:]
    fields = raw_line.rstrip('";\n\r').split("~")

    if len(fields) < 30:
        return None

    market = _detect_market(symbol)
    common = _COMMON_FIELDS

    # 提取公共字段
    q = TencentQuote(
        symbol=symbol,
        name=_get_field_str(fields, common["name"]),
        code=_get_field_str(fields, common["code"]),
        market=market,
        price=_get_field(fields, common["price"]),
        prev_close=_get_field(fields, common["prev_close"]),
        open=_get_field(fields, common["open"]),
        high=_get_field(fields, common["high"]),
        low=_get_field(fields, common["low"]),
        change=_get_field(fields, common["change"]),
        pct_change=_get_field(fields, common["pct_change"]),
        bid1_price=_get_field(fields, common["bid1_price"]),
        bid1_vol=_get_field(fields, common["bid1_vol"]),
        ask1_price=_get_field(fields, common["ask1_price"]),
        ask1_vol=_get_field(fields, common["ask1_vol"]),
        timestamp=_get_field_str(fields, common["timestamp"]),
        raw_field_count=len(fields),
    )

    # 根据市场类型提取扩展字段
    if market in ("A", "INDEX"):
        extra = _A_SHARE_EXTRA
        # A 股 volume 单位是手，转换为股（×100）
        vol_lots = _get_field(fields, extra.get("volume_lots", 36))
        q.volume = vol_lots * 100 if vol_lots > 0 else _get_field(fields, 6) * 100
        # 成交额：优先用精确值（万元→元）
        amount_wan = _get_field(fields, extra.get("amount_wan2", 57))
        if amount_wan <= 0:
            amount_wan = _get_field(fields, extra.get("amount_wan", 37))
        q.amount = amount_wan * 10000 if amount_wan > 0 else 0
        q.turnover_rate = _get_field(fields, extra.get("turnover_rate", 38))
        q.pe_ttm = _get_field(fields, extra.get("pe_ttm", 39))
        q.amplitude = _get_field(fields, extra.get("amplitude", 43))
        q.float_cap = _get_field(fields, extra.get("float_cap", 44))
        q.market_cap = _get_field(fields, extra.get("market_cap", 45))
        q.pb = _get_field(fields, extra.get("pb", 46))
        q.limit_up = _get_field(fields, extra.get("limit_up", 47))
        q.limit_down = _get_field(fields, extra.get("limit_down", 48))
        q.volume_ratio = _get_field(fields, extra.get("volume_ratio", 49))
        q.avg_price = _get_field(fields, extra.get("avg_price", 51))
        q.dividend_yield = _get_field(fields, extra.get("dividend_yield", 56))
        q.high_52w = _get_field(fields, extra.get("high_52w", 67))
        q.low_52w = _get_field(fields, extra.get("low_52w", 68))
        q.currency = _get_field_str(fields, extra.get("currency", 82), "CNY")

    elif market == "HK":
        extra = _HK_EXTRA
        # 港股 volume：优先用 [29] 实数，否则 [6]
        vol_real = _get_field(fields, extra.get("volume_real", 29))
        q.volume = vol_real if vol_real > 0 else _get_field(fields, 6)
        # 成交额 [37]
        q.amount = _get_field(fields, 37)
        q.turnover_rate = _get_field(fields, extra.get("turnover_rate", 47))
        q.pe_ttm = _get_field(fields, extra.get("pe_ttm", 39))
        q.amplitude = _get_field(fields, extra.get("amplitude", 43))
        q.market_cap = _get_field(fields, extra.get("market_cap", 44))
        q.float_cap = _get_field(fields, extra.get("float_cap", 45))
        q.high_52w = _get_field(fields, extra.get("high_52w", 48))
        q.low_52w = _get_field(fields, extra.get("low_52w", 49))
        q.volume_ratio = _get_field(fields, extra.get("volume_ratio", 50))
        q.pb = _get_field(fields, extra.get("pb", 57))
        q.avg_price = _get_field(fields, extra.get("avg_price", 73))
        q.currency = _get_field_str(fields, extra.get("currency", 75), "HKD")

    elif market == "US":
        extra = _US_EXTRA
        # 美股 volume：优先用 [36] 实数
        vol_real = _get_field(fields, extra.get("volume_real", 36))
        q.volume = vol_real if vol_real > 0 else _get_field(fields, 6)
        q.amount = _get_field(fields, extra.get("amount", 37))
        q.turnover_rate = _get_field(fields, extra.get("turnover_rate", 38))
        q.pe_ttm = _get_field(fields, extra.get("pe_ttm", 39))
        q.market_cap = _get_field(fields, extra.get("market_cap", 44))
        q.float_cap = _get_field(fields, extra.get("float_cap", 45))
        q.high_52w = _get_field(fields, extra.get("high_52w", 48))
        q.low_52w = _get_field(fields, extra.get("low_52w", 49))
        q.pb = _get_field(fields, extra.get("pb", 57))
        q.avg_price = _get_field(fields, extra.get("avg_price", 67))
        q.currency = "USD"

    return q


def parse_quotes(raw_text: str, symbols: List[str]) -> Dict[str, TencentQuote]:
    """
    解析批量行情响应。

    Args:
        raw_text: qt.gtimg.cn 返回的完整文本（多行）
        symbols: 请求时使用的 symbol 列表（与行顺序对应）

    Returns:
        {symbol: TencentQuote}
    """
    lines = raw_text.strip().split("\n")
    result: Dict[str, TencentQuote] = {}
    for i, line in enumerate(lines):
        if i >= len(symbols):
            break
        if not line.strip():
            continue
        q = parse_quote(symbols[i], line)
        if q and q.is_valid:
            result[symbols[i]] = q
    return result


# ─── HTTP 工具 ───────────────────────────────────────────────────────────────


def _http_get(url: str, timeout: int = 8, encoding: str = "gbk") -> Optional[str]:
    """带熔断器的 HTTP GET"""
    try:
        from core.circuit_breaker import get_breaker
        cb = get_breaker("tencent_qt", failure_threshold=3, cooldown_seconds=120.0)
        if not cb.allow():
            logger.warning("腾讯行情源熔断中（state=%s），跳过", cb.state())
            return None
    except Exception:
        cb = None

    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            data = resp.read().decode(encoding, errors="replace")
        if cb:
            cb.on_success()
        return data
    except Exception as exc:
        logger.debug("HTTP GET failed %s: %s", url, exc)
        if cb:
            cb.on_failure()
        return None


# ─── 数据源类 ────────────────────────────────────────────────────────────────


class TencentQuoteDataSource:
    """
    腾讯 qt.gtimg.cn 统一行情数据源。

    支持 A 股 / 指数 / 港股 / 美股，支持批量请求。
    内置 TTL 内存缓存和熔断器。

    Usage:
        src = TencentQuoteDataSource()
        q = src.fetch_quote('hk00700')
        quotes = src.fetch_quotes(['sh600519', 'hk00700', 'usAAPL'])
    """

    def __init__(self, cache_ttl: float = 30.0):
        """
        Args:
            cache_ttl: 缓存 TTL（秒），默认 30s
        """
        self._cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # symbol → (quote, expire_at)
        self._lock = threading.Lock()

    def _cache_get(self, symbol: str) -> Optional[TencentQuote]:
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            quote, expire_at = entry
            if time.monotonic() > expire_at:
                del self._cache[symbol]
                return None
            return quote

    def _cache_set(self, symbol: str, quote: TencentQuote) -> None:
        with self._lock:
            self._cache[symbol] = (quote, time.monotonic() + self._cache_ttl)

    def fetch_quote(self, symbol: str) -> Optional[TencentQuote]:
        """
        获取单只标的行情。

        Args:
            symbol: 标的代码，支持格式：
                - A 股: 'sh600519', 'sz000001', '600519.SH'
                - 指数: 'sh000001', 'sz399006'
                - 港股: 'hk00700', 'HK:00700'
                - 美股: 'usAAPL', 'US:AAPL'

        Returns:
            TencentQuote 或 None
        """
        normalized = self._normalize_symbol(symbol)
        cached = self._cache_get(normalized)
        if cached is not None:
            return cached

        result = self._fetch_single(normalized)
        if result and result.is_valid:
            self._cache_set(normalized, result)
        return result

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, TencentQuote]:
        """
        批量获取行情（自动分片，每批最多 50 只）。

        Args:
            symbols: 标的代码列表

        Returns:
            {原始symbol: TencentQuote}
        """
        if not symbols:
            return {}

        # 分离缓存命中和未命中
        result: Dict[str, TencentQuote] = {}
        missing: List[str] = []
        normalized_map: Dict[str, str] = {}  # normalized → original

        for sym in symbols:
            normalized = self._normalize_symbol(sym)
            normalized_map[normalized] = sym
            cached = self._cache_get(normalized)
            if cached is not None:
                result[sym] = cached
            else:
                missing.append(normalized)

        if not missing:
            return result

        # 分批请求（API 支持混合市场，无需按市场分组）
        for i in range(0, len(missing), BATCH_LIMIT):
            batch = missing[i : i + BATCH_LIMIT]
            url = "https://qt.gtimg.cn/q=" + ",".join(batch)
            raw = _http_get(url)
            if raw:
                parsed = parse_quotes(raw, batch)
                for norm_sym, q in parsed.items():
                    self._cache_set(norm_sym, q)
                    orig = normalized_map.get(norm_sym, norm_sym)
                    result[orig] = q

        return result

    def clear_cache(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    # ── K 线数据 ─────────────────────────────────────────────────────────

    def fetch_kline(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "year"] = "day",
        start: str = "2020-01-01",
        end: str = "2030-01-01",
        limit: int = 120,
        adjust: Literal["qfq", "hfq", "none"] = "qfq",
    ) -> pd.DataFrame:
        """
        获取 K 线数据（日/周/月/年），全市场通用。

        Args:
            symbol: 标的代码（任意格式，自动标准化）
            period: 'day' | 'week' | 'month' | 'year'
            start: 起始日期 'YYYY-MM-DD'
            end: 结束日期 'YYYY-MM-DD'
            limit: 返回根数
            adjust: 'qfq'=前复权 | 'hfq'=后复权 | 'none'=不复权

        Returns:
            DataFrame，列：date(datetime), open, close, high, low, volume
            失败时返回空 DataFrame

        Note:
            - year K 线仅有 1 根（起始年至今日），无历史年 K
            - adjust='none' 实际返回前复权（腾讯接口限制）
        """
        normalized = self._normalize_symbol(symbol)
        bars = self._fetch_kline_raw(normalized, period, start, end, limit, adjust)
        if not bars:
            return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])

        rows = []
        for bar in bars:
            if len(bar) < 6:
                continue
            try:
                rows.append({
                    "date": bar[0],
                    "open": float(bar[1]),
                    "close": float(bar[2]),
                    "high": float(bar[3]),
                    "low": float(bar[4]),
                    "volume": float(bar[5]),
                })
            except (ValueError, IndexError):
                continue

        if not rows:
            return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

    def fetch_minute_kline(
        self,
        symbol: str,
        period: Literal["1m", "5m", "15m", "30m", "60m"] = "5m",
        start: str = "",
        end: str = "",
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        获取分钟 K 线（⚠️ 仅港股可用，A 股/指数/美股不可用）。

        Args:
            symbol: 港股代码（如 'hk00700', 'HK:00700'）
            period: '1m' | '5m' | '15m' | '30m' | '60m'
            start: 起始日期 'YYYY-MM-DD'（可选，默认最近交易日）
            end: 结束日期 'YYYY-MM-DD'（可选）
            limit: 返回根数

        Returns:
            DataFrame，列：datetime, open, close, high, low, volume
            失败时返回空 DataFrame
        """
        normalized = self._normalize_symbol(symbol)
        market = _detect_market(normalized)
        if market != "HK":
            logger.warning("分钟 K 线仅支持港股，%s 的市场类型为 %s", symbol, market)
            return pd.DataFrame(columns=["datetime", "open", "close", "high", "low", "volume"])

        # 去掉 'm' 后缀，腾讯接口用纯数字
        p = period.replace("m", "")
        bars = self._fetch_kline_raw(normalized, p, start, end, limit, "qfq")
        if not bars:
            return pd.DataFrame(columns=["datetime", "open", "close", "high", "low", "volume"])

        rows = []
        for bar in bars:
            if len(bar) < 6:
                continue
            try:
                dt_str = bar[0]
                # 分钟 K 线时间格式：YYYYMMDDHHMMSS 或 YYYY-MM-DD HH:MM:SS
                if len(dt_str) == 14 and dt_str.isdigit():
                    dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
                else:
                    dt = pd.to_datetime(dt_str)
                rows.append({
                    "datetime": dt,
                    "open": float(bar[1]),
                    "close": float(bar[2]),
                    "high": float(bar[3]),
                    "low": float(bar[4]),
                    "volume": float(bar[5]),
                })
            except (ValueError, IndexError):
                continue

        if not rows:
            return pd.DataFrame(columns=["datetime", "open", "close", "high", "low", "volume"])

        df = pd.DataFrame(rows)
        df = df.sort_values("datetime").reset_index(drop=True)
        return df

    def _fetch_kline_raw(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
        limit: int,
        adjust: str,
    ) -> Optional[list]:
        """
        底层 K 线请求，返回原始 bar 列表。

        URL: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
             ?_var=kline_dayqfq
             &param={symbol},{period},{start},{end},{limit},{adjust}

        响应: var kline_dayqfq = {"data": {"sh600519": {"qfqday": [[...], ...]}}}

        注意: 日期必须为 YYYY-MM-DD 格式，YYYYMMDD 会导致 API 返回空数据。
        """

        # 统一日期格式为 YYYY-MM-DD
        def _fmt_date(d: str) -> str:
            d = d.strip()
            if len(d) == 8 and d.isdigit():
                return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            return d

        start = _fmt_date(start)
        end = _fmt_date(end)

        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq"
            f"&param={symbol},{period},{start},{end},{limit},{adjust}"
        )
        raw = _http_get(url, encoding="utf-8")
        if not raw:
            return None

        # 去掉 "var xxx = " 前缀
        eq = raw.find("=")
        if eq >= 0:
            raw = raw[eq + 1:].strip().rstrip(";")

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.debug("K 线 JSON 解析失败 %s: %s", symbol, e)
            return None

        data = obj.get("data", {})
        # API 可能返回空列表（如日期格式错误时）
        if isinstance(data, list):
            logger.debug("K 线 API 返回空数据列表 %s (可能日期格式错误)", symbol)
            return None
        sym_data = data.get(symbol, {})
        if not sym_data:
            return None

        # 查找匹配的 key：
        #   qfq + period → 'qfqday', 'qfqweek', 'qfqmonth'
        #   hfq + period → 'hfqday', 'hfqweek', 'hfqmonth'
        #   none/空 → 'day', 'week', 'month'
        #   港股/美股：直接用 period（'day', 'week'）无前缀
        prefix = adjust if adjust != "none" else ""
        candidates = []
        if prefix:
            candidates.append(f"{prefix}{period}")
        candidates.append(period)
        # 兼容 qfqday → day 等
        candidates.extend([f"qfq{period}", f"hfq{period}", "day"])

        bars = None
        for key in candidates:
            if key in sym_data:
                bars = sym_data[key]
                break

        # 最后尝试：遍历所有 key 找包含 period 的
        if bars is None:
            for key, val in sym_data.items():
                if isinstance(val, list) and period in key:
                    bars = val
                    break

        return bars

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _normalize_symbol(self, symbol: str) -> str:
        """
        标准化 symbol 为腾讯格式。

        '600519.SH' → 'sh600519'
        '000001.SZ' → 'sz000001'
        'HK:00700'  → 'hk00700'
        'US:AAPL'   → 'usAAPL'  （美股代码区分大小写）
        'sh600519'  → 'sh600519'（不变）
        """
        s = symbol.strip()

        # 处理 HK:xxx / US:xxx 格式
        if s.upper().startswith("HK:"):
            code = s[3:].strip()
            # 纯数字代码补齐 5 位（如 700 → 00700），字母代码保留原样（如 HSI）
            if code.isdigit():
                return f"hk{code.zfill(5)}"
            return f"hk{code}"
        if s.upper().startswith("US:"):
            return f"us{s[3:].strip()}"

        # 处理 xxx.SH / xxx.SZ 格式
        upper = s.upper()
        if upper.endswith(".SH"):
            return f"sh{s[:-3].strip()}"
        if upper.endswith(".SZ"):
            return f"sz{s[:-3].strip()}"
        if upper.endswith(".HK"):
            code = s[:-3].strip()
            if code.isdigit():
                return f"hk{code.zfill(5)}"
            return f"hk{code}"

        # 已经是 us/hk 格式（代码部分区分大小写，保留原样）
        # hkHSI, hkHSTECH, usAAPL 等需要保留混合大小写
        if s.lower().startswith(("us", "hk")):
            return s  # 保留原始大小写

        # 已经是 sh/sz 格式（A 股不区分大小写）
        lower = s.lower()
        if lower.startswith(("sh", "sz")):
            return lower

        # 纯数字代码，根据规则判断市场
        if s.isdigit():
            if s.startswith(("60", "68", "5")):
                return f"sh{s}"
            return f"sz{s}"

        # 纯字母，假设美股（保留大小写）
        if s.isalpha():
            return f"us{s.upper()}"

        return lower

    def _fetch_single(self, normalized: str) -> Optional[TencentQuote]:
        """获取单只标的（直接 HTTP）"""
        url = f"https://qt.gtimg.cn/q={normalized}"
        raw = _http_get(url)
        if not raw:
            return None
        return parse_quote(normalized, raw)


# ─── 便捷函数 ────────────────────────────────────────────────────────────────

_default_source: Optional[TencentQuoteDataSource] = None
_default_lock = threading.Lock()


def get_tencent_source() -> TencentQuoteDataSource:
    """获取全局 TencentQuoteDataSource 单例"""
    global _default_source
    with _default_lock:
        if _default_source is None:
            _default_source = TencentQuoteDataSource()
    return _default_source


def fetch_tencent_quote(symbol: str) -> Optional[TencentQuote]:
    """便捷函数：获取单只行情"""
    return get_tencent_source().fetch_quote(symbol)


def fetch_tencent_quotes(symbols: List[str]) -> Dict[str, TencentQuote]:
    """便捷函数：批量获取行情"""
    return get_tencent_source().fetch_quotes(symbols)


def fetch_tencent_kline(
    symbol: str,
    period: str = "day",
    start: str = "2020-01-01",
    end: str = "2030-01-01",
    limit: int = 120,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """便捷函数：获取 K 线"""
    return get_tencent_source().fetch_kline(symbol, period, start, end, limit, adjust)

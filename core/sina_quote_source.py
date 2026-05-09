# -*- coding: utf-8 -*-
"""
sina_quote_source.py — 新浪财经统一行情数据源
==============================================

合并 5 处重复的新浪 HTTP 代码为统一模块。

能力矩阵:
  - A 股实时行情 (hq.sinajs.cn, 34 字段含 5 档盘口)
  - 港股实时行情 (hq.sinajs.cn, 19 字段)
  - A 股日 K 线 (money.finance.sina.com.cn, scale=240, 最多 6000 根)
  - A 股分钟 K 线 (同上, scale=5/15/30/60)

限制:
  - 不支持美股行情/K 线
  - 港股 K 线不可靠（常返回 'null'）
  - 日 K 线无服务器端日期过滤，需客户端过滤

Usage:
  from core.sina_quote_source import get_sina_source, fetch_sina_quote
  src = get_sina_source()
  quote = src.fetch_quote('sh600519')
"""

import json
import logging
import random
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from .quote_data_source import (
    QuoteData,
    QuoteDataSource,
    _safe_float,
    _safe_int,
    detect_market,
    normalize_to_sina,
)

logger = logging.getLogger('sina_quote_source')


# ─── 常量 ────────────────────────────────────────────────────────────────────

_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/CN_MarketData.getKLineData"
)

_QUOTE_URL = "https://hq.sinajs.cn/list="

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

# 分钟 K 线周期映射
_PERIOD_TO_SCALE = {
    '1m': 1, '5m': 5, '15m': 15, '30m': 30, '60m': 60,
    '1': 1, '5': 5, '15': 15, '30': 30, '60': 60,
}


# ─── 解析器 ──────────────────────────────────────────────────────────────────


def _parse_a_share_quote(symbol: str, raw_line: str) -> Optional[QuoteData]:
    """
    解析新浪 A 股实时行情（34 字段）。

    字段布局（经实测 2026-04-15 验证）：
      [0] 股票代码  [1] 开盘  [2] 昨收  [3] 当前  [4] 最高  [5] 最低
      [8] 成交量(股)  [9] 成交额(元)
      [11,13,15,17,19] 买1-5价  [12,14,16,18,20] 买1-5量
      [21,23,25,27,29] 卖1-5价  [22,24,26,28] 卖1-4量
      [30] 日期  [31] 时间
    """
    try:
        content = raw_line.split('"')[1] if '"' in raw_line else ''
        if not content:
            return None
        fields = content.split(',')
        if len(fields) < 32:
            return None

        price = _safe_float(fields[3])
        if price <= 0:
            return None

        prev_close = _safe_float(fields[2])
        change = price - prev_close if prev_close else 0
        pct_change = (change / prev_close * 100) if prev_close else 0

        dt_str = f"{fields[30]} {fields[31]}" if len(fields) > 31 else ''

        return QuoteData(
            symbol=symbol,
            name=fields[0].strip(),
            code=symbol[2:] if len(symbol) > 2 else symbol,
            market=detect_market(symbol),
            price=price,
            prev_close=prev_close,
            open=_safe_float(fields[1]),
            high=_safe_float(fields[4]),
            low=_safe_float(fields[5]),
            change=change,
            pct_change=round(pct_change, 4),
            volume=_safe_float(fields[8]),
            amount=_safe_float(fields[9]),
            bid1_price=_safe_float(fields[11]) if len(fields) > 11 else 0,
            bid1_vol=_safe_float(fields[12]) if len(fields) > 12 else 0,
            ask1_price=_safe_float(fields[21]) if len(fields) > 21 else 0,
            ask1_vol=_safe_float(fields[22]) if len(fields) > 22 else 0,
            timestamp=dt_str,
            source='sina',
        )
    except Exception as e:
        logger.debug("解析 A 股行情失败 %s: %s", symbol, e)
        return None


def _parse_hk_quote(symbol: str, raw_line: str) -> Optional[QuoteData]:
    """
    解析新浪港股实时行情（19 字段）。

    字段布局：
      [0] 英文名  [1] 中文名  [2] 开盘  [3] 昨收  [4] 最高  [5] 最低
      [6] 最新  [7] 涨跌额  [8] 涨跌幅  [9] 买1价  [10] 买1量
      [11] 成交量  [12] 成交额  [13] 52周高  [14] 52周低
      [15] 市值  [16-17] 日期时间
    """
    try:
        content = raw_line.split('"')[1] if '"' in raw_line else ''
        if not content:
            return None
        fields = content.split(',')
        if len(fields) < 19:
            return None

        price = _safe_float(fields[6])
        if price <= 0:
            return None

        dt_str = f"{fields[17]} {fields[18]}" if len(fields) > 18 else ''
        # 港股日期格式: YYYY/MM/DD HH:MM
        ts = ''
        try:
            ts = datetime.strptime(dt_str.strip(), '%Y/%m/%d %H:%M').strftime('%Y-%m-%d %H:%M')
        except Exception:
            ts = dt_str

        name_cn = fields[1].strip()
        name_en = fields[0].strip()
        name = name_cn or name_en

        return QuoteData(
            symbol=symbol,
            name=name,
            code=symbol[2:] if len(symbol) > 2 else symbol,
            market='HK',
            price=price,
            prev_close=_safe_float(fields[3]),
            open=_safe_float(fields[2]),
            high=_safe_float(fields[4]),
            low=_safe_float(fields[5]),
            change=_safe_float(fields[7]),
            pct_change=_safe_float(fields[8]),
            volume=_safe_float(fields[11]),
            amount=_safe_float(fields[12]),
            bid1_price=_safe_float(fields[9]) if len(fields) > 9 else 0,
            bid1_vol=_safe_float(fields[10]) if len(fields) > 10 else 0,
            high_52w=_safe_float(fields[13]) if len(fields) > 13 else 0,
            low_52w=_safe_float(fields[14]) if len(fields) > 14 else 0,
            market_cap=_safe_float(fields[15]) if len(fields) > 15 else 0,
            timestamp=ts,
            currency='HKD',
            source='sina',
        )
    except Exception as e:
        logger.debug("解析港股行情失败 %s: %s", symbol, e)
        return None


# ─── 数据源类 ────────────────────────────────────────────────────────────────


class SinaQuoteDataSource(QuoteDataSource):
    """新浪财经统一行情数据源"""

    name = "SinaQuoteDataSource"

    def __init__(self, cache_ttl: float = 30.0):
        self._cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}  # {symbol: (QuoteData, timestamp)}
        self._cache_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_request_time: float = 0.0

    # ── 缓存 ──────────────────────────────────────────────────────────────

    def _cache_get(self, symbol: str) -> Optional[QuoteData]:
        with self._cache_lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            quote, ts = entry
            if time.monotonic() - ts > self._cache_ttl:
                del self._cache[symbol]
                return None
            return quote

    def _cache_set(self, symbol: str, quote: QuoteData) -> None:
        with self._cache_lock:
            self._cache[symbol] = (quote, time.monotonic())

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # ── 速率限制 ──────────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        """同域名 200ms 最低间隔"""
        with self._rate_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < 0.2:
                time.sleep(0.2 - elapsed)
            self._last_request_time = time.time()

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _http_get(self, url: str, encoding: str = 'utf-8', timeout: int = 8) -> Optional[str]:
        """带熔断器的 HTTP GET"""
        from .circuit_breaker import get_breaker
        cb = get_breaker('sina_quote', failure_threshold=3, cooldown_seconds=120.0)
        if not cb.allow():
            logger.warning("[SinaQuote] 熔断中，跳过请求")
            return None

        self._rate_limit()
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': random.choice(_USER_AGENTS),
                'Referer': 'https://finance.sina.com.cn',
            })
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
                raw = resp.read().decode(encoding, errors='replace')
            cb.on_success()
            return raw
        except Exception as e:
            cb.on_failure()
            logger.warning("[SinaQuote] HTTP 请求失败: %s", e)
            return None

    # ── 公开 API: 实时行情 ────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[QuoteData]:
        """获取单只标的实时行情"""
        cached = self._cache_get(symbol)
        if cached is not None:
            return cached

        sina_code = normalize_to_sina(symbol)
        market = detect_market(symbol)

        raw = self._http_get(f"{_QUOTE_URL}{sina_code}", encoding='gbk')
        if not raw:
            return None

        if market == 'HK':
            quote = _parse_hk_quote(sina_code, raw)
        else:
            quote = _parse_a_share_quote(sina_code, raw)

        if quote and quote.is_valid:
            self._cache_set(symbol, quote)
            return quote
        return None

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteData]:
        """批量获取实时行情"""
        if not symbols:
            return {}

        result: Dict[str, QuoteData] = {}
        missing: List[str] = []

        # 检查缓存
        for sym in symbols:
            cached = self._cache_get(sym)
            if cached is not None:
                result[sym] = cached
            else:
                missing.append(sym)

        if not missing:
            return result

        # 按市场分组批量请求
        a_share_codes: List[str] = []
        hk_codes: List[str] = []
        code_to_symbol: Dict[str, str] = {}

        for sym in missing:
            sina_code = normalize_to_sina(sym)
            market = detect_market(sym)
            code_to_symbol[sina_code] = sym
            if market == 'HK':
                hk_codes.append(sina_code)
            else:
                a_share_codes.append(sina_code)

        # A 股批量
        if a_share_codes:
            raw = self._http_get(f"{_QUOTE_URL}{','.join(a_share_codes)}", encoding='gbk')
            if raw:
                for line in raw.strip().split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    # 提取代码: var hq_str_sh600519="..."
                    try:
                        key = line.split('=')[0].split('_')[-1]
                        sym = code_to_symbol.get(key, key)
                        quote = _parse_a_share_quote(sym, line)
                        if quote and quote.is_valid:
                            result[sym] = quote
                            self._cache_set(sym, quote)
                    except Exception:
                        continue

        # 港股批量
        if hk_codes:
            raw = self._http_get(f"{_QUOTE_URL}{','.join(hk_codes)}", encoding='gbk')
            if raw:
                for line in raw.strip().split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        key = line.split('=')[0].split('_')[-1]
                        sym = code_to_symbol.get(key, key)
                        quote = _parse_hk_quote(sym, line)
                        if quote and quote.is_valid:
                            result[sym] = quote
                            self._cache_set(sym, quote)
                    except Exception:
                        continue

        return result

    # ── 公开 API: K 线 ────────────────────────────────────────────────────

    def fetch_daily_kline(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取日 K 线数据（A 股）。

        新浪接口返回全量历史（最多 6000 根），客户端截取最近 days 根。
        """
        sina_code = normalize_to_sina(symbol)
        url = f"{_KLINE_URL}?symbol={sina_code}&scale=240&ma=no&datalen={min(days, 6000)}"

        raw = self._http_get(url)
        if not raw:
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        # HK K 线常返回 'null'
        if raw.strip() == 'null' or raw.strip().startswith('null'):
            logger.info("[SinaQuote] HK K 线返回 null: %s", symbol)
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        try:
            data_list = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[SinaQuote] K 线 JSON 解析失败: %s", symbol)
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        if not data_list or not isinstance(data_list, list):
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        rows = []
        for item in data_list:
            if not isinstance(item, dict):
                continue
            rows.append({
                'date': item.get('day', ''),
                'open': _safe_float(item.get('open')),
                'high': _safe_float(item.get('high')),
                'low': _safe_float(item.get('low')),
                'close': _safe_float(item.get('close')),
                'volume': _safe_float(item.get('volume')),
            })

        if not rows:
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        return df

    def fetch_minute_kline(
        self,
        symbol: str,
        period: str = "15m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        获取分钟 K 线数据（A 股）。

        Args:
            period: '1m', '5m', '15m', '30m', '60m'
            limit: 返回的 K 线根数
        """
        scale = _PERIOD_TO_SCALE.get(period)
        if scale is None:
            logger.warning("[SinaQuote] 不支持的周期: %s", period)
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        sina_code = normalize_to_sina(symbol)
        url = f"{_KLINE_URL}?symbol={sina_code}&scale={scale}&ma=no&datalen={limit}"

        raw = self._http_get(url)
        if not raw:
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        if raw.strip() == 'null' or raw.strip().startswith('null'):
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        try:
            data_list = json.loads(raw)
        except json.JSONDecodeError:
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        if not data_list or not isinstance(data_list, list):
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        rows = []
        for item in data_list:
            if not isinstance(item, dict):
                continue
            rows.append({
                'datetime': item.get('day', ''),
                'open': _safe_float(item.get('open')),
                'high': _safe_float(item.get('high')),
                'low': _safe_float(item.get('low')),
                'close': _safe_float(item.get('close')),
                'volume': _safe_float(item.get('volume')),
            })

        if not rows:
            return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])

        df = pd.DataFrame(rows)
        return df

    def supported_markets(self) -> List[str]:
        return ['A', 'HK']


# ─── 单例与便捷函数 ──────────────────────────────────────────────────────────

_sina_source: Optional[SinaQuoteDataSource] = None
_sina_lock = threading.Lock()


def get_sina_source() -> SinaQuoteDataSource:
    """获取全局 SinaQuoteDataSource 单例"""
    global _sina_source
    if _sina_source is None:
        with _sina_lock:
            if _sina_source is None:
                _sina_source = SinaQuoteDataSource()
    return _sina_source


def fetch_sina_quote(symbol: str) -> Optional[QuoteData]:
    """便捷函数：获取单只行情"""
    return get_sina_source().fetch_quote(symbol)


def fetch_sina_quotes(symbols: List[str]) -> Dict[str, QuoteData]:
    """便捷函数：批量获取行情"""
    return get_sina_source().fetch_quotes(symbols)


def fetch_sina_daily_kline(symbol: str, days: int = 120) -> pd.DataFrame:
    """便捷函数：获取日 K 线"""
    return get_sina_source().fetch_daily_kline(symbol, days=days)


def fetch_sina_minute_kline(symbol: str, period: str = "15m", limit: int = 100) -> pd.DataFrame:
    """便捷函数：获取分钟 K 线"""
    return get_sina_source().fetch_minute_kline(symbol, period=period, limit=limit)

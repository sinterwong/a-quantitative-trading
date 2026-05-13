# -*- coding: utf-8 -*-
"""
data_gateway.providers.sina — 新浪财经数据源

能力矩阵:
  ┌────────────────┬──────┬───────┬──────┐
  │ 数据类型        │ A 股 │ INDEX │ HK   │
  ├────────────────┼──────┼───────┼──────┤
  │ QUOTE          │ ✓    │ ✓    │ ✓    │
  │ KLINE_DAILY    │ ✓    │ ✓    │ ✗    │
  │ KLINE_MINUTE   │ ✓    │ ✓    │ ✗    │
  └────────────────┴──────┴───────┴──────┘

字段权威声明:
  Quote 中的 5 档买卖盘(bid1/ask1)、成交时间戳由新浪权威覆盖。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..http import HttpClient, HttpError, get_http_client
from ..schemas import Quote
from ..symbols import detect_market, normalize_to_sina, safe_float
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.sina")

_QUOTE_URL = "https://hq.sinajs.cn/list="
_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/CN_MarketData.getKLineData"
)
_HEADERS = {"Referer": "https://finance.sina.com.cn"}

_PERIOD_TO_SCALE = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
    "daily": 240, "weekly": 1200, "monthly": 4800,
}


def _split_payload(raw_line: str) -> List[str]:
    """从 var hq_str_sh600519="..." 中切出字段数组。"""
    if '"' not in raw_line:
        return []
    content = raw_line.split('"')[1]
    return content.split(",") if content else []


def _parse_a_share(symbol: str, raw_line: str) -> Optional[Quote]:
    """A 股 34 字段解析。"""
    fields = _split_payload(raw_line)
    if len(fields) < 32:
        return None
    price = safe_float(fields[3])
    if price <= 0:
        return None
    prev_close = safe_float(fields[2])
    change = price - prev_close if prev_close else 0
    pct = (change / prev_close * 100) if prev_close else 0

    dt_str = f"{fields[30]} {fields[31]}" if len(fields) > 31 else ""
    try:
        ts = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        ts = datetime.now()

    return Quote(
        symbol=symbol,
        name=fields[0].strip(),
        code=symbol[2:] if len(symbol) > 2 else symbol,
        market=detect_market(symbol).value,
        price=price,
        prev_close=prev_close,
        open=safe_float(fields[1]),
        high=safe_float(fields[4]),
        low=safe_float(fields[5]),
        change=change,
        pct_change=round(pct, 4),
        volume=safe_float(fields[8]),
        amount=safe_float(fields[9]),
        bid1_price=safe_float(fields[11]) if len(fields) > 11 else 0,
        bid1_vol=safe_float(fields[12]) if len(fields) > 12 else 0,
        ask1_price=safe_float(fields[21]) if len(fields) > 21 else 0,
        ask1_vol=safe_float(fields[22]) if len(fields) > 22 else 0,
        timestamp=ts,
        currency="CNY",
    )


def _parse_hk(symbol: str, raw_line: str) -> Optional[Quote]:
    """港股 19 字段解析。"""
    fields = _split_payload(raw_line)
    if len(fields) < 19:
        return None
    price = safe_float(fields[6])
    if price <= 0:
        return None

    dt_str = f"{fields[17]} {fields[18]}" if len(fields) > 18 else ""
    try:
        ts = datetime.strptime(dt_str.strip(), "%Y/%m/%d %H:%M")
    except (ValueError, IndexError):
        ts = datetime.now()

    name = (fields[1].strip() or fields[0].strip())
    return Quote(
        symbol=symbol,
        name=name,
        code=symbol[2:] if len(symbol) > 2 else symbol,
        market=Market.HK.value,
        price=price,
        prev_close=safe_float(fields[3]),
        open=safe_float(fields[2]),
        high=safe_float(fields[4]),
        low=safe_float(fields[5]),
        change=safe_float(fields[7]),
        pct_change=safe_float(fields[8]),
        volume=safe_float(fields[11]),
        amount=safe_float(fields[12]),
        bid1_price=safe_float(fields[9]),
        bid1_vol=safe_float(fields[10]),
        high_52w=safe_float(fields[13]),
        low_52w=safe_float(fields[14]),
        market_cap=safe_float(fields[15]),
        currency="HKD",
        timestamp=ts,
    )


# ─── Provider ──────────────────────────────────────────────────────────────────


class SinaProvider(Provider):
    """新浪 hq.sinajs.cn / money.finance.sina.com.cn 数据源。"""

    name = "sina"

    def __init__(self, http: Optional[HttpClient] = None):
        self._http = http or get_http_client()

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.QUOTE,
                Capability.KLINE_DAILY,
                Capability.KLINE_MINUTE,
            }),
            markets=frozenset({Market.A, Market.INDEX, Market.HK}),
            priority_hint=0.80,
        )

    def supports(self, capability: Capability, market: Market) -> bool:
        # 新浪港股 K 线不稳定，视为不支持
        if capability in (Capability.KLINE_DAILY, Capability.KLINE_MINUTE):
            if market == Market.HK:
                return False
        return super().supports(capability, market)

    def field_authority(self) -> Dict[Capability, Dict[str, float]]:
        return {
            Capability.QUOTE: {
                "bid1_price": 1.2, "bid1_vol": 1.2,
                "ask1_price": 1.2, "ask1_vol": 1.2,
            }
        }

    # ── QUOTE ────────────────────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        sina_code = normalize_to_sina(symbol)
        market = detect_market(sina_code)
        try:
            text = self._http.get_text(
                f"{_QUOTE_URL}{sina_code}",
                headers=_HEADERS,
                encoding="gbk",
            )
        except HttpError as exc:
            raise ProviderError(f"sina.fetch_quote({sina_code}): {exc}") from exc

        if market == Market.HK:
            return _parse_hk(sina_code, text)
        return _parse_a_share(sina_code, text)

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}

        # 按市场分组(批 URL 区分 A / HK 解析路径)
        a_codes: List[str] = []
        hk_codes: List[str] = []
        code_to_sym: Dict[str, str] = {}
        for s in symbols:
            sina = normalize_to_sina(s)
            code_to_sym[sina] = s
            if detect_market(sina) == Market.HK:
                hk_codes.append(sina)
            else:
                a_codes.append(sina)

        result: Dict[str, Quote] = {}
        for codes, parser in [(a_codes, _parse_a_share), (hk_codes, _parse_hk)]:
            if not codes:
                continue
            try:
                text = self._http.get_text(
                    f"{_QUOTE_URL}{','.join(codes)}",
                    headers=_HEADERS,
                    encoding="gbk",
                )
            except HttpError as exc:
                raise ProviderError(f"sina.fetch_quotes: {exc}") from exc
            for line in text.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                try:
                    key = line.split("=")[0].split("_")[-1]
                except IndexError:
                    continue
                orig = code_to_sym.get(key, key)
                q = parser(key, line)
                if q is not None and q.is_valid:
                    result[orig] = q
        return result

    # ── KLINE ────────────────────────────────────────────────────────────────

    def fetch_kline_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        """日 K 线。新浪港股 K 线不稳定，supports() 已排除 HK。"""
        sina_code = normalize_to_sina(symbol)
        scale = _PERIOD_TO_SCALE.get("daily")
        if scale is None:
            return pd.DataFrame()
        n = min(days, 6000)
        url = f"{_KLINE_URL}?symbol={sina_code}&scale={scale}&ma=no&datalen={n}"
        return self._fetch_and_parse_kline(url, sina_code, is_minute=False)

    def fetch_kline_minute(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """分钟 K 线。新浪港股 K 线不稳定，supports() 已排除 HK。"""
        sina_code = normalize_to_sina(symbol)
        scale = _PERIOD_TO_SCALE.get(interval)
        if scale is None:
            return pd.DataFrame()
        url = f"{_KLINE_URL}?symbol={sina_code}&scale={scale}&ma=no&datalen={limit}"
        return self._fetch_and_parse_kline(url, sina_code, is_minute=True)

    def _fetch_and_parse_kline(
        self, url: str, sina_code: str, *, is_minute: bool
    ) -> pd.DataFrame:
        try:
            text = self._http.get_text(url, headers=_HEADERS, encoding="utf-8")
        except HttpError as exc:
            raise ProviderError(f"sina.kline({sina_code}): {exc}") from exc

        s = text.strip()
        if s == "null" or s.startswith("null"):
            return pd.DataFrame()
        try:
            items = json.loads(s)
        except (ValueError, json.JSONDecodeError):
            return pd.DataFrame()
        if not isinstance(items, list) or not items:
            return pd.DataFrame()

        time_col = "datetime" if is_minute else "date"
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append({
                time_col: item.get("day", ""),
                "open": safe_float(item.get("open")),
                "high": safe_float(item.get("high")),
                "low": safe_float(item.get("low")),
                "close": safe_float(item.get("close")),
                "volume": safe_float(item.get("volume")),
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
        df = df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)
        return df


__all__ = ["SinaProvider"]

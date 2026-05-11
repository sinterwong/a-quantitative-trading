# -*- coding: utf-8 -*-
"""
data_gateway.providers.tencent — 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn 数据源

能力矩阵:
  ┌────────────────┬──────┬───────┬──────┬──────┐
  │ 数据类型        │ A 股 │ INDEX │ HK   │ US   │
  ├────────────────┼──────┼───────┼──────┼──────┤
  │ QUOTE          │ ✓    │ ✓    │ ✓    │ ✓    │
  │ KLINE_DAILY    │ ✓    │ ✓    │ ✓    │ ✓    │
  │ KLINE_MINUTE   │ ✗    │ ✗    │ ✓    │ ✗    │
  │ MARKET_INDEX   │ ✓    │ ✓    │ ✓    │ ✓    │
  └────────────────┴──────┴───────┴──────┴──────┘

字段权威声明:
  Quote 中的 pe_ttm / pb / market_cap / float_cap / high_52w / low_52w /
  turnover_rate / amplitude 等 88-field 数据由腾讯独家覆盖,声明高权威。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..http import HttpClient, HttpError, get_http_client
from ..schemas import MarketIndexSnapshot, Quote
from ..symbols import detect_market, normalize_to_tencent, safe_float
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.tencent")


_QUOTE_URL = "https://qt.gtimg.cn/q="
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {"Referer": "https://finance.qq.com"}

BATCH_LIMIT = 50

_INTERVAL_MAP = {
    "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year",
    "1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60",
}

# ─── 88-field 字段位置(经实测 2026-05-08 验证) ─────────────────────────────
_COMMON = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume": 6, "bid1_price": 9, "bid1_vol": 10,
    "ask1_price": 19, "ask1_vol": 20, "timestamp": 30,
    "change": 31, "pct_change": 32, "high": 33, "low": 34,
}

_A_EXTRA = {
    "volume_lots": 36, "amount_wan": 37, "turnover_rate": 38, "pe_ttm": 39,
    "amplitude": 43, "float_cap": 44, "market_cap": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "volume_ratio": 49, "avg_price": 51,
    "dividend_yield": 56, "amount_wan2": 57,
    "high_52w": 67, "low_52w": 68, "currency": 82,
}

_HK_EXTRA = {
    "volume_real": 29, "pe_ttm": 39, "amplitude": 43,
    "market_cap": 44, "float_cap": 45, "turnover_rate": 47,
    "high_52w": 48, "low_52w": 49, "volume_ratio": 50,
    "pb": 57, "avg_price": 73, "currency": 75,
}

_US_EXTRA = {
    "volume_real": 36, "amount": 37, "turnover_rate": 38, "pe_ttm": 39,
    "market_cap": 44, "float_cap": 45, "high_52w": 48, "low_52w": 49,
    "pb": 57, "avg_price": 67,
}


def _get(fields: list, idx: int, default: float = 0.0) -> float:
    if 0 <= idx < len(fields):
        return safe_float(fields[idx], default)
    return default


def _get_str(fields: list, idx: int, default: str = "") -> str:
    if 0 <= idx < len(fields):
        try:
            return str(fields[idx]).strip()
        except Exception:
            return default
    return default


def _parse_timestamp(raw: str) -> datetime:
    """腾讯时间戳格式: YYYYMMDDHHMMSS。解析失败返回 datetime.now()。"""
    if not raw:
        return datetime.now()
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S")
    except ValueError:
        return datetime.now()


def parse_quote_line(symbol: str, raw_line: str) -> Optional[Quote]:
    """解析单行腾讯行情。失败返回 None(不抛异常,由调用方判定)。"""
    eq = raw_line.find('="')
    if eq >= 0:
        raw_line = raw_line[eq + 2:]
    fields = raw_line.rstrip('";\n\r').split("~")
    if len(fields) < 30:
        return None

    market = detect_market(symbol)
    q = Quote(
        symbol=symbol,
        name=_get_str(fields, _COMMON["name"]),
        code=_get_str(fields, _COMMON["code"]),
        market=market.value,
        price=_get(fields, _COMMON["price"]),
        prev_close=_get(fields, _COMMON["prev_close"]),
        open=_get(fields, _COMMON["open"]),
        high=_get(fields, _COMMON["high"]),
        low=_get(fields, _COMMON["low"]),
        change=_get(fields, _COMMON["change"]),
        pct_change=_get(fields, _COMMON["pct_change"]),
        bid1_price=_get(fields, _COMMON["bid1_price"]),
        bid1_vol=_get(fields, _COMMON["bid1_vol"]),
        ask1_price=_get(fields, _COMMON["ask1_price"]),
        ask1_vol=_get(fields, _COMMON["ask1_vol"]),
        timestamp=_parse_timestamp(_get_str(fields, _COMMON["timestamp"])),
    )

    if market in (Market.A, Market.INDEX):
        e = _A_EXTRA
        vol_lots = _get(fields, e["volume_lots"])
        q.volume = vol_lots * 100 if vol_lots > 0 else _get(fields, 6) * 100
        amt = _get(fields, e["amount_wan2"]) or _get(fields, e["amount_wan"])
        q.amount = amt * 10000 if amt > 0 else 0
        q.turnover_rate = _get(fields, e["turnover_rate"])
        q.pe_ttm = _get(fields, e["pe_ttm"])
        q.amplitude = _get(fields, e["amplitude"])
        q.float_cap = _get(fields, e["float_cap"])
        q.market_cap = _get(fields, e["market_cap"])
        q.pb = _get(fields, e["pb"])
        q.limit_up = _get(fields, e["limit_up"])
        q.limit_down = _get(fields, e["limit_down"])
        q.volume_ratio = _get(fields, e["volume_ratio"])
        q.avg_price = _get(fields, e["avg_price"])
        q.dividend_yield = _get(fields, e["dividend_yield"])
        q.high_52w = _get(fields, e["high_52w"])
        q.low_52w = _get(fields, e["low_52w"])
        q.currency = _get_str(fields, e["currency"], "CNY")

    elif market == Market.HK:
        e = _HK_EXTRA
        vol_real = _get(fields, e["volume_real"])
        q.volume = vol_real if vol_real > 0 else _get(fields, 6)
        q.amount = _get(fields, 37)
        q.turnover_rate = _get(fields, e["turnover_rate"])
        q.pe_ttm = _get(fields, e["pe_ttm"])
        q.amplitude = _get(fields, e["amplitude"])
        q.market_cap = _get(fields, e["market_cap"])
        q.float_cap = _get(fields, e["float_cap"])
        q.high_52w = _get(fields, e["high_52w"])
        q.low_52w = _get(fields, e["low_52w"])
        q.volume_ratio = _get(fields, e["volume_ratio"])
        q.pb = _get(fields, e["pb"])
        q.avg_price = _get(fields, e["avg_price"])
        q.currency = _get_str(fields, e["currency"], "HKD")

    elif market == Market.US:
        e = _US_EXTRA
        vol_real = _get(fields, e["volume_real"])
        q.volume = vol_real if vol_real > 0 else _get(fields, 6)
        q.amount = _get(fields, e["amount"])
        q.turnover_rate = _get(fields, e["turnover_rate"])
        q.pe_ttm = _get(fields, e["pe_ttm"])
        q.market_cap = _get(fields, e["market_cap"])
        q.float_cap = _get(fields, e["float_cap"])
        q.high_52w = _get(fields, e["high_52w"])
        q.low_52w = _get(fields, e["low_52w"])
        q.pb = _get(fields, e["pb"])
        q.avg_price = _get(fields, e["avg_price"])
        q.currency = "USD"

    return q if q.is_valid else None


def parse_quotes_text(text: str, symbols: List[str]) -> Dict[str, Quote]:
    """解析批量响应。symbols 列表顺序与响应行对齐。"""
    result: Dict[str, Quote] = {}
    for i, line in enumerate(text.strip().split("\n")):
        if i >= len(symbols):
            break
        if not line.strip():
            continue
        q = parse_quote_line(symbols[i], line)
        if q is not None:
            result[symbols[i]] = q
    return result


# ─── Provider ──────────────────────────────────────────────────────────────────


class TencentProvider(Provider):
    """腾讯 qt.gtimg.cn 数据源。"""

    name = "tencent"

    def __init__(self, http: Optional[HttpClient] = None):
        self._http = http or get_http_client()

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.QUOTE,
                Capability.KLINE_DAILY,
                Capability.KLINE_MINUTE,
                Capability.MARKET_INDEX,
            }),
            markets=frozenset({Market.A, Market.INDEX, Market.HK, Market.US}),
            priority_hint=0.85,
        )

    def supports(self, capability: Capability, market: Market) -> bool:
        # 腾讯分钟 K 仅港股可用
        if capability == Capability.KLINE_MINUTE and market != Market.HK:
            return False
        return super().supports(capability, market)

    def field_authority(self) -> Dict[Capability, Dict[str, float]]:
        # 88-field 独家字段:对其声明高权威
        quote_authority = {
            "pe_ttm": 1.3, "pb": 1.3, "market_cap": 1.3, "float_cap": 1.3,
            "high_52w": 1.3, "low_52w": 1.3, "turnover_rate": 1.2,
            "amplitude": 1.2, "limit_up": 1.2, "limit_down": 1.2,
            "volume_ratio": 1.2, "dividend_yield": 1.2,
        }
        return {Capability.QUOTE: quote_authority}

    # ── QUOTE ────────────────────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        sym = normalize_to_tencent(symbol)
        try:
            text = self._http.get_text(
                f"{_QUOTE_URL}{sym}", headers=_HEADERS, encoding="gbk",
            )
        except HttpError as exc:
            raise ProviderError(f"tencent.fetch_quote({sym}): {exc}") from exc
        return parse_quote_line(sym, text)

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        normalized_map: Dict[str, str] = {
            normalize_to_tencent(s): s for s in symbols
        }
        norm_syms = list(normalized_map.keys())
        result: Dict[str, Quote] = {}

        for i in range(0, len(norm_syms), BATCH_LIMIT):
            batch = norm_syms[i : i + BATCH_LIMIT]
            url = f"{_QUOTE_URL}{','.join(batch)}"
            try:
                text = self._http.get_text(url, headers=_HEADERS, encoding="gbk")
            except HttpError as exc:
                raise ProviderError(f"tencent.fetch_quotes batch: {exc}") from exc
            parsed = parse_quotes_text(text, batch)
            for norm_sym, q in parsed.items():
                orig = normalized_map.get(norm_sym, norm_sym)
                result[orig] = q
        return result

    # ── KLINE ────────────────────────────────────────────────────────────────

    def fetch_kline(
        self,
        symbol: str,
        interval: str = "daily",
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        sym = normalize_to_tencent(symbol)
        market = detect_market(sym)
        period = _INTERVAL_MAP.get(interval)
        if period is None:
            return pd.DataFrame()

        is_minute = interval in ("1m", "5m", "15m", "30m", "60m")
        if is_minute and market != Market.HK:
            return pd.DataFrame()  # 腾讯仅港股分钟 K

        # 日 K:用 days 反推区间;分钟 K:固定 limit
        if is_minute:
            start, end = "", ""
            n = limit
        else:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=days * 2)
            start = start_dt.strftime("%Y-%m-%d")
            end = end_dt.strftime("%Y-%m-%d")
            n = days

        adj = adjust if adjust in ("qfq", "hfq") else ""
        param = f"{sym},{period},{start},{end},{n},{adj}"
        url = f"{_KLINE_URL}?_var=k&param={param}"

        try:
            text = self._http.get_text(url, headers=_HEADERS, encoding="utf-8")
        except HttpError as exc:
            raise ProviderError(f"tencent.fetch_kline({sym},{interval}): {exc}") from exc

        eq = text.find("=")
        if eq >= 0:
            text = text[eq + 1:].strip().rstrip(";")
        try:
            obj = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return pd.DataFrame()

        data = obj.get("data", {})
        if isinstance(data, list):
            return pd.DataFrame()
        sym_data = data.get(sym, {})
        if not sym_data:
            return pd.DataFrame()

        # 查找 bar 数组:qfq+period > period > 任意包含 period 的 key
        prefix = adj if adj else ""
        candidates = ([f"{prefix}{period}"] if prefix else []) + [
            period, f"qfq{period}", f"hfq{period}", "day",
        ]
        bars = None
        for k in candidates:
            if k in sym_data:
                bars = sym_data[k]
                break
        if bars is None:
            for k, v in sym_data.items():
                if isinstance(v, list) and period in k:
                    bars = v
                    break
        if not bars:
            return pd.DataFrame()

        rows = []
        for bar in bars:
            if len(bar) < 6:
                continue
            try:
                rows.append({
                    "date" if not is_minute else "datetime": bar[0],
                    "open": float(bar[1]),
                    "high": float(bar[3]),
                    "low": float(bar[4]),
                    "close": float(bar[2]),
                    "volume": float(bar[5]),
                })
            except (ValueError, IndexError):
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        time_col = "datetime" if is_minute else "date"
        if is_minute:
            # 港股分钟时间戳形如 YYYYMMDDHHMM(SS)
            df[time_col] = df[time_col].apply(_parse_kline_dt)
        else:
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
            df = df.dropna(subset=[time_col])
        df = df.sort_values(time_col).reset_index(drop=True)
        return df

    # ── MARKET_INDEX ─────────────────────────────────────────────────────────

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        """复用 fetch_quote。code 直接当作 symbol 传(usSPY/hkHSI 等)。"""
        q = self.fetch_quote(code)
        if q is None or not q.is_valid:
            return None
        return MarketIndexSnapshot(
            code=code,
            name=q.name,
            price=q.price,
            prev_close=q.prev_close,
            change_pct=q.pct_change,
            timestamp=q.timestamp,
        )


def _parse_kline_dt(raw) -> Optional[datetime]:
    """分钟 K 时间戳 YYYYMMDDHHMM(SS) 解析。"""
    s = str(raw)
    try:
        if len(s) == 14 and s.isdigit():
            return datetime.strptime(s, "%Y%m%d%H%M%S")
        if len(s) == 12 and s.isdigit():
            return datetime.strptime(s, "%Y%m%d%H%M")
        return pd.to_datetime(s)
    except Exception:
        return None


__all__ = ["TencentProvider", "parse_quote_line", "parse_quotes_text"]

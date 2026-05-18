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
  │ MARKET_INDEX   │ ✓    │ ✓    │ ✓    │  ← 新增（新浪 hq.sinajs.cn 支持 A/INDEX/HK 指数）
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
from ..schemas import MarketIndexSnapshot, Quote, SectorConstituent, SectorRanking
from ..symbols import detect_market, normalize_to_sina, safe_float
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.sina")

_QUOTE_URL = "https://hq.sinajs.cn/list="
_SINA_SECTORS_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_SINA_CONSTITUENTS_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeData"
)
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
                Capability.MARKET_INDEX,       # 新浪 hq.sinajs.cn/list=s_sh000001 指数接口
                Capability.SECTOR_RANKING,     # 新浪行业板块（东方财富断连时备用）
                Capability.SECTOR_CONSTITUENTS, # 新浪 Market_Center.getHQNodeData 板块成分股
            }),
            markets=frozenset({Market.A, Market.INDEX, Market.HK}),
            priority_hint=0.80,
        )

    def supports(self, capability: Capability, market: Market) -> bool:
        # 港股 K 线不稳定，视为不支持
        if capability in (Capability.KLINE_DAILY, Capability.KLINE_MINUTE):
            if market == Market.HK:
                return False
        # 指数 K 线：Sina 的 normalize_to_sina 对上交所指数（000xxx）会错误归一
        # 为 sz000300（深证路径），导致 Sina 返回 null；腾讯已全覆盖 INDEX K-line，
        # 新浪不参与指数 K 线路由
        if capability in (Capability.KLINE_DAILY, Capability.KLINE_MINUTE):
            if market == Market.INDEX:
                return False
        # MARKET_INDEX 不支持美股（新浪美股指数接口不同）
        if capability == Capability.MARKET_INDEX and market == Market.US:
            return False
        # SECTOR_CONSTITUENTS 走 Market.GLOBAL 路由（板块代码与 A 股市场无关）
        # 注：SECTOR_RANKING 的网关路由用的是 Market.A（gateway.sectors），
        # 故此处只放行 SECTOR_CONSTITUENTS，不扩大未被路由的能力面。
        if capability == Capability.SECTOR_CONSTITUENTS and market == Market.GLOBAL:
            return True
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

    # ── MARKET_INDEX ─────────────────────────────────────────────────────────

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        """A 股 / 指数快照。新浪指数前缀 s_，URL: hq.sinajs.cn/list=s_sh000001。

        指数格式（6 字段）:
            [name, price, prev_close, change, change_pct, volume]
        与 A 股 34 字段格式不同，走独立解析路径。
        """
        sina_code = normalize_to_sina(code)
        # 新浪指数前缀 s_，与普通股票区分
        index_code = f"s_{sina_code}" if not sina_code.startswith("s_") else sina_code
        try:
            text = self._http.get_text(
                f"{_QUOTE_URL}{index_code}",
                headers=_HEADERS,
                encoding="gbk",
            )
        except HttpError as exc:
            raise ProviderError(f"sina.fetch_market_index({index_code}): {exc}") from exc

        fields = _split_payload(text)
        if len(fields) < 6:
            return None
        # 字段: [0]name [1]price [2]change(金额) [3]change_pct(%) [4]volume [5]amount
        price = safe_float(fields[1])
        if price <= 0:
            return None
        change = safe_float(fields[2])
        prev_close = price - change          # prev_close = price - change金额
        change_pct = safe_float(fields[3])   # change_pct 直接从字段3取

        return MarketIndexSnapshot(
            code=code,
            name=fields[0].strip(),
            price=price,
            prev_close=prev_close,
            change_pct=change_pct,
            timestamp=datetime.now(),
        )

    # ── KLINE ────────────────────────────────────────────────────────────────

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

    # ── SECTOR_RANKING ───────────────────────────────────────────────────────

    def fetch_sectors(self, limit: int = 100) -> List[SectorRanking]:
        """新浪行业板块（newSinaHy.php），无资金流数据。

        东方财富断连时的备用数据源，数据质量低于东方财富：
        - 有板块名称、涨跌幅
        - 无 net_flow / amount
        - rank_flow 不可信（基于 change_pct 而非资金流）
        """
        import re
        try:
            text = self._http.get_text(
                _SINA_SECTORS_URL,
                headers={"Referer": "https://finance.sina.com.cn/"},
                encoding="gbk",
            )
        except HttpError as exc:
            raise ProviderError(f"sina.fetch_sectors: {exc}") from exc

        m = re.search(
            r"S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})",
            text, re.DOTALL,
        )
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"sina.fetch_sectors: JSON 解析失败: {exc}") from exc

        result: List[SectorRanking] = []
        for i, (code, vals) in enumerate(data.items(), 1):
            parts = vals.split(",")
            if len(parts) < 6:
                continue
            name = parts[1].strip()
            if not name:
                continue
            change_pct = float(parts[5]) if parts[5] else 0.0
            result.append(SectorRanking(
                code=f"SINA_{code}",
                name=name,
                change_pct=change_pct,
                net_flow=0.0,
                amount=0.0,
                rank_perf=i,
                rank_flow=0,
            ))
        return result[:limit]

    # ── SECTOR_CONSTITUENTS ───────────────────────────────────────────────────

    def fetch_sector_constituents(
        self,
        code: str,
        limit: int = 20,
    ) -> List[SectorConstituent]:
        """新浪行业板块成分股（Market_Center.getHQNodeData）。

        code 格式支持:
          - 'SINA_new_xxx'  → strip 前缀后得 'new_xxx'
          - 'EM_BK0xxx'     → strip 前缀后得 'BK0xxx'（与 eastmoney 行为对称；
                              新浪 node 不识别 EM_ 板块码时上游返回空列表）
          - 'new_xxx'       → 直接使用
        """
        node_code = code
        if node_code.startswith("SINA_"):
            node_code = node_code.split("_", 1)[1]
        elif node_code.startswith("EM_"):
            node_code = node_code[3:]

        params = {
            "num": limit,
            "sort": "change",     # 按涨幅排序
            "asc": 0,             # 降序
            "node": node_code,
            "_s_r_a": "page",
        }
        try:
            text = self._http.get_text(
                _SINA_CONSTITUENTS_URL,
                params=params,
                headers={"Referer": "https://finance.sina.com.cn/"},
                encoding="utf-8",
            )
        except HttpError as exc:
            raise ProviderError(f"sina.fetch_sector_constituents({code}): {exc}") from exc

        try:
            records = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"sina.fetch_sector_constituents({code}): JSON 解析失败: {exc}"
            ) from exc

        if not isinstance(records, list):
            return []

        result: List[SectorConstituent] = []
        for rec in records:
            sym = str(rec.get("symbol", ""))
            name = str(rec.get("name", ""))
            if not sym or not name:
                continue
            # SectorConstituent.symbol 契约要求标准化代码（sh600519 / hk00700 …）。
            # 新浪通常已返回标准前缀；归一化是对 schema 的兜底承诺，
            # 不依赖上游格式碰巧合规。
            result.append(SectorConstituent(
                symbol=normalize_to_sina(sym),
                name=name,
                price=safe_float(rec.get("trade")),
                change_pct=safe_float(rec.get("changepercent")),
                amount=safe_float(rec.get("amount")),
                volume=safe_float(rec.get("volume")),
            ))
        return result[:limit]


__all__ = ["SinaProvider"]

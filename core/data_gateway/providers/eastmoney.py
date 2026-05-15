# -*- coding: utf-8 -*-
"""
data_gateway.providers.eastmoney — 东方财富板块数据源

能力矩阵:
  - SECTOR_RANKING: 全市场板块涨跌幅 + 资金流排名(唯一来源)
  - SECTOR_CONSTITUENTS: 单板块成分股(唯一来源)
  - NORTH_FLOW: 北向/南向资金净流入(来自 kamt.rtmin / kamt 端点)

封禁感知:
  对 ConnectionResetError / RemoteDisconnected 等"疑似封禁"信号,
  HttpClient 已将其归一为 HttpError(retriable=True),provider 层
  直接抛 ProviderError 让 gateway 健康度记录失败 + 触发熔断。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..capabilities import Capability, Market, ProviderCapability
from ..http import HttpClient, HttpError, get_http_client, parse_jsonp
from ..schemas import MarketIndexSnapshot, NorthFlow, SectorConstituent, SectorRanking, Quote
from ..symbols import normalize_to_sina, detect_market
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.eastmoney")

_BASE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

_KAMT_REALTIME_URL = (
    "https://push2.eastmoney.com/api/qt/kamt.rtmin/get"
    "?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)
_KAMT_DAILY_URL = (
    "https://push2.eastmoney.com/api/qt/kamt/get"
    "?fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56"
)
_KAMT_HEADERS = {"Referer": "https://data.eastmoney.com/"}

_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,"
    "f23,f24,f25,f22,f11,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,"
    "f204,f205,f124"
)


def _safe_float(raw: Any) -> float:
    """东方财富用'-'表示无数据（如停牌/未上市），统一转0.0。"""
    if raw is None or raw == '' or raw == '-':
        return 0.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def _parse_amount(raw: Any) -> float:
    """成交额归一为元。东方财富有时给万元、有时给元、偶尔给亿元。"""
    if raw is None or raw == '' or raw == '-':
        return 0.0
    try:
        val = float(raw)
        if 0 < val < 1:
            return val * 1e8
        if val < 1e6:
            return val * 1e4
        return val
    except (TypeError, ValueError):
        return 0.0


# ── secid 格式转换 ─────────────────────────────────────────────────────────
# Eastmoney ulist/clist 接口用 "市场.代码" 格式 secid
# 市场代码: 1=沪A  0=深A  116=港股  105=美股  h=沪深指数

_EM_MARKET_CODE = {
    "SH": "1",   # 沪市
    "SZ": "0",   # 深市
    "HK": "116", # 港股
    "US": "105", # 美股（需要特殊处理，道琼斯等用 105.DJIA）
}

def _symbol_to_secid(symbol: str) -> str:
    """将 sh600519 / hk00700 / usAAPL 转为东方财富 secid 格式 1.600519 / 116.HK00700。"""
    s = symbol.strip().lower()
    # 剥掉 sh/sz/hk/us 前缀
    prefix_map = {"sh": "sh", "sz": "sz", "hk": "hk", "us": "us"}
    for p, key in prefix_map.items():
        if s.startswith(p):
            code = s[len(p):]
            market_key = key.upper()
            break
    else:
        # 纯数字代码，默认沪市
        return f"1.{symbol}"

    if market_key == "HK":
        return f"116.{code.upper()}"
    if market_key == "US":
        return f"105.{code.upper()}"
    em_code = _EM_MARKET_CODE.get(market_key, "1")
    return f"{em_code}.{code}"


def _index_code_to_secid(code: str) -> str:
    """将 000001 / sh000001 / hkHSI / usDJIA 转为东方财富 secid。

    东方财富指数 secid 格式：
      沪深指数: h.000001（上证）h.399001（深证）
      港股指数: 116.HSI（恒生）116.HSCEI（国企指数）
      美股指数: 105.DJIA（道琼斯）105.IXIC（纳斯达克）105.SPX（标普）
    """
    s = code.strip().lower()
    # 已经是 secid 格式（包含 .）
    if "." in s and any(s.startswith(x) for x in ["h.", "1.", "0.", "116.", "105."]):
        return s.upper()

    # 剥前缀
    for p in ("sh", "sz", "hk", "us", "h"):
        if s.startswith(p):
            rest = s[len(p):]
            rest_upper = rest.upper()
            if p in ("hk"):
                return f"116.{rest_upper}"
            if p in ("us"):
                return f"105.{rest_upper}"
            if p == "h":
                return f"h.{rest}"
            # sh/sz → h 前缀
            return f"h.{rest}"
    # 纯数字（默认当沪深指数处理）
    return f"h.{code}"


class EastmoneyProvider(Provider):
    """东方财富 push2.eastmoney.com 板块数据源。"""

    name = "eastmoney"

    def __init__(self, http: Optional[HttpClient] = None):
        self._http = http or get_http_client()

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.QUOTE,           # push2 ulist.np（A股/港股/指数实时行情）
                Capability.MARKET_INDEX,    # 同上，指数快照
                Capability.SECTOR_RANKING,  # push2 clist（已有）
                Capability.SECTOR_CONSTITUENTS,  # push2 clist（已有）
                Capability.NORTH_FLOW,      # kamt 实时/日总结（已有）
            }),
            markets=frozenset({Market.A, Market.INDEX, Market.HK}),
            priority_hint=0.70,
        )

    def supports(self, capability: Capability, market: Market) -> bool:
        """QUOTE / MARKET_INDEX / SECTOR_* / NORTH_FLOW 均只支持 A / INDEX / HK 市场。"""
        if market not in (Market.A, Market.INDEX, Market.HK):
            return False
        return super().supports(capability, market)

    def _request(self, fs_param: str) -> Optional[dict]:
        """基于 http client 的请求（供 fetch_sector_constituents 等方法使用）。"""
        params = {
            "cb": "jQuery",
            "pn": 1, "pz": 200, "po": 1, "np": 1,
            "ut": "b", "fltt": 2, "invt": 2, "fid": "f3",
            "fs": fs_param,
            "fields": _FIELDS,
        }
        try:
            text = self._http.get_text(_BASE_URL, params=params, headers=_HEADERS)
            return json.loads(parse_jsonp(text))
        except HttpError as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): JSON 解析失败: {exc}") from exc

    def _request_em(self, fs_param: str) -> Optional[dict]:
        import subprocess, shlex

        params = {
            "cb": "jQuery",
            "pn": 1, "pz": 200, "po": 1, "np": 1,
            "ut": "b", "fltt": 2, "invt": 2, "fid": "f3",
            "fs": fs_param,
            "fields": _FIELDS,
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{_BASE_URL}?{qs}"
        referer = _HEADERS["Referer"]
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        # 通过 shell 执行 curl，还原 terminal 行为
        curl_cmd = (
            f"/usr/bin/curl -s --connect-timeout 10 --max-time 15 "
            f"-4 --http1.1 {shlex.quote(url)} "
            f"-H {shlex.quote('Referer: ' + referer)} "
            f"-H {shlex.quote(ua)}"
        )
        try:
            result = subprocess.run(
                ["sh", "-c", curl_cmd],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise ProviderError(f"eastmoney.request({fs_param}): curl failed (rc={result.returncode})")
            text = result.stdout.strip()
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): curl timeout") from exc
        except OSError as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): curl not found") from exc

        try:
            return json.loads(parse_jsonp(text))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): JSON 解析失败: {exc}") from exc

    # ── SECTOR_RANKING ───────────────────────────────────────────────────────

    def fetch_sectors(self, limit: int = 100) -> List[SectorRanking]:
        # 尝试 _request_em（curl via subprocess，对 WSL 网络更稳定）
        # 失败后尝试 _request（http client，备用路径）
        raw = None
        for req_fn in (self._request_em, self._request):
            try:
                raw = req_fn("m:90+t:2+f:!50")
                if raw and isinstance(raw, dict):
                    break
            except ProviderError:
                continue
        if not raw or not isinstance(raw, dict):
            raise ProviderError("eastmoney.fetch_sectors: 无数据返回")

        records = ((raw.get("data") or {}).get("diff") or [])
        if not isinstance(records, list) or not records:
            raise ProviderError("eastmoney.fetch_sectors: diff 字段为空")

        sectors: List[SectorRanking] = []
        for i, rec in enumerate(records, 1):
            code = str(rec.get("f12", ""))
            name = str(rec.get("f14", ""))
            if not code or not name:
                continue
            sectors.append(SectorRanking(
                code=f"EM_{code}",
                name=name,
                change_pct=_safe_float(rec.get("f3")),
                net_flow=_parse_amount(rec.get("f62")),
                amount=_parse_amount(rec.get("f20")),
                rank_perf=i,
                rank_flow=0,
            ))
        for rank, sec in enumerate(sorted(sectors, key=lambda s: s.net_flow, reverse=True), 1):
            sec.rank_flow = rank
        return sectors[:limit]

    # ── SECTOR_CONSTITUENTS ──────────────────────────────────────────────────

    def fetch_sector_constituents(
        self,
        code: str,
        limit: int = 20,
    ) -> List[SectorConstituent]:
        # 标准化 code → 东方财富纯代码
        em_code = code
        if code.startswith("SINA_"):
            em_code = code.split("_", 1)[1]
        elif code.startswith("EM_"):
            em_code = code[3:]
        fs_param = f"b:{em_code}"

        raw = self._request(fs_param)
        if raw is None:
            return []
        records = ((raw.get("data") or {}).get("diff") or [])
        if not isinstance(records, list):
            return []

        out: List[SectorConstituent] = []
        for rec in records:
            sym = str(rec.get("f12", ""))
            name = str(rec.get("f14", ""))
            if not sym or not name:
                continue
            out.append(SectorConstituent(
                symbol=normalize_to_sina(sym),
                name=name,
                price=_safe_float(rec.get("f2")),
                change_pct=_safe_float(rec.get("f3")),
                amount=_parse_amount(rec.get("f20")),
                volume=_safe_float(rec.get("f6")),
            ))
        out.sort(key=lambda c: c.change_pct, reverse=True)
        return out[:limit]

    # ── NORTH_FLOW ───────────────────────────────────────────────────────────

    def fetch_north_flow(self) -> Optional[NorthFlow]:
        """实时 → 日总结兜底链。两端都失败则抛 ProviderError。"""
        snap = self._fetch_kamt_realtime()
        if snap is not None:
            return snap
        return self._fetch_kamt_daily()

    def _fetch_kamt_realtime(self) -> Optional[NorthFlow]:
        try:
            data = self._http.get_json(
                _KAMT_REALTIME_URL, headers=_KAMT_HEADERS,
            )
        except HttpError:
            return None
        kamt = (data or {}).get("data") if isinstance(data, dict) else None
        if not kamt:
            return None
        n2s = self._parse_kamt_series(kamt.get("n2s", []))   # 北向(港资买 A)
        s2n = self._parse_kamt_series(kamt.get("s2n", []))   # 南向
        net_north = (n2s.get("cum_amount", 0) - s2n.get("cum_amount", 0))
        net_yi = net_north / 1e8

        # net=0 且 amount=0 同时发生时，判定为接口异常数据，放弃实时数据
        # 让 fetch_north_flow() 兜底到日总结接口
        if net_yi == 0 and n2s.get("amount", 0) == 0:
            logger.debug("kamt realtime: net=0 and amount=0, treating as abnormal, falling through to daily")
            return None

        return NorthFlow(
            net_north_yi=net_yi,
            net_south_yi=0.0,
            direction=("BUY" if net_yi > 0 else "SELL" if net_yi < 0 else "NEUTRAL"),
            stale=False,
            timestamp=datetime.now(),
        )

    def _fetch_kamt_daily(self) -> Optional[NorthFlow]:
        try:
            data = self._http.get_json(
                _KAMT_DAILY_URL, headers=_KAMT_HEADERS,
            )
        except HttpError as exc:
            raise ProviderError(f"eastmoney.fetch_north_flow daily: {exc}") from exc
        d = (data or {}).get("data") if isinstance(data, dict) else None
        if not d:
            raise ProviderError("eastmoney.fetch_north_flow: 双源均无数据")
        hk2sh = d.get("hk2sh", {}) or {}
        sh2hk = d.get("sh2hk", {}) or {}
        # dayNetAmtIn 单位 万元 → 转亿元(除以 10000 = 万元*10000元/亿)
        north_yi = float(hk2sh.get("dayNetAmtIn", 0) or 0) / 10000
        south_yi = float(sh2hk.get("dayNetAmtIn", 0) or 0) / 10000
        return NorthFlow(
            net_north_yi=north_yi,
            net_south_yi=south_yi,
            direction=("BUY" if north_yi > 0 else "SELL" if north_yi < 0 else "NEUTRAL"),
            stale=False,
            timestamp=datetime.now(),
        )

    @staticmethod
    def _parse_kamt_series(series: list) -> Dict[str, float]:
        """从 KAMT realtime 时间序列中取最后一条非空记录。"""
        for entry in reversed(series or []):
            parts = (entry or "").split(",")
            if len(parts) >= 6 and parts[0]:
                try:
                    return {
                        "amount": float(parts[3] or 0),
                        "cum_amount": float(parts[5] or 0),
                    }
                except (ValueError, IndexError):
                    continue
        return {"amount": 0.0, "cum_amount": 0.0}

    # ── QUOTE ─────────────────────────────────────────────────────────────────

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        """通过 push2 ulist.np 获取个股实时行情（A股/港股）。"""
        secid = _symbol_to_secid(symbol)
        try:
            data = self._http.get_json(
                _ULIST_URL,
                params={
                    "fltt": 2,
                    "invt": 2,
                    "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f62,f104,f105,f106",
                    "secids": secid,
                },
                headers=_HEADERS,
            )
        except HttpError as exc:
            raise ProviderError(f"eastmoney.fetch_quote({symbol}): {exc}") from exc

        diff = ((data or {}).get("data") or {}).get("diff") or []
        if not diff:
            return None
        rec = diff[0]

        price = _safe_float(rec.get("f2"))
        if price <= 0:
            return None
        pct = _safe_float(rec.get("f3"))
        change = _safe_float(rec.get("f4"))
        market = detect_market(symbol)
        name = str(rec.get("f14", ""))
        code = str(rec.get("f12", ""))

        return Quote(
            symbol=symbol,
            name=name,
            code=code,
            market=market.value,
            price=price,
            prev_close=price - change if change else 0,
            open=_safe_float(rec.get("f5")),
            high=_safe_float(rec.get("f15")),
            low=_safe_float(rec.get("f16")),
            change=change,
            pct_change=pct,
            volume=_safe_float(rec.get("f6")),
            amount=_safe_float(rec.get("f62")),
            pe_ttm=_safe_float(rec.get("f9")) if "f9" else 0.0,
            pb=_safe_float(rec.get("f23")) if "f23" else 0.0,
            high_52w=_safe_float(rec.get("f104")) if "f104" else 0.0,
            low_52w=_safe_float(rec.get("f105")) if "f105" else 0.0,
            turnover_rate=_safe_float(rec.get("f10")) if "f10" else 0.0,
            timestamp=datetime.now(),
            currency="CNY" if market in (Market.A, Market.INDEX) else "HKD",
        )

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """批量个股行情（通过 ulist.np 批量接口）。"""
        if not symbols:
            return {}
        secids = [_symbol_to_secid(s) for s in symbols]
        try:
            data = self._http.get_json(
                _ULIST_URL,
                params={
                    "fltt": 2,
                    "invt": 2,
                    "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f62",
                    "secids": ",".join(secids),
                },
                headers=_HEADERS,
            )
        except HttpError as exc:
            raise ProviderError(f"eastmoney.fetch_quotes: {exc}") from exc

        diff = ((data or {}).get("data") or {}).get("diff") or []
        out: Dict[str, Quote] = {}
        for i, rec in enumerate(diff):
            if i >= len(symbols):
                break
            sym = symbols[i]
            price = _safe_float(rec.get("f2"))
            if price <= 0:
                continue
            pct = _safe_float(rec.get("f3"))
            change = _safe_float(rec.get("f4"))
            market = detect_market(sym)
            out[sym] = Quote(
                symbol=sym,
                name=str(rec.get("f14", "")),
                code=str(rec.get("f12", "")),
                market=market.value,
                price=price,
                prev_close=price - change if change else 0,
                open=_safe_float(rec.get("f5")),
                high=_safe_float(rec.get("f15")),
                low=_safe_float(rec.get("f16")),
                change=change,
                pct_change=pct,
                volume=_safe_float(rec.get("f6")),
                amount=_safe_float(rec.get("f62")),
                timestamp=datetime.now(),
                currency="CNY" if market in (Market.A, Market.INDEX) else "HKD",
            )
        return out

    # ── MARKET_INDEX ─────────────────────────────────────────────────────────

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        """通过 push2 ulist.np 获取指数快照（上证/深证/恒生等）。"""
        secid = _index_code_to_secid(code)
        try:
            data = self._http.get_json(
                _ULIST_URL,
                params={
                    "fltt": 2,
                    "invt": 2,
                    "fields": "f2,f3,f4,f12,f14",
                    "secids": secid,
                },
                headers=_HEADERS,
            )
        except HttpError as exc:
            raise ProviderError(f"eastmoney.fetch_market_index({code}): {exc}") from exc

        diff = ((data or {}).get("data") or {}).get("diff") or []
        if not diff:
            return None
        rec = diff[0]
        price = _safe_float(rec.get("f2"))
        if price <= 0:
            return None
        pct = _safe_float(rec.get("f3"))
        change = _safe_float(rec.get("f4"))
        return MarketIndexSnapshot(
            code=code,
            name=str(rec.get("f14", "")),
            price=price,
            prev_close=price - change if change else 0,
            change_pct=pct,
            timestamp=datetime.now(),
        )


__all__ = ["EastmoneyProvider"]

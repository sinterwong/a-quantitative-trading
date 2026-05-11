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
from ..schemas import NorthFlow, SectorConstituent, SectorRanking
from ..symbols import normalize_to_sina
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.eastmoney")

_BASE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
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


def _parse_amount(raw: Any) -> float:
    """成交额归一为元。东方财富有时给万元、有时给元、偶尔给亿元。"""
    if raw is None:
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


class EastmoneyProvider(Provider):
    """东方财富 push2.eastmoney.com 板块数据源。"""

    name = "eastmoney"

    def __init__(self, http: Optional[HttpClient] = None):
        self._http = http or get_http_client()

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.SECTOR_RANKING,
                Capability.SECTOR_CONSTITUENTS,
                Capability.NORTH_FLOW,
            }),
            markets=frozenset({Market.GLOBAL}),
            # 实测时好时坏 — 给低 priority_hint,健康度系统会动态降权
            priority_hint=0.55,
        )

    def _request(self, fs_param: str) -> Optional[dict]:
        params = {
            "cb": "jQuery",
            "pn": 1, "pz": 200, "po": 1, "np": 1,
            "ut": "b", "fltt": 2, "invt": 2, "fid": "f3",
            "fs": fs_param,
            "fields": _FIELDS,
        }
        try:
            text = self._http.get_text(
                _BASE_URL, params=params, headers=_HEADERS, encoding="utf-8",
            )
        except HttpError as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): {exc}") from exc

        try:
            return json.loads(parse_jsonp(text))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"eastmoney.request({fs_param}): JSON 解析失败: {exc}") from exc

    # ── SECTOR_RANKING ───────────────────────────────────────────────────────

    def fetch_sectors(self, limit: int = 100) -> List[SectorRanking]:
        raw = self._request("m:90+t:2+f:!50")
        if raw is None:
            return []
        records = ((raw.get("data") or {}).get("diff") or [])
        if not isinstance(records, list):
            return []

        sectors: List[SectorRanking] = []
        for i, rec in enumerate(records, 1):
            code = str(rec.get("f12", ""))
            name = str(rec.get("f14", ""))
            if not code or not name:
                continue
            sectors.append(SectorRanking(
                code=f"EM_{code}",
                name=name,
                change_pct=float(rec.get("f3", 0) or 0),
                net_flow=_parse_amount(rec.get("f62", 0)),
                amount=_parse_amount(rec.get("f20", 0)),
                rank_perf=i,
                rank_flow=0,
            ))
        # 按资金流补 rank_flow
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
                price=float(rec.get("f2", 0) or 0),
                change_pct=float(rec.get("f3", 0) or 0),
                amount=_parse_amount(rec.get("f20", 0)),
                volume=float(rec.get("f6", 0) or 0),
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


__all__ = ["EastmoneyProvider"]

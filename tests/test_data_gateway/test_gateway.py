# -*- coding: utf-8 -*-
"""
DataGateway 单元测试 — 路由 / 字段合并 / 降级 / 缓存 / 并发。

完全用 mock provider 验证编排逻辑,不触碰真实 HTTP。
"""

from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.data_gateway.capabilities import Capability, MacroIndicator, Market, ProviderCapability
from core.data_gateway.gateway import DataGateway
from core.data_gateway.health import HealthTracker
from core.data_gateway.providers.base import Provider, ProviderError
from core.data_gateway.schemas import (
    BalanceSheet, Fundamentals, MarketIndexSnapshot, NorthFlow, Quote,
    SectorConstituent, SectorRanking,
)


# ── Mock providers ──────────────────────────────────────────────────────────


class _FakeProvider(Provider):
    """通用 mock provider,行为通过构造参数控制。"""

    def __init__(
        self,
        name: str,
        *,
        capabilities=(Capability.QUOTE,),
        markets=(Market.A,),
        priority_hint: float = 0.5,
        quote_value: Optional[Quote] = None,
        quotes_value: Optional[Dict[str, Quote]] = None,
        kline_value: Optional[pd.DataFrame] = None,
        fundamentals_value: Optional[Fundamentals] = None,
        sectors_value: Optional[List[SectorRanking]] = None,
        constituents_value: Optional[List[SectorConstituent]] = None,
        north_value: Optional[NorthFlow] = None,
        index_value: Optional[MarketIndexSnapshot] = None,
        macro_value: Optional[pd.DataFrame] = None,
        balance_value: Optional[BalanceSheet] = None,
        margin_flow_value: Optional[pd.DataFrame] = None,
        news_value: Optional[List[str]] = None,
        raise_on: Optional[str] = None,
        field_authorities: Optional[Dict[Capability, Dict[str, float]]] = None,
    ):
        self.name = name
        self._caps = frozenset(capabilities)
        self._mkts = frozenset(markets)
        self._hint = priority_hint
        self._quote = quote_value
        self._quotes = quotes_value or {}
        self._kline = kline_value if kline_value is not None else pd.DataFrame()
        self._fund = fundamentals_value
        self._sectors = sectors_value or []
        self._constituents = constituents_value or []
        self._north = north_value
        self._index = index_value
        self._macro = macro_value if macro_value is not None else pd.DataFrame()
        self._balance = balance_value
        self._margin = margin_flow_value if margin_flow_value is not None else pd.DataFrame()
        self._news = news_value if news_value is not None else []
        self._raise_on = raise_on
        self._authority = field_authorities or {}
        self.call_log: List[str] = []

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=self._caps, markets=self._mkts, priority_hint=self._hint,
        )

    def field_authority(self):
        return self._authority

    def _maybe_raise(self, fn: str):
        if self._raise_on == fn:
            raise ProviderError(f"{self.name}.{fn} mocked failure")

    def fetch_quote(self, symbol):
        self.call_log.append(f"fetch_quote:{symbol}")
        self._maybe_raise("fetch_quote")
        return self._quote

    def fetch_quotes(self, symbols):
        self.call_log.append(f"fetch_quotes:{len(symbols)}")
        self._maybe_raise("fetch_quotes")
        return self._quotes

    def fetch_kline_daily(self, symbol, **kw):
        self.call_log.append(f"fetch_kline_daily:{symbol}")
        self._maybe_raise("fetch_kline_daily")
        return self._kline

    def fetch_kline_minute(self, symbol, **kw):
        self.call_log.append(f"fetch_kline_minute:{symbol}:{kw.get('interval', '5m')}")
        self._maybe_raise("fetch_kline_minute")
        return self._kline

    def fetch_fundamentals(self, symbol):
        self.call_log.append(f"fetch_fundamentals:{symbol}")
        self._maybe_raise("fetch_fundamentals")
        return self._fund

    def fetch_sectors(self, limit=100):
        self.call_log.append(f"fetch_sectors:{limit}")
        self._maybe_raise("fetch_sectors")
        return self._sectors

    def fetch_sector_constituents(self, code, limit=20):
        self.call_log.append(f"fetch_sector_constituents:{code}")
        self._maybe_raise("fetch_sector_constituents")
        return self._constituents

    def fetch_north_flow(self):
        self.call_log.append("fetch_north_flow")
        self._maybe_raise("fetch_north_flow")
        return self._north

    def fetch_market_index(self, code):
        self.call_log.append(f"fetch_market_index:{code}")
        self._maybe_raise("fetch_market_index")
        return self._index

    def fetch_macro(self, indicator: MacroIndicator):
        self.call_log.append(f"fetch_macro:{indicator}")
        self._maybe_raise("fetch_macro")
        return self._macro

    def fetch_balance_sheet(self, symbol):
        self.call_log.append(f"fetch_balance_sheet:{symbol}")
        self._maybe_raise("fetch_balance_sheet")
        return self._balance

    def fetch_margin_flow(self, symbol, start=None, end=None):
        self.call_log.append(f"fetch_margin_flow:{symbol}")
        self._maybe_raise("fetch_margin_flow")
        return self._margin

    def fetch_news_headlines(self, symbol, n=20):
        self.call_log.append(f"fetch_news_headlines:{symbol}:{n}")
        self._maybe_raise("fetch_news_headlines")
        return self._news


@pytest.fixture
def gw():
    """全新 gateway + 重置健康度。"""
    from core.circuit_breaker import reset_all
    reset_all()
    return DataGateway(health=HealthTracker(warmup_count=1), max_parallel=4)


# ── 注册 / 列出 provider ────────────────────────────────────────────────────


def test_register_and_list(gw):
    p = _FakeProvider("p1")
    gw.register_provider(p)
    assert gw.providers() == [p]
    gw.unregister_provider("p1")
    assert gw.providers() == []


# ── Capability + market 路由 ────────────────────────────────────────────────


def test_quote_routes_only_to_supporting_providers(gw):
    a_only = _FakeProvider("a_only", markets=(Market.A,),
                           quote_value=Quote(symbol="sh600519", price=100))
    hk_only = _FakeProvider("hk_only", markets=(Market.HK,),
                            quote_value=Quote(symbol="hk00700", price=300))
    gw.register_provider(a_only)
    gw.register_provider(hk_only)

    q = gw.quote("sh600519")
    assert q is not None and q.price == 100
    # hk_only 不应被调用
    assert all("fetch_quote" not in c for c in hk_only.call_log)


def test_provider_without_capability_skipped(gw):
    """声明 KLINE_DAILY 但请求 QUOTE 时不应被调用。"""
    p = _FakeProvider("kline_only", capabilities=(Capability.KLINE_DAILY,),
                      quote_value=Quote(symbol="x", price=999))
    gw.register_provider(p)
    assert gw.quote("sh600519") is None
    assert p.call_log == []


# ── 字段级合并(Quote) ──────────────────────────────────────────────────────


def test_quote_merges_complementary_fields(gw):
    """A 有 price 无 pe_ttm,B 有 pe_ttm 无 price → 取两家之长。"""
    a = _FakeProvider("A", priority_hint=0.9,
                      quote_value=Quote(symbol="sh600519", price=100, pe_ttm=0))
    b = _FakeProvider("B", priority_hint=0.9,
                      quote_value=Quote(symbol="sh600519", price=0, pe_ttm=25))
    gw.register_provider(a)
    gw.register_provider(b)
    q = gw.quote("sh600519")
    assert q.price == 100
    assert q.pe_ttm == 25

    prov = gw.provenance("quote:sh600519")
    assert prov["price"] == "A"
    assert prov["pe_ttm"] == "B"


def test_quote_field_authority_wins_over_health(gw):
    """B 健康度低但对 pe_ttm 声明 5.0 权威 → 该字段取 B。"""
    a = _FakeProvider(
        "A", priority_hint=0.9,
        quote_value=Quote(symbol="sh600519", price=100, pe_ttm=20),
    )
    b = _FakeProvider(
        "B", priority_hint=0.3,
        quote_value=Quote(symbol="sh600519", price=100, pe_ttm=25),
        field_authorities={Capability.QUOTE: {"pe_ttm": 5.0}},
    )
    gw.register_provider(a)
    gw.register_provider(b)
    q = gw.quote("sh600519")
    # 0.9 * 1.0 = 0.9 vs 0.3 * 5.0 = 1.5 → B 胜
    assert q.pe_ttm == 25


# ── 错误处理 / 降级 ─────────────────────────────────────────────────────────


def test_quote_one_provider_raises_other_used(gw):
    bad = _FakeProvider("bad", raise_on="fetch_quote")
    good = _FakeProvider("good", quote_value=Quote(symbol="x", price=42))
    gw.register_provider(bad)
    gw.register_provider(good)
    assert gw.quote("sh600519").price == 42


def test_quote_all_providers_fail_returns_none(gw):
    bad = _FakeProvider("bad", raise_on="fetch_quote")
    bad2 = _FakeProvider("bad2", raise_on="fetch_quote")
    gw.register_provider(bad)
    gw.register_provider(bad2)
    assert gw.quote("sh600519") is None


# ── 不可合并: kline 顺序 failover ─────────────────────────────────────────────


def test_kline_sequential_first_success_wins(gw):
    """K 线第一个成功源即返回,其余不调用。"""
    df1 = pd.DataFrame({"date": ["2026-05-08"], "close": [100]})
    a = _FakeProvider("A", priority_hint=0.9,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=df1)
    b = _FakeProvider("B", priority_hint=0.8,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=pd.DataFrame({"date": ["2026-05-08"], "close": [200]}))
    gw.register_provider(a)
    gw.register_provider(b)
    df = gw.kline("sh600519")
    assert df["close"].iloc[0] == 100
    # B 因 A 已成功而未被调用
    assert len(b.call_log) == 0


def test_kline_minute_routes_to_minute_capability(gw):
    a = _FakeProvider("A", capabilities=(Capability.KLINE_DAILY,),
                      kline_value=pd.DataFrame({"d": [1]}))  # daily only
    b = _FakeProvider("B", capabilities=(Capability.KLINE_MINUTE,),
                      kline_value=pd.DataFrame({"d": [2]}))
    gw.register_provider(a)
    gw.register_provider(b)
    df = gw.kline("sh600519", interval="5m")
    assert df["d"].iloc[0] == 2
    # daily provider 不应被调用
    assert a.call_log == []


# ── 缓存 ─────────────────────────────────────────────────────────────────────


def test_quote_cache_hit_avoids_provider(gw):
    p = _FakeProvider("p", quote_value=Quote(symbol="x", price=10))
    gw.register_provider(p)
    gw.quote("sh600519")
    gw.quote("sh600519")  # 第二次应命中缓存
    assert len([c for c in p.call_log if "fetch_quote" in c]) == 1


def test_invalidate_cache(gw):
    p = _FakeProvider("p", quote_value=Quote(symbol="x", price=10))
    gw.register_provider(p)
    gw.quote("sh600519")
    gw.invalidate_cache()
    gw.quote("sh600519")
    assert len([c for c in p.call_log if "fetch_quote" in c]) == 2


# ── 批量 quotes ─────────────────────────────────────────────────────────────


def test_quotes_batch_groups_by_market(gw):
    a = _FakeProvider("A", markets=(Market.A,),
                      quotes_value={"sh600519": Quote(symbol="sh600519", price=100)})
    hk = _FakeProvider("HK", markets=(Market.HK,),
                       quotes_value={"hk00700": Quote(symbol="hk00700", price=300)})
    gw.register_provider(a)
    gw.register_provider(hk)
    out = gw.quotes(["sh600519", "hk00700"])
    assert out["sh600519"].price == 100
    assert out["hk00700"].price == 300


def test_quotes_merges_across_providers(gw):
    """同 symbol 多源时,quotes 也字段级合并。"""
    a = _FakeProvider(
        "A", priority_hint=0.9,
        quotes_value={"sh600519": Quote(symbol="sh600519", price=100, pe_ttm=0)},
    )
    b = _FakeProvider(
        "B", priority_hint=0.9,
        quotes_value={"sh600519": Quote(symbol="sh600519", price=0, pe_ttm=25)},
    )
    gw.register_provider(a)
    gw.register_provider(b)
    out = gw.quotes(["sh600519"])
    assert out["sh600519"].price == 100
    assert out["sh600519"].pe_ttm == 25


def test_quotes_empty_input():
    gw = DataGateway()
    assert gw.quotes([]) == {}


# ── sectors / north_flow / market_index ─────────────────────────────────────


def test_sectors_routes_to_global_market(gw):
    p = _FakeProvider("em", capabilities=(Capability.SECTOR_RANKING,),
                      markets=(Market.A,),
                      sectors_value=[SectorRanking(code="X", name="x", change_pct=1)])
    gw.register_provider(p)
    out = gw.sectors(limit=10)
    assert len(out) == 1


def test_north_flow_failover(gw):
    bad = _FakeProvider("bad", capabilities=(Capability.NORTH_FLOW,),
                        markets=(Market.GLOBAL,), raise_on="fetch_north_flow")
    good = _FakeProvider("good", capabilities=(Capability.NORTH_FLOW,),
                         markets=(Market.GLOBAL,),
                         north_value=NorthFlow(net_north_yi=5.0, direction="BUY"))
    gw.register_provider(bad)
    gw.register_provider(good)
    nf = gw.north_flow()
    assert nf is not None
    assert nf.net_north_yi == 5.0


def test_market_index_falls_back_to_global(gw):
    """A 市场无 provider 时,自动降级到 GLOBAL provider(yfinance 兜底语义)。"""
    p = _FakeProvider(
        "fb", capabilities=(Capability.MARKET_INDEX,),
        markets=(Market.GLOBAL,),
        index_value=MarketIndexSnapshot(code="VIX", price=18.5),
    )
    gw.register_provider(p)
    idx = gw.market_index("VIX")
    assert idx is not None
    assert idx.price == 18.5


# ── 健康度自适应 ────────────────────────────────────────────────────────────


def test_unhealthy_provider_loses_to_healthy(gw):
    """连续失败的 provider 健康度下降,后续被排到后面。"""
    health = HealthTracker(warmup_count=1)
    gw = DataGateway(health=health, max_parallel=2)

    bad = _FakeProvider("bad", priority_hint=0.9, raise_on="fetch_quote")
    good = _FakeProvider("good", priority_hint=0.1,
                         quote_value=Quote(symbol="x", price=42))
    gw.register_provider(bad)
    gw.register_provider(good)

    # 第一次:并发问两家,bad 失败被记录
    q1 = gw.quote("sh600519")
    assert q1.price == 42

    gw.invalidate_cache()
    # bad 健康度应已下降
    bad_score = health.score("bad", Capability.QUOTE, priority_hint=0.9)
    good_score = health.score("good", Capability.QUOTE, priority_hint=0.1)
    assert bad_score < good_score


# ── 熔断器集成 ──────────────────────────────────────────────────────────────


def test_circuit_breaker_blocks_after_threshold(gw):
    """连续失败触发熔断,即使 capability 匹配也跳过。"""
    from core.circuit_breaker import reset_all
    reset_all()

    p = _FakeProvider("p", raise_on="fetch_quote")
    gw.register_provider(p)
    # 默认 failure_threshold=3
    for _ in range(5):
        gw.invalidate_cache()
        gw.quote("sh600519")

    # 现在 p 应被熔断,_candidates_for 应剔除它
    cands = gw._candidates_for(Capability.QUOTE, Market.A)
    assert cands == []
    reset_all()


# ── macro / fundamentals ────────────────────────────────────────────────────


def test_macro_routes_to_macro_capability(gw):
    p = _FakeProvider("ak", capabilities=(Capability.MACRO,),
                      markets=(Market.GLOBAL,),
                      macro_value=pd.DataFrame({"date": ["2026-05"], "pmi": [50.5]}))
    gw.register_provider(p)
    from core.data_gateway.capabilities import MacroIndicator
    df = gw.macro(MacroIndicator.PMI)
    assert not df.empty
    assert df["pmi"].iloc[0] == 50.5


def test_fundamentals_merge(gw):
    a = _FakeProvider("A", capabilities=(Capability.FUNDAMENTALS,),
                      markets=(Market.GLOBAL,),
                      fundamentals_value=Fundamentals(symbol="x", pe_ttm=20, roe_ttm=0))
    b = _FakeProvider("B", capabilities=(Capability.FUNDAMENTALS,),
                      markets=(Market.GLOBAL,),
                      fundamentals_value=Fundamentals(symbol="x", pe_ttm=0, roe_ttm=15))
    gw.register_provider(a)
    gw.register_provider(b)
    f = gw.fundamentals("sh600519")
    assert f.pe_ttm == 20
    assert f.roe_ttm == 15


# ── balance_sheet ───────────────────────────────────────────────────────────


def test_balance_sheet_basic_route(gw):
    """单 provider 提供 balance_sheet 即返回。"""
    p = _FakeProvider(
        "baostock_mock",
        capabilities=(Capability.BALANCE_SHEET,),
        markets=(Market.A,),
        balance_value=BalanceSheet(
            symbol="sh600519", debt_to_equity=35.2,
            current_ratio=2.1, quick_ratio=1.8,
        ),
    )
    gw.register_provider(p)
    bs = gw.balance_sheet("sh600519")
    assert bs is not None
    assert bs.debt_to_equity == 35.2
    assert bs.current_ratio == 2.1


def test_balance_sheet_cache_hit_avoids_provider(gw):
    p = _FakeProvider(
        "p", capabilities=(Capability.BALANCE_SHEET,), markets=(Market.A,),
        balance_value=BalanceSheet(symbol="sh600519", debt_to_equity=10),
    )
    gw.register_provider(p)
    gw.balance_sheet("sh600519")
    gw.balance_sheet("sh600519")  # 第二次命中缓存
    assert len([c for c in p.call_log if "fetch_balance_sheet" in c]) == 1


def test_balance_sheet_no_provider_returns_none(gw):
    """无支持 BALANCE_SHEET 的 provider 时返回 None。"""
    p = _FakeProvider("only_quote", capabilities=(Capability.QUOTE,))
    gw.register_provider(p)
    assert gw.balance_sheet("sh600519") is None


def test_balance_sheet_merge_across_providers(gw):
    """两家都给 balance_sheet 时,字段级合并。"""
    a = _FakeProvider(
        "A", capabilities=(Capability.BALANCE_SHEET,), markets=(Market.A,),
        priority_hint=0.9,
        balance_value=BalanceSheet(symbol="x", debt_to_equity=30, current_ratio=0),
    )
    b = _FakeProvider(
        "B", capabilities=(Capability.BALANCE_SHEET,), markets=(Market.A,),
        priority_hint=0.9,
        balance_value=BalanceSheet(symbol="x", debt_to_equity=0, current_ratio=2.5),
    )
    gw.register_provider(a)
    gw.register_provider(b)
    bs = gw.balance_sheet("sh600519")
    assert bs.debt_to_equity == 30
    assert bs.current_ratio == 2.5


# ── margin_flow / news_headlines ─────────────────────────────────────────────


def test_margin_flow_routes_via_gateway(gw):
    """gw.margin_flow() 顺序 failover,第一个非空 provider 即返回。"""
    df = pd.DataFrame(
        {"margin_balance": [1e8, 1.1e8], "short_balance": [1e6, 1.05e6]},
        index=pd.to_datetime(["2026-05-13", "2026-05-14"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.MARGIN_FLOW,), markets=(Market.GLOBAL,),
        margin_flow_value=df,
    )
    gw.register_provider(p)
    out = gw.margin_flow("sh600519")
    assert not out.empty
    assert "margin_balance" in out.columns
    assert "short_balance" in out.columns


def test_margin_flow_cache_hit_avoids_provider(gw):
    df = pd.DataFrame({"margin_balance": [1e8]}, index=pd.to_datetime(["2026-05-14"]))
    p = _FakeProvider(
        "ak", capabilities=(Capability.MARGIN_FLOW,), markets=(Market.GLOBAL,),
        margin_flow_value=df,
    )
    gw.register_provider(p)
    gw.margin_flow("sh600519")
    gw.margin_flow("sh600519")
    assert len([c for c in p.call_log if "fetch_margin_flow" in c]) == 1


def test_news_headlines_routes_via_gateway(gw):
    p = _FakeProvider(
        "ak", capabilities=(Capability.NEWS_HEADLINES,), markets=(Market.GLOBAL,),
        news_value=["利好消息 A", "公司公告 B", "行业动态 C"],
    )
    gw.register_provider(p)
    out = gw.news_headlines("sh600519", n=10)
    assert len(out) == 3
    assert out[0] == "利好消息 A"


def test_news_headlines_no_provider_returns_empty(gw):
    p = _FakeProvider("only_quote", capabilities=(Capability.QUOTE,))
    gw.register_provider(p)
    assert gw.news_headlines("sh600519") == []


# ── 全局 singleton ──────────────────────────────────────────────────────────


def test_get_gateway_registers_default_providers():
    from core.data_gateway.gateway import get_gateway, reset_gateway
    reset_gateway(None)
    gw = get_gateway()
    names = {p.name for p in gw.providers()}
    assert names == {"tencent", "sina", "eastmoney", "yfinance", "baostock", "akshare"}
    reset_gateway(None)

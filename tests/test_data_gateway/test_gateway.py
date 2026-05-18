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
    BalanceSheet, Fundamentals, MarketIndexSnapshot, NewsItem, NorthFlow,
    Quote, SectorConstituent, SectorRanking,
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
        north_history_value: Optional[pd.DataFrame] = None,
        fundamentals_history_value: Optional[pd.DataFrame] = None,
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
        self._north_history = north_history_value if north_history_value is not None else pd.DataFrame()
        self._fund_history = fundamentals_history_value if fundamentals_history_value is not None else pd.DataFrame()
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

    def fetch_north_flow_history(self, days=252):
        self.call_log.append(f"fetch_north_flow_history:{days}")
        self._maybe_raise("fetch_north_flow_history")
        return self._north_history

    def fetch_fundamentals_history(self, symbol, start=None, end=None):
        self.call_log.append(f"fetch_fundamentals_history:{symbol}")
        self._maybe_raise("fetch_fundamentals_history")
        return self._fund_history

    def fetch_news_headlines(self, symbol, n=20):
        self.call_log.append(f"fetch_news_headlines:{symbol}:{n}")
        self._maybe_raise("fetch_news_headlines")
        # G5：base 接口已升级到 List[NewsItem]。允许测试传入 List[str]
        # 字面量以保持表达精简，这里 best-effort 包装一下。
        out = []
        for it in self._news:
            if isinstance(it, NewsItem):
                out.append(it)
            else:
                out.append(NewsItem(title=str(it), source=self.name))
        return out


@pytest.fixture
def gw():
    """全新 gateway + 重置健康度（不启用 L2 落盘，避免测试间互相污染）。"""
    from core.circuit_breaker import reset_all
    reset_all()
    return DataGateway(
        health=HealthTracker(warmup_count=1),
        max_parallel=4,
        enable_disk_cache=False,
    )


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


# ── G1: kline 多源列级合并 ─────────────────────────────────────────────────


def test_kline_merges_multiple_sources_high_score_wins_overlap(gw):
    """K 线并发问多源；重叠日期/列由 score(priority_hint) 高的源胜出。"""
    df_a = pd.DataFrame(
        {"close": [100], "volume": [1000]},
        index=pd.to_datetime(["2026-05-08"]),
    )
    df_b = pd.DataFrame(
        {"close": [200], "volume": [2000]},
        index=pd.to_datetime(["2026-05-08"]),
    )
    a = _FakeProvider("A", priority_hint=0.9,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=df_a)
    b = _FakeProvider("B", priority_hint=0.8,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=df_b)
    gw.register_provider(a)
    gw.register_provider(b)
    df = gw.kline("sh600519")
    # 重叠列：高分 A 胜出
    assert df["close"].iloc[0] == 100
    assert df["volume"].iloc[0] == 1000
    # 两源都被并发调用(G1 行为)
    assert len(b.call_log) >= 1


def test_kline_merges_complementary_dates_and_columns(gw):
    """A 提供 OHLCV，B 提供 turnover_rate；合并后列并集 + 索引并集。"""
    df_a = pd.DataFrame(
        {"open": [10, 11], "close": [11, 12], "volume": [100, 200]},
        index=pd.to_datetime(["2026-05-08", "2026-05-09"]),
    )
    df_b = pd.DataFrame(
        {"turnover_rate": [1.5, 2.0, 2.5]},
        index=pd.to_datetime(["2026-05-09", "2026-05-10", "2026-05-11"]),
    )
    a = _FakeProvider("A", priority_hint=0.9,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=df_a)
    b = _FakeProvider("B", priority_hint=0.8,
                      capabilities=(Capability.KLINE_DAILY,),
                      kline_value=df_b)
    gw.register_provider(a)
    gw.register_provider(b)
    df = gw.kline("sh600519")
    # 列并集：OHLCV + turnover_rate
    assert "open" in df.columns
    assert "turnover_rate" in df.columns
    # 索引并集：2026-05-08 ~ 2026-05-11
    assert len(df) == 4
    # A 没有 2026-05-10 的 close，应为 NaN（kline ffill=False）
    assert pd.isna(df.loc["2026-05-10", "close"])
    # B 没有 2026-05-08 的 turnover，应为 NaN
    assert pd.isna(df.loc["2026-05-08", "turnover_rate"])


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
    gw = DataGateway(enable_disk_cache=False)
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
    gw = DataGateway(health=health, max_parallel=2, enable_disk_cache=False)

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


# ── fundamentals_history 列级合并 (W1-2 联动) ───────────────────────────────


def _FakeHistoryProvider(name, columns_dict, priority=0.5):
    """构造一个返回特定列字典的 FUNDAMENTALS_HISTORY provider。"""
    from core.data_gateway.providers.base import Provider as _P

    class _HP(_P):
        def __init__(self):
            self.name = name
            self._cols = columns_dict
            self._priority = priority
            self.call_log = []

        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.FUNDAMENTALS_HISTORY}),
                markets=frozenset({Market.GLOBAL}),
                priority_hint=self._priority,
            )

        def supports(self, capability, market):
            decl = self.declare()
            if capability not in decl.capabilities:
                return False
            return Market.GLOBAL in decl.markets or market in decl.markets

        def fetch_fundamentals_history(self, symbol, start=None, end=None):
            self.call_log.append(f"fetch_fundamentals_history:{symbol}")
            idx = pd.bdate_range(
                start=start or "2024-01-01", end=end or "2024-03-31",
            )
            cols = {k: [v[0]] * len(idx) for k, v in self._cols.items()}
            return pd.DataFrame(cols, index=idx)

    return _HP()


def test_fundamentals_history_column_merge(gw):
    """两个 provider 各贡献不同列 → 合并后并集。"""
    p1 = _FakeHistoryProvider(
        "akshare", {"roe_ttm": [10.0] * 5, "eps_ttm": [0.5] * 5},
        priority=0.3,
    )
    p2 = _FakeHistoryProvider(
        "baostock", {"debt_to_equity": [25.0] * 5, "current_ratio": [2.5] * 5},
        priority=0.75,
    )
    gw.register_provider(p1)
    gw.register_provider(p2)
    df = gw.fundamentals_history("sh600519", "2024-01-01", "2024-01-08")
    assert not df.empty
    # 两家的列都应在
    for c in ("roe_ttm", "eps_ttm", "debt_to_equity", "current_ratio"):
        assert c in df.columns, f"{c} missing"


def test_fundamentals_history_overlap_higher_priority_wins(gw):
    """重叠列 → 高优先级源胜出。"""
    p1 = _FakeHistoryProvider(
        "low", {"roe_ttm": [5.0] * 5}, priority=0.2,
    )
    p2 = _FakeHistoryProvider(
        "high", {"roe_ttm": [15.0] * 5}, priority=0.9,
    )
    gw.register_provider(p1)
    gw.register_provider(p2)
    df = gw.fundamentals_history("sh600519", "2024-01-01", "2024-01-08")
    # roe_ttm 应来自 high(15.0)
    assert df["roe_ttm"].dropna().iloc[-1] == 15.0


def test_fundamentals_history_empty_when_no_provider(gw):
    df = gw.fundamentals_history("sh600519")
    assert df.empty


# ── north_flow_history (W2-1) ────────────────────────────────────────────────


def test_north_flow_history_routes_to_provider(gw):
    df_mock = pd.DataFrame(
        {"north_flow": [10.0, -5.0, 20.0]},
        index=pd.to_datetime(["2026-05-12", "2026-05-13", "2026-05-14"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.NORTH_FLOW,), markets=(Market.GLOBAL,),
        north_history_value=df_mock,
    )
    gw.register_provider(p)
    out = gw.north_flow_history(days=10)
    assert not out.empty
    assert "north_flow" in out.columns


def test_north_flow_history_cache_hit_avoids_provider(gw):
    df_mock = pd.DataFrame(
        {"north_flow": [10.0]}, index=pd.to_datetime(["2026-05-14"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.NORTH_FLOW,), markets=(Market.GLOBAL,),
        north_history_value=df_mock,
    )
    gw.register_provider(p)
    gw.north_flow_history(days=20)
    gw.north_flow_history(days=20)
    assert len([c for c in p.call_log if "fetch_north_flow_history" in c]) == 1


# ── 全局 singleton ──────────────────────────────────────────────────────────


def test_get_gateway_registers_default_providers():
    from core.data_gateway.gateway import get_gateway, reset_gateway
    reset_gateway(None)
    gw = get_gateway()
    names = {p.name for p in gw.providers()}
    assert names == {"tencent", "sina", "eastmoney", "yfinance", "baostock", "akshare"}
    reset_gateway(None)


# ── G8: L2 落盘缓存集成 ────────────────────────────────────────────────────


def test_default_gateway_enables_disk_cache(tmp_path):
    """默认构造启用 TieredCache，cache_dir 可由参数指定。"""
    from core.data_gateway.cache import TieredCache
    gw = DataGateway(cache_dir=str(tmp_path / "gw_cache"))
    assert isinstance(gw._cache, TieredCache)
    assert gw._cache._disk is not None


def test_disk_cache_can_be_disabled():
    from core.data_gateway.cache import MemoryCache
    gw = DataGateway(enable_disk_cache=False)
    assert isinstance(gw._cache, MemoryCache)


def test_fundamentals_history_survives_l1_purge_via_l2(tmp_path):
    """fundamentals_history 走 L2 落盘：L1 清掉后仍能从 disk 回填，不再调 provider。"""
    cache_dir = str(tmp_path / "gw_cache")
    df_mock = pd.DataFrame(
        {"roe_ttm": [10.0, 11.0], "eps_ttm": [0.5, 0.6]},
        index=pd.to_datetime(["2024-03-31", "2024-06-30"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.FUNDAMENTALS_HISTORY,),
        markets=(Market.GLOBAL,), fundamentals_history_value=df_mock,
    )

    # 第一个 gateway：拉数据 + 落盘
    gw1 = DataGateway(
        health=HealthTracker(warmup_count=1), max_parallel=2,
        cache_dir=cache_dir,
    )
    gw1.register_provider(p)
    out1 = gw1.fundamentals_history("sh600519")
    assert not out1.empty
    n_calls_1 = len([c for c in p.call_log if "fetch_fundamentals_history" in c])
    assert n_calls_1 == 1

    # 第二个 gateway（模拟进程重启）：清掉 L1 但 L2 文件还在
    gw2 = DataGateway(
        health=HealthTracker(warmup_count=1), max_parallel=2,
        cache_dir=cache_dir,
    )
    p2 = _FakeProvider(
        "ak", capabilities=(Capability.FUNDAMENTALS_HISTORY,),
        markets=(Market.GLOBAL,), fundamentals_history_value=df_mock,
    )
    gw2.register_provider(p2)
    out2 = gw2.fundamentals_history("sh600519")
    assert not out2.empty
    # 关键断言：新 gateway 一次都没调 provider，全部走 L2
    assert len([c for c in p2.call_log if "fetch_fundamentals_history" in c]) == 0


def test_quote_does_not_persist_to_disk(tmp_path):
    """Quote 不在持久化白名单，即使 disk cache 开启也不应落盘。"""
    cache_dir = str(tmp_path / "gw_cache")
    p = _FakeProvider(
        "p", quote_value=Quote(symbol="sh600519", price=100),
    )
    gw = DataGateway(
        health=HealthTracker(warmup_count=1), max_parallel=2,
        cache_dir=cache_dir,
    )
    gw.register_provider(p)
    gw.quote("sh600519")
    # disk cache 目录应该不存在或为空(quote 不落盘)
    import os
    if os.path.exists(cache_dir):
        files = [f for f in os.listdir(cache_dir) if f.endswith(".parquet")]
        assert len(files) == 0


# ── G3: 时序缓存全量+切片 ────────────────────────────────────────────────


def test_fundamentals_history_caches_full_serves_slices(gw):
    """同一 symbol 不同时间窗口请求共享缓存：第 2 次起不再调 provider。"""
    full_df = pd.DataFrame(
        {"roe_ttm": [8.0, 9.0, 10.0, 11.0, 12.0]},
        index=pd.to_datetime(["2023-01-01", "2023-04-01", "2023-07-01",
                              "2023-10-01", "2024-01-01"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.FUNDAMENTALS_HISTORY,),
        markets=(Market.GLOBAL,), fundamentals_history_value=full_df,
    )
    gw.register_provider(p)

    # 第一次 miss：拉全量
    out1 = gw.fundamentals_history("sh600519", "2023-01-01", "2023-12-31")
    assert len(out1) == 4    # 2023 内 4 个季报点

    # 第二次完全不同的窗口：应命中缓存，provider 0 次额外调用
    out2 = gw.fundamentals_history("sh600519", "2023-07-01", "2024-12-31")
    assert len(out2) == 3
    n_calls = len([c for c in p.call_log if "fetch_fundamentals_history" in c])
    assert n_calls == 1     # 仍是首次的那 1 次


def test_fundamentals_history_fetch_ignores_start_end(gw):
    """G3: 拉取 provider 时不再传 start/end，让 provider 给最长可得序列。"""
    captured = {"args": None}

    class _SpyProvider(Provider):
        name = "spy"
        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.FUNDAMENTALS_HISTORY}),
                markets=frozenset({Market.GLOBAL}),
                priority_hint=0.5,
            )
        def fetch_fundamentals_history(self, symbol, start=None, end=None):
            captured["args"] = (symbol, start, end)
            return pd.DataFrame(
                {"roe_ttm": [10.0]},
                index=pd.to_datetime(["2024-01-01"]),
            )

    gw.register_provider(_SpyProvider())
    gw.fundamentals_history("sh600519", start="2024-06-01", end="2024-12-31")
    assert captured["args"] == ("sh600519", None, None)


def test_kline_caches_wide_serves_narrow(gw):
    """kline 缓存"宽窗口"，多次窄请求共享。"""
    wide_df = pd.DataFrame(
        {"open": list(range(100)), "close": list(range(1, 101)),
         "high": list(range(2, 102)), "low": list(range(100)),
         "volume": list(range(100))},
        index=pd.date_range("2024-01-01", periods=100, freq="B"),
    )
    p = _FakeProvider(
        "p", capabilities=(Capability.KLINE_DAILY,),
        markets=(Market.A,), kline_value=wide_df,
    )
    gw.register_provider(p)

    out1 = gw.kline("sh600519", interval="daily", days=30)
    assert len(out1) == 30
    out2 = gw.kline("sh600519", interval="daily", days=50)
    assert len(out2) == 50

    # 两次 kline 调用，provider 只被命中 1 次
    assert len([c for c in p.call_log if "fetch_kline_daily" in c]) == 1


def test_kline_first_fetch_widens_to_default(gw):
    """首次 miss 时 days/limit 被放宽到 _WIDE_FETCH 默认值。"""
    captured = {"kw": None}

    class _SpyP(Provider):
        name = "spy"
        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.KLINE_DAILY}),
                markets=frozenset({Market.A}),
                priority_hint=0.5,
            )
        def fetch_kline_daily(self, symbol, **kw):
            captured["kw"] = kw
            return pd.DataFrame(
                {"close": list(range(50))},
                index=pd.date_range("2024-01-01", periods=50, freq="B"),
            )

    gw.register_provider(_SpyP())
    gw.kline("sh600519", interval="daily", days=20)
    # _WIDE_FETCH[KLINE_DAILY] = {"days": 730, "limit": 730}
    assert captured["kw"]["days"] == 730


def test_fund_flow_caches_full_serves_slices(gw):
    """fund_flow 缓存全量，按 start/end 切片。"""
    full_df = pd.DataFrame(
        {"main_net_inflow": list(range(60))},
        index=pd.date_range("2024-01-01", periods=60, freq="B"),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.FUND_FLOW,),
        markets=(Market.GLOBAL,),
    )
    p._fund_flow = full_df    # 通过实例变量直接注入(FakeProvider 字段命名问题)

    # FakeProvider 用 margin_flow_value=... 兼容 fund_flow？让我们直接给一个适配
    # 简单点：mock fetch_fund_flow
    class _FFProvider(Provider):
        name = "ff"
        call_log: List[str] = []
        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.FUND_FLOW}),
                markets=frozenset({Market.GLOBAL}),
                priority_hint=0.5,
            )
        def fetch_fund_flow(self, symbol, start=None, end=None):
            self.call_log.append(f"fetch_fund_flow:{symbol}:{start}:{end}")
            return full_df

    ff = _FFProvider()
    gw.register_provider(ff)

    out1 = gw.fund_flow("sh600519", start="2024-01-15", end="2024-02-15")
    assert (out1.index >= pd.Timestamp("2024-01-15")).all()
    assert (out1.index <= pd.Timestamp("2024-02-15")).all()

    out2 = gw.fund_flow("sh600519", start="2024-02-20", end="2024-03-31")
    assert (out2.index >= pd.Timestamp("2024-02-20")).all()
    # provider 只调用 1 次
    assert len(ff.call_log) == 1
    # 拉取时不传 start/end
    assert ff.call_log[0].endswith(":None:None")


def test_north_flow_history_caches_full_serves_tail(gw):
    """north_flow_history 缓存全量，按 days 取末尾。"""
    full_df = pd.DataFrame(
        {"north_flow": list(range(500))},
        index=pd.date_range("2023-01-01", periods=500, freq="B"),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.NORTH_FLOW,), markets=(Market.GLOBAL,),
        north_history_value=full_df,
    )
    gw.register_provider(p)

    out1 = gw.north_flow_history(days=30)
    assert len(out1) == 30
    out2 = gw.north_flow_history(days=100)
    assert len(out2) == 100
    out3 = gw.north_flow_history(days=500)
    assert len(out3) == 500

    # 3 次请求，provider 只被命中 1 次
    assert len([c for c in p.call_log if "fetch_north_flow_history" in c]) == 1


def test_north_flow_history_first_fetch_widens(gw):
    """首次 miss 时 days 放宽到 5 年。"""
    captured = {"days": None}

    class _SpyP(Provider):
        name = "spy"
        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.NORTH_FLOW}),
                markets=frozenset({Market.GLOBAL}),
                priority_hint=0.5,
            )
        def fetch_north_flow_history(self, days=252):
            captured["days"] = days
            return pd.DataFrame(
                {"north_flow": [1.0]},
                index=pd.to_datetime(["2024-01-01"]),
            )

    gw.register_provider(_SpyP())
    gw.north_flow_history(days=30)
    # _WIDE_FETCH[NORTH_FLOW] = {"days": 1825}
    assert captured["days"] == 1825


def test_invalidate_fundamentals_history_precise(gw):
    """G3 后缓存键只有 symbol，精确 invalidate 即可。"""
    df = pd.DataFrame(
        {"roe_ttm": [10.0]}, index=pd.to_datetime(["2024-01-01"]),
    )
    p = _FakeProvider(
        "ak", capabilities=(Capability.FUNDAMENTALS_HISTORY,),
        markets=(Market.GLOBAL,), fundamentals_history_value=df,
    )
    gw.register_provider(p)

    gw.fundamentals_history("sh600519")
    gw.fundamentals_history("sh000001")
    gw.invalidate_fundamentals_history("sh600519")

    # sh600519 被清，再请求会触发 provider
    gw.fundamentals_history("sh600519")
    n_calls_a = len([c for c in p.call_log if "fetch_fundamentals_history:sh600519" in c])
    assert n_calls_a == 2
    # sh000001 仍命中缓存
    gw.fundamentals_history("sh000001")
    n_calls_b = len([c for c in p.call_log if "fetch_fundamentals_history:sh000001" in c])
    assert n_calls_b == 1


# ── G1: _merged_history_fetch helper 直接测试 ─────────────────────────────


def test_merged_history_fetch_complementary_columns(gw):
    """两源贡献不同列 → 合并后列并集，每列 provenance 正确。"""
    df_a = pd.DataFrame(
        {"roe_ttm": [10.0, 11.0]},
        index=pd.to_datetime(["2024-03-31", "2024-06-30"]),
    )
    df_b = pd.DataFrame(
        {"eps_yoy": [20.0, 25.0]},
        index=pd.to_datetime(["2024-03-31", "2024-06-30"]),
    )
    a = _FakeProvider("baostock", priority_hint=0.9,
                      capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                      markets=(Market.GLOBAL,),
                      fundamentals_history_value=df_a)
    b = _FakeProvider("akshare", priority_hint=0.5,
                      capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                      markets=(Market.GLOBAL,),
                      fundamentals_history_value=df_b)
    gw.register_provider(a)
    gw.register_provider(b)

    merged, prov = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
    )
    assert "roe_ttm" in merged.columns
    assert "eps_yoy" in merged.columns
    assert prov["roe_ttm"] == "baostock"
    assert prov["eps_yoy"] == "akshare"


def test_merged_history_fetch_overlap_high_score_wins(gw):
    """同列重叠值 → 高 score 源胜出。"""
    idx = pd.to_datetime(["2024-01-01", "2024-04-01"])
    df_hi = pd.DataFrame({"roe_ttm": [12.0, 13.0]}, index=idx)
    df_lo = pd.DataFrame({"roe_ttm": [8.0, 9.0]}, index=idx)
    hi = _FakeProvider("hi", priority_hint=0.95,
                       capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                       markets=(Market.GLOBAL,),
                       fundamentals_history_value=df_hi)
    lo = _FakeProvider("lo", priority_hint=0.20,
                       capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                       markets=(Market.GLOBAL,),
                       fundamentals_history_value=df_lo)
    gw.register_provider(hi)
    gw.register_provider(lo)

    merged, prov = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
    )
    assert merged["roe_ttm"].tolist() == [12.0, 13.0]
    assert prov["roe_ttm"] == "hi"


def test_merged_history_fetch_low_score_fills_high_score_gap(gw):
    """高 score 源缺某行 → 低 score 源补缺。"""
    df_hi = pd.DataFrame(
        {"roe_ttm": [10.0]},
        index=pd.to_datetime(["2024-01-01"]),
    )
    df_lo = pd.DataFrame(
        {"roe_ttm": [8.0, 9.0]},
        index=pd.to_datetime(["2024-01-01", "2024-04-01"]),
    )
    hi = _FakeProvider("hi", priority_hint=0.95,
                       capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                       markets=(Market.GLOBAL,),
                       fundamentals_history_value=df_hi)
    lo = _FakeProvider("lo", priority_hint=0.20,
                       capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                       markets=(Market.GLOBAL,),
                       fundamentals_history_value=df_lo)
    gw.register_provider(hi)
    gw.register_provider(lo)

    merged, _ = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
        ffill=False,
    )
    # 索引并集 [2024-01-01, 2024-04-01]
    assert len(merged) == 2
    # 2024-01-01：hi 有值（10），用 hi
    assert merged.loc["2024-01-01", "roe_ttm"] == 10.0
    # 2024-04-01：hi 缺，用 lo 的 9
    assert merged.loc["2024-04-01", "roe_ttm"] == 9.0


def test_merged_history_fetch_all_empty_returns_empty(gw):
    a = _FakeProvider("a", priority_hint=0.5,
                      capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                      markets=(Market.GLOBAL,),
                      fundamentals_history_value=pd.DataFrame())
    gw.register_provider(a)
    merged, prov = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
    )
    assert merged.empty
    assert prov == {}


def test_merged_history_fetch_no_candidates_returns_empty(gw):
    """无声明该 capability 的 provider → 空。"""
    merged, prov = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
    )
    assert merged.empty
    assert prov == {}


def test_merged_history_fetch_object_column_does_not_raise(gw):
    """pandas ≥ 2.2 移除了 pd.to_numeric(errors='ignore')，旧代码在 object
    列上会抛 ValueError。本回归保证：

      - 列里混入字符串（如 'N/A'）时合并不抛异常
      - 该列保持 object dtype（无法整体转 numeric，按"保留原状"语义处理）
      - 邻近的纯数值列被合并后仍是 numeric dtype
    """
    idx = pd.to_datetime(["2024-03-31", "2024-06-30"])
    df_a = pd.DataFrame(
        {"roe_ttm": [10.0, 11.0], "note": ["A", "N/A"]}, index=idx,
    )
    df_b = pd.DataFrame(
        {"roe_ttm": [9.5, None], "note": [None, "B"]}, index=idx,
    )
    a = _FakeProvider("a", priority_hint=0.9,
                      capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                      markets=(Market.GLOBAL,),
                      fundamentals_history_value=df_a)
    b = _FakeProvider("b", priority_hint=0.3,
                      capabilities=(Capability.FUNDAMENTALS_HISTORY,),
                      markets=(Market.GLOBAL,),
                      fundamentals_history_value=df_b)
    gw.register_provider(a)
    gw.register_provider(b)

    merged, _ = gw._merged_history_fetch(
        Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
        "fetch_fundamentals_history", "sh600519", None, None,
        ffill=False,
    )
    # 关键：不抛异常即过；下面只做轻量结构断言
    assert "roe_ttm" in merged.columns
    assert "note" in merged.columns
    # roe_ttm 仍是 numeric，note 保留 non-numeric（object 或 pandas 3.0
    # 的 StringDtype 均可，反正不是数值列）
    assert pd.api.types.is_numeric_dtype(merged["roe_ttm"])
    assert not pd.api.types.is_numeric_dtype(merged["note"])


def test_kline_daily_persists_kline_minute_does_not(tmp_path):
    """KLINE_DAILY 在白名单内落盘；KLINE_MINUTE 不在白名单不落盘。"""
    cache_dir = str(tmp_path / "gw_cache")
    kline_df = pd.DataFrame(
        {"open": [10], "close": [11], "high": [11.5], "low": [9.8], "volume": [1000]},
        index=pd.to_datetime(["2024-01-02"]),
    )
    p = _FakeProvider(
        "p", capabilities=(Capability.KLINE_DAILY, Capability.KLINE_MINUTE),
        markets=(Market.A, Market.HK), kline_value=kline_df,
    )
    gw = DataGateway(
        health=HealthTracker(warmup_count=1), max_parallel=2,
        cache_dir=cache_dir,
    )
    gw.register_provider(p)
    gw.kline("sh600519", interval="daily")
    gw.kline("hk00700", interval="5m")

    import os
    files = [f for f in os.listdir(cache_dir) if f.endswith(".parquet")]
    # 只有 1 个 parquet 文件（daily 的），minute 的不落盘
    assert len(files) == 1

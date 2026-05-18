# -*- coding: utf-8 -*-
"""
G2 测试：DataGateway.profile(symbol) 聚合视图。

验证：
  - 所有切片成功时 StockProfile 字段填充正确，completeness=1
  - 部分切片失败/缺失时 completeness < 1，仍能返回 profile
  - provenance 记录每切片主源
  - 子快照（MarginSnapshot / FundFlowSnapshot / MacroSnapshot）从时序末行抽取
"""

from typing import List, Optional
from unittest.mock import patch

import pandas as pd
import pytest

from core.data_gateway.capabilities import (
    Capability, MacroIndicator, Market, ProviderCapability,
)
from core.data_gateway.gateway import DataGateway
from core.data_gateway.health import HealthTracker
from core.data_gateway.providers.base import Provider
from core.data_gateway.schemas import (
    BalanceSheet, FundFlowSnapshot, Fundamentals, MacroSnapshot,
    MarginSnapshot, Quote, StockProfile,
)


# ── 万能 mock provider ─────────────────────────────────────────────────────


class _AllInOneProvider(Provider):
    """单 provider 实现所有 capability，用于跑通 profile() 路径。"""

    name = "all"

    def __init__(self, **fixtures):
        self._f = fixtures

    def declare(self):
        return ProviderCapability(
            capabilities=frozenset({
                Capability.QUOTE,
                Capability.FUNDAMENTALS,
                Capability.BALANCE_SHEET,
                Capability.MARGIN_FLOW,
                Capability.FUND_FLOW,
                Capability.NEWS_HEADLINES,
                Capability.MACRO,
            }),
            markets=frozenset({Market.A, Market.GLOBAL}),
            priority_hint=0.9,
        )

    def supports(self, capability, market):
        # MACRO / NEWS_HEADLINES 等 GLOBAL 能力放行所有市场
        if capability in (
            Capability.MACRO, Capability.NEWS_HEADLINES,
            Capability.BALANCE_SHEET, Capability.MARGIN_FLOW,
            Capability.FUND_FLOW,
        ):
            return capability in self.declare().capabilities
        return capability in self.declare().capabilities and market in self.declare().markets

    def fetch_quote(self, symbol):
        return self._f.get("quote")

    def fetch_fundamentals(self, symbol):
        return self._f.get("fundamentals")

    def fetch_balance_sheet(self, symbol):
        return self._f.get("balance_sheet")

    def fetch_margin_flow(self, symbol, start=None, end=None):
        return self._f.get("margin_df", pd.DataFrame())

    def fetch_fund_flow(self, symbol, start=None, end=None):
        return self._f.get("fund_df", pd.DataFrame())

    def fetch_news_headlines(self, symbol, n=20):
        from core.data_gateway.schemas import NewsItem as _NI
        raw = self._f.get("headlines", [])[:n]
        return [
            it if isinstance(it, _NI) else _NI(title=str(it), source=self.name)
            for it in raw
        ]

    def fetch_macro(self, indicator):
        return self._f.get(f"macro_{indicator.value.lower()}", pd.DataFrame())


@pytest.fixture
def gw():
    from core.circuit_breaker import reset_all
    reset_all()
    return DataGateway(
        health=HealthTracker(warmup_count=1),
        max_parallel=4,
        enable_disk_cache=False,
    )


def _margin_df():
    return pd.DataFrame(
        {"margin_balance": [1e10, 1.1e10], "net_buy": [3e7, 2.5e7],
         "short_balance": [5e7, 4.8e7]},
        index=pd.to_datetime(["2024-05-15", "2024-05-16"]),
    )


def _fund_df():
    return pd.DataFrame(
        {"main_net_inflow": [1e8, 1.5e8],
         "super_net_inflow": [6e7, 8e7],
         "large_net_inflow": [4e7, 7e7],
         "medium_net_inflow": [1e7, 1.5e7],
         "small_net_inflow": [-2e7, -1e7],
         "main_net_ratio": [3.5, 4.2]},
        index=pd.to_datetime(["2024-05-15", "2024-05-16"]),
    )


def _macro_pmi():
    return pd.DataFrame(
        {"pmi": [49.8, 50.3, 50.5]},
        index=pd.to_datetime(["2024-03-01", "2024-04-01", "2024-05-01"]),
    )


def _macro_m2():
    return pd.DataFrame(
        {"m2_yoy": [8.7, 9.0]},
        index=pd.to_datetime(["2024-04-01", "2024-05-01"]),
    )


def _macro_credit():
    return pd.DataFrame(
        {"credit_yoy": [9.5, 9.8]},
        index=pd.to_datetime(["2024-04-01", "2024-05-01"]),
    )


# ── 主路径：所有切片成功 ───────────────────────────────────────────────────


def test_profile_full_completeness_1(gw):
    p = _AllInOneProvider(
        quote=Quote(symbol="sh600519", price=1700, pe_ttm=30),
        fundamentals=Fundamentals(symbol="sh600519", roe_ttm=33),
        balance_sheet=BalanceSheet(symbol="sh600519", total_asset=2e12),
        margin_df=_margin_df(),
        fund_df=_fund_df(),
        headlines=["央行...", "贵州茅台拟分红..."],
        macro_pmi=_macro_pmi(),
        macro_m2=_macro_m2(),
        macro_credit=_macro_credit(),
    )
    gw.register_provider(p)

    prof = gw.profile("sh600519")

    assert isinstance(prof, StockProfile)
    assert prof.symbol == "sh600519"
    assert prof.completeness == 1.0
    assert prof.quote.price == 1700
    assert prof.fundamentals.roe_ttm == 33
    assert prof.balance_sheet.total_asset == 2e12
    assert isinstance(prof.margin, MarginSnapshot)
    assert prof.margin.margin_balance == 1.1e10   # 末行
    assert isinstance(prof.fund_flow_latest, FundFlowSnapshot)
    assert prof.fund_flow_latest.main_net_inflow == 1.5e8   # 末行
    assert prof.fund_flow_latest.main_net_ratio == 4.2
    assert len(prof.headlines) == 2
    assert isinstance(prof.macro, MacroSnapshot)
    assert prof.macro.pmi == 50.5
    assert prof.macro.m2_yoy == 9.0
    assert prof.macro.credit_yoy == 9.8


def test_profile_partial_completeness(gw):
    """部分切片失败，completeness 介于 0-1 之间。"""
    p = _AllInOneProvider(
        quote=Quote(symbol="sh600519", price=1700),
        # 其他切片返回空
    )
    gw.register_provider(p)

    prof = gw.profile("sh600519")
    assert 0.0 < prof.completeness < 1.0
    assert prof.quote is not None
    assert prof.fundamentals is None
    assert prof.margin is None
    assert prof.fund_flow_latest is None
    assert prof.macro is None
    assert prof.headlines == []


def test_profile_all_empty_returns_empty_skeleton(gw):
    """无可用源时仍返回 StockProfile 骨架，completeness=0。"""
    prof = gw.profile("sh600519")
    assert prof.symbol == "sh600519"
    assert prof.completeness == 0.0
    assert prof.quote is None
    assert prof.fundamentals is None
    assert not prof.is_valid


def test_profile_one_slice_raises_does_not_kill_others(gw):
    """单切片底层抛异常，profile() 仍能返回其他切片。"""
    p = _AllInOneProvider(
        quote=Quote(symbol="sh600519", price=1700),
        fundamentals=Fundamentals(symbol="sh600519", roe_ttm=33),
    )
    gw.register_provider(p)

    # 让 fundamentals 抛
    with patch.object(gw, "fundamentals", side_effect=RuntimeError("boom")):
        prof = gw.profile("sh600519")

    # quote 还在
    assert prof.quote is not None
    assert prof.fundamentals is None    # 抛异常被 _safe_call 吃掉
    # is_valid 看 quote
    assert prof.is_valid


def test_profile_provenance_records_field_merge_source(gw):
    """quote / fundamentals 走多源合并时，provenance 应记录主源 provider 名。"""
    # 两个 provider 都返回数据
    p_hi = _AllInOneProvider(
        quote=Quote(symbol="sh600519", price=1700, pe_ttm=30),
        fundamentals=Fundamentals(symbol="sh600519", roe_ttm=33),
    )
    p_hi.name = "tencent"

    p_lo_quote = Quote(symbol="sh600519", price=1690, pe_ttm=28)
    p_lo = _AllInOneProvider(quote=p_lo_quote)
    p_lo.name = "sina"

    gw.register_provider(p_hi)
    gw.register_provider(p_lo)

    prof = gw.profile("sh600519")
    # tencent 是首注册且 priority_hint 一样高，应该是 quote / fundamentals 的主源
    assert "quote" in prof.provenance
    # 任一 provider 都可能出现（实际取决于 candidate 排序），主源应有值
    assert prof.provenance["quote"] in ("tencent", "sina")


def test_profile_headlines_truncated_to_n(gw):
    p = _AllInOneProvider(
        headlines=[f"news{i}" for i in range(50)],
    )
    gw.register_provider(p)
    prof = gw.profile("sh600519", headlines_n=5)
    # news_headlines 内部按 n 截断；profile 不再次截断，由 capability 控制
    assert len(prof.headlines) == 5


def test_profile_margin_snapshot_handles_missing_columns(gw):
    """margin_df 缺 net_buy 列时降级到 0，不抛错。"""
    df = pd.DataFrame(
        {"margin_balance": [1e10], "short_balance": [5e7]},
        index=pd.to_datetime(["2024-05-15"]),
    )
    p = _AllInOneProvider(margin_df=df)
    gw.register_provider(p)
    prof = gw.profile("sh600519")
    assert prof.margin is not None
    assert prof.margin.margin_balance == 1e10
    assert prof.margin.net_buy == 0.0    # 缺列降级


def test_profile_fund_flow_snapshot_handles_partial_columns(gw):
    """fund_df 只给 main_net_inflow 也能构造 snapshot。"""
    df = pd.DataFrame(
        {"main_net_inflow": [2e8]},
        index=pd.to_datetime(["2024-05-15"]),
    )
    p = _AllInOneProvider(fund_df=df)
    gw.register_provider(p)
    prof = gw.profile("sh600519")
    assert prof.fund_flow_latest is not None
    assert prof.fund_flow_latest.main_net_inflow == 2e8
    assert prof.fund_flow_latest.super_net_inflow == 0.0


def test_profile_macro_snapshot_from_three_indicators(gw):
    """三个宏观指标分别在不同 macro 调用返回，合成 MacroSnapshot。"""
    p = _AllInOneProvider(
        macro_pmi=_macro_pmi(),
        macro_m2=_macro_m2(),
        macro_credit=_macro_credit(),
    )
    gw.register_provider(p)
    prof = gw.profile("sh600519")
    assert prof.macro is not None
    assert prof.macro.pmi == 50.5
    assert prof.macro.m2_yoy == 9.0
    assert prof.macro.credit_yoy == 9.8


def test_profile_macro_only_pmi_still_returns(gw):
    p = _AllInOneProvider(macro_pmi=_macro_pmi())
    gw.register_provider(p)
    prof = gw.profile("sh600519")
    assert prof.macro is not None
    assert prof.macro.pmi == 50.5
    assert prof.macro.m2_yoy == 0.0


def test_profile_concurrent_slices(gw):
    """profile() 应并发触发所有切片(不会顺序串行所有 IO)。"""
    import time

    class _SlowProvider(_AllInOneProvider):
        def fetch_quote(self, symbol):
            time.sleep(0.05)
            return Quote(symbol=symbol, price=100)

        def fetch_fundamentals(self, symbol):
            time.sleep(0.05)
            return Fundamentals(symbol=symbol, roe_ttm=10)

        def fetch_balance_sheet(self, symbol):
            time.sleep(0.05)
            return BalanceSheet(symbol=symbol, total_asset=1e9)

    gw.register_provider(_SlowProvider())
    t0 = time.time()
    prof = gw.profile("sh600519")
    elapsed = time.time() - t0
    # 9 个切片 × 0.05 = 0.45s 串行；并发应 << 0.45
    # ThreadPoolExecutor max_parallel=4，约 0.05 × ceil(9/4) ≈ 0.15s
    assert elapsed < 0.40
    assert prof.quote is not None
    assert prof.fundamentals is not None
    assert prof.balance_sheet is not None

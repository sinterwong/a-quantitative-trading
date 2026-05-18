# -*- coding: utf-8 -*-
"""
G4 — ROUTING_POLICY + DataGateway._route() 路由分派测试。

验证：
  1. 路由表覆盖完整（每个公开方法都有对应 policy）。
  2. _route 把三种已实现策略正确分派到对应底层原语。
  3. 改 policy 即改路由：把 FAILOVER 改成 MERGE_FIELDS 后行为切换。
  4. FAILOVER 现在写 {"_provider": name} 到 _last_provenance。
  5. MERGE_LISTS 占位（G5 实现）；未登记 (cap, fn) → KeyError。
"""

from typing import Optional
from unittest.mock import patch

import pandas as pd
import pytest

from core.data_gateway.capabilities import (
    Capability, CapabilityPolicy, Market, ProviderCapability,
    ROUTING_POLICY, RoutingStrategy, get_policy,
)
from core.data_gateway.gateway import DataGateway
from core.data_gateway.providers.base import Provider
from core.data_gateway.schemas import Fundamentals, Quote


# ── 路由表覆盖检查 ────────────────────────────────────────────────────────────


def test_routing_policy_covers_all_public_fetch_methods():
    """每个 gateway 实际调用的 (Capability, fetch_*) 都应在 ROUTING_POLICY。

    缺漏 → 运行时 KeyError。本测试保证新增 capability 时同步登记。
    """
    expected = {
        (Capability.QUOTE, "fetch_quote"),
        (Capability.QUOTE, "fetch_quotes"),
        (Capability.KLINE_DAILY, "fetch_kline_daily"),
        (Capability.KLINE_MINUTE, "fetch_kline_minute"),
        (Capability.FUNDAMENTALS, "fetch_fundamentals"),
        (Capability.SECTOR_RANKING, "fetch_sectors"),
        (Capability.SECTOR_CONSTITUENTS, "fetch_sector_constituents"),
        (Capability.NORTH_FLOW, "fetch_north_flow"),
        (Capability.NORTH_FLOW, "fetch_north_flow_history"),
        (Capability.MARKET_INDEX, "fetch_market_index"),
        (Capability.MACRO, "fetch_macro"),
        (Capability.FUNDAMENTALS_HISTORY, "fetch_fundamentals_history"),
        (Capability.BALANCE_SHEET, "fetch_balance_sheet"),
        (Capability.MARGIN_FLOW, "fetch_margin_flow"),
        (Capability.FUND_FLOW, "fetch_fund_flow"),
        (Capability.NEWS_HEADLINES, "fetch_news_headlines"),
    }
    assert expected.issubset(ROUTING_POLICY.keys()), (
        "缺少路由声明: " + str(expected - ROUTING_POLICY.keys())
    )


def test_get_policy_unknown_raises_keyerror():
    """未登记的 (cap, fn) 抛 KeyError，杜绝静默走默认分支。"""
    with pytest.raises(KeyError, match="未登记的路由策略"):
        get_policy(Capability.QUOTE, "fetch_does_not_exist")


def test_routing_policy_default_strategies_unchanged_post_g4():
    """G4 是纯重构，关键 capability 的默认策略不能在重构里被悄悄改掉。

    G5 后会显式把 NEWS_HEADLINES 改成 MERGE_LISTS，那时同步改这里。
    """
    assert get_policy(Capability.QUOTE, "fetch_quote").strategy is (
        RoutingStrategy.MERGE_FIELDS
    )
    assert get_policy(Capability.FUNDAMENTALS, "fetch_fundamentals").strategy is (
        RoutingStrategy.MERGE_FIELDS
    )
    assert get_policy(Capability.KLINE_DAILY, "fetch_kline_daily").strategy is (
        RoutingStrategy.MERGE_FRAMES
    )
    assert get_policy(
        Capability.FUNDAMENTALS_HISTORY, "fetch_fundamentals_history",
    ).ffill is True, "季报历史必须 ffill"
    assert get_policy(Capability.KLINE_DAILY, "fetch_kline_daily").ffill is False, (
        "K 线缺失多为停牌，禁止 ffill"
    )
    # G5: news 切到 MERGE_LISTS（EM kuaixun + AkShare 财联社电报多源去重）
    assert get_policy(Capability.NEWS_HEADLINES, "fetch_news_headlines").strategy is (
        RoutingStrategy.MERGE_LISTS
    )


# ── 分派器行为：用 spy 验证 _route 调对底层原语 ─────────────────────────────


@pytest.fixture
def gw():
    return DataGateway(enable_disk_cache=False)


def test_route_failover_calls_sequential_and_wraps_provider_name(gw):
    """FAILOVER 策略：调用 _sequential_fetch + 把 provider_name 包成 prov dict。"""
    with patch.object(
        gw, "_sequential_fetch", return_value=("HIT", "tencent"),
    ) as spy:
        result, prov = gw._route(
            Capability.SECTOR_RANKING, Market.A, "fetch_sectors", 50,
        )
    spy.assert_called_once_with(
        Capability.SECTOR_RANKING, Market.A, "fetch_sectors", 50,
    )
    assert result == "HIT"
    assert prov == {"_provider": "tencent"}


def test_route_failover_no_source_returns_empty_prov(gw):
    """FAILOVER 无源：prov 应为空 dict，与"有源时给 _provider"对称。"""
    with patch.object(gw, "_sequential_fetch", return_value=(None, None)):
        result, prov = gw._route(
            Capability.SECTOR_RANKING, Market.A, "fetch_sectors", 50,
        )
    assert result is None
    assert prov == {}


def test_route_merge_fields_passes_skip_fields_from_policy(gw):
    """MERGE_FIELDS：skip_fields 从 ROUTING_POLICY 取出来塞进去，不再硬编码。"""
    with patch.object(
        gw, "_merged_fetch", return_value=("MERGED", {"pe_ttm": "tencent"}),
    ) as spy:
        gw._route(
            Capability.QUOTE, Market.A, "fetch_quote", "sh600519",
        )
    # _merged_fetch(cap, market, fn_name, skip_fields, *args, **kwargs)
    args, kwargs = spy.call_args
    assert args[0] is Capability.QUOTE
    assert args[2] == "fetch_quote"
    # skip_fields 来自 policy
    expected_skip = get_policy(Capability.QUOTE, "fetch_quote").skip_fields
    assert args[3] == expected_skip
    assert args[4] == "sh600519"   # 实际业务 arg 跟在后面


def test_route_merge_frames_passes_ffill_from_policy(gw):
    """MERGE_FRAMES：ffill 从 policy 取，季报 ffill=True、K 线 ffill=False。"""
    with patch.object(
        gw, "_merged_history_fetch", return_value=(pd.DataFrame(), {}),
    ) as spy:
        gw._route(
            Capability.FUNDAMENTALS_HISTORY, Market.GLOBAL,
            "fetch_fundamentals_history", "sh600519", None, None,
        )
    args, kwargs = spy.call_args
    assert kwargs["ffill"] is True   # 季报必须 ffill

    spy.reset_mock()
    with patch.object(
        gw, "_merged_history_fetch", return_value=(pd.DataFrame(), {}),
    ) as spy2:
        gw._route(
            Capability.KLINE_DAILY, Market.A, "fetch_kline_daily",
            "sh600519", days=120,
        )
    _, k = spy2.call_args
    assert k["ffill"] is False
    assert k["days"] == 120


def test_route_merge_lists_dispatches_to_merged_list_fetch(gw):
    """G5: MERGE_LISTS 走 _merged_list_fetch（替代 G4 的 NotImplementedError）。"""
    with patch.object(
        gw, "_merged_list_fetch", return_value=(["X"], {"em": "1"}),
    ) as spy:
        result, prov = gw._route(
            Capability.NEWS_HEADLINES, Market.GLOBAL,
            "fetch_news_headlines", "sh600519", 10,
        )
    spy.assert_called_once_with(
        Capability.NEWS_HEADLINES, Market.GLOBAL,
        "fetch_news_headlines", "sh600519", 10,
    )
    assert result == ["X"]
    assert prov == {"em": "1"}


def test_route_unknown_capability_raises_keyerror(gw):
    """未登记的 (cap, fn) 抛 KeyError，避免静默落入默认分支。"""
    with pytest.raises(KeyError, match="未登记的路由策略"):
        gw._route(Capability.QUOTE, Market.A, "fetch_not_a_real_method")


# ── 行为可切换性：改 policy 即改路由 ──────────────────────────────────────


class _MinimalProvider(Provider):
    """只实现 fetch_sectors 的极简 provider，用于切策略行为切换。"""

    name = "minp"

    def __init__(self, sectors):
        self._sectors = sectors

    def declare(self):
        return ProviderCapability(
            capabilities=frozenset({Capability.SECTOR_RANKING}),
            markets=frozenset({Market.A}),
            priority_hint=0.8,
        )

    def fetch_sectors(self, limit=100):
        return self._sectors


def test_switching_policy_actually_changes_dispatched_primitive(monkeypatch):
    """同一调用，改 ROUTING_POLICY 中的 strategy 后，被调用的底层原语随之切换。

    这是 G4"声明式"的核心保证：策略由元数据驱动，不由 gateway 方法内部固化。
    """
    gw = DataGateway(enable_disk_cache=False)

    # 默认 FAILOVER：_sequential_fetch 被调用
    with patch.object(
        gw, "_sequential_fetch", return_value=(["sec1"], "minp"),
    ) as seq_spy, patch.object(
        gw, "_merged_fetch",
    ) as merge_spy:
        gw._route(Capability.SECTOR_RANKING, Market.A, "fetch_sectors", 10)
    seq_spy.assert_called_once()
    merge_spy.assert_not_called()

    # 改 policy 为 MERGE_FIELDS：_merged_fetch 被调用
    monkeypatch.setitem(
        ROUTING_POLICY,
        (Capability.SECTOR_RANKING, "fetch_sectors"),
        CapabilityPolicy(
            RoutingStrategy.MERGE_FIELDS, skip_fields=("name",),
        ),
    )
    with patch.object(gw, "_sequential_fetch") as seq_spy2, patch.object(
        gw, "_merged_fetch", return_value=("X", {}),
    ) as merge_spy2:
        gw._route(Capability.SECTOR_RANKING, Market.A, "fetch_sectors", 10)
    seq_spy2.assert_not_called()
    merge_spy2.assert_called_once()
    # 切到 MERGE_FIELDS 后，policy.skip_fields 透传到 _merged_fetch
    assert merge_spy2.call_args[0][3] == ("name",)


# ── 集成层：FAILOVER 现在写 _provider 到 _last_provenance ───────────────


def test_failover_writes_provider_to_last_provenance():
    """G4 副作用：margin_flow / market_index 等 FAILOVER 路径调用后，
    self._last_provenance[key] = {"_provider": <name>}。
    G2 留下的 `margin best-effort` 注释从此作废。
    """
    from core.circuit_breaker import reset_all
    reset_all()
    gw = DataGateway(enable_disk_cache=False)

    df = pd.DataFrame(
        {"margin_balance": [1.0, 2.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

    class _MgnProvider(Provider):
        name = "akshare"

        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.MARGIN_FLOW}),
                markets=frozenset({Market.GLOBAL, Market.A}),
                priority_hint=0.8,
            )

        def fetch_margin_flow(self, symbol, start=None, end=None):
            return df

    gw.register_provider(_MgnProvider())

    out = gw.margin_flow("sh600519")
    assert not out.empty
    prov = gw.provenance("margin_flow:sh600519:None:None")
    assert prov == {"_provider": "akshare"}, (
        f"FAILOVER 应记录 _provider；got {prov}"
    )

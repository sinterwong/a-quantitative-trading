# -*- coding: utf-8 -*-
"""schema 完整性元数据 — missing_capabilities / stale_seconds / confidence。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.data_gateway.capabilities import Capability, Market
from core.data_gateway.gateway import DataGateway, _stale_seconds
from core.data_gateway.merge import Candidate, merge_field_level
from core.data_gateway.schemas import (
    BalanceSheet, Fundamentals, Quote, StockProfile,
)


# ── Quote.confidence by merge_field_level ───────────────────────────────────


def test_quote_confidence_single_source_uses_health():
    q = Quote(symbol="x", price=100.0)
    obj, _ = merge_field_level([Candidate("A", q, health=0.42)])
    assert obj.confidence == pytest.approx(0.42)


def test_quote_confidence_merged_averages_health():
    a = Quote(symbol="x", price=100.0)
    b = Quote(symbol="x", price=100.0)
    obj, _ = merge_field_level([
        Candidate("A", a, health=0.9),
        Candidate("B", b, health=0.5),
    ])
    # (0.9 + 0.5) / 2 = 0.7
    assert obj.confidence == pytest.approx(0.7)


def test_quote_confidence_clamped_to_unit_interval():
    """Candidate 已把 health 截到 [0,1]，输出也应在范围内。"""
    a = Quote(symbol="x", price=100.0)
    obj, _ = merge_field_level([Candidate("A", a, health=2.0)])
    assert 0.0 <= obj.confidence <= 1.0


def test_quote_confidence_default_on_dataclass():
    """没有合并、直接构造的 Quote.confidence 默认为 1.0。"""
    q = Quote(symbol="x", price=100.0)
    assert q.confidence == 1.0


# ── Fundamentals.stale_seconds / BalanceSheet.stale_seconds ──────────────────


def test_stale_seconds_helper_zero_when_fresh():
    assert _stale_seconds(datetime.now()) == 0


def test_stale_seconds_helper_positive_for_old_timestamp():
    old = datetime.now() - timedelta(seconds=120)
    val = _stale_seconds(old)
    assert 115 <= val <= 130


def test_stale_seconds_helper_negative_clock_drift_returns_zero():
    future = datetime.now() + timedelta(seconds=60)
    assert _stale_seconds(future) == 0


def test_fundamentals_stale_seconds_default_zero():
    f = Fundamentals(symbol="x", pe_ttm=10.0)
    assert f.stale_seconds == 0


def test_fundamentals_cache_hit_stamps_stale_seconds():
    """从缓存返回的 Fundamentals 应携带 stale_seconds，反映 timestamp 与 now 的差。"""
    gw = DataGateway(enable_disk_cache=False)
    old_ts = datetime.now() - timedelta(seconds=45)
    cached = Fundamentals(symbol="600519.SH", pe_ttm=10.0, timestamp=old_ts)
    gw._cache.set("fundamentals:600519.SH", cached, ttl=600)

    out = gw.fundamentals("600519.SH")
    assert out is not None
    assert 40 <= out.stale_seconds <= 60


def test_balance_sheet_cache_hit_stamps_stale_seconds():
    gw = DataGateway(enable_disk_cache=False)
    old_ts = datetime.now() - timedelta(seconds=300)
    cached = BalanceSheet(symbol="600519.SH", total_asset=1.0, timestamp=old_ts)
    gw._cache.set("balance_sheet:600519.SH", cached, ttl=900)

    out = gw.balance_sheet("600519.SH")
    assert out is not None
    assert 295 <= out.stale_seconds <= 320


# ── StockProfile.missing_capabilities ────────────────────────────────────────


def test_missing_capabilities_default_empty():
    prof = StockProfile(symbol="x")
    assert prof.missing_capabilities == []


def test_missing_capabilities_populated_when_slots_empty():
    """build_profile：所有切片返回 None 时 missing_capabilities 列出全部 7 项。"""
    from core.data_gateway.profile import build_profile

    class _NullGateway:
        """提供 build_profile 所需的最小桩。所有 fetch 全返回 None。"""

        def __init__(self):
            self._last_provenance = {}

        def _get_profile_executor(self):
            from concurrent.futures import ThreadPoolExecutor
            return ThreadPoolExecutor(max_workers=2)

        def quote(self, *_a, **_kw): return None
        def fundamentals(self, *_a, **_kw): return None
        def balance_sheet(self, *_a, **_kw): return None
        def margin_flow(self, *_a, **_kw): return pd.DataFrame()
        def fund_flow(self, *_a, **_kw): return pd.DataFrame()
        def news_headlines(self, *_a, **_kw): return []
        def macro(self, *_a, **_kw): return pd.DataFrame()
        def provenance(self, _key): return {}

    prof = build_profile(_NullGateway(), "600519.SH")
    expected = {"quote", "fundamentals", "balance_sheet",
                "margin", "fund_flow", "headlines", "macro"}
    assert set(prof.missing_capabilities) == expected
    assert prof.completeness == 0.0


def test_missing_capabilities_partial():
    """仅 quote 切片有值时，其余 6 项进入 missing_capabilities。"""
    from core.data_gateway.profile import build_profile

    class _PartialGateway:
        def __init__(self):
            self._last_provenance = {}

        def _get_profile_executor(self):
            from concurrent.futures import ThreadPoolExecutor
            return ThreadPoolExecutor(max_workers=2)

        def quote(self, *_a, **_kw):
            return Quote(symbol="600519.SH", price=1000.0)

        def fundamentals(self, *_a, **_kw): return None
        def balance_sheet(self, *_a, **_kw): return None
        def margin_flow(self, *_a, **_kw): return pd.DataFrame()
        def fund_flow(self, *_a, **_kw): return pd.DataFrame()
        def news_headlines(self, *_a, **_kw): return []
        def macro(self, *_a, **_kw): return pd.DataFrame()
        def provenance(self, _key): return {}

    prof = build_profile(_PartialGateway(), "600519.SH")
    assert "quote" not in prof.missing_capabilities
    assert "fundamentals" in prof.missing_capabilities
    assert prof.completeness == pytest.approx(1.0 / 7.0)

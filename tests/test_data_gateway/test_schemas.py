# -*- coding: utf-8 -*-
"""
schemas.py 单元测试 — 数据契约的默认值、is_valid、计算属性。
"""

from datetime import datetime

from core.data_gateway.schemas import (
    BalanceSheet,
    Fundamentals,
    MarketIndexSnapshot,
    NorthFlow,
    Quote,
    SectorConstituent,
    SectorRanking,
)


# ── Quote ────────────────────────────────────────────────────────────────────


def test_quote_defaults_invalid():
    q = Quote()
    assert q.is_valid is False
    assert q.price == 0.0
    assert isinstance(q.timestamp, datetime)


def test_quote_valid_when_price_positive():
    assert Quote(price=10.5).is_valid is True


def test_quote_day_change():
    q = Quote(price=11.0, prev_close=10.0)
    assert q.day_change == 1.0


def test_quote_field_superset():
    """确保 Quote 同时包含原 data_layer.Quote 和 QuoteData 的字段。"""
    fields = {f for f in Quote.__dataclass_fields__}
    required = {
        # data_layer.Quote
        "symbol", "price", "prev_close", "pct_change", "high", "low",
        "volume_ratio", "pe_ttm", "pb", "turnover_rate", "market_cap",
        "float_cap", "high_52w", "low_52w", "limit_up", "limit_down",
        "timestamp",
        # QuoteData
        "name", "code", "market", "open", "avg_price", "change", "volume",
        "amount", "bid1_price", "bid1_vol", "ask1_price", "ask1_vol",
        "dividend_yield", "amplitude", "currency",
    }
    missing = required - fields
    assert not missing, f"Quote 缺少字段: {missing}"


def test_quote_does_not_leak_source():
    """source / _field_sources / merge 必须不在 Quote 上(provenance 由旁路记录)。"""
    fields = set(Quote.__dataclass_fields__)
    assert "source" not in fields
    assert "_field_sources" not in fields
    assert not hasattr(Quote(), "merge")


# ── Fundamentals ─────────────────────────────────────────────────────────────


def test_fundamentals_defaults():
    f = Fundamentals()
    assert f.is_valid is False
    assert f.industry == ""


def test_fundamentals_valid_when_symbol_set():
    assert Fundamentals(symbol="sh600519").is_valid is True


def test_fundamentals_new_fields():
    """eps_yoy / asset_yoy / profit_yoy / revenue_yoy / dividend_yield 新增字段"""
    f = Fundamentals(
        symbol="sh600519",
        profit_yoy=-4.5,
        revenue_yoy=11.2,
        eps_yoy=-3.8,
        asset_yoy=1.6,
        industry="白酒",
    )
    assert f.is_valid is True
    assert f.profit_yoy == -4.5
    assert f.eps_yoy == -3.8
    assert f.asset_yoy == 1.6
    assert f.industry == "白酒"


# ── BalanceSheet ──────────────────────────────────────────────────────────────


def test_balance_sheet_defaults():
    b = BalanceSheet()
    assert b.is_valid is False
    assert b.total_asset == 0.0
    assert b.current_ratio == 0.0


def test_balance_sheet_valid_when_symbol_set():
    assert BalanceSheet(symbol="sh600519").is_valid is True


def test_balance_sheet_fields():
    b = BalanceSheet(
        symbol="sh600519",
        total_asset=250_000_000_000.0,
        total_liability=80_000_000_000.0,
        debt_to_equity=32.0,
        current_ratio=3.5,
        quick_ratio=2.8,
        equity=170_000_000_000.0,
    )
    assert b.is_valid is True
    assert b.total_asset == 250_000_000_000.0
    assert b.debt_to_equity == 32.0
    assert b.current_ratio == 3.5
    assert b.quick_ratio == 2.8


# ── SectorRanking / SectorConstituent ────────────────────────────────────────


def test_sector_ranking():
    s = SectorRanking(code="BK0716", name="华为汽车", change_pct=3.5)
    assert s.is_valid is True
    assert s.rank_perf == 0


def test_sector_constituent():
    c = SectorConstituent(symbol="sh600519", name="贵州茅台", price=1234.5)
    assert c.is_valid is True


# ── NorthFlow ────────────────────────────────────────────────────────────────


def test_north_flow_defaults():
    n = NorthFlow()
    assert n.direction == "NEUTRAL"
    assert n.stale is False


# ── MarketIndexSnapshot ──────────────────────────────────────────────────────


def test_market_index_snapshot():
    idx = MarketIndexSnapshot(code="VIX", name="VIX", price=18.5, change_pct=-1.2)
    assert idx.is_valid is True
    assert MarketIndexSnapshot().is_valid is False


# ── Capability 枚举 ───────────────────────────────────────────────────────────


def test_capability_balance_sheet_exists():
    """BALANCE_SHEET 是新的 capability 枚举成员"""
    from core.data_gateway.capabilities import Capability
    assert hasattr(Capability, "BALANCE_SHEET")
    assert Capability.BALANCE_SHEET.value == "balance_sheet"

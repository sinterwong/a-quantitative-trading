# -*- coding: utf-8 -*-
"""字段级矛盾检测 — divergence_pct 计算 + 阈值 WARNING。"""

from __future__ import annotations

import logging
from typing import Any, Dict

import pandas as pd
import pytest

from core.data_gateway.capabilities import Capability, RoutingStrategy
from core.data_gateway.gateway import DataGateway
from core.data_gateway.merge import (
    Candidate, DIVERGENCE_SUFFIX, _field_divergence, merge_field_level,
)
from core.data_gateway.schemas import Fundamentals, Quote


# ── _field_divergence ────────────────────────────────────────────────────────


def test_field_divergence_zero_when_identical():
    assert _field_divergence([10.0, 10.0]) == 0.0


def test_field_divergence_relative_for_numeric():
    # max=110, min=100, max_abs=110, (110-100)/110
    div = _field_divergence([100.0, 110.0])
    assert div == pytest.approx(10.0 / 110.0, rel=1e-9)


def test_field_divergence_all_zero_returns_zero():
    assert _field_divergence([0, 0.0]) == 0.0


def test_field_divergence_strings_binary():
    assert _field_divergence(["a", "b"]) == 1.0
    assert _field_divergence(["x", "x"]) == 0.0


# ── merge_field_level 在 provenance 中带 __divergence ────────────────────────


def test_merge_field_level_records_divergence_in_provenance():
    a = Quote(symbol="600519.SH", price=100.0)
    b = Quote(symbol="600519.SH", price=110.0)
    _obj, prov = merge_field_level([
        Candidate("A", a, health=0.9),
        Candidate("B", b, health=0.5),
    ])
    key = f"price{DIVERGENCE_SUFFIX}"
    assert key in prov
    assert float(prov[key]) == pytest.approx(10.0 / 110.0, abs=1e-3)


def test_merge_field_level_no_divergence_key_when_consistent():
    a = Quote(symbol="x", price=100.0)
    b = Quote(symbol="x", price=100.0)
    _obj, prov = merge_field_level([
        Candidate("A", a, health=0.9),
        Candidate("B", b, health=0.5),
    ])
    assert f"price{DIVERGENCE_SUFFIX}" not in prov


def test_merge_field_level_single_source_no_divergence():
    a = Quote(symbol="x", price=100.0)
    _obj, prov = merge_field_level([Candidate("A", a, health=0.9)])
    assert not any(k.endswith(DIVERGENCE_SUFFIX) for k in prov)


# ── DataGateway 阈值 + WARNING 日志 ──────────────────────────────────────────


class _DummyProvider:
    """最小化 provider 桩，返回固定结果给 _merged_fetch 走通。"""

    name = "dummy"

    def __init__(self, name: str, result: Any):
        self.name = name
        self._result = result

    def declare(self):  # pragma: no cover - DataGateway 在测试里不调
        raise NotImplementedError

    def field_authority(self):
        return {}


def _make_gw_with_warning_capture(caplog, monkeypatch, threshold: str = "0.05"):
    monkeypatch.setenv("TRADING_DIVERGENCE_THRESHOLD", threshold)
    caplog.set_level(logging.WARNING, logger="data_gateway.gateway")
    return DataGateway(enable_disk_cache=False)


def test_warn_divergences_logs_when_above_threshold(monkeypatch, caplog):
    gw = _make_gw_with_warning_capture(caplog, monkeypatch, threshold="0.05")
    prov: Dict[str, str] = {
        "price": "tencent",
        f"price{DIVERGENCE_SUFFIX}": "0.0909",  # 9% 差异
    }
    gw._warn_divergences(Capability.QUOTE, "fetch_quote", prov, "600519.SH")
    assert any("字段差异超阈值" in rec.message for rec in caplog.records)
    assert any("field=price" in rec.message for rec in caplog.records)


def test_warn_divergences_silent_within_threshold(monkeypatch, caplog):
    gw = _make_gw_with_warning_capture(caplog, monkeypatch, threshold="0.10")
    prov = {
        "price": "tencent",
        f"price{DIVERGENCE_SUFFIX}": "0.05",
    }
    gw._warn_divergences(Capability.QUOTE, "fetch_quote", prov, "600519.SH")
    assert not any("字段差异超阈值" in rec.message for rec in caplog.records)


def test_warn_divergences_threshold_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("TRADING_DIVERGENCE_THRESHOLD", "not-a-number")
    assert DataGateway._divergence_threshold() == 0.05


# ── _merged_history_fetch 列级差异 ──────────────────────────────────────────


def test_merged_history_records_column_divergence(monkeypatch):
    """模拟两个源对同一 (date, close) 给出不同价格，应记录 __divergence。"""
    monkeypatch.setenv("TRADING_DIVERGENCE_THRESHOLD", "0.99")  # 不让 WARNING 干扰

    gw = DataGateway(enable_disk_cache=False)

    idx = pd.to_datetime(["2026-01-01", "2026-01-02"])
    df_a = pd.DataFrame({"close": [10.0, 11.0]}, index=idx)
    df_b = pd.DataFrame({"close": [10.5, 11.0]}, index=idx)  # row0 差 5%

    class _StubProvider:
        def __init__(self, name: str, df: pd.DataFrame):
            self.name = name
            self._df = df

        def supports(self, *_a, **_kw):
            return True

        def declare(self):
            from core.data_gateway.capabilities import (
                Capability as Cap, Market, ProviderCapability,
            )
            return ProviderCapability(
                capabilities=frozenset({Cap.KLINE_DAILY}),
                markets=frozenset({Market.A, Market.HK, Market.US}),
                priority_hint=0.8,
            )

        def field_authority(self):
            return {}

        def fetch_kline_daily(self, *_a, **_kw):
            return self._df

    gw.register_provider(_StubProvider("A", df_a))
    gw.register_provider(_StubProvider("B", df_b))

    merged, prov = gw._merged_history_fetch(
        Capability.KLINE_DAILY, None, "fetch_kline_daily",
    )
    assert not merged.empty
    div_key = f"close{DIVERGENCE_SUFFIX}"
    assert div_key in prov
    # close 列 (10.0 vs 10.5) → (0.5 / 10.5) ≈ 0.0476
    assert float(prov[div_key]) == pytest.approx(0.5 / 10.5, rel=1e-3)

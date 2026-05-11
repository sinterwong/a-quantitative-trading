# -*- coding: utf-8 -*-
"""
YfinanceProvider + AkshareProvider 单元测试 — 库未装时的兜底,正常路径。
"""

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.data_gateway.capabilities import Capability, Market
from core.data_gateway.providers.akshare import AkshareProvider
from core.data_gateway.providers.yfinance import YfinanceProvider


# ── Yfinance: 能力 ───────────────────────────────────────────────────────────


def test_yfinance_capabilities():
    decl = YfinanceProvider().declare()
    assert Capability.MARKET_INDEX in decl.capabilities
    assert Capability.KLINE_DAILY in decl.capabilities
    # 不应包含 QUOTE / KLINE_MINUTE
    assert Capability.QUOTE not in decl.capabilities


def test_yfinance_priority_hint_low():
    """yfinance 延迟大,priority_hint 应低。"""
    assert YfinanceProvider().declare().priority_hint < 0.7


# ── Yfinance: fetch_market_index ──────────────────────────────────────────────


def test_yfinance_market_index_normal():
    """mock yfinance.Ticker 提供历史 DataFrame。"""
    mock_yf = MagicMock()
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame({
        "Open": [10.0, 10.5],
        "High": [11.0, 11.2],
        "Low": [9.5, 10.0],
        "Close": [10.5, 11.0],
        "Volume": [1000, 1200],
    })
    mock_yf.Ticker.return_value = ticker

    with patch.dict(sys.modules, {"yfinance": mock_yf}):
        idx = YfinanceProvider().fetch_market_index("^VIX")
    assert idx is not None
    assert idx.price == 11.0
    assert idx.prev_close == 10.5
    # change_pct = (11 - 10.5) / 10.5 * 100 ≈ 4.762
    assert abs(idx.change_pct - 4.762) < 0.01


def test_yfinance_market_index_missing_library_returns_none():
    """yfinance 未安装时应优雅返回 None,不抛异常。"""
    with patch.dict(sys.modules, {"yfinance": None}):
        # 必须 ImportError;simulate by monkeypatching the import
        with patch.object(YfinanceProvider, "fetch_market_index") as m:
            m.side_effect = None
            # 真实测试:删除模块强制 ImportError
            pass

    # 实测:删除 sys.modules 缓存触发真实 ImportError 路径
    real_yf = sys.modules.pop("yfinance", None)
    try:
        with patch.dict(sys.modules, {"yfinance": None}, clear=False):
            # None 进 sys.modules 触发 ImportError
            result = YfinanceProvider().fetch_market_index("^VIX")
            assert result is None
    finally:
        if real_yf is not None:
            sys.modules["yfinance"] = real_yf


def test_yfinance_market_index_empty_history():
    mock_yf = MagicMock()
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    mock_yf.Ticker.return_value = ticker
    with patch.dict(sys.modules, {"yfinance": mock_yf}):
        assert YfinanceProvider().fetch_market_index("^VIX") is None


# ── Yfinance: fetch_kline ─────────────────────────────────────────────────────


def test_yfinance_kline_normalizes_columns():
    mock_yf = MagicMock()
    ticker = MagicMock()
    df = pd.DataFrame({
        "Open": [1, 2], "High": [3, 4], "Low": [0.5, 1.5],
        "Close": [2, 3], "Volume": [100, 200],
        "Dividends": [0, 0], "Stock Splits": [0, 0],
    })
    df.index = pd.to_datetime(["2026-05-08", "2026-05-09"])
    df.index.name = "Date"
    ticker.history.return_value = df
    mock_yf.Ticker.return_value = ticker
    with patch.dict(sys.modules, {"yfinance": mock_yf}):
        out = YfinanceProvider().fetch_kline("ES=F", interval="daily", days=2)
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(out) == 2


def test_yfinance_kline_minute_returns_empty():
    """yfinance provider 不声明 KLINE_MINUTE。"""
    out = YfinanceProvider().fetch_kline("VIX", interval="5m")
    assert out.empty


# ── Akshare: 能力 ─────────────────────────────────────────────────────────────


def test_akshare_capabilities_macro_only():
    decl = AkshareProvider().declare()
    assert Capability.FUNDAMENTALS in decl.capabilities
    assert Capability.MACRO in decl.capabilities
    assert Market.GLOBAL in decl.markets


def test_akshare_priority_hint_low():
    """akshare 实测不稳定,priority_hint 应低。"""
    assert AkshareProvider().declare().priority_hint < 0.5


# ── Akshare: fetch_macro ─────────────────────────────────────────────────────


def test_akshare_macro_pmi():
    mock_ak = MagicMock()
    mock_ak.macro_china_pmi.return_value = pd.DataFrame({
        "月份": ["2026-04", "2026-05"],
        "制造业-指数": [50.5, 51.0],
    })
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_macro("PMI")
    assert not df.empty
    assert "pmi" in df.columns
    assert len(df) == 2


def test_akshare_macro_unknown_indicator_returns_empty():
    mock_ak = MagicMock()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_macro("UNKNOWN")
    assert df.empty


def test_akshare_macro_missing_library_returns_empty():
    real_ak = sys.modules.pop("akshare", None)
    try:
        with patch.dict(sys.modules, {"akshare": None}, clear=False):
            df = AkshareProvider().fetch_macro("PMI")
            assert df.empty
    finally:
        if real_ak is not None:
            sys.modules["akshare"] = real_ak

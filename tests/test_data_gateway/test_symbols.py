# -*- coding: utf-8 -*-
"""
symbols.py 单元测试 — 市场检测 + 代码格式归一化 + 容错转换。
"""

import math

import pytest

from core.data_gateway.capabilities import Market
from core.data_gateway.symbols import (
    detect_market,
    normalize_to_sina,
    normalize_to_tencent,
    safe_float,
    safe_int,
)


# ── safe_float / safe_int ─────────────────────────────────────────────────────


@pytest.mark.parametrize("val,expected", [
    (None, 0.0),
    ("", 0.0),
    ("-", 0.0),
    ("--", 0.0),
    ("3.14", 3.14),
    (3.14, 3.14),
    ("abc", 0.0),
    (float("nan"), 0.0),
])
def test_safe_float(val, expected):
    out = safe_float(val)
    assert out == expected and not math.isnan(out)


def test_safe_float_custom_default():
    assert safe_float(None, default=99.0) == 99.0


@pytest.mark.parametrize("val,expected", [
    ("12", 12),
    ("12.7", 12),
    (None, 0),
    ("", 0),
])
def test_safe_int(val, expected):
    assert safe_int(val) == expected


# ── detect_market ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    ("sh600519", Market.A),
    ("sz000001", Market.A),
    ("600519.SH", Market.A),
    ("000001.SZ", Market.A),
    ("sh000001", Market.INDEX),
    ("sz399006", Market.INDEX),
    ("000001.SH", Market.INDEX),
    ("399006.SZ", Market.INDEX),
    ("hk00700", Market.HK),
    ("HK:00700", Market.HK),
    ("00700.HK", Market.HK),
    ("usAAPL", Market.US),
    ("US:AAPL", Market.US),
    ("AAPL", Market.US),
    ("600519", Market.A),
    ("000001", Market.INDEX),  # 000xxx 视为指数
])
def test_detect_market(symbol, expected):
    assert detect_market(symbol) == expected


# ── normalize_to_sina ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("inp,expected", [
    ("600519.SH", "sh600519"),
    ("000001.SZ", "sz000001"),
    ("HK:00700", "hk00700"),
    ("HK:700", "hk00700"),       # 不足 5 位补零
    ("00700.HK", "hk00700"),
    ("US:AAPL", "gb_aapl"),
    ("sh600519", "sh600519"),
    ("AAPL", "gb_aapl"),
    ("600519", "sh600519"),
    ("000001", "sz000001"),
    ("510300", "sh510300"),     # 51 开头(ETF)归沪
    ("588000", "sh588000"),     # 58 开头(科创板)归沪
    ("680000", "sh680000"),     # 68 开头(科创板)归沪
])
def test_normalize_to_sina(inp, expected):
    assert normalize_to_sina(inp) == expected


# ── normalize_to_tencent ──────────────────────────────────────────────────────


@pytest.mark.parametrize("inp,expected", [
    ("600519.SH", "sh600519"),
    ("000001.SZ", "sz000001"),
    ("HK:00700", "hk00700"),
    ("00700.HK", "hk00700"),
    ("US:AAPL", "usAAPL"),
    ("usAAPL", "usAAPL"),       # 已是腾讯格式,保留大小写
    ("AAPL", "usAAPL"),
    ("sh600519", "sh600519"),
])
def test_normalize_to_tencent(inp, expected):
    assert normalize_to_tencent(inp) == expected

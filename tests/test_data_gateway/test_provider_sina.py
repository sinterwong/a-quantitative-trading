# -*- coding: utf-8 -*-
"""
SinaProvider 单元测试 — A 股 / 港股解析,K 线,能力声明。
"""

from unittest.mock import MagicMock

import pytest

from core.data_gateway.capabilities import Capability, Market
from core.data_gateway.http import HttpError
from core.data_gateway.providers.base import ProviderError
from core.data_gateway.providers.sina import SinaProvider, _parse_a_share, _parse_hk


# ── 样本构造 ─────────────────────────────────────────────────────────────────


def _build_a_share(symbol="sh600519"):
    """新浪 A 股 34 字段。"""
    f = [""] * 34
    f[0] = "贵州茅台"
    f[1] = "1232.00"      # open
    f[2] = "1230.00"      # prev_close
    f[3] = "1234.50"      # price
    f[4] = "1240.00"      # high
    f[5] = "1228.00"      # low
    f[8] = "1234500"      # volume
    f[9] = "152234500"    # amount
    f[11] = "1234.30"     # bid1_price
    f[12] = "20"          # bid1_vol
    f[21] = "1234.70"     # ask1_price
    f[22] = "200"         # ask1_vol
    f[30] = "2026-05-08"  # date
    f[31] = "15:30:00"    # time
    return f'var hq_str_{symbol}="' + ",".join(f) + '";'


def _build_hk(symbol="hk00700"):
    """新浪港股 19 字段。"""
    f = [""] * 19
    f[0] = "TENCENT"      # name_en
    f[1] = "腾讯控股"      # name_cn
    f[2] = "300.00"       # open
    f[3] = "298.00"       # prev_close
    f[4] = "305.00"       # high
    f[5] = "297.00"       # low
    f[6] = "302.00"       # price
    f[7] = "4.00"         # change
    f[8] = "1.34"         # pct_change
    f[9] = "301.90"       # bid1_price
    f[10] = "5000"        # bid1_vol
    f[11] = "12345000"    # volume
    f[12] = "3712890000"  # amount
    f[13] = "488.00"      # high_52w
    f[14] = "260.00"      # low_52w
    f[15] = "2780.5"      # market_cap
    f[17] = "2026/05/08"  # date
    f[18] = "16:08"       # time
    return f'var hq_str_{symbol}="' + ",".join(f) + '";'


# ── 解析: A 股 ─────────────────────────────────────────────────────────────────


def test_parse_a_share():
    q = _parse_a_share("sh600519", _build_a_share())
    assert q is not None
    assert q.symbol == "sh600519"
    assert q.name == "贵州茅台"
    assert q.code == "600519"
    assert q.market == "A"
    assert q.price == 1234.50
    assert q.prev_close == 1230.00
    assert q.high == 1240.00
    assert q.low == 1228.00
    assert q.volume == 1234500
    assert q.amount == 152234500
    assert q.bid1_price == 1234.30
    assert q.ask1_price == 1234.70
    # 涨跌幅计算: (1234.5 - 1230) / 1230 * 100 ≈ 0.3659
    assert abs(q.pct_change - 0.3659) < 0.01
    assert q.currency == "CNY"


def test_parse_a_share_invalid_short_returns_none():
    assert _parse_a_share("sh600519", 'var hq_str_sh600519="a,b,c";') is None


def test_parse_a_share_zero_price_returns_none():
    """price=0 视为不合法。"""
    f = ["x"] * 34
    f[3] = "0.00"
    f[30] = "2026-05-08"
    f[31] = "15:30:00"
    bad = f'var hq_str_sh600519="' + ",".join(f) + '";'
    assert _parse_a_share("sh600519", bad) is None


# ── 解析: 港股 ─────────────────────────────────────────────────────────────────


def test_parse_hk():
    q = _parse_hk("hk00700", _build_hk())
    assert q is not None
    assert q.symbol == "hk00700"
    assert q.name == "腾讯控股"
    assert q.market == "HK"
    assert q.price == 302.00
    assert q.high_52w == 488.00
    assert q.low_52w == 260.00
    assert q.market_cap == 2780.5
    assert q.currency == "HKD"


def test_parse_hk_falls_back_to_english_name():
    text = _build_hk().replace(",腾讯控股,", ",,")
    q = _parse_hk("hk00700", text)
    assert q is not None
    assert q.name == "TENCENT"


# ── 能力声明 ─────────────────────────────────────────────────────────────────


def test_capabilities():
    decl = SinaProvider().declare()
    assert Capability.QUOTE in decl.capabilities
    assert Capability.KLINE_DAILY in decl.capabilities


def test_supports_hk_kline_disabled():
    """新浪港股 K 线常返回 null,supports() 应当返回 False。"""
    p = SinaProvider()
    assert p.supports(Capability.KLINE_DAILY, Market.HK) is False
    assert p.supports(Capability.KLINE_MINUTE, Market.HK) is False
    assert p.supports(Capability.KLINE_DAILY, Market.A) is True


def test_field_authority_declares_quote_depth():
    """新浪 5 档买卖盘比腾讯权威。"""
    auth = SinaProvider().field_authority()
    qa = auth[Capability.QUOTE]
    assert qa["bid1_price"] > 1.0
    assert qa["ask1_price"] > 1.0


# ── fetch_quote: mock ─────────────────────────────────────────────────────────


def test_fetch_quote_a_share():
    http = MagicMock()
    http.get_text.return_value = _build_a_share()
    p = SinaProvider(http=http)
    q = p.fetch_quote("sh600519")
    assert q is not None
    assert q.price == 1234.50


def test_fetch_quote_hk_routes_to_hk_parser():
    http = MagicMock()
    http.get_text.return_value = _build_hk()
    p = SinaProvider(http=http)
    q = p.fetch_quote("hk00700")
    assert q is not None
    assert q.market == "HK"


def test_fetch_quote_http_error_wraps():
    http = MagicMock()
    http.get_text.side_effect = HttpError("conn reset")
    p = SinaProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_quote("sh600519")


# ── fetch_quotes 批量 ───────────────────────────────────────────────────────


def test_fetch_quotes_groups_by_market():
    """A 股 + 港股 应分两个请求。"""
    http = MagicMock()
    http.get_text.side_effect = [_build_a_share(), _build_hk()]
    p = SinaProvider(http=http)
    out = p.fetch_quotes(["sh600519", "hk00700"])
    assert http.get_text.call_count == 2
    assert "sh600519" in out
    assert "hk00700" in out


# ── fetch_kline ──────────────────────────────────────────────────────────────


def test_fetch_kline_null_returns_empty():
    http = MagicMock()
    http.get_text.return_value = "null"
    p = SinaProvider(http=http)
    df = p.fetch_kline_daily("sh600519")
    assert df.empty


def test_fetch_kline_parses_json():
    http = MagicMock()
    http.get_text.return_value = (
        '[{"day":"2026-05-08","open":"1232","high":"1240",'
        '"low":"1228","close":"1234.5","volume":"12345000"}]'
    )
    p = SinaProvider(http=http)
    df = p.fetch_kline_daily("sh600519")
    assert len(df) == 1
    assert "date" in df.columns
    assert df["close"].iloc[0] == 1234.5


def test_fetch_kline_unknown_interval_returns_empty():
    http = MagicMock()
    p = SinaProvider(http=http)
    df = p.fetch_kline_minute("sh600519", interval="bogus")
    assert df.empty
    # 未发请求
    assert http.get_text.call_count == 0

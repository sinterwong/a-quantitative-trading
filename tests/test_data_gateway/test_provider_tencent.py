# -*- coding: utf-8 -*-
"""
TencentProvider 单元测试 — 解析 / 字段映射 / 路由 / mock HttpClient。
"""

from unittest.mock import MagicMock

import pytest

from core.data_gateway.capabilities import Capability, Market
from core.data_gateway.providers.tencent import (
    TencentProvider,
    parse_quote_line,
    parse_quotes_text,
)


# ── 解析: A 股 ─────────────────────────────────────────────────────────────────

# 按 _COMMON / _A_EXTRA 字段位置精确构造的 A 股样本(88 字段)
def _build_a_share_sample(symbol="sh600519") -> str:
    f = [""] * 88
    # _COMMON
    f[0] = "1"
    f[1] = "贵州茅台"     # name
    f[2] = "600519"        # code
    f[3] = "1234.50"       # price
    f[4] = "1230.00"       # prev_close
    f[5] = "1232.00"       # open
    f[6] = "12345"         # volume(lots)
    f[9] = "1234.30"       # bid1_price
    f[10] = "20"           # bid1_vol
    f[19] = "1234.70"      # ask1_price
    f[20] = "200"          # ask1_vol
    f[30] = "20260508153000"  # timestamp
    f[31] = "4.50"         # change
    f[32] = "0.37"         # pct_change
    f[33] = "1240.00"      # high
    f[34] = "1228.00"      # low
    # _A_EXTRA
    f[36] = "12345"        # volume_lots
    f[37] = "1521.5"       # amount_wan
    f[38] = "0.10"         # turnover_rate
    f[39] = "21.5"         # pe_ttm
    f[43] = "1.0"          # amplitude
    f[44] = "1.45"         # float_cap
    f[45] = "1.55"         # market_cap
    f[46] = "1.23"         # pb
    f[47] = "1888.00"      # limit_up
    f[48] = "1100.00"      # limit_down
    f[49] = "10.5"         # volume_ratio
    f[51] = "1234.10"      # avg_price
    f[56] = "0.50"         # dividend_yield
    f[57] = "1522.34"      # amount_wan2(更精确)
    f[67] = "1600.00"      # high_52w
    f[68] = "800.00"       # low_52w
    f[82] = "CNY"
    return f'v_{symbol}="' + "~".join(f) + '";'


_A_SHARE_SAMPLE = _build_a_share_sample()


def test_parse_a_share_quote():
    q = parse_quote_line("sh600519", _A_SHARE_SAMPLE)
    assert q is not None
    assert q.symbol == "sh600519"
    assert q.name == "贵州茅台"
    assert q.code == "600519"
    assert q.market == "A"
    assert q.price == 1234.50
    assert q.prev_close == 1230.00
    assert q.high == 1240.00
    assert q.low == 1228.00
    # A 股 volume 单位转换:手 → 股 (×100)
    assert q.volume == 12345 * 100
    # 88-field 字段
    assert q.pe_ttm == 21.5
    assert q.pb == 1.23
    assert q.market_cap == 1.55
    assert q.float_cap == 1.45
    assert q.volume_ratio == 10.5
    assert q.limit_up == 1888.00
    assert q.limit_down == 1100.00
    assert q.high_52w == 1600.00
    assert q.low_52w == 800.00
    assert q.currency == "CNY"
    assert q.change == 4.50
    assert q.pct_change == 0.37
    assert q.is_valid


def test_parse_invalid_short_line():
    """少于 30 字段的响应应当返回 None。"""
    assert parse_quote_line("sh600519", 'v_sh600519="1~name~code";') is None


def test_parse_empty_returns_none():
    assert parse_quote_line("sh600519", "") is None


def test_parse_invalid_price_returns_none():
    """price=0 视为无效。"""
    fake = ('v_sh000000="1~未知~000000~0.00~0.00~0.00~0~0~0~0~0~0~0~0~0~0~'
            + '0~0~0~0~0~0~0~0~0~0~0~0~0~0~~0~0~0~0~~~~~~~~~~~~~~~~~~~~~~~~'
            + '~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~CNY";')
    assert parse_quote_line("sh000000", fake) is None


# ── 批量解析 ─────────────────────────────────────────────────────────────────


def test_parse_quotes_text_aligns_by_position():
    text = _A_SHARE_SAMPLE + "\n" + _build_a_share_sample("sh600518")
    quotes = parse_quotes_text(text, ["sh600519", "sh600518"])
    assert set(quotes) == {"sh600519", "sh600518"}


def test_parse_quotes_text_extra_symbols_ignored():
    text = _A_SHARE_SAMPLE  # 仅一行
    quotes = parse_quotes_text(text, ["sh600519", "sh600518"])
    assert "sh600519" in quotes
    assert "sh600518" not in quotes


# ── 能力声明 ─────────────────────────────────────────────────────────────────


def test_capabilities_declaration():
    decl = TencentProvider().declare()
    assert Capability.QUOTE in decl.capabilities
    assert Capability.KLINE_DAILY in decl.capabilities
    assert Capability.KLINE_MINUTE in decl.capabilities
    assert Capability.MARKET_INDEX in decl.capabilities


def test_supports_minute_only_hk():
    p = TencentProvider()
    assert p.supports(Capability.KLINE_MINUTE, Market.HK) is True
    assert p.supports(Capability.KLINE_MINUTE, Market.A) is False
    assert p.supports(Capability.KLINE_MINUTE, Market.US) is False
    assert p.supports(Capability.KLINE_DAILY, Market.A) is True


def test_supports_us_kline_daily_excluded():
    """腾讯美股日K只返回1条历史数据（接口限制），应排除，走yfinance。"""
    p = TencentProvider()
    assert p.supports(Capability.KLINE_DAILY, Market.US) is False
    assert p.supports(Capability.KLINE_DAILY, Market.HK) is True
    assert p.supports(Capability.KLINE_DAILY, Market.A) is True
    # QUOTE 对 US 仍然支持（腾讯美股行情有数据）
    assert p.supports(Capability.QUOTE, Market.US) is True


def test_field_authority_declares_88_fields():
    """腾讯独家的 88-field 字段应当声明高权威(> 1.0)。"""
    auth = TencentProvider().field_authority()
    assert Capability.QUOTE in auth
    qa = auth[Capability.QUOTE]
    for f in ("pe_ttm", "pb", "market_cap", "high_52w"):
        assert qa[f] > 1.0, f"{f} 未声明高权威"


# ── fetch_quote: mock HttpClient ─────────────────────────────────────────────


def test_fetch_quote_uses_http_client():
    http = MagicMock()
    http.get_text.return_value = _A_SHARE_SAMPLE
    p = TencentProvider(http=http)
    q = p.fetch_quote("sh600519")
    assert q is not None
    assert q.name == "贵州茅台"
    # URL 应包含归一化后的代码
    call_url = http.get_text.call_args.args[0]
    assert "sh600519" in call_url


def test_fetch_quote_normalizes_symbol():
    """传入 600519.SH 时应自动归一为 sh600519。"""
    http = MagicMock()
    http.get_text.return_value = _A_SHARE_SAMPLE
    p = TencentProvider(http=http)
    p.fetch_quote("600519.SH")
    assert "sh600519" in http.get_text.call_args.args[0]


def test_fetch_quote_http_error_raises_provider_error():
    """HttpError 应包装为 ProviderError(gateway 据此触发健康度记录)。"""
    from core.data_gateway.http import HttpError
    from core.data_gateway.providers.base import ProviderError

    http = MagicMock()
    http.get_text.side_effect = HttpError("network fail", retriable=True)
    p = TencentProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_quote("sh600519")


# ── fetch_quotes 批量 ───────────────────────────────────────────────────────


def test_fetch_quotes_batches_within_limit():
    from core.data_gateway.providers.tencent import BATCH_LIMIT
    http = MagicMock()
    http.get_text.return_value = ""  # 空响应,只关心调用次数
    p = TencentProvider(http=http)

    syms = [f"sh{600000 + i}" for i in range(BATCH_LIMIT * 2 + 5)]
    p.fetch_quotes(syms)
    # 应被分成 3 批
    assert http.get_text.call_count == 3


def test_fetch_quotes_empty_returns_empty():
    p = TencentProvider(http=MagicMock())
    assert p.fetch_quotes([]) == {}


# ── fetch_kline: 路由 ────────────────────────────────────────────────────────


def test_fetch_kline_minute_returns_empty_for_a_share():
    """腾讯 A 股分钟 K 不支持 → 直接返回空,不发请求。"""
    http = MagicMock()
    p = TencentProvider(http=http)
    df = p.fetch_kline_minute("sh600519", interval="5m")
    assert df.empty
    # 不应调用 HTTP
    assert http.get_text.call_count == 0


def test_fetch_kline_unknown_interval_returns_empty():
    http = MagicMock()
    p = TencentProvider(http=http)
    assert p.fetch_kline_minute("sh600519", interval="garbage").empty
    assert http.get_text.call_count == 0


def test_fetch_kline_daily_parses_response():
    sample = (
        'k={"code":0,"data":{"sh600519":{"qfqday":[' +
        '["2026-05-08",1234.5,1240.0,1245.0,1230.0,12345.0],' +
        '["2026-05-09",1240.0,1245.0,1250.0,1238.0,15000.0]]}}};'
    )
    http = MagicMock()
    http.get_text.return_value = sample
    p = TencentProvider(http=http)
    df = p.fetch_kline_daily("sh600519", days=2)
    assert len(df) == 2
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 1245.0


# ── fetch_market_index ──────────────────────────────────────────────────────


def test_fetch_market_index_via_quote():
    """market_index 复用 fetch_quote 路径(usSPY/hkHSI 等)。"""
    http = MagicMock()
    http.get_text.return_value = _A_SHARE_SAMPLE
    p = TencentProvider(http=http)
    idx = p.fetch_market_index("sh600519")
    assert idx is not None
    assert idx.price == 1234.50
    assert idx.change_pct == 0.37

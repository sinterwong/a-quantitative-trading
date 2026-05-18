# -*- coding: utf-8 -*-
"""
EastmoneyProvider 单元测试 — 板块排名 / 成分股 / 北向资金。
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.data_gateway.capabilities import Capability
from core.data_gateway.http import HttpError
from core.data_gateway.providers.base import ProviderError
from core.data_gateway.providers.eastmoney import EastmoneyProvider


def _wrap_jsonp(payload: dict) -> str:
    return f"jQuery({json.dumps(payload)});"


# ── 能力声明 ─────────────────────────────────────────────────────────────────


def test_capabilities():
    decl = EastmoneyProvider().declare()
    assert Capability.QUOTE in decl.capabilities
    assert Capability.MARKET_INDEX in decl.capabilities
    assert Capability.SECTOR_RANKING in decl.capabilities
    assert Capability.SECTOR_CONSTITUENTS in decl.capabilities
    assert Capability.NORTH_FLOW in decl.capabilities
    # ulist.np 实测稳定，priority_hint 已上调
    assert decl.priority_hint == 0.70


# ── fetch_sectors ────────────────────────────────────────────────────────────


def test_fetch_sectors_parses_jsonp():
    # fetch_sectors 优先走 _request_em（subprocess curl），直接 mock 该方法返回解析后的 dict
    payload = {"data": {"diff": [
        {"f12": "BK0716", "f14": "华为汽车", "f3": 3.45,
         "f62": 1.5e8, "f20": 2.3e10},
        {"f12": "BK0801", "f14": "白酒", "f3": 1.20,
         "f62": -5.0e7, "f20": 1.0e10},
    ]}}
    from unittest.mock import patch
    p = EastmoneyProvider()
    with patch.object(p, "_request_em", return_value=payload):
        out = p.fetch_sectors(limit=10)
    assert len(out) == 2
    assert out[0].code == "EM_BK0716"
    assert out[0].name == "华为汽车"
    assert out[0].change_pct == 3.45
    assert out[0].rank_perf == 1
    # rank_flow 按 net_flow 降序: BK0716 > BK0801
    assert out[0].rank_flow == 1
    assert out[1].rank_flow == 2


def test_fetch_sectors_empty_data():
    # EastmoneyProvider.fetch_sectors 在两个请求路径都失败时 raise ProviderError
    p = EastmoneyProvider()
    with patch.object(p, "_request_em", return_value=None):
        with patch.object(p, "_request", return_value=None):
            with pytest.raises(ProviderError):
                p.fetch_sectors()


def test_fetch_sectors_http_error_wraps():
    # EastmoneyProvider.fetch_sectors 将 ProviderError 透传
    p = EastmoneyProvider()
    with patch.object(p, "_request_em", side_effect=ProviderError("conn reset")):
        with patch.object(p, "_request", side_effect=ProviderError("conn reset")):
            with pytest.raises(ProviderError):
                p.fetch_sectors()


def test_fetch_sectors_limit_applied():
    payload = {"data": {"diff": [
        {"f12": f"BK{i:04d}", "f14": f"sec{i}", "f3": 1.0, "f62": 1.0, "f20": 1.0}
        for i in range(20)
    ]}}
    p = EastmoneyProvider()
    with patch.object(p, "_request_em", return_value=payload):
        out = p.fetch_sectors(limit=5)
    assert len(out) == 5


# ── fetch_sector_constituents ────────────────────────────────────────────────


def test_fetch_sector_constituents_parses():
    payload = {"data": {"diff": [
        {"f12": "600519", "f14": "贵州茅台", "f2": 1234.5, "f3": 2.0,
         "f20": 1e10, "f6": 1e6},
        {"f12": "000858", "f14": "五粮液", "f2": 168.5, "f3": -1.5,
         "f20": 5e9, "f6": 5e5},
    ]}}
    http = MagicMock()
    http.get_text.return_value = _wrap_jsonp(payload)
    p = EastmoneyProvider(http=http)
    out = p.fetch_sector_constituents("BK0716")
    assert len(out) == 2
    # 按涨跌幅降序: 茅台 > 五粮液
    assert out[0].name == "贵州茅台"
    assert out[0].symbol == "sh600519"
    assert out[1].symbol == "sz000858"


def test_fetch_sector_constituents_strips_em_prefix():
    """传入 EM_BK0716 时应剥离前缀,fs 参数应为 b:BK0716。"""
    http = MagicMock()
    http.get_text.return_value = _wrap_jsonp({"data": {"diff": []}})
    p = EastmoneyProvider(http=http)
    p.fetch_sector_constituents("EM_BK0716")
    call_params = http.get_text.call_args.kwargs["params"]
    assert call_params["fs"] == "b:BK0716"


def test_fetch_sector_constituents_strips_sina_prefix():
    http = MagicMock()
    http.get_text.return_value = _wrap_jsonp({"data": {"diff": []}})
    p = EastmoneyProvider(http=http)
    p.fetch_sector_constituents("SINA_GNhwqc")
    call_params = http.get_text.call_args.kwargs["params"]
    assert call_params["fs"] == "b:GNhwqc"


# ── fetch_north_flow ─────────────────────────────────────────────────────────


def test_fetch_north_flow_realtime():
    """实时端点成功 → 直接用。"""
    realtime_payload = {"data": {
        "n2s": ["10:00,foo,bar,1.5,baz,2000000000"],  # cum_amount = 20亿
        "s2n": ["10:00,foo,bar,0.5,baz,1000000000"],  # cum_amount = 10亿
    }}
    http = MagicMock()
    http.get_json.return_value = realtime_payload
    p = EastmoneyProvider(http=http)
    nf = p.fetch_north_flow()
    assert nf is not None
    # net = 20 - 10 = 10亿
    assert abs(nf.net_north_yi - 10.0) < 0.01
    assert nf.direction == "BUY"


def test_fetch_north_flow_falls_back_to_daily():
    """实时返回 None → 用日总结。"""
    daily_payload = {"data": {
        # dayNetAmtIn 单位 万元 → /10000 → 亿元
        "hk2sh": {"dayNetAmtIn": 50000},   # 5 亿
        "sh2hk": {"dayNetAmtIn": -10000},  # -1 亿
    }}
    http = MagicMock()
    http.get_json.side_effect = [
        {"data": None},   # realtime 空
        daily_payload,    # daily 兜底
    ]
    p = EastmoneyProvider(http=http)
    nf = p.fetch_north_flow()
    assert nf is not None
    assert nf.net_north_yi == 5.0
    assert nf.net_south_yi == -1.0


def test_fetch_north_flow_both_fail_raises():
    http = MagicMock()
    http.get_json.side_effect = [
        {"data": None},
        {"data": None},
    ]
    p = EastmoneyProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_north_flow()


# ── fetch_news_headlines ────────────────────────────────────────────────────


def _kuaixun_payload(lives: list) -> str:
    """模拟 EastMoney kuaixun 接口的 `var ajaxResult={...};` 回包。"""
    return f"var ajaxResult={json.dumps({'LivesList': lives})};"


def test_capability_includes_news_headlines():
    decl = EastmoneyProvider().declare()
    assert Capability.NEWS_HEADLINES in decl.capabilities


def test_supports_news_headlines_ignores_market():
    """NEWS_HEADLINES 是全市场快讯，任何 market 都应返回 True。"""
    from core.data_gateway.capabilities import Market

    p = EastmoneyProvider()
    assert p.supports(Capability.NEWS_HEADLINES, Market.GLOBAL)
    assert p.supports(Capability.NEWS_HEADLINES, Market.A)
    assert p.supports(Capability.NEWS_HEADLINES, Market.US)


def test_fetch_news_headlines_parses_titles():
    http = MagicMock()
    http.get_text.return_value = _kuaixun_payload([
        {"title": "央行：保持流动性合理充裕", "content": "正文...",
         "showtime": "2026-05-18 21:40:43"},
        {"title": "证监会重拳整治财务造假", "content": "",
         "showtime": "2026-05-18 21:30:00"},
    ])
    p = EastmoneyProvider(http=http)
    items = p.fetch_news_headlines("600519.SH", n=5)
    assert len(items) == 2
    assert "央行" in items[0].title
    assert items[0].source == "eastmoney"
    # G5：timestamp 从 showtime 解析
    assert items[0].timestamp.year == 2026
    assert items[0].timestamp.hour == 21 and items[0].timestamp.minute == 40
    # 验证 HttpClient 注入路径，URL 走 newsapi.eastmoney.com
    call_args = http.get_text.call_args
    assert "newsapi.eastmoney.com" in call_args[0][0]
    assert "getlist_102" in call_args[0][0]


def test_fetch_news_headlines_falls_back_to_content_when_title_missing():
    http = MagicMock()
    long_content = "国务院常务会议研究" + "X" * 100   # >60 字会被截断
    short_content = "短消息：A 股开盘红"            # <60 字保留原文
    http.get_text.return_value = _kuaixun_payload([
        {"title": "", "content": long_content, "showtime": "2026-05-18 10:00:00"},
        {"title": None, "content": short_content},   # showtime 缺失
    ])
    p = EastmoneyProvider(http=http)
    items = p.fetch_news_headlines("000001.SZ", n=5)
    assert len(items) == 2
    assert items[0].title.endswith("...")
    assert len(items[0].title) == 60
    assert items[1].title == short_content
    # showtime 缺失 → timestamp 为 None，gateway 合并时会排在最后
    assert items[1].timestamp is None


def test_fetch_news_headlines_respects_n_limit():
    http = MagicMock()
    http.get_text.return_value = _kuaixun_payload([
        {"title": f"标题{i}", "content": ""} for i in range(20)
    ])
    p = EastmoneyProvider(http=http)
    items = p.fetch_news_headlines("anysymbol", n=3)
    assert len(items) == 3


def test_fetch_news_headlines_http_error_raises_provider_error():
    http = MagicMock()
    http.get_text.side_effect = HttpError("connect timeout")
    p = EastmoneyProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_news_headlines("600519.SH")


def test_fetch_news_headlines_bad_json_raises_provider_error():
    http = MagicMock()
    http.get_text.return_value = "not a json blob at all"
    p = EastmoneyProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_news_headlines("600519.SH")


def test_fetch_news_headlines_empty_lives_returns_empty_list():
    http = MagicMock()
    http.get_text.return_value = _kuaixun_payload([])
    p = EastmoneyProvider(http=http)
    items = p.fetch_news_headlines("600519.SH")
    assert items == []


def test_fetch_news_headlines_symbol_is_ignored():
    """不同 symbol 得到同一份全市场快讯（说明 symbol 参数被忽略）。"""
    http = MagicMock()
    http.get_text.return_value = _kuaixun_payload([
        {"title": "全市场新闻 X", "content": ""}
    ])
    p = EastmoneyProvider(http=http)
    items_1 = p.fetch_news_headlines("600519.SH", n=5)
    items_2 = p.fetch_news_headlines("000001.SZ", n=5)
    assert [it.title for it in items_1] == [it.title for it in items_2]

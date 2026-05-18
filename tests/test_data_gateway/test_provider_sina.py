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


def test_supports_index_kline_disabled():
    """新浪 normalize_to_sina 对上交所指数代码(000xxx)错误归一为深证路径，
    导致 K 线返回 null；腾讯已全覆盖 INDEX K-line，新浪应排除。
    """
    p = SinaProvider()
    assert p.supports(Capability.KLINE_DAILY, Market.INDEX) is False
    assert p.supports(Capability.KLINE_MINUTE, Market.INDEX) is False
    # QUOTE / MARKET_INDEX 对 INDEX 仍支持（新浪有指数快照接口）
    assert p.supports(Capability.QUOTE, Market.INDEX) is True
    assert p.supports(Capability.MARKET_INDEX, Market.INDEX) is True


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


# ── fetch_sector_constituents ───────────────────────────────────────────────


def _build_constituents_payload(n=3):
    """Sina Market_Center.getHQNodeData 返回的 JSON 数组样本。"""
    import json as _json
    records = []
    base_price = 100.0
    for i in range(n):
        records.append({
            "symbol": f"sh60000{i}",
            "name": f"个股{i}",
            "trade": f"{base_price + i:.2f}",
            "changepercent": f"{5.0 - i:.3f}",  # 递减,验证 server 已排序
            "amount": f"{1_000_000 * (i + 1)}",
            "volume": f"{10_000 * (i + 1)}",
        })
    return _json.dumps(records)


def test_capabilities_includes_sector_constituents():
    """SECTOR_CONSTITUENTS 需在 declare 中显式声明,网关才会路由。"""
    decl = SinaProvider().declare()
    assert Capability.SECTOR_CONSTITUENTS in decl.capabilities


def test_supports_sector_constituents_global():
    """SECTOR_CONSTITUENTS 走 Market.GLOBAL 路由(板块代码与 A 股市场无关)。"""
    p = SinaProvider()
    assert p.supports(Capability.SECTOR_CONSTITUENTS, Market.GLOBAL) is True


def test_supports_sector_ranking_not_globalized():
    """SECTOR_RANKING 不应被错误地放行到 GLOBAL — 网关用 Market.A 路由它。

    防止 supports() 越权扩面回潮:declare/supports 的分工是"声明能力 +
    收窄场景",未被网关路由的组合不应在 supports() 中返回 True。
    """
    p = SinaProvider()
    assert p.supports(Capability.SECTOR_RANKING, Market.GLOBAL) is False
    # Market.A 路径仍走默认 (declare ∩ markets) 逻辑
    assert p.supports(Capability.SECTOR_RANKING, Market.A) is True


def test_fetch_sector_constituents_parses():
    http = MagicMock()
    http.get_text.return_value = _build_constituents_payload(n=3)
    p = SinaProvider(http=http)
    out = p.fetch_sector_constituents("SINA_new_qcwl")
    assert len(out) == 3
    assert out[0].symbol == "sh600000"
    assert out[0].name == "个股0"
    assert out[0].price == 100.00
    assert out[0].change_pct == 5.0
    assert out[0].amount == 1_000_000.0
    assert out[0].volume == 10_000.0
    # 服务端已按涨幅降序;解析保持原顺序
    assert out[0].change_pct > out[-1].change_pct


def test_fetch_sector_constituents_strips_sina_prefix():
    """SINA_ 前缀须剥离后作为新浪 node 参数。"""
    http = MagicMock()
    http.get_text.return_value = "[]"
    p = SinaProvider(http=http)
    p.fetch_sector_constituents("SINA_new_qcwl")
    _, kwargs = http.get_text.call_args
    assert kwargs["params"]["node"] == "new_qcwl"


def test_fetch_sector_constituents_strips_em_prefix():
    """EM_ 前缀须剥离 — 与 eastmoney 行为对称,避免上游误把 EM_ 板块码
    透传给新浪 node 参数(会被静默忽略,得到空结果)。"""
    http = MagicMock()
    http.get_text.return_value = "[]"
    p = SinaProvider(http=http)
    p.fetch_sector_constituents("EM_BK0716")
    _, kwargs = http.get_text.call_args
    assert kwargs["params"]["node"] == "BK0716"


def test_fetch_sector_constituents_no_prefix_passthrough():
    http = MagicMock()
    http.get_text.return_value = "[]"
    p = SinaProvider(http=http)
    p.fetch_sector_constituents("new_qcwl")
    _, kwargs = http.get_text.call_args
    assert kwargs["params"]["node"] == "new_qcwl"


def test_fetch_sector_constituents_respects_limit_client_side():
    """即便上游返回多于 limit 行,客户端也要兜底切片。"""
    http = MagicMock()
    http.get_text.return_value = _build_constituents_payload(n=10)
    p = SinaProvider(http=http)
    out = p.fetch_sector_constituents("SINA_new_qcwl", limit=3)
    assert len(out) == 3


def test_fetch_sector_constituents_normalizes_symbol():
    """schemas.SectorConstituent.symbol 契约要求标准化代码;
    即便上游意外返回裸数字(600519),也应归一为 sh600519。"""
    import json as _json
    http = MagicMock()
    http.get_text.return_value = _json.dumps([
        {"symbol": "600519", "name": "贵州茅台",
         "trade": "1234.5", "changepercent": "0",
         "amount": "0", "volume": "0"},
    ])
    p = SinaProvider(http=http)
    out = p.fetch_sector_constituents("SINA_new_qcwl")
    assert out[0].symbol == "sh600519"


def test_fetch_sector_constituents_non_list_returns_empty():
    """异常 JSON 形态(例如 dict / 空 dict)应安全降级为空列表,
    不抛异常 — 让 gateway 走下家。"""
    http = MagicMock()
    http.get_text.return_value = '{"result": "error"}'
    p = SinaProvider(http=http)
    out = p.fetch_sector_constituents("SINA_new_qcwl")
    assert out == []


def test_fetch_sector_constituents_skips_empty_records():
    """缺 symbol / name 的脏数据应被跳过,而不污染下游。"""
    import json as _json
    http = MagicMock()
    http.get_text.return_value = _json.dumps([
        {"symbol": "", "name": "无 symbol"},
        {"symbol": "sh600000", "name": ""},
        {"symbol": "sh600519", "name": "贵州茅台",
         "trade": "1234", "changepercent": "1",
         "amount": "100", "volume": "10"},
    ])
    p = SinaProvider(http=http)
    out = p.fetch_sector_constituents("SINA_new_qcwl")
    assert len(out) == 1
    assert out[0].symbol == "sh600519"


def test_fetch_sector_constituents_http_error_wraps():
    http = MagicMock()
    http.get_text.side_effect = HttpError("conn reset")
    p = SinaProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_sector_constituents("SINA_new_qcwl")


def test_fetch_sector_constituents_json_decode_error_wraps():
    http = MagicMock()
    http.get_text.return_value = "not a json"
    p = SinaProvider(http=http)
    with pytest.raises(ProviderError):
        p.fetch_sector_constituents("SINA_new_qcwl")

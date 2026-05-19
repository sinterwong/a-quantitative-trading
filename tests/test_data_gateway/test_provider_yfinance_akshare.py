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
        out = YfinanceProvider().fetch_kline_daily("ES=F", days=2)
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(out) == 2


def test_yfinance_kline_minute_returns_empty():
    """yfinance provider 不声明 KLINE_MINUTE。"""
    out = YfinanceProvider().fetch_kline_minute("VIX", interval="5m")
    assert out.empty


# ── Akshare: 能力 ─────────────────────────────────────────────────────────────


def test_akshare_capabilities_macro_only():
    decl = AkshareProvider().declare()
    assert Capability.FUNDAMENTALS in decl.capabilities
    assert Capability.MACRO in decl.capabilities
    assert Capability.NEWS_HEADLINES in decl.capabilities   # G5
    assert Market.GLOBAL in decl.markets


def test_akshare_priority_hint_low():
    """akshare 实测不稳定,priority_hint 应低。"""
    assert AkshareProvider().declare().priority_hint < 0.5


def test_akshare_field_authority_declares_fundamentals():
    """AkShare 贡献 revenue_yoy/profit_yoy 等成长字段，权威应低于 Baostock(1.0)。"""
    auth = AkshareProvider().field_authority()
    assert Capability.FUNDAMENTALS in auth
    fa = auth[Capability.FUNDAMENTALS]
    for f in ("roe_ttm", "eps_ttm", "revenue_yoy", "profit_yoy"):
        assert f in fa, f"{f} 未声明权威"
        assert 0 < fa[f] < 1.0, f"{f} 权威 ({fa[f]}) 应低于 Baostock 的 1.0"


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


# ── Akshare: fetch_news_headlines (G5) ──────────────────────────────────────


def _cls_payload(rows):
    """构造 stock_info_global_cls 返回的 DataFrame。"""
    return pd.DataFrame(rows, columns=["标题", "内容", "发布日期", "发布时间"])


def test_akshare_news_headlines_parses_cls_payload():
    from core.data_gateway.schemas import NewsItem

    mock_ak = MagicMock()
    mock_ak.stock_info_global_cls.return_value = _cls_payload([
        ["油价跳水",     "正文1", "2026-05-18", "21:13:49"],
        ["美股开盘下跌", "正文2", "2026-05-18", "21:30:00"],
    ])
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        items = AkshareProvider().fetch_news_headlines("anysymbol", n=5)
    assert len(items) == 2
    assert all(isinstance(it, NewsItem) for it in items)
    assert items[0].title == "油价跳水"
    assert items[0].source == "akshare"
    assert items[0].timestamp.year == 2026 and items[0].timestamp.hour == 21
    # symbol="全部" 是约定调用
    assert mock_ak.stock_info_global_cls.call_args.kwargs.get("symbol") == "全部"


def test_akshare_news_headlines_falls_back_to_truncated_content_when_title_missing():
    mock_ak = MagicMock()
    long_body = "财联社快讯：" + "X" * 80
    mock_ak.stock_info_global_cls.return_value = _cls_payload([
        ["", long_body, "2026-05-18", "10:00:00"],
    ])
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        items = AkshareProvider().fetch_news_headlines("X")
    assert len(items) == 1
    assert items[0].title.endswith("...") and len(items[0].title) == 60


def test_akshare_news_headlines_respects_n():
    mock_ak = MagicMock()
    mock_ak.stock_info_global_cls.return_value = _cls_payload([
        [f"标题{i}", "", "2026-05-18", "10:00:00"] for i in range(10)
    ])
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        items = AkshareProvider().fetch_news_headlines("X", n=3)
    assert len(items) == 3


def test_akshare_news_headlines_missing_library_returns_empty_list():
    real_ak = sys.modules.pop("akshare", None)
    try:
        with patch.dict(sys.modules, {"akshare": None}, clear=False):
            items = AkshareProvider().fetch_news_headlines("X")
            assert items == []
    finally:
        if real_ak is not None:
            sys.modules["akshare"] = real_ak


def test_akshare_news_headlines_provider_error_on_exception():
    from core.data_gateway.providers.base import ProviderError

    mock_ak = MagicMock()
    mock_ak.stock_info_global_cls.side_effect = RuntimeError("net down")
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        with pytest.raises(ProviderError):
            AkshareProvider().fetch_news_headlines("X")


def test_akshare_news_headlines_invalid_datetime_returns_none_ts():
    mock_ak = MagicMock()
    mock_ak.stock_info_global_cls.return_value = _cls_payload([
        ["badtime", "正文", "not-a-date", "??:??"],
    ])
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        items = AkshareProvider().fetch_news_headlines("X")
    assert len(items) == 1
    assert items[0].timestamp is None     # 解析失败留 None
    assert items[0].title == "badtime"


# ── Akshare: _normalize_indicator_em 新增字段 (W1-1) ────────────────────────


def test_akshare_normalize_indicator_em_eps_yoy_direct():
    """直接字段 EPSJBHBZC 存在时优先消费。"""
    raw = pd.DataFrame({
        "REPORT_DATE": ["2024-03-31", "2024-06-30", "2024-09-30"],
        "EPSJB": [0.5, 1.1, 1.6],
        "EPSJBHBZC": [None, 25.0, 30.0],   # 直接 YoY %
    })
    daily = AkshareProvider._normalize_indicator_em(raw, "2024-04-01", "2024-10-01")
    assert "eps_yoy" in daily.columns
    last_val = daily["eps_yoy"].dropna().iloc[-1]
    assert abs(last_val - 30.0) < 1e-6


def test_akshare_normalize_indicator_em_eps_yoy_fallback_self_compute():
    """无 EPSJBHBZC 时从 EPSJB 自算 YoY。"""
    raw = pd.DataFrame({
        "REPORT_DATE": ["2024-03-31", "2024-06-30"],
        "EPSJB": [1.0, 1.5],   # 50% YoY
    })
    daily = AkshareProvider._normalize_indicator_em(raw, "2024-04-01", "2024-07-01")
    eps_yoy = daily["eps_yoy"].dropna()
    assert not eps_yoy.empty
    # YoY = (1.5 - 1.0) / 1.0 * 100 = 50
    assert abs(eps_yoy.iloc[-1] - 50.0) < 1e-6


def test_akshare_normalize_indicator_em_asset_yoy_direct():
    raw = pd.DataFrame({
        "REPORT_DATE": ["2024-03-31", "2024-06-30"],
        "ROEJQ": [10.0, 11.0],
        "TOTALASSETSGRRATE": [None, 8.5],
    })
    daily = AkshareProvider._normalize_indicator_em(raw, "2024-04-01", "2024-07-01")
    assert "asset_yoy" in daily.columns
    assert abs(daily["asset_yoy"].dropna().iloc[-1] - 8.5) < 1e-6


def test_akshare_normalize_indicator_em_asset_yoy_fallback():
    raw = pd.DataFrame({
        "REPORT_DATE": ["2024-03-31", "2024-06-30"],
        "TOTALASSETS": [1000.0, 1100.0],   # +10% YoY
    })
    daily = AkshareProvider._normalize_indicator_em(raw, "2024-04-01", "2024-07-01")
    assert "asset_yoy" in daily.columns
    assert abs(daily["asset_yoy"].dropna().iloc[-1] - 10.0) < 1e-6


def test_akshare_normalize_indicator_em_dividend_yield_passes_through():
    raw = pd.DataFrame({
        "REPORT_DATE": ["2024-06-30"],
        "DIVIDENDYIELD": [3.2],
    })
    daily = AkshareProvider._normalize_indicator_em(raw, "2024-07-01", "2024-07-15")
    assert "dividend_yield" in daily.columns
    assert abs(daily["dividend_yield"].dropna().iloc[-1] - 3.2) < 1e-6


# ── Akshare: fetch_margin_flow (单日快照) ───────────────────────────────────


def _make_margin_sse_snapshot() -> pd.DataFrame:
    """模拟 ak.stock_margin_detail_sse(date=...) 返回的市场快照。"""
    return pd.DataFrame({
        "信用交易日期": ["20240515", "20240515", "20240515"],
        "标的证券代码": ["600519", "601318", "688981"],
        "标的证券简称": ["贵州茅台", "中国平安", "中芯国际"],
        "融资余额": [1.0e10, 2.5e10, 5.5e9],
        "融资买入额": [1.5e8, 3.2e8, 2.1e8],
        "融资偿还额": [1.2e8, 2.8e8, 1.9e8],
        "融券余额": [1.0e7, 2.0e7, 5.0e6],
    })


def _make_margin_szse_snapshot() -> pd.DataFrame:
    """模拟 ak.stock_margin_detail_szse(date=...) 返回的市场快照。"""
    return pd.DataFrame({
        "证券代码": ["000001", "300750", "159915"],
        "融资余额": [1.0e10, 2.0e10, 3.0e9],
        "融资买入额": [1.2e8, 2.5e8, 1.0e7],
        "融资偿还额": [1.1e8, 2.4e8, 0.9e7],
        "融券余额": [5.0e6, 1.0e7, 0],
    })


def test_akshare_fetch_margin_flow_routes_sse_for_60x():
    """600xxx → 沪市，路由到 stock_margin_detail_sse。"""
    mock_ak = MagicMock()
    mock_ak.stock_margin_detail_sse.return_value = _make_margin_sse_snapshot()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow("600519.SH", end="20240515")

    mock_ak.stock_margin_detail_sse.assert_called_once_with(date="20240515")
    mock_ak.stock_margin_detail_szse.assert_not_called()
    assert len(df) == 1
    assert df["margin_balance"].iloc[0] == 1.0e10
    # net_buy = 融资买入额 - 融资偿还额 = 1.5e8 - 1.2e8 = 3.0e7
    assert abs(df["net_buy"].iloc[0] - 3.0e7) < 1.0
    assert df["short_balance"].iloc[0] == 1.0e7


def test_akshare_fetch_margin_flow_routes_szse_for_000():
    """000001 → 深市，路由到 stock_margin_detail_szse。"""
    mock_ak = MagicMock()
    mock_ak.stock_margin_detail_szse.return_value = _make_margin_szse_snapshot()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow("000001.SZ", end="20240515")

    mock_ak.stock_margin_detail_szse.assert_called_once_with(date="20240515")
    mock_ak.stock_margin_detail_sse.assert_not_called()
    assert len(df) == 1
    assert df["margin_balance"].iloc[0] == 1.0e10


def test_akshare_fetch_margin_flow_szse_etf_159():
    """159xxx 是深交所 ETF，必须路由到 SZSE 接口（之前被误归 SSE，已修复）。"""
    mock_ak = MagicMock()
    mock_ak.stock_margin_detail_szse.return_value = _make_margin_szse_snapshot()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow("159915.SZ", end="20240515")

    mock_ak.stock_margin_detail_szse.assert_called_once_with(date="20240515")
    mock_ak.stock_margin_detail_sse.assert_not_called()
    assert len(df) == 1


def test_akshare_fetch_margin_flow_with_start_returns_empty():
    """传 start 即表明要时序，本源不支持 → 返回空（不调 ak）。"""
    mock_ak = MagicMock()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow(
            "600519.SH", start="20240101", end="20240515",
        )
    assert df.empty
    mock_ak.stock_margin_detail_sse.assert_not_called()
    mock_ak.stock_margin_detail_szse.assert_not_called()


def test_akshare_fetch_margin_flow_missing_symbol_returns_empty():
    """快照里没有该 symbol → 返回空 DataFrame。"""
    mock_ak = MagicMock()
    mock_ak.stock_margin_detail_sse.return_value = _make_margin_sse_snapshot()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow("600000.SH", end="20240515")
    assert df.empty


def test_akshare_fetch_margin_flow_empty_raw_returns_empty():
    mock_ak = MagicMock()
    mock_ak.stock_margin_detail_sse.return_value = pd.DataFrame()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_margin_flow("600519.SH", end="20240515")
    assert df.empty


def test_akshare_normalize_margin_snapshot_no_symbol_col():
    """raw 缺少 '标的证券代码'/'证券代码' 列 → 返回空。"""
    raw = pd.DataFrame({"融资余额": [1e10], "融券余额": [1e7]})
    out = AkshareProvider._normalize_margin_snapshot(raw, "600519.SH", "20240515")
    assert out.empty


def test_akshare_normalize_margin_timeseries_still_works():
    """旧时序模式（兼容老 raw 格式）：保留下游 MarginDataStore 的归一路径。"""
    raw = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=5, freq="B"),
        "rz_ye": [1e10, 1.01e10, 1.02e10, 1.03e10, 1.04e10],
        "rq_ye": [1e7, 1.05e7, 1.1e7, 1.15e7, 1.2e7],
    })
    out = AkshareProvider._normalize_margin(raw, None, None)
    assert "margin_balance" in out.columns
    assert "short_balance" in out.columns
    assert len(out) == 5


# ── Akshare: fetch_fund_flow ──────────────────────────────────────────────────


def _make_fund_flow_raw(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame({
        "日期": pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y-%m-%d"),
        "收盘价": [10.0 + i * 0.1 for i in range(n)],
        "涨跌幅": [0.5 + i * 0.1 for i in range(n)],
        "主力净流入-净额": [1e7 * (i + 1) for i in range(n)],
        "主力净流入-净占比": [1.0 + i * 0.1 for i in range(n)],
        "超大单净流入-净额": [5e6 * (i + 1) for i in range(n)],
        "超大单净流入-净占比": [0.5 + i * 0.05 for i in range(n)],
        "大单净流入-净额": [3e6 * (i + 1) for i in range(n)],
        "大单净流入-净占比": [0.3 + i * 0.05 for i in range(n)],
        "中单净流入-净额": [1e6 * (i + 1) for i in range(n)],
        "中单净流入-净占比": [0.1 + i * 0.02 for i in range(n)],
        "小单净流入-净额": [5e5 * (i + 1) for i in range(n)],
        "小单净流入-净占比": [0.05 + i * 0.01 for i in range(n)],
    })


def test_akshare_fetch_fund_flow_routes_sh_for_60x():
    mock_ak = MagicMock()
    mock_ak.stock_individual_fund_flow.return_value = _make_fund_flow_raw()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_fund_flow("600519.SH")
    mock_ak.stock_individual_fund_flow.assert_called_once_with(stock="600519", market="sh")
    assert "main_net_inflow" in df.columns
    assert len(df) == 10


def test_akshare_fetch_fund_flow_routes_sz_for_000():
    mock_ak = MagicMock()
    mock_ak.stock_individual_fund_flow.return_value = _make_fund_flow_raw()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_fund_flow("000001.SZ")
    mock_ak.stock_individual_fund_flow.assert_called_once_with(stock="000001", market="sz")
    assert "main_net_ratio" in df.columns


def test_akshare_fetch_fund_flow_szse_etf_159():
    """159xxx → 深交所 ETF（修复 159 错路由到 sh 的 bug）。"""
    mock_ak = MagicMock()
    mock_ak.stock_individual_fund_flow.return_value = _make_fund_flow_raw()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        AkshareProvider().fetch_fund_flow("159915.SZ")
    mock_ak.stock_individual_fund_flow.assert_called_once_with(stock="159915", market="sz")


def test_akshare_fetch_fund_flow_sh_etf_510():
    """510xxx → 上交所 ETF。"""
    mock_ak = MagicMock()
    mock_ak.stock_individual_fund_flow.return_value = _make_fund_flow_raw()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        AkshareProvider().fetch_fund_flow("510300.SH")
    mock_ak.stock_individual_fund_flow.assert_called_once_with(stock="510300", market="sh")


def test_akshare_fetch_fund_flow_missing_library_returns_empty():
    real_ak = sys.modules.pop("akshare", None)
    try:
        with patch.dict(sys.modules, {"akshare": None}, clear=False):
            df = AkshareProvider().fetch_fund_flow("600519.SH")
            assert df.empty
    finally:
        if real_ak is not None:
            sys.modules["akshare"] = real_ak


def test_akshare_fetch_fund_flow_empty_raw_returns_empty():
    mock_ak = MagicMock()
    mock_ak.stock_individual_fund_flow.return_value = pd.DataFrame()
    with patch.dict(sys.modules, {"akshare": mock_ak}):
        df = AkshareProvider().fetch_fund_flow("600519.SH")
    assert df.empty


def test_akshare_normalize_fund_flow_start_end_filter():
    raw = _make_fund_flow_raw(20)
    out = AkshareProvider._normalize_fund_flow(raw, "2024-01-05", "2024-01-15")
    assert (out.index >= pd.Timestamp("2024-01-05")).all()
    assert (out.index <= pd.Timestamp("2024-01-15")).all()


def test_akshare_normalize_fund_flow_no_date_col_returns_empty():
    raw = pd.DataFrame({"主力净流入-净额": [1e7]})
    out = AkshareProvider._normalize_fund_flow(raw, None, None)
    assert out.empty


# ── Akshare: declare 包含 FUND_FLOW / MARGIN_FLOW / NEWS_HEADLINES (G5) ─


def test_akshare_declare_includes_fund_flow():
    decl = AkshareProvider().declare()
    assert Capability.FUND_FLOW in decl.capabilities
    assert Capability.MARGIN_FLOW in decl.capabilities
    # G5: 财联社电报作 news_headlines 第二源
    assert Capability.NEWS_HEADLINES in decl.capabilities

# -*- coding: utf-8 -*-
"""
baostock Provider 单元测试 — mock 掉 baostock API，只测逻辑。
"""
from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest


class TestBaostockSymbolConversion:
    """_symbol_to_bs 符号转换测试。"""

    def test_sh_code(self):
        from core.data_gateway.providers.baostock import _symbol_to_bs
        assert _symbol_to_bs("sh600519") == "sh.600519"

    def test_sz_code(self):
        from core.data_gateway.providers.baostock import _symbol_to_bs
        assert _symbol_to_bs("sz000001") == "sz.000001"

    def test_already_sh_prefix(self):
        from core.data_gateway.providers.baostock import _symbol_to_bs
        assert _symbol_to_bs("sh.600519") == "sh.600519"


class TestBaostockBalanceSheet:
    """fetch_balance_sheet 逻辑测试（mock baostock API）。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_balance_sheet_maps_fields(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider

        # Mock session + mock query_balance_data
        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data.return_value.__class__ = None  # 标记有数据

        # 构造假的 DataFrame
        import pandas as pd
        mock_df = pd.DataFrame([{
            "code": "sh.600519",
            "pubDate": "2026-04-17",
            "statDate": "2025-12-31",
            "currentRatio": "5.090027",
            "quickRatio": "3.851832",
            "cashRatio": "1.041929",
            "YOYLiability": "-0.123964",
            "liabilityToAsset": "0.164154",
            "assetToEquity": "1.196392",
        }])
        mock_rs.get_data = MagicMock(return_value=mock_df)

        mock_session._bs.query_balance_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        bs = provider.fetch_balance_sheet("sh600519")

        assert bs.symbol == "sh600519"
        assert bs.current_ratio == 5.090027
        assert bs.quick_ratio == 3.851832
        assert bs.debt_to_equity == pytest.approx(16.4154, rel=1e-3)  # 0.164154 × 100
        assert bs.equity == 0.0  # assetToEquity 是杠杆倍数，不映射到 equity

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_balance_sheet_empty_returns_defaults(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_balance_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        bs = provider.fetch_balance_sheet("sh600519")
        assert bs.symbol == "sh600519"
        assert bs.current_ratio == 0.0


class TestBaostockDeclare:
    """declare() 返回值包含 BALANCE_SHEET。"""

    def test_declare_includes_balance_sheet(self):
        from core.data_gateway.providers.baostock import BaostockProvider
        from core.data_gateway.capabilities import Capability

        caps = BaostockProvider().declare()
        assert Capability.BALANCE_SHEET in caps.capabilities


class TestBaostockFieldAuthority:
    """A 股 FUNDAMENTALS 主源应声明 roe_ttm/eps_ttm 基准权威(1.0) +
    independent industry 字段权威。"""

    def test_field_authority_declares_fundamentals(self):
        from core.data_gateway.providers.baostock import BaostockProvider
        from core.data_gateway.capabilities import Capability

        auth = BaostockProvider().field_authority()
        assert Capability.FUNDAMENTALS in auth
        fa = auth[Capability.FUNDAMENTALS]
        assert fa["roe_ttm"] >= 1.0, "ROE 主源权威应 ≥ 1.0"
        assert fa["eps_ttm"] >= 1.0, "EPS 主源权威应 ≥ 1.0"
        assert fa["industry"] >= 1.0, "industry 是 Baostock 独家字段，应声明高权威"
        assert "profit_yoy" in fa


class TestBaostockFetchFundamentalsGrowthFields:
    """fetch_fundamentals 填入 profit_yoy / eps_yoy / asset_yoy。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_fills_yoy_fields(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        # profit（最新一期）
        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.344620",
            "epsTTM": "66.05",
            "netProfit": "85310324833.67",
            "MBRevenue": "172054171890.91",
            "statDate": "2025-12-31",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        # cashflow / operation / dupont 返回空
        for fn in ["query_cash_flow_data", "query_operation_data", "query_dupont_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        # growth（YoY）
        mock_growth = MagicMock()
        mock_growth.error_msg = "success"
        mock_growth.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "statDate": "2025-12-31",
            "YOYNI": "-0.045049",       # -4.5%
            "YOYEPSBasic": "-0.043415", # -4.3%
            "YOYAsset": "0.016358",     # +1.6%
        }]))
        mock_session._bs.query_growth_data = MagicMock(return_value=mock_growth)

        # stock_basic（名称）
        mock_basic_rs = MagicMock()
        mock_basic_rs.error_msg = "success"
        mock_basic_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "code_name": "贵州茅台"
        }]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic_rs)

        # industry
        mock_ind_rs = MagicMock()
        mock_ind_rs.error_msg = "success"
        mock_ind_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "industry": "C15酒、饮料和精制茶制造业"
        }]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind_rs)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        assert f.symbol == "sh600519"
        assert f.name == "贵州茅台"
        assert f.industry == "C15酒、饮料和精制茶制造业"
        assert f.profit_yoy == pytest.approx(-0.045049)
        assert f.eps_yoy == pytest.approx(-0.043415)
        assert f.asset_yoy == pytest.approx(0.016358)


class TestBaostockFundamentalsHistory:
    """W1-2: fetch_fundamentals_history 输出 balance sheet 日频时序。"""

    def test_normalize_balance_history_maps_fields(self):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        raw = pd.DataFrame([
            {"statDate": "2024-03-31", "liabilityToAsset": "0.20",
             "currentRatio": "2.5", "quickRatio": "2.0"},
            {"statDate": "2024-06-30", "liabilityToAsset": "0.25",
             "currentRatio": "2.6", "quickRatio": "2.1"},
            {"statDate": "2024-09-30", "liabilityToAsset": "0.22",
             "currentRatio": "2.7", "quickRatio": "2.2"},
        ])
        daily = BaostockProvider._normalize_balance_history(
            raw, "2024-04-01", "2024-10-15",
        )

        assert "debt_to_equity" in daily.columns
        assert "current_ratio" in daily.columns
        assert "quick_ratio" in daily.columns
        # 周末季末 (03-31/06-30) 也应被前向填充到日频
        # 最新值应反映 2024-09-30 的数据
        assert daily["debt_to_equity"].iloc[-1] == pytest.approx(22.0)  # 0.22 × 100
        assert daily["current_ratio"].iloc[-1] == pytest.approx(2.7)

    def test_normalize_balance_history_empty(self):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd
        out = BaostockProvider._normalize_balance_history(
            pd.DataFrame(), "2024-01-01", "2024-12-31",
        )
        assert out.empty

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_history_routes(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        # 模拟 query_balance_data 在某个季度返回数据
        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "statDate": "2024-12-31",
            "liabilityToAsset": "0.18",
            "currentRatio": "3.1",
            "quickRatio": "2.5",
        }]))
        mock_session._bs.query_balance_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_fundamentals_history("sh600519", "2025-01-01", "2025-03-31")
        assert not df.empty
        assert df["debt_to_equity"].iloc[-1] == pytest.approx(18.0)


# ─── T0-1: K线新增 peTTM/pbMRQ/psTTM/pcfNcfTTM ─────────────────────────────────

class TestBaostockKlineValuationFields:
    """T0-1: K线 fields 加入 peTTM/pbMRQ/psTTM/pcfNcfTTM。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_kline_includes_peTTM_pbMRQ_psTTM_pcfNcfTTM(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame([
            {
                "date": "2026-05-19",
                "open": "1800.0", "high": "1850.0",
                "low": "1790.0", "close": "1830.0",
                "volume": "5000000", "amount": "9000000000",
                "peTTM": "28.5", "pbMRQ": "5.2",
                "psTTM": "18.1", "pcfNcfTTM": "15.3",
            },
            {
                "date": "2026-05-20",
                "open": "1830.0", "high": "1860.0",
                "low": "1820.0", "close": "1845.0",
                "volume": "5200000", "amount": "9500000000",
                "peTTM": "29.0", "pbMRQ": "5.3",
                "psTTM": "18.5", "pcfNcfTTM": "15.7",
            },
        ]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_kline_daily("sh600519", days=5)

        assert not df.empty
        # 估值列存在且为数值类型
        for col in ["peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]:
            assert col in df.columns, f"{col} 列缺失"
            assert df[col].dtype in (float, "float64"), f"{col} 应为数值"
        # 最新一行值正确
        latest = df.iloc[-1]
        assert latest["peTTM"] == pytest.approx(29.0)
        assert latest["pbMRQ"] == pytest.approx(5.3)
        assert latest["psTTM"] == pytest.approx(18.5)
        assert latest["pcfNcfTTM"] == pytest.approx(15.7)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_kline_missing_valuation_fields_does_not_crash(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        # baostock 老数据可能不返回 peTTM/pbMRQ/psTTM/pcfNcfTTM 列
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame([
            {
                "date": "2026-05-19",
                "open": "1800.0", "high": "1850.0",
                "low": "1790.0", "close": "1830.0",
                "volume": "5000000", "amount": "9000000000",
                # 估值列全部缺失——代码不会崩溃，列不存在则无估值数据
            },
        ]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_kline_daily("sh600519", days=5)

        # 不应抛错；价格/成交量列正常返回
        assert not df.empty
        assert "close" in df.columns
        assert "volume" in df.columns


# ─── T0-2: dividend_yield 股息率计算 ─────────────────────────────────────────

class TestBaostockDividendYield:
    """T0-2: fetch_fundamentals 填充 dividend_yield（近4期累计/当前股价）。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_fills_dividend_yield(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        # profit — 提供 epsTTM（用于反推股价）
        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.34",
            "epsTTM": "66.0",
            "netProfit": "85000000000.0",
            "MBRevenue": "170000000000.0",
            "totalShare": "1200000000.0",
            "nIncomeAttrP": "42000000000.0",
            "statDate": "2025-12-31",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        # 其余四表返回空
        for fn in ["query_cash_flow_data", "query_operation_data",
                   "query_dupont_data", "query_growth_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        # stock_basic
        mock_basic = MagicMock()
        mock_basic.error_msg = "success"
        mock_basic.get_data = MagicMock(return_value=pd.DataFrame([{
            "code_name": "贵州茅台",
        }]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic)

        # industry
        mock_ind = MagicMock()
        mock_ind.error_msg = "success"
        mock_ind.get_data = MagicMock(return_value=pd.DataFrame([{
            "industry": "C15酒、饮料和精制茶制造业",
        }]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind)

        # 近5天K线：peTTM=28.5 → 股价 = 28.5 * 66.0 = 1881
        mock_kline = MagicMock()
        mock_kline.error_msg = "success"
        mock_kline.get_data = MagicMock(return_value=pd.DataFrame([
            {"date": "2026-05-19", "close": "1881.0", "peTTM": "28.5"},
        ]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_kline)

        # 除权除息：近4期每股税前股利各 3.0、2.8、2.5、2.2 → 累计 10.5
        def _make_div_rs(year: str, cash_per_share: float):
            rs = MagicMock()
            rs.error_msg = "success"
            rs.get_data = MagicMock(return_value=pd.DataFrame([{
                "dividCashPsBeforeTax": str(cash_per_share),
            }]))
            return rs

        def _div_side_effect(code: str, year: str, yearType: str):
            # year 通过 keyword argument 传入
            div_map = {
                "2026": 3.0, "2025": 2.8,
                "2024": 2.5, "2023": 2.2,
            }
            return _make_div_rs(year, div_map.get(str(year), 0.0))

        mock_session._bs.query_dividend_data = MagicMock(side_effect=_div_side_effect)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        # 股息率 = 10.5 / 1881 * 100 ≈ 0.558%
        assert f.dividend_yield == pytest.approx(0.558, rel=0.02)
        # 基本面核心字段仍正确
        assert f.symbol == "sh600519"
        assert f.eps_ttm == pytest.approx(66.0)
        assert f.roe_ttm == pytest.approx(34.0)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_dividend_yield_zero_when_no_dividend(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.34",
            "epsTTM": "66.0",
            "netProfit": "85000000000.0",
            "MBRevenue": "170000000000.0",
            "totalShare": "1200000000.0",
            "nIncomeAttrP": "42000000000.0",
            "statDate": "2025-12-31",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        for fn in ["query_cash_flow_data", "query_operation_data",
                   "query_dupont_data", "query_growth_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        mock_basic = MagicMock()
        mock_basic.error_msg = "success"
        mock_basic.get_data = MagicMock(return_value=pd.DataFrame([{"code_name": "贵州茅台"}]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic)

        mock_ind = MagicMock()
        mock_ind.error_msg = "success"
        mock_ind.get_data = MagicMock(return_value=pd.DataFrame([{"industry": "C15"}]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind)

        # K线有 peTTM 但除权除息返回空
        mock_kline = MagicMock()
        mock_kline.error_msg = "success"
        mock_kline.get_data = MagicMock(return_value=pd.DataFrame([
            {"date": "2026-05-19", "close": "1881.0", "peTTM": "28.5"},
        ]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_kline)

        mock_div = MagicMock()
        mock_div.error_msg = "success"
        mock_div.get_data = MagicMock(return_value=pd.DataFrame())  # 无分红
        mock_session._bs.query_dividend_data = MagicMock(return_value=mock_div)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        assert f.dividend_yield == 0.0


# ─── T0-3: Fundamentals 新增 net_margin / gross_margin / bps ────────────────

class TestBaostockNewFundamentalsFields:
    """T0-3: Fundamentals 新增 net_margin / gross_margin / bps。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_fills_net_margin_gross_margin(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        # profit 包含 npMargin / gpMargin（小数格式）
        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.34",
            "epsTTM": "66.0",
            "netProfit": "85000000000.0",
            "MBRevenue": "170000000000.0",
            "totalShare": "1200000000.0",
            "nIncomeAttrP": "42000000000.0",
            "statDate": "2025-12-31",
            "npMargin": "0.501",   # 50.1%
            "gpMargin": "0.742",    # 74.2%
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        for fn in ["query_cash_flow_data", "query_operation_data",
                   "query_dupont_data", "query_growth_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        mock_basic = MagicMock()
        mock_basic.error_msg = "success"
        mock_basic.get_data = MagicMock(return_value=pd.DataFrame([{"code_name": "贵州茅台"}]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic)

        mock_ind = MagicMock()
        mock_ind.error_msg = "success"
        mock_ind.get_data = MagicMock(return_value=pd.DataFrame([{"industry": "C15"}]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind)

        mock_kline = MagicMock()
        mock_kline.error_msg = "success"
        mock_kline.get_data = MagicMock(return_value=pd.DataFrame([{
            "date": "2026-05-19", "close": "1881.0", "peTTM": "28.5",
        }]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_kline)

        mock_div = MagicMock()
        mock_div.error_msg = "success"
        mock_div.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_dividend_data = MagicMock(return_value=mock_div)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        # 小数 × 100 → %
        assert f.net_margin == pytest.approx(50.1, rel=1e-3)
        assert f.gross_margin == pytest.approx(74.2, rel=1e-3)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_fills_bps(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        # epsTTM=66, roeAvg=0.34 → BPS = (net_profit/roeAvg) / total_share
        # = (85B/0.34) / 1.2B ≈ 208.33
        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.34",
            "epsTTM": "0.0",        # epsTTM 为空，触发备用公式
            "netProfit": "85000000000.0",
            "MBRevenue": "170000000000.0",
            "totalShare": "1200000000.0",
            "nIncomeAttrP": "42000000000.0",
            "statDate": "2025-12-31",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        for fn in ["query_cash_flow_data", "query_operation_data",
                   "query_dupont_data", "query_growth_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        mock_basic = MagicMock()
        mock_basic.error_msg = "success"
        mock_basic.get_data = MagicMock(return_value=pd.DataFrame([{"code_name": "贵州茅台"}]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic)

        mock_ind = MagicMock()
        mock_ind.error_msg = "success"
        mock_ind.get_data = MagicMock(return_value=pd.DataFrame([{"industry": "C15"}]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind)

        mock_kline = MagicMock()
        mock_kline.error_msg = "success"
        mock_kline.get_data = MagicMock(return_value=pd.DataFrame([{
            "date": "2026-05-19", "close": "1881.0", "peTTM": "28.5",
        }]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_kline)

        mock_div = MagicMock()
        mock_div.error_msg = "success"
        mock_div.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_dividend_data = MagicMock(return_value=mock_div)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        # bps_val = (42B/0.34) / 1.2B ≈ 102.94
        assert f.bps > 0, "BPS 应为正值"
        assert isinstance(f.bps, float)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_bps_zero_when_roe_zero(self, mock_get_session):
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        # roeAvg=0 → 无法估算净资产，BPS 应为 0.0
        mock_profit = MagicMock()
        mock_profit.error_msg = "success"
        mock_profit.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "roeAvg": "0.0",
            "epsTTM": "0.0",
            "netProfit": "0.0",
            "MBRevenue": "0.0",
            "totalShare": "0.0",
            "nIncomeAttrP": "0.0",
            "statDate": "2025-12-31",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit)

        for fn in ["query_cash_flow_data", "query_operation_data",
                   "query_dupont_data", "query_growth_data"]:
            mock_empty = MagicMock()
            mock_empty.error_msg = "success"
            mock_empty.get_data = MagicMock(return_value=pd.DataFrame())
            setattr(mock_session._bs, fn, MagicMock(return_value=mock_empty))

        mock_basic = MagicMock()
        mock_basic.error_msg = "success"
        mock_basic.get_data = MagicMock(return_value=pd.DataFrame([{"code_name": "贵州茅台"}]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_basic)

        mock_ind = MagicMock()
        mock_ind.error_msg = "success"
        mock_ind.get_data = MagicMock(return_value=pd.DataFrame([{"industry": "C15"}]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind)

        mock_kline = MagicMock()
        mock_kline.error_msg = "success"
        mock_kline.get_data = MagicMock(return_value=pd.DataFrame([{
            "date": "2026-05-19", "close": "1881.0", "peTTM": "0.0",
        }]))
        mock_session._bs.query_history_k_data_plus = MagicMock(return_value=mock_kline)

        mock_div = MagicMock()
        mock_div.error_msg = "success"
        mock_div.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_dividend_data = MagicMock(return_value=mock_div)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        f = provider.fetch_fundamentals("sh600519")

        assert f.bps == 0.0


# ─── T0-4: fetch_fundamentals_history 六表全量19列归一化 ─────────────────────

class TestBaostockFundamentalsHistorySixTables:
    """T0-4: fetch_fundamentals_history 扩展为六表全量输出（19列日频前向填充序列）。"""

    def test_normalize_financial_history_all_19_columns(self):
        """_normalize_financial_history 应输出全部19个列。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        # revenue_yoy = pct_change(periods=4) 需要至少 8 期季度数据
        profit_data = [
            # 2023 Q1~Q4（作基准，无 revenue_yoy 输出）
            {"statDate": "2023-03-31", "gpMargin": "0.72", "npMargin": "0.48",
             "epsTTM": "50.0", "roeAvg": "0.31",
             "MBRevenue": "150000000000.0", "netProfit": "72000000000.0"},
            {"statDate": "2023-06-30", "gpMargin": "0.71", "npMargin": "0.47",
             "epsTTM": "51.0", "roeAvg": "0.32",
             "MBRevenue": "155000000000.0", "netProfit": "72850000000.0"},
            {"statDate": "2023-09-30", "gpMargin": "0.70", "npMargin": "0.46",
             "epsTTM": "52.0", "roeAvg": "0.30",
             "MBRevenue": "148000000000.0", "netProfit": "68080000000.0"},
            {"statDate": "2023-12-31", "gpMargin": "0.73", "npMargin": "0.49",
             "epsTTM": "53.0", "roeAvg": "0.33",
             "MBRevenue": "160000000000.0", "netProfit": "78400000000.0"},
            # 2024 Q1~Q4（产生非NaN revenue_yoy）
            {"statDate": "2024-03-31", "gpMargin": "0.74", "npMargin": "0.50",
             "epsTTM": "55.0", "roeAvg": "0.33",
             "MBRevenue": "170000000000.0", "netProfit": "85000000000.0"},
            {"statDate": "2024-06-30", "gpMargin": "0.73", "npMargin": "0.49",
             "epsTTM": "58.0", "roeAvg": "0.34",
             "MBRevenue": "180000000000.0", "netProfit": "88200000000.0"},
            {"statDate": "2024-09-30", "gpMargin": "0.72", "npMargin": "0.48",
             "epsTTM": "57.0", "roeAvg": "0.32",
             "MBRevenue": "165000000000.0", "netProfit": "79200000000.0"},
            {"statDate": "2024-12-31", "gpMargin": "0.71", "npMargin": "0.47",
             "epsTTM": "60.0", "roeAvg": "0.35",
             "MBRevenue": "190000000000.0", "netProfit": "89300000000.0"},
        ]
        tables = {
            "profit": pd.DataFrame(profit_data),
            "balance": pd.DataFrame([
                {"statDate": "2024-03-31",
                 "liabilityToAsset": "0.18", "currentRatio": "3.1", "quickRatio": "2.5"},
                {"statDate": "2024-06-30",
                 "liabilityToAsset": "0.20", "currentRatio": "3.0", "quickRatio": "2.4"},
            ]),
            "cashflow": pd.DataFrame([
                {"statDate": "2024-03-31", "CFOToNP": "1.2", "CFOToOR": "0.25"},
                {"statDate": "2024-06-30", "CFOToNP": "1.15", "CFOToOR": "0.24"},
            ]),
            "operation": pd.DataFrame([
                {"statDate": "2024-03-31",
                 "AssetTurnRatio": "0.55", "INVTurnDays": "800.0", "NRTurnDays": "120.0"},
                {"statDate": "2024-06-30",
                 "AssetTurnRatio": "0.58", "INVTurnDays": "780.0", "NRTurnDays": "115.0"},
            ]),
            "growth": pd.DataFrame([
                {"statDate": "2024-03-31",
                 "YOYEquity": "0.08", "YOYNI": "-0.04"},
                {"statDate": "2024-06-30",
                 "YOYEquity": "0.09", "YOYNI": "-0.03"},
            ]),
            "dupont": pd.DataFrame([
                {"statDate": "2024-03-31",
                 "dupontROE": "0.30", "dupontAssetStoEquity": "1.50"},
                {"statDate": "2024-06-30",
                 "dupontROE": "0.32", "dupontAssetStoEquity": "1.55"},
            ]),
        }

        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            tables, "2024-04-01", "2024-07-15",
        )

        expected_cols = {
            # profit
            "gross_margin", "net_margin", "eps_ttm", "roe_ttm",
            "revenue_ttm", "profit_ttm",
            # balance
            "debt_to_equity", "current_ratio", "quick_ratio",
            # cashflow
            "cfo_to_profit", "cfo_to_revenue",
            # operation
            "asset_turn", "inv_turn_days", "nr_turn_days",
            # growth
            "equity_yoy", "profit_yoy",
            # dupont
            "dupont_roe", "equity_multiplier",
            # revenue_yoy 自算
            "revenue_yoy",
        }
        missing = expected_cols - set(daily.columns)
        assert not missing, f"缺少列: {missing}"
        assert len(daily.columns) >= 19

    def test_normalize_financial_history_gross_margin_scaled(self):
        """gpMargin 小数 0.74 → gross_margin 应为 74.0（×100）。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        tables = {
            "profit": pd.DataFrame([
                {"statDate": "2024-06-30",
                 "gpMargin": "0.742", "npMargin": "0.501",
                 "epsTTM": "66.0", "roeAvg": "0.344",
                 "MBRevenue": "170000000000.0", "netProfit": "85000000000.0",
                 },
            ]),
        }
        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            tables, "2024-07-01", "2024-07-10",
        )
        assert daily["gross_margin"].iloc[-1] == pytest.approx(74.2, rel=1e-2)
        assert daily["net_margin"].iloc[-1] == pytest.approx(50.1, rel=1e-2)
        assert daily["roe_ttm"].iloc[-1] == pytest.approx(34.4, rel=1e-2)

    def test_normalize_financial_history_debt_to_equity_scaled(self):
        """liabilityToAsset 小数 0.18 → debt_to_equity 应为 18.0（×100）。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        tables = {
            "balance": pd.DataFrame([
                {"statDate": "2024-06-30",
                 "liabilityToAsset": "0.18",
                 "currentRatio": "3.1", "quickRatio": "2.5",
                 },
            ]),
        }
        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            tables, "2024-07-01", "2024-07-10",
        )
        assert daily["debt_to_equity"].iloc[-1] == pytest.approx(18.0)

    def test_normalize_financial_history_forward_fill(self):
        """季末 06-30 的值应前向填充至 07-01。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        tables = {
            "profit": pd.DataFrame([
                {"statDate": "2024-06-30",
                 "gpMargin": "0.74", "npMargin": "0.50",
                 "epsTTM": "66.0", "roeAvg": "0.34",
                 "MBRevenue": "170000000000.0", "netProfit": "85000000000.0",
                 },
            ]),
        }
        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            tables, "2024-07-01", "2024-07-05",
        )
        # 07-01~07-05 都应拿到 06-30 的值（前向填充）
        assert not daily["gross_margin"].isna().any()

    def test_normalize_financial_history_empty_tables(self):
        """六表全部为空时返回空 DataFrame。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            {}, "2024-01-01", "2024-12-31",
        )
        assert daily.empty

    def test_normalize_financial_history_partial_tables(self):
        """仅有 profit 表时也正常工作，其余列为 NaN。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        tables = {
            "profit": pd.DataFrame([
                {"statDate": "2024-06-30",
                 "gpMargin": "0.74", "npMargin": "0.50",
                 "epsTTM": "66.0", "roeAvg": "0.34",
                 "MBRevenue": "170000000000.0", "netProfit": "85000000000.0",
                 },
            ]),
        }
        provider = BaostockProvider()
        daily = provider._normalize_financial_history(
            tables, "2024-07-01", "2024-07-10",
        )
        assert "gross_margin" in daily.columns
        # cfo_to_profit 只在有 cashflow 表时才生成；profit 表单独存在时该列为 NaN
        assert "cfo_to_profit" not in daily.columns  # 无 cashflow 表，不生成该列

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_history_calls_six_tables(self, mock_get_session):
        """fetch_fundamentals_history 应调用全部6个财务表查询。"""
        from core.data_gateway.providers.baostock import BaostockProvider
        import pandas as pd

        mock_session = MagicMock()

        def _make_empty_rs(*args, **kwargs):
            rs = MagicMock()
            rs.error_msg = "success"
            rs.get_data = MagicMock(return_value=pd.DataFrame())
            return rs

        mock_session._bs.query_profit_data = MagicMock(side_effect=_make_empty_rs)
        mock_session._bs.query_cash_flow_data = MagicMock(side_effect=_make_empty_rs)
        mock_session._bs.query_operation_data = MagicMock(side_effect=_make_empty_rs)
        mock_session._bs.query_dupont_data = MagicMock(side_effect=_make_empty_rs)
        mock_session._bs.query_growth_data = MagicMock(side_effect=_make_empty_rs)
        mock_session._bs.query_balance_data = MagicMock(side_effect=_make_empty_rs)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_fundamentals_history("sh600519", "2024-01-01", "2024-12-31")

        # 空表 → 最终结果为空 DataFrame（不抛错）
        assert df.empty
        # 六个 fetcher 都被调用过
        assert mock_session._bs.query_profit_data.called
        assert mock_session._bs.query_cash_flow_data.called
        assert mock_session._bs.query_operation_data.called
        assert mock_session._bs.query_dupont_data.called
        assert mock_session._bs.query_growth_data.called
        assert mock_session._bs.query_balance_data.called


# ─── P1 新功能测试 ────────────────────────────────────────────────────────────

class TestBaostockDupontMetrics:
    """P1-1: fetch_dupont_metrics — 杜邦分析指标快照。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_dupont_metrics_maps_all_fields(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_df = pd.DataFrame([{
            "code": "sh.600519",
            "pubDate": "2026-04-17",
            "statDate": "2025-12-31",
            "dupontROE": "0.3012",        # 已 ×100 → 30.12%
            "dupontNetMargin": "0.2715",  # 27.15%
            "dupontAssetTurn": "0.85",    # 次
            "dupontAssetStoEquity": "1.95",  # 倍
            "dupontTaxBurden": "0.8200",  # 82%
            "dupontIntburden": "0.9100",   # 91%
            "dupontEbittogr": "0.3520",   # 35.20%
        }])
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session._bs.query_dupont_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        dm = provider.fetch_dupont_metrics("sh600519")

        assert dm.symbol == "sh600519"
        assert dm.roe == pytest.approx(0.3012)
        assert dm.net_margin == pytest.approx(0.2715)
        assert dm.asset_turn == pytest.approx(0.85)
        assert dm.equity_multiplier == pytest.approx(1.95)
        assert dm.tax_burden == pytest.approx(0.8200)
        assert dm.int_burden == pytest.approx(0.9100)
        assert dm.ebit_to_revenue == pytest.approx(0.3520)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_dupont_metrics_empty_data_returns_default(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_dupont_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        dm = provider.fetch_dupont_metrics("sh600519")

        assert dm.symbol == "sh600519"
        # symbol 有值 → is_valid 为 True（数据为空但对象本身有效）
        assert dm.is_valid
        # 数值字段均为零（API 无数据）
        assert dm.roe == 0.0
        assert dm.net_margin == 0.0


class TestBaostockOperationMetrics:
    """P1-2: fetch_operation_metrics — 运营能力指标快照。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_operation_metrics_maps_all_fields(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_df = pd.DataFrame([{
            "code": "sh.600519",
            "pubDate": "2026-04-17",
            "statDate": "2025-12-31",
            "nrTurnDays": "45.3",        # 天
            "invTurnDays": "1200.5",     # 天
            "assetTurnRatio": "0.55",     # 次
            "caTurnRatio": "0.82",        # 次
        }])
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session._bs.query_operation_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        om = provider.fetch_operation_metrics("sh600519")

        assert om.symbol == "sh600519"
        assert om.nr_turn_days == pytest.approx(45.3)
        assert om.inv_turn_days == pytest.approx(1200.5)
        assert om.asset_turn == pytest.approx(0.55)
        assert om.ca_turn == pytest.approx(0.82)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_operation_metrics_empty_data_returns_default(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_session = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_operation_data = MagicMock(return_value=mock_rs)
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        om = provider.fetch_operation_metrics("sh600519")

        assert om.symbol == "sh600519"
        # symbol 有值 → is_valid 为 True
        assert om.is_valid
        # 数值字段均为零
        assert om.nr_turn_days == 0.0
        assert om.inv_turn_days == 0.0


class TestBaostockFundamentalsGrowthFields:
    """P1-3: fetch_fundamentals 填充 equity_yoy / pni_yoy（来自 growth_data）。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_fundamentals_fills_equity_yoy_and_pni_yoy(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_session = MagicMock()

        # profit_df
        mock_profit_rs = MagicMock()
        mock_profit_rs.error_msg = "success"
        mock_profit_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "pubDate": "2026-04-17",
            "statDate": "2025-12-31",
            "roeAvg": "0.30",
            "epsTTM": "8.50",
            "MBRevenue": "42000000000",
            "netProfit": "15000000000",
            "totalShare": "1200000000",
            "npMargin": "0.35",
            "gpMargin": "0.75",
        }]))
        mock_session._bs.query_profit_data = MagicMock(return_value=mock_profit_rs)

        # cashflow（空）
        mock_cf_rs = MagicMock()
        mock_cf_rs.error_msg = "success"
        mock_cf_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_cash_flow_data = MagicMock(return_value=mock_cf_rs)

        # operation（空）
        mock_op_rs = MagicMock()
        mock_op_rs.error_msg = "success"
        mock_op_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_operation_data = MagicMock(return_value=mock_op_rs)

        # dupont（空）
        mock_dp_rs = MagicMock()
        mock_dp_rs.error_msg = "success"
        mock_dp_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session._bs.query_dupont_data = MagicMock(return_value=mock_dp_rs)

        # growth（非空，有 equity_yoy 和 pni_yoy）
        mock_gr_rs = MagicMock()
        mock_gr_rs.error_msg = "success"
        mock_gr_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "pubDate": "2026-04-17",
            "statDate": "2025-12-31",
            "YOYNI": "0.15",
            "YOYEPSBasic": "0.12",
            "YOYAsset": "0.08",
            "YOYEquity": "0.06",   # 6%
            "YOYPNI": "0.14",       # 14%
        }]))
        mock_session._bs.query_growth_data = MagicMock(return_value=mock_gr_rs)

        # industry
        mock_ind_rs = MagicMock()
        mock_ind_rs.error_msg = "success"
        mock_ind_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519",
            "code_name": "贵州茅台",
            "industry": "白酒",
        }]))
        mock_session._bs.query_stock_industry = MagicMock(return_value=mock_ind_rs)

        # name query
        mock_name_rs = MagicMock()
        mock_name_rs.error_msg = "success"
        mock_name_rs.get_data = MagicMock(return_value=pd.DataFrame([{
            "code": "sh.600519", "code_name": "贵州茅台",
        }]))
        mock_session._bs.query_stock_basic = MagicMock(return_value=mock_name_rs)

        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        fm = provider.fetch_fundamentals("sh600519")

        assert fm.symbol == "sh600519"
        assert fm.equity_yoy == pytest.approx(0.06)   # growth YOYEquity 小数 → 直接赋值
        assert fm.pni_yoy == pytest.approx(0.14)        # growth YOYPNI 小数 → 直接赋值
        assert fm.industry == "白酒"


class TestBaostockFundamentalsHistoryExtendedFields:
    """P1-4: _normalize_financial_history 新增 pni_yoy / tax_burden / int_burden / ebit_to_revenue。"""

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_normalize_includes_new_dupont_and_growth_fields(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        provider = BaostockProvider()

        tables = {
            "profit": pd.DataFrame({
                "statDate": ["2025-12-31", "2024-12-31", "2023-12-31"],
                "roeAvg": ["0.30", "0.28", "0.25"],
                "gpMargin": ["0.75", "0.73", "0.71"],
                "npMargin": ["0.35", "0.33", "0.30"],
                "epsTTM": ["8.5", "7.8", "7.1"],
                "MBRevenue": [4.2e10, 3.8e10, 3.5e10],
                "netProfit": [1.5e10, 1.3e10, 1.1e10],
            }),
            "balance": pd.DataFrame({
                "statDate": ["2025-12-31", "2024-12-31"],
                "liabilityToAsset": ["0.16", "0.18"],
                "currentRatio": ["5.09", "4.80"],
                "quickRatio": ["3.85", "3.60"],
            }),
            "cashflow": pd.DataFrame({
                "statDate": ["2025-12-31"],
                "CFOToNP": ["1.20"],
                "CFOToOR": ["0.90"],
            }),
            "operation": pd.DataFrame({
                "statDate": ["2025-12-31"],
                "AssetTurnRatio": ["0.55"],
                "INVTurnDays": ["1200.5"],
                "NRTurnDays": ["45.3"],
            }),
            "dupont": pd.DataFrame({
                "statDate": ["2025-12-31", "2024-12-31"],
                "dupontROE": ["0.3012", "0.2800"],
                "dupontAssetStoEquity": ["1.95", "1.90"],
                "dupontTaxBurden": ["0.82", "0.81"],
                "dupontIntburden": ["0.91", "0.90"],
                "dupontEbittogr": ["0.352", "0.340"],
            }),
            "growth": pd.DataFrame({
                "statDate": ["2025-12-31"],
                "YOYEquity": ["0.06"],
                "YOYNI": ["0.15"],
                "YOYPNI": ["0.14"],
            }),
        }

        df = provider._normalize_financial_history(tables, None, None)

        assert not df.empty
        # 新增 dupont 字段
        assert "tax_burden" in df.columns
        assert "int_burden" in df.columns
        assert "ebit_to_revenue" in df.columns
        assert "pni_yoy" in df.columns
        # 值正确（normalize 中已对小数字段 ×100，所以 0.82 → 82.0）
        assert df["tax_burden"].dropna().iloc[-1] == pytest.approx(82.0)
        assert df["int_burden"].dropna().iloc[-1] == pytest.approx(91.0)
        assert df["ebit_to_revenue"].dropna().iloc[-1] == pytest.approx(35.2)
        assert df["pni_yoy"].dropna().iloc[-1] == pytest.approx(14.0)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_normalize_missing_dupont_and_growth_tables(self, mock_get_session):
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        provider = BaostockProvider()

        # 只有 profit 表
        tables = {
            "profit": pd.DataFrame({
                "statDate": ["2025-12-31"],
                "roeAvg": ["0.30"],
                "gpMargin": ["0.75"],
                "npMargin": ["0.35"],
                "epsTTM": ["8.5"],
                "MBRevenue": [4.2e10],
                "netProfit": [1.5e10],
            }),
        }

        df = provider._normalize_financial_history(tables, None, None)

        assert not df.empty
        # dupont 字段不存在（因为没有 dupont 表）
        assert "tax_burden" not in df.columns
        assert "int_burden" not in df.columns
        assert "pni_yoy" not in df.columns
        # profit 基础字段仍然存在
        assert "roe_ttm" in df.columns

    def test_dividend_record_is_valid(self):
        """DividendRecord.is_valid: 无效（symbol 空）| 有效（有现金分红）。"""
        from core.data_gateway.schemas import DividendRecord

        empty = DividendRecord(symbol="")
        assert not empty.is_valid

        cash_only = DividendRecord(symbol="sh600519", cash_per_share=2.5)
        assert cash_only.is_valid

        stock_only = DividendRecord(symbol="sh600519", stock_per_share=0.5)
        assert stock_only.is_valid

        zero = DividendRecord(symbol="sh600519")
        assert not zero.is_valid

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_dividend_basic(self, mock_get_session):
        """fetch_dividend 返回 2 条记录，字段映射正确。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600519", "dividPlanAnnounceDate": "2024-04-03",
             "dividOperateDate": "2024-06-19", "dividPayDate": "2024-06-19",
             "dividStockMarketDate": "", "dividCashPsBeforeTax": "30.876",
             "dividStocksPs": "0.0", "dividReserveToStockPs": ""},
            {"code": "sh.600519", "dividPlanAnnounceDate": "2024-11-09",
             "dividOperateDate": "2024-12-20", "dividPayDate": "2024-12-20",
             "dividStockMarketDate": "", "dividCashPsBeforeTax": "23.882",
             "dividStocksPs": "0.0", "dividReserveToStockPs": ""},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_dividend_data.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_dividend("sh600519", year=2024)

        assert len(records) == 2
        # 按 operate_date 倒序
        assert records[0].operate_date == datetime(2024, 12, 20)
        assert records[0].cash_per_share == 23.882
        assert records[1].operate_date == datetime(2024, 6, 19)
        assert records[1].cash_per_share == 30.876
        assert all(r.symbol == "sh600519" for r in records)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_dividend_year_filter(self, mock_get_session):
        """指定 year=2024 只查询 2024 年。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600519", "dividPlanAnnounceDate": "2024-04-03",
             "dividOperateDate": "2024-06-19", "dividPayDate": "2024-06-19",
             "dividStockMarketDate": "", "dividCashPsBeforeTax": "30.876",
             "dividStocksPs": "0.0", "dividReserveToStockPs": ""},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_dividend_data.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_dividend("sh600519", year=2024)

        mock_session._bs.query_dividend_data.assert_called_once_with(
            "sh.600519", year="2024", yearType="operate",
        )
        assert len(records) == 1
        assert records[0].cash_per_share == 30.876

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_dividend_stock_and_reserve(self, mock_get_session):
        """有送股和转增时 DividendRecord 仍为 valid。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600036", "dividPlanAnnounceDate": "2024-03-28",
             "dividOperateDate": "2024-06-20", "dividPayDate": "2024-06-20",
             "dividStockMarketDate": "2024-06-20",
             "dividCashPsBeforeTax": "10.0",
             "dividStocksPs": "0.5",
             "dividReserveToStockPs": "0.3"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_dividend_data.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_dividend("sh600036", year=2024)

        assert len(records) == 1
        r = records[0]
        assert r.cash_per_share == 10.0
        assert r.stock_per_share == 0.5
        assert r.reserve_to_stock == 0.3
        assert r.stock_market_date == datetime(2024, 6, 20)
        assert r.is_valid

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_dividend_empty_result(self, mock_get_session):
        """query 返回空 DataFrame 时返回空列表，不抛异常。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=pd.DataFrame())
        mock_session = MagicMock()
        mock_session._bs.query_dividend_data.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_dividend("sh600519", year=2020)

        assert records == []

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_dividend_api_error_skipped(self, mock_get_session):
        """单年份查询失败（error_msg != success）时继续查询其他年份。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df_ok = pd.DataFrame([
            {"code": "sh.600519", "dividPlanAnnounceDate": "2024-04-03",
             "dividOperateDate": "2024-06-19", "dividPayDate": "2024-06-19",
             "dividStockMarketDate": "", "dividCashPsBeforeTax": "30.876",
             "dividStocksPs": "0.0", "dividReserveToStockPs": ""},
        ])
        mock_rs_ok = MagicMock()
        mock_rs_ok.error_msg = "success"
        mock_rs_ok.get_data = MagicMock(return_value=mock_df_ok)
        mock_rs_fail = MagicMock()
        mock_rs_fail.error_msg = "error: no data"

        mock_session = MagicMock()
        # 前两次调用 fail，第三次 ok
        mock_session._bs.query_dividend_data.side_effect = [
            mock_rs_fail, mock_rs_fail, mock_rs_ok
        ]
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_dividend("sh600519")  # 默认4年

        assert len(records) == 1

    def test_industry_classification_is_valid(self):
        """IndustryClassification.is_valid: 有 symbol 即为有效。"""
        from core.data_gateway.schemas import IndustryClassification

        empty = IndustryClassification(symbol="")
        assert not empty.is_valid

        valid = IndustryClassification(symbol="sh600519", industry="制造业")
        assert valid.is_valid

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_industry_classification_basic(self, mock_get_session):
        """fetch_industry_classification 返回正确字段映射。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600519", "code_name": "贵州茅台",
             "industry": "C15酒、饮料和精制茶制造业",
             "industryClassification": "证监会行业分类",
             "updateDate": "2026-05-18"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_stock_industry.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        ic = provider.fetch_industry_classification("sh600519")

        assert ic is not None
        assert ic.symbol == "sh600519"
        assert ic.code_name == "贵州茅台"
        assert ic.industry == "C15酒、饮料和精制茶制造业"
        assert ic.classification == "证监会行业分类"
        assert ic.update_date == "2026-05-18"
        assert ic.is_valid

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_industry_classification_not_found(self, mock_get_session):
        """目标股票不在全市场数据中时返回 None。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600000", "code_name": "浦发银行",
             "industry": "J66货币金融服务",
             "industryClassification": "证监会行业分类",
             "updateDate": "2026-05-18"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_stock_industry.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        ic = provider.fetch_industry_classification("sh600519")

        assert ic is None

    def test_index_constituent_is_valid(self):
        """IndexConstituent.is_valid: 有 index_code 和 symbol 即为有效。"""
        from core.data_gateway.schemas import IndexConstituent

        empty = IndexConstituent(index_code="hs300", symbol="")
        assert not empty.is_valid

        empty2 = IndexConstituent(index_code="", symbol="sh600519")
        assert not empty2.is_valid

        valid = IndexConstituent(index_code="hs300", symbol="sh600519", code_name="贵州茅台")
        assert valid.is_valid

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_index_constituents_basic(self, mock_get_session):
        """fetch_index_constituents('hs300') 返回 2 条记录，字段映射正确。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600519", "code_name": "贵州茅台", "updateDate": "2026-05-18"},
            {"code": "sh.600036", "code_name": "招商银行", "updateDate": "2026-05-18"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_hs300_stocks.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_index_constituents("hs300")

        assert len(records) == 2
        assert records[0].index_code == "hs300"
        # 按 symbol 排序：sh600036 < sh600519
        assert records[0].symbol == "sh600036"
        assert records[0].code_name == "招商银行"
        assert records[1].symbol == "sh600519"
        assert records[1].code_name == "贵州茅台"
        assert all(r.update_date == "2026-05-18" for r in records)

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_index_constituents_invalid_code(self, mock_get_session):
        """无效 index_code 直接返回空列表，不调 API。"""
        from core.data_gateway.providers.baostock import BaostockProvider

        provider = BaostockProvider()
        records = provider.fetch_index_constituents("invalid")

        assert records == []
        mock_get_session.return_value._bs.query_hs300_stocks.assert_not_called()

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_index_constituents_sz50(self, mock_get_session):
        """sz50 调用 query_sz50_stocks 方法。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"code": "sh.600519", "code_name": "贵州茅台", "updateDate": "2026-05-18"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_sz50_stocks.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        records = provider.fetch_index_constituents("sz50")

        assert len(records) == 1
        mock_session._bs.query_sz50_stocks.assert_called_once()

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_trade_calendar_basic(self, mock_get_session):
        """fetch_trade_calendar 返回正确 DataFrame 列和排序。"""
        import pandas as pd
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_df = pd.DataFrame([
            {"calendar_date": "2026-05-07", "is_trading_day": "1"},
            {"calendar_date": "2026-05-06", "is_trading_day": "1"},
            {"calendar_date": "2026-05-09", "is_trading_day": "0"},
        ])
        mock_rs = MagicMock()
        mock_rs.error_msg = "success"
        mock_rs.get_data = MagicMock(return_value=mock_df)
        mock_session = MagicMock()
        mock_session._bs.query_trade_dates.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_trade_calendar("2026-05-01", "2026-05-31")

        assert list(df.columns) == ["calendar_date", "is_trading_day"]
        assert len(df) == 3
        # 按 calendar_date 排序
        assert df.iloc[0]["calendar_date"] == "2026-05-06"
        assert df.iloc[1]["calendar_date"] == "2026-05-07"
        assert df.iloc[2]["calendar_date"] == "2026-05-09"

    @patch("core.data_gateway.providers.baostock._get_session")
    def test_fetch_trade_calendar_empty(self, mock_get_session):
        """API 返回错误时返回空 DataFrame（带正确列名）。"""
        from core.data_gateway.providers.baostock import BaostockProvider

        mock_rs = MagicMock()
        mock_rs.error_msg = "fail"
        mock_session = MagicMock()
        mock_session._bs.query_trade_dates.return_value = mock_rs
        mock_get_session.return_value = mock_session

        provider = BaostockProvider()
        df = provider.fetch_trade_calendar("2026-05-01", "2026-05-31")

        assert df.empty
        assert list(df.columns) == ["calendar_date", "is_trading_day"]

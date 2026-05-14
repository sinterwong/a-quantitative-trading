# -*- coding: utf-8 -*-
"""
baostock Provider 单元测试 — mock 掉 baostock API，只测逻辑。
"""
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

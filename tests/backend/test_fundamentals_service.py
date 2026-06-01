"""验证 fetch_fundamentals 在 dividend_yield 链路全断时，不会回退到腾讯失真值。

背景: 山西汾酒 600809.SH 案例，腾讯 88-field 字段 56 返回 0.60（"动态股息率"，失真），
真实 TTM 应为 ~5.1%。修复前会无声回退到腾讯 0.60；修复后应返回 0.0 并标记 unavailable。
"""
from unittest.mock import MagicMock, patch


def test_fetch_fundamentals_does_not_fallback_to_tencent_when_fundamentals_zero():
    """当 Fundamentals.dividend_yield=0（链路全断），应明确返回 0 标记，
    而不是无声回退到腾讯 88-field 的"动态股息率"值。
    """
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "山西汾酒"
    mock_quote.pe_ttm = 14.19
    mock_quote.pb = 3.46
    mock_quote.dividend_yield = 0.60  # 腾讯失真
    mock_quote.market_cap = 1552.0
    mock_quote.price = 127.22

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 0.0  # akshare 写死 / baostock 失败
    mock_fundamentals.revenue_yoy = -9.68
    mock_fundamentals.profit_yoy = -19.03
    mock_fundamentals.roe_ttm = 12.57
    mock_fundamentals.eps_ttm = 4.41
    mock_fundamentals.ocf_to_profit = 1.53
    mock_fundamentals.industry = "白酒"
    mock_fundamentals.sector = "消费"

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("600809.SH")

    # 关键断言：不再回退到腾讯失真值
    assert result["dividend_yield"] == 0.0, (
        f"Expected 0.0 (链路全断) but got {result['dividend_yield']} "
        f"(可能回退到腾讯 88-field 失真值)"
    )
    # 必须有不可用标记
    assert result.get("dividend_yield_unavailable") is True


def test_fetch_fundamentals_uses_fundamentals_when_positive():
    """正常路径: Fundamentals.dividend_yield > 0 时优先用，不回退到腾讯。"""
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "测试股"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.5
    mock_quote.dividend_yield = 0.02
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 5.1  # 真实 TTM
    mock_fundamentals.revenue_yoy = 0.0
    mock_fundamentals.profit_yoy = 0.0
    mock_fundamentals.roe_ttm = 0.0
    mock_fundamentals.eps_ttm = 1.0
    mock_fundamentals.ocf_to_profit = 0.0
    mock_fundamentals.industry = ""
    mock_fundamentals.sector = ""

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("test.SH")

    assert result["dividend_yield"] == 5.1
    assert result.get("dividend_yield_unavailable") is False

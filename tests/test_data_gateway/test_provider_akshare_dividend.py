"""验证 akshare _fetch_a_share_fundamentals 能从 stock_zh_a_spot_em 补全 dividend_yield。

背景 (W1-6): 原实现 dividend_yield=0.0 写死, 因 stock_financial_abstract 不含此字段。
修复: 新增 get_dividend_yield_from_spot 辅助函数, 从全 A 股快照取股息率(%)。
"""
from unittest.mock import MagicMock, patch
import pandas as pd


def _make_abstract_df():
    """模拟 stock_financial_abstract 最小可用返回值(包含 EPS)。"""
    return pd.DataFrame({
        "选项": ["常用指标", "常用指标", "成长能力", "成长能力"],
        "指标": ["基本每股收益", "归母净利润", "营业总收入增长率", "归属母公司净利润增长率"],
        "20250331": [4.41, 100.0, -9.68, -19.03],
    })


def test_a_share_fundamentals_includes_dividend_yield_from_spot_em():
    """_fetch_a_share_fundamentals 应从 stock_zh_a_spot_em 补全 dividend_yield。"""
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    mock_ak.stock_financial_abstract.return_value = _make_abstract_df()
    mock_ak.stock_zh_a_spot_em.return_value = pd.DataFrame({
        "代码": ["600809", "000001", "600519"],
        "名称": ["山西汾酒", "平安银行", "贵州茅台"],
        "最新价": [127.22, 12.5, 1680.0],
        "股息率": [5.12, 3.20, 1.45],  # 关键字段: %
    })

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    # 关键断言: dividend_yield 不再是写死的 0.0
    assert result is not None
    assert result.dividend_yield == 5.12, (
        f"Expected 5.12 (from stock_zh_a_spot_em) but got {result.dividend_yield}"
    )


def test_a_share_fundamentals_handles_spot_em_failure():
    """stock_zh_a_spot_em 失败时优雅降级到 0.0（不抛异常）。"""
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    mock_ak.stock_financial_abstract.return_value = _make_abstract_df()
    mock_ak.stock_zh_a_spot_em.side_effect = Exception("network error")

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    # 降级但不崩
    assert result is not None
    assert result.dividend_yield == 0.0


def test_a_share_fundamentals_spot_em_column_name_variants():
    """列名兼容: akshare 不同版本可能是"股息率"或"股息率(%)"等。"""
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    mock_ak.stock_financial_abstract.return_value = _make_abstract_df()
    # 列名带括号变体
    mock_ak.stock_zh_a_spot_em.return_value = pd.DataFrame({
        "代码": ["600809"],
        "股息率(%)": [4.8],
    })

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    assert result is not None
    assert result.dividend_yield == 4.8

"""端到端验证: 修复后 dividend_yield 链路对真实场景行为正确。

背景 (W1-6): 修复山西汾酒 600809.SH 报告"股息率 0.60%"(腾讯失真) vs
真实 ~5.1% 的数据错误。本测试套件覆盖 4 个修复层(Layer 1/2/3/4)的端到端契约。

不依赖真实网络 — 用 mock 模拟:
- 腾讯 quote.dividend_yield (失真值 0.60)
- akshare Fundamentals.dividend_yield (真实值 5.12, 或 baostock 兜底 4.75)
- baostock 分红记录 (4 条)
"""
from unittest.mock import MagicMock, patch
import pytest


# ── 测试 1: Layer 4 - 后端不再回退到腾讯失真值 ──────────────────────────

def test_backend_does_not_fallback_to_tencent_distortion():
    """山西汾酒场景: 腾讯返回 0.60 (失真), 后端必须用 Fundamentals 真实值,
    永不回退到腾讯。

    修复前: backend_dy = 0.60 (回退到 q.dividend_yield)
    修复后: backend_dy = 5.12 (Fundamentals 提供的真实值)
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

    # 模拟 akshare Layer 1 补全后的真实股息率
    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 5.12  # 来自 stock_zh_a_spot_em
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

    assert result["dividend_yield"] == 5.12, (
        f"应取 Fundamentals 真实值 5.12, 实际={result['dividend_yield']} "
        f"(回退到腾讯失真 0.60 = bug 未修复)"
    )
    assert result["dividend_yield"] != 0.60, "绝不能等于腾讯失真值"
    assert result["dividend_yield_unavailable"] is False


# ── 测试 2: Layer 4 - unavailable 显式标记 ──────────────────────────────

def test_backend_marks_unavailable_when_link_fully_broken():
    """当 Fundamentals.dividend_yield=0 (akshare/baostock 全部失败),
    后端必须显式标记 unavailable=True, 不回退到腾讯。
    """
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "test"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.0
    mock_quote.dividend_yield = 0.60  # 腾讯失真
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 0.0  # 链路全断
    mock_fundamentals.revenue_yoy = 0
    mock_fundamentals.profit_yoy = 0
    mock_fundamentals.roe_ttm = 0
    mock_fundamentals.eps_ttm = 1
    mock_fundamentals.ocf_to_profit = 0
    mock_fundamentals.industry = ""
    mock_fundamentals.sector = ""

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("test.SH")

    assert result["dividend_yield"] == 0.0
    assert result["dividend_yield_unavailable"] is True
    assert result["dividend_yield"] != mock_quote.dividend_yield, (
        "链路全断时绝不能回退到腾讯失真值"
    )


# ── 测试 3: Layer 4 - fundamentals=None 时也走 unavailable ─────────────

def test_backend_unavailable_when_fundamentals_none():
    """Fundamentals 整个对象不可用时, dividend_yield=0, unavailable=True。"""
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "test"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.0
    mock_quote.dividend_yield = 0.02  # 腾讯有值
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = None  # 整个不可用

        result = fetch_fundamentals("test.SH")

    assert result["dividend_yield"] == 0.0
    assert result["dividend_yield_unavailable"] is True
    assert result["dividend_yield"] != mock_quote.dividend_yield


# ── 测试 4: Layer 1 - akshare 补字段的契约 (via Provider 接口) ──────────

def test_akshare_a_share_fundamentals_calls_spot_em():
    """Layer 1 修复: _fetch_a_share_fundamentals 应调用 stock_zh_a_spot_em
    来补全 dividend_yield, 而非写死 0.0。
    """
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    mock_ak.stock_financial_abstract.return_value = MagicMock(
        **{
            "empty": False,
            "columns": MagicMock(__getitem__=lambda *a: "20250331"),
            "__getitem__": lambda *a: None,
        }
    )
    # 上面 mock 复杂, 改用更直接的方式: 模拟两个数据源
    import pandas as pd
    mock_ak.stock_financial_abstract.return_value = pd.DataFrame({
        "选项": ["常用指标", "成长能力", "成长能力"],
        "指标": ["基本每股收益", "营业总收入增长率", "归属母公司净利润增长率"],
        "20250331": [4.41, -9.68, -19.03],
    })
    mock_ak.stock_zh_a_spot_em.return_value = pd.DataFrame({
        "代码": ["600809"],
        "股息率": [5.12],
    })

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    assert result is not None
    assert result.dividend_yield == 5.12, (
        f"akshare Layer 1 应从 stock_zh_a_spot_em 取 5.12, 实际={result.dividend_yield}"
    )
    # 关键: 必须调用过 stock_zh_a_spot_em
    assert mock_ak.stock_zh_a_spot_em.called, (
        "Layer 1 修复要求 _fetch_a_share_fundamentals 调用 stock_zh_a_spot_em"
    )


# ── 测试 5: Layer 2 - 腾讯 dividend_yield 权威 ≤ akshare ───────────────

def test_tencent_dividend_yield_authority_is_low():
    """Layer 2 修复: 腾讯 dividend_yield 权威必须 ≤ 0.5, 防止 quote 端
    MERGE_FIELDS 合并时压过其他源(若未来其他 provider 也提供此字段)。

    注: akshare FUNDAMENTALS 端未单独声明 dividend_yield 权威, 走默认 0.0,
    腾讯 0.5 < 默认 0.0 仍然可能胜出, 但 backend.services.fundamentals Layer 4
    修复已保证腾讯值永远不会被回退, 双层防御。
    """
    from core.data_gateway.providers.tencent import TencentProvider
    from core.data_gateway.capabilities import Capability

    tencent = TencentProvider()
    tencent_auth = (
        tencent.field_authority()
        .get(Capability.QUOTE, {})
        .get("dividend_yield")
    )

    assert tencent_auth is not None, "腾讯 quote 应声明 dividend_yield 权威"
    assert tencent_auth <= 0.5, (
        f"腾讯 dividend_yield 权威应 ≤ 0.5 (山西汾酒案例权威从 1.2 降到 0.5), "
        f"实际={tencent_auth}"
    )


# ── 测试 6: Layer 3 - 兜底失败时显式 warning ──────────────────────────

def test_gateway_logs_warning_when_dividend_records_empty():
    """Layer 3 修复: dividend() 返回空 (无分红或 baostock 不可用),
    应 logger.warning 而非静默 pass。
    """
    from core.data_gateway import get_gateway

    gw = get_gateway()
    gw._cache.clear()
    f = MagicMock()
    f.dividend_yield = 0.0
    f.pe_ttm = 14.0
    f.pb = 3.0
    f.eps_ttm = 4.0

    with patch.object(gw, "_route", return_value=(f, {})):
        with patch.object(gw, "quote", return_value=MagicMock(price=127.0)):
            with patch.object(gw, "dividend", return_value=[]):
                with patch.object(gw, "_calc_ttm_dividend_yield", return_value=0.0):
                    with patch("core.data_gateway.gateway.logger") as mock_log:
                        try:
                            gw.fundamentals("600809.SH")
                        except Exception:
                            pass

                        assert mock_log.warning.called, (
                            "dividend() 返回空时, 应有 logger.warning 提示"
                        )


def test_gateway_logs_warning_when_baostock_missing():
    """Layer 3 修复: baostock 不可用导致 dividend() 抛异常时, 应 logger.warning
    提示需 pip install baostock。
    """
    from core.data_gateway import get_gateway

    gw = get_gateway()
    gw._cache.clear()
    f = MagicMock()
    f.dividend_yield = 0.0
    f.pe_ttm = 14.0
    f.pb = 3.0
    f.eps_ttm = 4.0

    with patch.object(gw, "_route", return_value=(f, {})):
        with patch.object(gw, "quote", return_value=MagicMock(price=127.0)):
            with patch.object(
                gw, "dividend",
                side_effect=Exception("No module named 'baostock'"),
            ):
                with patch("core.data_gateway.gateway.logger") as mock_log:
                    try:
                        gw.fundamentals("600809.SH")
                    except Exception:
                        pass

                    warning_calls = mock_log.warning.call_args_list
                    assert len(warning_calls) > 0, "应 logger.warning"
                    all_warn_str = " ".join(str(c) for c in warning_calls)
                    assert "baostock" in all_warn_str.lower(), (
                        f"warning 应提示 baostock, 实际: {all_warn_str[:200]}"
                    )


# ── 测试 7: 防御性 - EPS/ROE/revenue_yoy 等其他字段不受影响 ─────────────

def test_other_fundamental_fields_unaffected_by_dividend_fix():
    """确保 dividend_yield 修复没有副作用, 其他字段仍正常返回。"""
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "test"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.5
    mock_quote.dividend_yield = 0.02
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 3.5
    mock_fundamentals.revenue_yoy = 5.0
    mock_fundamentals.profit_yoy = 10.0
    mock_fundamentals.roe_ttm = 15.0
    mock_fundamentals.eps_ttm = 1.0
    mock_fundamentals.ocf_to_profit = 1.2
    mock_fundamentals.industry = "Test"
    mock_fundamentals.sector = "Sector"

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("test.SH")

    assert result["pe"] == 10.0
    assert result["pb"] == 1.5
    assert result["dividend_yield"] == 3.5
    assert result["dividend_yield_unavailable"] is False
    assert result["revenue_yoy"] == 5.0
    assert result["profit_yoy"] == 10.0
    assert result["roe_ttm"] == 15.0
    assert result["eps_ttm"] == 1.0
    assert result["ocf_to_profit"] == 1.2
    assert result["industry"] == "Test"
    assert result["sector"] == "Sector"

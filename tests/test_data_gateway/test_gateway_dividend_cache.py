"""验证 gw.fundamentals() 触发 dividend_yield 兜底时，自动预热 dividend() 缓存，
避免分析时冷启动延迟；baostock 不可用时显式 logger.warning。

背景 (W1-6 Layer 3 增强): 原实现 dividend_yield<=0 兜底时静默 pass，
baostock 不可用时无任何提示，运维难定位。
"""
from unittest.mock import MagicMock, patch


def _make_merged_dy(dy: float = 0.0) -> MagicMock:
    """构造一个最小可用的 merged Fundamentals mock。"""
    m = MagicMock()
    m.dividend_yield = dy
    m.pe_ttm = 14.0
    m.pb = 3.0  # 必须显式赋值, 否则 '<=' 比较 MagicMock 抛 TypeError
    m.eps_ttm = 4.0
    m.roe_ttm = 12.0
    m.revenue_yoy = -9.0
    m.profit_yoy = -19.0
    m.ocf_to_profit = 1.5
    m.industry = ""
    m.sector = ""
    return m


def test_fundamentals_warms_dividend_cache_on_fallback():
    """当 fundamentals.dividend_yield<=0 触发兜底时，应先调一次 dividend() 预热缓存。"""
    from core.data_gateway import get_gateway

    gw = get_gateway()
    # 清缓存, 确保走完整路径
    gw._cache.clear()
    f = _make_merged_dy(dy=0.0)

    with patch.object(gw, "_route", return_value=(f, {})):
        with patch.object(gw, "quote", return_value=MagicMock(price=127.0)):
            with patch.object(gw, "dividend", return_value=[]) as mock_div:
                with patch.object(gw, "_calc_ttm_dividend_yield", return_value=0.0):
                    try:
                        gw.fundamentals("600809.SH")
                    except Exception:
                        pass

                    assert mock_div.called, (
                        "dividend() 应在兜底路径中被调用以预热缓存"
                    )


def test_fundamentals_logs_warning_when_baostock_missing():
    """baostock 不可用导致兜底失败时，应 logger.warning 而非静默 pass。"""
    from core.data_gateway import get_gateway

    gw = get_gateway()
    gw._cache.clear()
    f = _make_merged_dy(dy=0.0)

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
                    assert len(warning_calls) > 0, (
                        "应 logger.warning 提示 baostock 不可用，便于运维定位"
                    )
                    all_warn_str = " ".join(str(c) for c in warning_calls)
                    assert "baostock" in all_warn_str.lower() or "dividend" in all_warn_str.lower(), (
                        f"warning 内容应提到 baostock/dividend, 实际: {all_warn_str[:200]}"
                    )


def test_fundamentals_logs_warning_when_dividend_records_empty():
    """dividend() 成功但返回空（无分红或 baostock 取不到）时，也应 warning。"""
    from core.data_gateway import get_gateway

    gw = get_gateway()
    gw._cache.clear()
    f = _make_merged_dy(dy=0.0)

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
                            "dividend() 返回空时也应有 warning 提示"
                        )

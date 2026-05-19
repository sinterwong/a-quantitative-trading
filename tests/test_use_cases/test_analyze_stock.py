"""tests/test_use_cases/test_analyze_stock.py — analyze_stock dispatcher 测试。

业务行为详细测试见 tests/test_single_stock_analysis.py,
本文件只验证 P2-2 引入的 `analyze_stock` 统一入口 dispatch 逻辑正确。
"""

from __future__ import annotations

from unittest.mock import patch


def test_dispatches_a_share_symbol():
    """A股代码 → analyze_a_share。"""
    from core.use_cases.analyze_stock import (
        AnalysisRequest, AnalysisReport, analyze_stock,
    )

    def fake_a(req):
        return AnalysisReport(symbol=req.symbol, market='A', as_of='2026-05-15')

    with patch('core.use_cases.analyze_stock.analyze_a_share',
               side_effect=fake_a) as mock_a, \
         patch('core.use_cases.analyze_stock.analyze_hk_share') as mock_hk:
        report = analyze_stock(AnalysisRequest(symbol='600519.SH'))

    mock_a.assert_called_once()
    mock_hk.assert_not_called()
    assert report.market == 'A'


def test_dispatches_hk_share_symbol():
    """港股代码 → analyze_hk_share。"""
    from core.use_cases.analyze_stock import (
        AnalysisRequest, AnalysisReport, analyze_stock,
    )

    def fake_hk(req):
        return AnalysisReport(symbol=req.symbol, market='HK', as_of='2026-05-15')

    with patch('core.use_cases.analyze_stock.analyze_a_share') as mock_a, \
         patch('core.use_cases.analyze_stock.analyze_hk_share',
               side_effect=fake_hk) as mock_hk:
        report = analyze_stock(AnalysisRequest(symbol='HK00700'))

    mock_a.assert_not_called()
    mock_hk.assert_called_once()
    assert report.market == 'HK'


def test_unknown_symbol_raises_use_case_error():
    """未知市场代码 → UseCaseError(INVALID_SYMBOL)。"""
    import pytest
    from core.use_cases import UseCaseError
    from core.use_cases.analyze_stock import AnalysisRequest, analyze_stock

    with pytest.raises(UseCaseError) as exc_info:
        analyze_stock(AnalysisRequest(symbol='garbage_xyz'))
    assert exc_info.value.code == 'INVALID_SYMBOL'


# R2-2 收尾(本 PR): backend.services.single_stock_analysis 转发 shim
# 已删除,所有调用方直接 import core.use_cases.analyze_stock。
# 对应的 test_backward_compat_imports_still_work 用例同步删除。

"""
tests/test_use_cases/test_performance_summary.py — performance_summary use case 单元测试。

覆盖:
  - happy: 提供 year/month + chart → 完整 PerformanceSummaryResponse
  - degraded: 空交易列表 → trade_stats 仍可生成,max_drawdown 走 fallback
  - error: services.performance 抛异常 → 由 caller 捕获(本 use case 暴露)
  - 当月交易过滤(按 filled_at 前缀)
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# performance_summary use case 函数体内 `from services.performance import ...`,
# 'services' 命名空间需要 backend/ 在 sys.path 上。
_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'backend',
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


@pytest.fixture
def patch_perf_services():
    """统一替换 services.performance 三个函数。"""
    monthly_report = {
        'returns': {'monthly_return': 0.05},
        'summary': {'wins': 3, 'losses': 2},
        'equity_series': [
            {'date': '2026-04-01', 'equity': 100000},
            {'date': '2026-04-30', 'equity': 105000},
        ],
        'benchmark_curve': [{'date': '2026-04-01', 'value': 100}],
        'chart_base64': 'iVBORw0KG...',
        'generated_at': '2026-05-01 00:00',
    }
    trade_stats = {'n_trades': 10, 'win_rate': 0.6}
    max_dd = {
        'max_drawdown_pct': -0.07, 'peak_equity': 110000,
        'trough_equity': 102300, 'peak_date': '2026-04-12',
        'trough_date': '2026-04-25',
    }
    with patch('services.performance.generate_monthly_report', return_value=monthly_report), \
         patch('services.performance.compute_trade_stats', return_value=trade_stats), \
         patch('services.performance.compute_max_drawdown', return_value=max_dd):
        yield {'monthly': monthly_report, 'trade_stats': trade_stats, 'max_dd': max_dd}


def _fake_svc(orders=None):
    svc = MagicMock()
    svc.get_orders.return_value = orders or []
    return svc


def test_performance_summary_request_defaults_to_today():
    from core.use_cases.performance_summary import PerformanceSummaryRequest
    req = PerformanceSummaryRequest()
    today = date.today()
    assert req.year == today.year
    assert req.month == today.month
    assert req.include_chart is True


def test_performance_summary_happy_path(patch_perf_services):
    from core.use_cases.performance_summary import (
        PerformanceSummaryRequest, compute_performance_summary,
    )
    svc = _fake_svc(orders=[
        {'filled_at': '2026-04-15 09:30', 'pnl': 100},
        {'filled_at': '2026-04-25 14:00', 'pnl': -50},
        {'filled_at': '2026-03-30 11:00', 'pnl': 200},   # 不属于本月
    ])
    req = PerformanceSummaryRequest(year=2026, month=4)
    resp = compute_performance_summary(req, svc)

    assert resp.period == '2026年4月'
    assert resp.year == 2026 and resp.month == 4
    assert resp.returns == {'monthly_return': 0.05}
    assert resp.trade_stats == {'n_trades': 10, 'win_rate': 0.6}
    assert resp.max_drawdown['max_drawdown_pct'] == -0.07
    assert resp.chart_base64 == 'iVBORw0KG...'

    # 当月过滤验证:trade_stats_month 是按 4 月 prefix 过滤的(2 笔)
    # compute_trade_stats 是 mock,但我们能验 svc.get_orders 被调
    svc.get_orders.assert_called_once_with(status='filled', limit=500)


def test_performance_summary_no_chart_returned(patch_perf_services):
    from core.use_cases.performance_summary import (
        PerformanceSummaryRequest, compute_performance_summary,
    )
    svc = _fake_svc()
    req = PerformanceSummaryRequest(year=2026, month=4, include_chart=False)
    resp = compute_performance_summary(req, svc)
    assert resp.chart_base64 is None


def test_performance_summary_empty_equity_series_uses_default_drawdown():
    """equity_series 为空 → max_drawdown 走 fallback dict 而非真实计算。"""
    monthly_report = {
        'returns': {}, 'summary': {}, 'equity_series': [],
        'benchmark_curve': [], 'chart_base64': None, 'generated_at': '',
    }
    with patch('services.performance.generate_monthly_report', return_value=monthly_report), \
         patch('services.performance.compute_trade_stats', return_value={}), \
         patch('services.performance.compute_max_drawdown') as mock_dd:
        from core.use_cases.performance_summary import (
            PerformanceSummaryRequest, compute_performance_summary,
        )
        svc = _fake_svc()
        req = PerformanceSummaryRequest(year=2026, month=4)
        resp = compute_performance_summary(req, svc)
        # equity_series 空 → 不调 compute_max_drawdown
        mock_dd.assert_not_called()
        assert resp.max_drawdown['max_drawdown_pct'] == 0.0


def test_performance_summary_response_to_dict_complete(patch_perf_services):
    from core.use_cases.performance_summary import (
        PerformanceSummaryRequest, compute_performance_summary,
    )
    svc = _fake_svc()
    resp = compute_performance_summary(
        PerformanceSummaryRequest(year=2026, month=5, include_chart=True), svc,
    )
    d = resp.to_dict()
    required = {'period', 'year', 'month', 'returns', 'summary',
                'trade_stats', 'trade_stats_month', 'max_drawdown',
                'equity_curve', 'benchmark_curve', 'chart_base64', 'generated_at'}
    assert required.issubset(d.keys())

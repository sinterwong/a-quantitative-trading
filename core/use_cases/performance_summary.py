"""
core/use_cases/performance_summary.py — 绩效聚合 use case (P2-8 收尾)

把 backend/api.py 中 /performance/summary 端点聚合三个 services.performance
函数(generate_monthly_report / compute_trade_stats / compute_max_drawdown)
的逻辑下沉到本层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass
class PerformanceSummaryRequest:
    year: int = 0   # 0 → today.year
    month: int = 0  # 0 → today.month
    include_chart: bool = True

    def __post_init__(self) -> None:
        today = date.today()
        self.year = self.year or today.year
        self.month = self.month or today.month


@dataclass
class PerformanceSummaryResponse:
    period: str
    year: int
    month: int
    returns: Dict[str, Any]
    summary: Dict[str, Any]
    trade_stats: Dict[str, Any]
    trade_stats_month: Dict[str, Any]
    max_drawdown: Dict[str, Any]
    equity_curve: List[Any]
    benchmark_curve: List[Any]
    chart_base64: Optional[str] = None
    generated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        returns = self.returns or {}
        trade_stats = self.trade_stats or {}
        # Flatten nested structures into top-level keys for UI convenience.
        # annual_return / sharpe are calculated here; if missing from trade_stats
        # the field is simply absent (UI falls back to "—").
        return {
            'period': self.period,
            'year': self.year,
            'month': self.month,
            # flatten returns
            'total_return_pct': returns.get('total_return_pct'),
            'annual_return': returns.get('annual_return'),
            'initial_capital': returns.get('initial_capital'),
            'total_equity': returns.get('total_equity'),
            # pass-through
            'summary': self.summary,
            'trade_stats': self.trade_stats,
            'trade_stats_month': self.trade_stats_month,
            'max_drawdown': self.max_drawdown,
            'returns': self.returns,   # 保留原始嵌套结构（测试依赖）
            'equity_curve': self.equity_curve,
            'benchmark_curve': self.benchmark_curve,
            'chart_base64': self.chart_base64,
            'generated_at': self.generated_at,
            # flatten trade-stats scalars the UI expects
            'sharpe': trade_stats.get('sharpe'),
            'max_drawdown_pct': (self.max_drawdown or {}).get('max_drawdown_pct'),
        }


def compute_performance_summary(req: PerformanceSummaryRequest,
                                portfolio_svc: Any) -> PerformanceSummaryResponse:
    """聚合月度报告 + 全量交易统计 + 当月交易统计 + 最大回撤。"""
    from services.performance import (
        generate_monthly_report,
        compute_trade_stats,
        compute_max_drawdown,
    )

    report = generate_monthly_report(year=req.year, month=req.month,
                                     include_chart=req.include_chart)
    trades = portfolio_svc.get_orders(status='filled', limit=500)
    month_str = f"{req.year}-{req.month:02d}"
    month_trades = [t for t in trades if (t.get('filled_at') or '').startswith(month_str)]

    equity_series = report.get('equity_series', [])
    max_dd = compute_max_drawdown(equity_series) if equity_series else {
        'max_drawdown_pct': 0.0, 'peak_equity': 0,
        'trough_equity': 0, 'peak_date': '', 'trough_date': '',
    }

    return PerformanceSummaryResponse(
        period=f"{req.year}年{req.month}月",
        year=req.year,
        month=req.month,
        returns=report.get('returns', {}),
        summary=report.get('summary', {}),
        trade_stats=compute_trade_stats(trades),
        trade_stats_month=compute_trade_stats(month_trades),
        max_drawdown=max_dd,
        equity_curve=equity_series[-30:],
        benchmark_curve=report.get('benchmark_curve', [])[-30:],
        chart_base64=report.get('chart_base64') if req.include_chart else None,
        generated_at=report.get('generated_at'),
    )

"""
core/use_cases/system_health.py — 系统健康度 use case (P2-8 批次 4)

把 backend/api.py 中 /analysis/health 端点的简单规则汇总下沉到本层:
持仓 / 现金占比 / 浮亏 / 最近一次分析时间 → OK | WARN | CRITICAL。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


CASH_RATIO_WARN = 0.05      # 现金占比 <5% 触发 WARN
PNL_WARN_PCT = -0.05        # 未实现亏损 / equity < -5% → WARN
PNL_CRIT_PCT = -0.10        # 未实现亏损 / equity < -10% → CRITICAL


@dataclass
class SystemHealthReport:
    level: str               # 'OK' | 'WARN' | 'CRITICAL'
    reasons: List[str] = field(default_factory=list)
    n_positions: int = 0
    total_unrealized_pnl: float = 0.0
    cash: float = 0.0
    equity: float = 0.0
    latest_analysis: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'level': self.level,
            'reasons': self.reasons,
            'n_positions': self.n_positions,
            'total_unrealized_pnl': round(self.total_unrealized_pnl, 2),
            'cash': round(self.cash, 2),
            'equity': round(self.equity, 2),
            'latest_analysis': self.latest_analysis,
        }


def compute_system_health(portfolio_svc: Any,
                          analysis_dir: str = '') -> SystemHealthReport:
    """
    汇总系统健康度。

    portfolio_svc 需提供 get_portfolio_summary() / get_positions()。
    analysis_dir 为空时不查最近分析时间(返回 latest_analysis=None)。
    """
    summary = portfolio_svc.get_portfolio_summary()
    positions = portfolio_svc.get_positions()
    n_positions = len(positions)
    total_pnl = sum(float(p.get('unrealized_pnl', 0) or 0) for p in positions)
    cash = float(summary.get('cash', 0) or 0)
    equity = float(summary.get('total_equity', 0) or 0)

    level = 'OK'
    reasons: List[str] = []

    if equity > 0 and cash / equity < CASH_RATIO_WARN:
        level = 'WARN'
        reasons.append(f'现金占比仅 {cash/equity*100:.1f}%，低于 {CASH_RATIO_WARN*100:.0f}%')

    if equity > 0 and total_pnl < PNL_WARN_PCT * equity:
        level = 'WARN'
        reasons.append(f'未实现亏损 {total_pnl:.0f} 超过总权益 {abs(PNL_WARN_PCT)*100:.0f}%')

    if equity > 0 and total_pnl < PNL_CRIT_PCT * equity:
        level = 'CRITICAL'
        reasons.append(f'未实现亏损 {total_pnl:.0f} 超过总权益 {abs(PNL_CRIT_PCT)*100:.0f}%')

    latest_analysis: Optional[str] = None
    if analysis_dir and os.path.isdir(analysis_dir):
        try:
            files = sorted(os.listdir(analysis_dir), reverse=True)
            if files:
                latest_analysis = files[0]
        except Exception:
            pass

    return SystemHealthReport(
        level=level,
        reasons=reasons,
        n_positions=n_positions,
        total_unrealized_pnl=total_pnl,
        cash=cash,
        equity=equity,
        latest_analysis=latest_analysis,
    )

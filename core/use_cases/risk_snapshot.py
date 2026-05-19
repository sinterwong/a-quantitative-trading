"""
core/use_cases/risk_snapshot.py — 组合风控快照 use case (P2-8 收尾)

把 backend/api.py 中 /risk/status 内联越权访问 monitor._peak_equity / _dd_warn
等私有属性的逻辑下沉到本层。返回 RiskSnapshot dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RiskSnapshot:
    total_equity: float = 0.0
    peak_equity: float = 0.0
    current_drawdown: float = 0.0
    dd_warn_threshold: float = 0.0
    dd_stop_threshold: float = 0.0
    risk_warn_fired: bool = False
    risk_stop_fired: bool = False
    kelly_pct: Optional[float] = None
    position_count: int = 0
    sector_exposure: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_equity': round(self.total_equity, 2),
            'peak_equity': round(self.peak_equity, 2),
            'current_drawdown': round(self.current_drawdown, 4),
            'dd_warn_threshold': self.dd_warn_threshold,
            'dd_stop_threshold': self.dd_stop_threshold,
            'risk_warn_fired': self.risk_warn_fired,
            'risk_stop_fired': self.risk_stop_fired,
            'kelly_pct': round(self.kelly_pct, 4) if self.kelly_pct is not None else None,
            'position_count': self.position_count,
            'sector_exposure': self.sector_exposure,
        }


def compute_sector_exposure(positions: List[Dict[str, Any]]) -> Dict[str, float]:
    """按 sector 字段聚合持仓市值占比。"""
    sector_mv: Dict[str, float] = {}
    total = 0.0
    for p in positions:
        mv = float(p.get('shares', 0)) * float(p.get('current_price', 0) or 0)
        total += mv
        sector = p.get('sector', 'unknown')
        sector_mv[sector] = sector_mv.get(sector, 0.0) + mv
    if total <= 0:
        return {}
    return {k: round(v / total, 4) for k, v in sector_mv.items()}


def get_risk_snapshot(portfolio_svc: Any, monitor: Optional[Any] = None) -> RiskSnapshot:
    """
    计算组合风控快照。

    monitor 为可选 IntradayMonitor;若提供,从其 get_status() 读取
    peak_equity / dd_warn / dd_stop / risk_*_fired / kelly_pct,避免端点
    直接触碰 monitor 私有属性。
    """
    positions = portfolio_svc.get_positions()
    summary = portfolio_svc.get_portfolio_summary(refresh_prices_now=True)
    current_equity = float(summary.get('total_equity', 0) or 0)
    sector_exposure = compute_sector_exposure(positions)

    snap = RiskSnapshot(
        total_equity=current_equity,
        position_count=len(positions),
        sector_exposure=sector_exposure,
    )

    if monitor is not None:
        try:
            status = monitor.get_status()
            snap.peak_equity = float(status.get('peak_equity', 0) or 0)
            snap.dd_warn_threshold = float(status.get('dd_warn', 0) or 0)
            snap.dd_stop_threshold = float(status.get('dd_stop', 0) or 0)
            snap.risk_warn_fired = bool(status.get('risk_warn_fired', False))
            snap.risk_stop_fired = bool(status.get('risk_stop_fired', False))
            snap.kelly_pct = status.get('kelly_pct')
            if snap.peak_equity > 0:
                snap.current_drawdown = 1 - (current_equity / snap.peak_equity)
        except Exception:
            pass

    return snap

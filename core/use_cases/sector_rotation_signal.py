"""
core/use_cases/sector_rotation_signal.py — 行业轮动信号 use case (P2-8 批次 4)

把 backend/api.py 中 /analysis/sector_rotation 端点的内联业务逻辑下沉到本层:
拉行业 ETF 价格 → 调 SectorRotationStrategy → 落 signals 表。

输入:SectorRotationRequest
输出:SectorRotationResponse
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import UseCaseError


@dataclass
class SectorRotationRequest:
    top_n: int = 3
    lookback_days: int = 60
    rebalance_days: int = 21
    momentum_method: str = 'return'   # 'return' | 'sharpe'
    current_holdings: List[str] = field(default_factory=list)


@dataclass
class SectorRotationResponse:
    rebalance_date: str
    buy: List[str]
    sell: List[str]
    hold: List[str]
    scores: Dict[str, float]
    top_n: int
    universe_size: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            'rebalance_date': self.rebalance_date,
            'buy': self.buy,
            'sell': self.sell,
            'hold': self.hold,
            'scores': self.scores,
            'top_n': self.top_n,
            'universe_size': self.universe_size,
        }


def run_sector_rotation(req: SectorRotationRequest,
                        portfolio_svc: Optional[Any] = None,
                        *,
                        data_layer: Optional[Any] = None) -> SectorRotationResponse:
    """
    执行行业轮动信号生成。

    Args:
        req: 输入参数。
        portfolio_svc: 若传入,则 buy 信号会被记录到 signals 表
            (供 IntradayMonitor 复用)。
        data_layer: 可选——直接注入数据层(测试)。
            为 None 时回退到 :func:`core.data_layer.get_data_layer`。
    """
    from core.strategies.sector_rotation import SectorRotationStrategy, DEFAULT_SECTOR_ETFS

    strategy = SectorRotationStrategy(
        top_n=req.top_n,
        lookback_days=req.lookback_days,
        rebalance_days=req.rebalance_days,
        momentum_method=req.momentum_method,
    )

    if data_layer is None:
        from core.data_layer import get_data_layer
        data_layer = get_data_layer()
    dl = data_layer
    price_data: Dict[str, Any] = {}
    for sym in DEFAULT_SECTOR_ETFS:
        df = dl.get_bars(sym, days=max(req.lookback_days + 20, 90))
        if df is not None and not df.empty:
            price_data[sym] = df

    if not price_data:
        raise UseCaseError('无法获取行业 ETF 行情数据', 'DATA_UNAVAILABLE')

    signal = strategy.latest_signal(price_data, current_holdings=req.current_holdings)

    if portfolio_svc is not None:
        for sym in signal.buy:
            portfolio_svc.record_signal(
                sym, 'BUY', signal.scores.get(sym, 0),
                f'行业轮动买入: 动量分 {signal.scores.get(sym, 0):.4f}',
            )

    return SectorRotationResponse(
        rebalance_date=signal.rebalance_date,
        buy=signal.buy,
        sell=signal.sell,
        hold=signal.hold,
        scores=signal.scores,
        top_n=signal.top_n,
        universe_size=len(price_data),
    )

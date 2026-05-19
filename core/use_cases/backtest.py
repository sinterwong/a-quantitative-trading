"""
core/use_cases/backtest.py — 回测 use case (P2-5)

把 BacktestEngine 的"加载数据 + 加策略 + 跑回测 + 提取关键指标"封装为
统一入口。所有 caller (CLI / REST / UI) 走这里,不再各自拼装。

设计:
- 数据从 DataGateway 拉取(默认),也可由 caller 显式注入(测试场景)
- 策略由 (factor_name, threshold, params) 描述,内部用 FactorRegistry 创建
- 输出 dataclass 保留绩效指标 + 必要的回测元数据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from core.use_cases import UseCaseError


@dataclass
class StrategySpec:
    """单个策略描述(因子 + 阈值 + 参数)。"""
    factor_name: str
    threshold: float = 1.0
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestRequest:
    symbol: str
    start: Optional[str] = None           # 'YYYY-MM-DD'
    end: Optional[str] = None
    days: int = 252
    strategies: List[StrategySpec] = field(default_factory=list)
    initial_equity: float = 100_000.0
    commission_rate: float = 0.0003
    slippage_bps: float = 5.0

    # 显式注入数据(测试用),为 None 时由 use case 从 DataGateway 拉取
    injected_data: Optional[pd.DataFrame] = None


@dataclass
class BacktestResponse:
    symbol: str
    n_bars: int
    n_trades: int
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    factor_ic: float
    factor_ir: float
    summary_text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'n_bars': self.n_bars,
            'n_trades': self.n_trades,
            'total_return': round(self.total_return, 6),
            'annual_return': round(self.annual_return, 6),
            'sharpe': round(self.sharpe, 4),
            'max_drawdown_pct': round(self.max_drawdown_pct, 6),
            'win_rate': round(self.win_rate, 4),
            'profit_factor': round(self.profit_factor, 4),
            'factor_ic': round(self.factor_ic, 6),
            'factor_ir': round(self.factor_ir, 6),
            'summary': self.summary_text,
        }


def _fetch_data(req: BacktestRequest) -> pd.DataFrame:
    """通过 DataGateway 拉取历史 K 线。"""
    if req.injected_data is not None:
        if req.injected_data.empty:
            raise UseCaseError(
                f'injected data is empty for {req.symbol}',
                code='DATA_UNAVAILABLE',
            )
        return req.injected_data
    try:
        from core.data_gateway import get_gateway
        df = get_gateway().kline(
            req.symbol, interval='daily',
            days=req.days, limit=req.days,
        )
    except Exception as exc:
        raise UseCaseError(
            f'failed to fetch kline for {req.symbol}: {exc}',
            code='DATA_UNAVAILABLE',
        )
    if df is None or df.empty:
        raise UseCaseError(
            f'no kline data returned for {req.symbol}',
            code='DATA_UNAVAILABLE',
        )

    # 列名归一(provider 间差异:date / timestamp)
    from core.data_gateway import normalize_kline_index
    df = normalize_kline_index(df)

    # 时间窗口过滤
    if req.start:
        df = df[df.index >= pd.Timestamp(req.start)]
    if req.end:
        df = df[df.index <= pd.Timestamp(req.end)]

    return df.sort_index()


def run_backtest(req: BacktestRequest) -> BacktestResponse:
    """跑一个回测,返回结构化结果。

    Raises
    ------
    UseCaseError(code='DATA_UNAVAILABLE')
        无可用数据
    UseCaseError(code='NO_STRATEGY')
        未指定任何策略
    UseCaseError(code='UNKNOWN_FACTOR')
        策略中引用了未注册的因子名
    """
    if not req.strategies:
        raise UseCaseError(
            'no strategies specified for backtest', code='NO_STRATEGY',
        )

    df = _fetch_data(req)

    from core.backtest_engine import BacktestEngine, BacktestConfig
    from core.factor_registry import registry

    cfg = BacktestConfig(
        initial_equity=req.initial_equity,
        commission_rate=req.commission_rate,
        slippage_bps=req.slippage_bps,
    )
    engine = BacktestEngine(config=cfg)
    engine.load_data(req.symbol, df)

    for spec in req.strategies:
        try:
            factor = registry.create(spec.factor_name, **spec.params)
        except Exception as exc:
            raise UseCaseError(
                f'unknown or invalid factor {spec.factor_name}: {exc}',
                code='UNKNOWN_FACTOR',
            )
        engine.add_strategy(factor, threshold=spec.threshold)

    result = engine.run()

    return BacktestResponse(
        symbol=req.symbol,
        n_bars=len(df),
        n_trades=result.n_trades,
        total_return=result.total_return,
        annual_return=result.annual_return,
        sharpe=result.sharpe,
        max_drawdown_pct=result.max_drawdown_pct,
        win_rate=result.win_rate,
        profit_factor=result.profit_factor,
        factor_ic=result.factor_ic,
        factor_ir=result.factor_ir,
        summary_text=result.summary(),
    )


__all__ = [
    'StrategySpec',
    'BacktestRequest',
    'BacktestResponse',
    'run_backtest',
]

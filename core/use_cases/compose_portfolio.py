"""
core/use_cases/compose_portfolio.py — 组合优化建议 use case (P2-6)

输入持仓现状 + universe + 风险参数,输出建议权重(PortfolioAdvice)。
本 use case 仅产出"建议",不下单 - 实际下单由 caller 调 OMS / Broker。

支持的方法(来自 PortfolioOptimizer):
- min_variance:全局最小方差
- max_sharpe:最大夏普(切线组合)
- risk_parity:等风险贡献
- max_diversification:最大分散化比率
- equal_weight:等权基准

数据:
- 默认从 DataGateway 拉每只 symbol 的日 K,构造收益率矩阵
- 可显式注入 returns(测试)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from core.use_cases import UseCaseError

logger = logging.getLogger(__name__)


@dataclass
class ComposePortfolioRequest:
    """组合优化输入。"""
    universe: List[str]                  # 资产代码列表(≥2)
    method: str = 'min_variance'         # min_variance / max_sharpe / risk_parity /
                                         # max_diversification / equal_weight
    history_days: int = 252
    max_weight: float = 0.25
    min_weight: float = 0.0
    cov_method: str = 'ledoit_wolf'
    rf_annual: float = 0.02              # 年化无风险利率
    # 显式注入 returns(测试用),为 None 则 use case 自拉
    injected_returns: Optional[pd.DataFrame] = None


@dataclass
class PortfolioAdvice:
    """建议权重 + 元数据。"""
    method: str
    weights: Dict[str, float] = field(default_factory=dict)
    n_assets: int = 0
    expected_return: float = 0.0         # 组合预期年化收益(基于历史均值)
    expected_vol: float = 0.0            # 组合预期年化波动
    sharpe: float = 0.0
    diagnostics: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'method': self.method,
            'weights': {k: round(float(v), 6) for k, v in self.weights.items()},
            'n_assets': self.n_assets,
            'expected_return': round(self.expected_return, 6),
            'expected_vol': round(self.expected_vol, 6),
            'sharpe': round(self.sharpe, 4),
            'diagnostics': self.diagnostics,
        }


_SUPPORTED_METHODS = {
    'min_variance', 'max_sharpe', 'risk_parity',
    'max_diversification', 'equal_weight',
}


def _build_returns_matrix(
    universe: List[str], days: int,
) -> Tuple[pd.DataFrame, List[dict]]:
    """从 DataGateway 拉每只 symbol 的日 K,组装收益率矩阵。

    返回 (returns_df, excluded) — excluded 中的每条记录形如
    ``{'symbol': '...', 'reason': '...'}``,让 caller 可以把降级原因
    透传到响应里(之前是 `except Exception: continue` 全静默)。
    """
    from core.data_gateway import get_gateway, normalize_kline_index
    gw = get_gateway()

    series_dict: Dict[str, pd.Series] = {}
    excluded: List[dict] = []
    for sym in universe:
        try:
            df = gw.kline(sym, interval='daily', days=days, limit=days)
        except Exception as exc:
            logger.warning('compose_portfolio: kline fetch failed for %s: %s', sym, exc)
            excluded.append({'symbol': sym, 'reason': f'kline_error: {exc}'})
            continue
        if df is None or df.empty:
            excluded.append({'symbol': sym, 'reason': 'no_data'})
            continue
        df = normalize_kline_index(df)
        if 'close' not in df.columns:
            excluded.append({'symbol': sym, 'reason': 'missing_close_column'})
            continue
        series_dict[sym] = df['close'].pct_change().dropna()

    if len(series_dict) < 2:
        raise UseCaseError(
            f'insufficient data: only {len(series_dict)} symbols have returns',
            code='DATA_UNAVAILABLE',
        )
    return pd.DataFrame(series_dict).dropna(), excluded


def compose_portfolio(req: ComposePortfolioRequest) -> PortfolioAdvice:
    """生成组合权重建议。

    Raises
    ------
    UseCaseError(code='INVALID_METHOD')
    UseCaseError(code='DATA_UNAVAILABLE')
    UseCaseError(code='TOO_FEW_ASSETS')
    """
    if req.method not in _SUPPORTED_METHODS:
        raise UseCaseError(
            f'unsupported method: {req.method}', code='INVALID_METHOD',
        )
    if len(req.universe) < 2:
        raise UseCaseError(
            'need at least 2 assets in universe', code='TOO_FEW_ASSETS',
        )

    excluded: List[dict] = []
    if req.injected_returns is not None:
        returns = req.injected_returns
        if returns.empty or returns.shape[1] < 2:
            raise UseCaseError(
                'injected_returns empty or single asset',
                code='DATA_UNAVAILABLE',
            )
    else:
        returns, excluded = _build_returns_matrix(req.universe, req.history_days)

    from core.portfolio_optimizer import PortfolioOptimizer

    opt = PortfolioOptimizer(
        returns=returns,
        cov_method=req.cov_method,
        max_weight=req.max_weight,
        min_weight=req.min_weight,
        rf=req.rf_annual / 252,
    )

    method_fn = getattr(opt, req.method, None)
    if method_fn is None:
        raise UseCaseError(
            f'method not implemented on PortfolioOptimizer: {req.method}',
            code='INVALID_METHOD',
        )
    weights_series: pd.Series = method_fn()

    # 计算预期收益/波动/sharpe(年化)
    w = weights_series.values
    mu_daily = returns.mean().values
    cov_daily = returns.cov().values
    er_daily = float(w @ mu_daily)
    vol_daily = float((w @ cov_daily @ w) ** 0.5)
    er_annual = er_daily * 252
    vol_annual = vol_daily * (252 ** 0.5)
    sharpe = (er_annual - req.rf_annual) / vol_annual if vol_annual > 1e-12 else 0.0

    diagnostics: Dict[str, str] = {
        'cov_method': req.cov_method,
        'history_bars': str(len(returns)),
    }
    if excluded:
        # 仅放符号+原因摘要,避免 diagnostics 过大
        diagnostics['excluded_symbols'] = ','.join(
            f"{e['symbol']}({e['reason']})" for e in excluded
        )

    return PortfolioAdvice(
        method=req.method,
        weights={str(k): float(v) for k, v in weights_series.items()},
        n_assets=len(weights_series),
        expected_return=er_annual,
        expected_vol=vol_annual,
        sharpe=sharpe,
        diagnostics=diagnostics,
    )


__all__ = [
    'ComposePortfolioRequest',
    'PortfolioAdvice',
    'compose_portfolio',
]

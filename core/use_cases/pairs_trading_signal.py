"""
core/use_cases/pairs_trading_signal.py — 配对交易信号 use case (P2-8 批次 4)

把 backend/api.py 中 /analysis/pairs_trading 端点的内联业务逻辑下沉到本层:
价格矩阵拉取 → 协整筛选 → 逐对算最新 z-score 信号。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from . import UseCaseError

logger = logging.getLogger(__name__)


@dataclass
class PairsTradingRequest:
    symbols: List[str] = field(default_factory=list)
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    lookback_days: int = 60
    screen_days: int = 252
    max_pairs: int = 5


@dataclass
class PairsTradingResponse:
    pairs: List[Dict[str, Any]] = field(default_factory=list)
    n_pairs_found: int = 0
    # 每个 entry: {"pair": "A|B", "error": "<message>"}
    # 单对计算失败不阻塞整体,但调用方能看到失败明细。
    warnings: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'pairs': self.pairs,
            'n_pairs_found': self.n_pairs_found,
            'warnings': self.warnings,
        }


def find_pairs_signals(
    req: PairsTradingRequest,
    *,
    data_layer: Any = None,
) -> PairsTradingResponse:
    """筛选协整配对并返回当前 z-score 信号。

    Args:
        req: 输入参数。
        data_layer: 可选——直接注入数据层(测试 / 多源场景)。
            为 None 时回退到 :func:`core.data_layer.get_data_layer`。
    """
    if len(req.symbols) < 2:
        raise UseCaseError('至少提供 2 个标的用于配对筛选', 'INVALID_INPUT')

    import pandas as pd
    from core.strategies.pairs_trading import find_cointegrated_pairs, PairsTradingStrategy

    if data_layer is None:
        from core.data_layer import get_data_layer
        data_layer = get_data_layer()
    dl = data_layer
    price_dict = {}
    for sym in req.symbols:
        df = dl.get_bars(sym, days=req.screen_days + 30)
        if df is not None and not df.empty and 'close' in df.columns:
            price_dict[sym] = df['close']

    if len(price_dict) < 2:
        raise UseCaseError('有效行情数据不足 2 个标的', 'DATA_UNAVAILABLE')

    price_df = pd.DataFrame(price_dict).dropna()
    pairs = find_cointegrated_pairs(price_df, lookback_days=req.screen_days)

    results: List[Dict[str, Any]] = []
    warnings: List[Dict[str, str]] = []
    for sym_a, sym_b, _corr, _pval in pairs[:req.max_pairs]:
        try:
            strat = PairsTradingStrategy(
                symbol_a=sym_a, symbol_b=sym_b,
                entry_z=req.entry_z, exit_z=req.exit_z, stop_z=req.stop_z,
                lookback_days=req.lookback_days,
            )
            all_signals = strat.generate_signals(price_df)
            signal = all_signals[-1] if all_signals else None
            if signal:
                results.append({
                    'symbol_a': sym_a,
                    'symbol_b': sym_b,
                    'signal': {
                        'date': signal.date,
                        'spread_zscore': round(signal.spread_zscore, 4),
                        'action_a': signal.action_a,
                        'action_b': signal.action_b,
                        'spread': round(signal.spread, 6),
                    },
                })
        except Exception as exc:
            # 不阻塞其它配对计算,但把失败原因带回去——
            # 之前是 `except Exception: continue` 静默吞掉,调用方完全不知道。
            logger.warning('pair %s|%s signal failed: %s', sym_a, sym_b, exc)
            warnings.append({
                'pair': f'{sym_a}|{sym_b}',
                'error': f'pair_signal_error: {exc}',
            })

    return PairsTradingResponse(
        pairs=results,
        n_pairs_found=len(pairs),
        warnings=warnings,
    )

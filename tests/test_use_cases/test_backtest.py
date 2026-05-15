"""tests/test_use_cases/test_backtest.py — 回测 use case 测试 (P2-5)。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv(n=300, seed=42):
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0.01, 0.2, n))
    return pd.DataFrame({
        'open': close, 'high': close * 1.01,
        'low': close * 0.99, 'close': close,
        'volume': rng.integers(1e5, 1e6, n).astype(float),
    }, index=dates)


def test_no_strategy_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.backtest import BacktestRequest, run_backtest
    with pytest.raises(UseCaseError) as exc:
        run_backtest(BacktestRequest(
            symbol='sh600519', strategies=[],
            injected_data=_make_ohlcv(50),
        ))
    assert exc.value.code == 'NO_STRATEGY'


def test_unknown_factor_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    with pytest.raises(UseCaseError) as exc:
        run_backtest(BacktestRequest(
            symbol='sh600519',
            injected_data=_make_ohlcv(50),
            strategies=[StrategySpec(factor_name='NotARealFactor')],
        ))
    assert exc.value.code == 'UNKNOWN_FACTOR'


def test_empty_data_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    with pytest.raises(UseCaseError) as exc:
        run_backtest(BacktestRequest(
            symbol='sh600519',
            injected_data=pd.DataFrame(),   # 空
            strategies=[StrategySpec(factor_name='RSI')],
        ))
    assert exc.value.code == 'DATA_UNAVAILABLE'


def test_happy_path_returns_metrics():
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    resp = run_backtest(BacktestRequest(
        symbol='sh600519',
        initial_equity=100_000.0,
        injected_data=_make_ohlcv(300),
        strategies=[
            StrategySpec(factor_name='RSI', threshold=1.0,
                         params={'period': 14}),
        ],
    ))
    assert resp.symbol == 'sh600519'
    assert resp.n_bars == 300
    # 不强求一定有交易,但字段存在且为合理类型
    assert isinstance(resp.n_trades, int)
    assert isinstance(resp.total_return, (int, float))
    assert isinstance(resp.sharpe, (int, float))
    assert resp.summary_text  # 非空


def test_to_dict_serializable():
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    resp = run_backtest(BacktestRequest(
        symbol='sh510310',
        injected_data=_make_ohlcv(150),
        strategies=[StrategySpec(factor_name='RSI')],
    ))
    d = resp.to_dict()
    assert d['symbol'] == 'sh510310'
    assert 'sharpe' in d
    assert 'summary' in d


def test_data_fetch_failure_raises():
    """gateway 失败时抛 DATA_UNAVAILABLE。"""
    from unittest.mock import MagicMock, patch
    from core.use_cases import UseCaseError
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    gw = MagicMock()
    gw.kline.side_effect = RuntimeError('net fail')
    with patch('core.data_gateway.get_gateway', return_value=gw):
        with pytest.raises(UseCaseError) as exc:
            run_backtest(BacktestRequest(
                symbol='sh600519',
                strategies=[StrategySpec(factor_name='RSI')],
            ))
    assert exc.value.code == 'DATA_UNAVAILABLE'

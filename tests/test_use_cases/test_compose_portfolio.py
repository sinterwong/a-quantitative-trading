"""tests/test_use_cases/test_compose_portfolio.py — 组合优化 use case 测试 (P2-6)。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_returns(n_days=252, n_assets=3, seed=42):
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n_days, freq='B')
    cols = [f'sym{i}' for i in range(n_assets)]
    data = rng.normal(0.0005, 0.012, size=(n_days, n_assets))
    return pd.DataFrame(data, index=dates, columns=cols)


def test_invalid_method_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    with pytest.raises(UseCaseError) as exc:
        compose_portfolio(ComposePortfolioRequest(
            universe=['A', 'B'], method='nonsense_method',
            injected_returns=_make_returns(),
        ))
    assert exc.value.code == 'INVALID_METHOD'


def test_too_few_assets_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    with pytest.raises(UseCaseError) as exc:
        compose_portfolio(ComposePortfolioRequest(
            universe=['A'], method='min_variance',
            injected_returns=_make_returns(n_assets=2),
        ))
    assert exc.value.code == 'TOO_FEW_ASSETS'


def test_empty_returns_raises():
    from core.use_cases import UseCaseError
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    with pytest.raises(UseCaseError) as exc:
        compose_portfolio(ComposePortfolioRequest(
            universe=['A', 'B'], method='min_variance',
            injected_returns=pd.DataFrame(),
        ))
    assert exc.value.code == 'DATA_UNAVAILABLE'


def test_min_variance_happy_path():
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    returns = _make_returns(n_assets=5)  # 用 5 个让 max_weight=0.25 可行
    adv = compose_portfolio(ComposePortfolioRequest(
        universe=list(returns.columns),
        method='min_variance',
        injected_returns=returns,
        max_weight=0.5,
    ))
    assert adv.method == 'min_variance'
    assert adv.n_assets == 5
    # 权重和应接近 1
    total = sum(adv.weights.values())
    assert abs(total - 1.0) < 1e-3
    # 所有 weight 非负
    for v in adv.weights.values():
        assert v >= -1e-6


def test_equal_weight_method():
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    returns = _make_returns(n_assets=4)
    adv = compose_portfolio(ComposePortfolioRequest(
        universe=list(returns.columns),
        method='equal_weight',
        max_weight=1.0,  # equal_weight 单权重可能超过默认 0.25
        injected_returns=returns,
    ))
    assert adv.method == 'equal_weight'
    # equal_weight 应为 1/N
    for v in adv.weights.values():
        assert abs(v - 0.25) < 1e-3


def test_diagnostics_populated():
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    adv = compose_portfolio(ComposePortfolioRequest(
        universe=['a', 'b', 'c'], method='min_variance',
        injected_returns=_make_returns(n_assets=3),
    ))
    assert 'cov_method' in adv.diagnostics
    assert 'history_bars' in adv.diagnostics


def test_to_dict_serializable():
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    adv = compose_portfolio(ComposePortfolioRequest(
        universe=['a', 'b'], method='min_variance',
        injected_returns=_make_returns(n_assets=2),
    ))
    d = adv.to_dict()
    assert d['method'] == 'min_variance'
    assert 'weights' in d
    assert 'sharpe' in d

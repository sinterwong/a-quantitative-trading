"""
tests/test_use_cases/test_sector_rotation_signal.py — sector_rotation_signal use case 单元测试。

覆盖:
  - happy: DataLayer 返回价格 → 完整 SectorRotationResponse + 信号被记录
  - degraded: portfolio_svc=None → 不记录信号但仍生成 response
  - error: price_data 全为空 → UseCaseError(DATA_UNAVAILABLE)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.use_cases import UseCaseError
from core.use_cases.sector_rotation_signal import (
    SectorRotationRequest, SectorRotationResponse, run_sector_rotation,
)


def _bars(n=120, start=10.0):
    """返回一个 DataFrame,close 列从 start 单调上升。"""
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'date': dates,
        'open': [start + i * 0.05 for i in range(n)],
        'high': [start + i * 0.05 + 0.1 for i in range(n)],
        'low': [start + i * 0.05 - 0.1 for i in range(n)],
        'close': [start + i * 0.05 for i in range(n)],
        'volume': [100000] * n,
    })


@pytest.fixture
def patch_data_layer():
    """让 get_data_layer().get_bars() 返回非空 DataFrame。"""
    dl = MagicMock()
    dl.get_bars.return_value = _bars(120)
    with patch('core.data_layer.get_data_layer', return_value=dl):
        yield dl


def test_sector_rotation_happy_path(patch_data_layer):
    svc = MagicMock()
    req = SectorRotationRequest(
        top_n=3, lookback_days=60, rebalance_days=21,
        momentum_method='return', current_holdings=[],
    )
    resp = run_sector_rotation(req, portfolio_svc=svc)
    assert isinstance(resp, SectorRotationResponse)
    assert resp.top_n == 3
    assert resp.universe_size >= 1
    # 每个 buy 信号都被记录
    assert svc.record_signal.call_count == len(resp.buy)


def test_sector_rotation_without_portfolio_svc(patch_data_layer):
    req = SectorRotationRequest()
    resp = run_sector_rotation(req, portfolio_svc=None)
    assert isinstance(resp, SectorRotationResponse)
    assert resp.top_n == 3   # 默认值


def test_sector_rotation_raises_when_no_price_data():
    """所有 get_bars 都返回 None → UseCaseError(DATA_UNAVAILABLE)。"""
    dl = MagicMock()
    dl.get_bars.return_value = None
    with patch('core.data_layer.get_data_layer', return_value=dl):
        req = SectorRotationRequest()
        with pytest.raises(UseCaseError) as exc_info:
            run_sector_rotation(req)
        assert exc_info.value.code == 'DATA_UNAVAILABLE'


def test_sector_rotation_to_dict_keys():
    resp = SectorRotationResponse(
        rebalance_date='2026-05-15', buy=['510310.SH'], sell=[], hold=[],
        scores={'510310.SH': 0.5}, top_n=1, universe_size=3,
    )
    d = resp.to_dict()
    assert set(d.keys()) == {
        'rebalance_date', 'buy', 'sell', 'hold',
        'scores', 'top_n', 'universe_size',
    }


def test_sector_rotation_request_defaults():
    req = SectorRotationRequest()
    assert req.top_n == 3
    assert req.lookback_days == 60
    assert req.momentum_method == 'return'
    assert req.current_holdings == []

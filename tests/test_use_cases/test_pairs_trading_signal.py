"""
tests/test_use_cases/test_pairs_trading_signal.py — pairs_trading_signal use case 单元测试。

覆盖:
  - error: symbols < 2 → UseCaseError(INVALID_INPUT)
  - error: 数据全部缺失 → UseCaseError(DATA_UNAVAILABLE)
  - happy: 协整配对返回 → 包装为 response,signal 字段完整
  - happy: max_pairs 截断
  - 单个 strat.latest_signal 抛异常 → 该对被跳过,不影响其它
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.use_cases import UseCaseError
from core.use_cases.pairs_trading_signal import (
    PairsTradingRequest, PairsTradingResponse, find_pairs_signals,
)


def _price_series(start=10.0, n=300):
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    return pd.DataFrame({
        'date': dates,
        'close': [start + i * 0.01 for i in range(n)],
    })


def _fake_signal(date='2026-05-15', z=2.5, action_a='SELL', action_b='BUY'):
    sig = MagicMock()
    sig.date = date
    sig.spread_zscore = z
    sig.action_a = action_a
    sig.action_b = action_b
    sig.spread = 0.123456
    return sig


def test_pairs_trading_rejects_single_symbol():
    with pytest.raises(UseCaseError) as exc_info:
        find_pairs_signals(PairsTradingRequest(symbols=['A.SH']))
    assert exc_info.value.code == 'INVALID_INPUT'


def test_pairs_trading_raises_when_no_data():
    """所有 get_bars 都返回空 → DATA_UNAVAILABLE。"""
    dl = MagicMock()
    dl.get_bars.return_value = None
    with patch('core.data_layer.get_data_layer', return_value=dl):
        with pytest.raises(UseCaseError) as exc_info:
            find_pairs_signals(PairsTradingRequest(
                symbols=['A.SH', 'B.SH'], screen_days=252,
            ))
        assert exc_info.value.code == 'DATA_UNAVAILABLE'


@pytest.fixture
def patch_data_and_pairs():
    """提供两支带 close 列的 DataFrame + 协整对结果。"""
    dl = MagicMock()
    dl.get_bars.return_value = _price_series()
    with patch('core.data_layer.get_data_layer', return_value=dl), \
         patch('core.strategies.pairs_trading.find_cointegrated_pairs',
               return_value=[('A.SH', 'B.SH')]), \
         patch('core.strategies.pairs_trading.PairsTradingStrategy') as mock_strat:
        strat = MagicMock()
        strat.latest_signal.return_value = _fake_signal()
        mock_strat.return_value = strat
        yield {'pairs_count': 1}


def test_pairs_trading_happy_path(patch_data_and_pairs):
    req = PairsTradingRequest(
        symbols=['A.SH', 'B.SH'],
        entry_z=2.0, exit_z=0.5, stop_z=4.0,
        lookback_days=60, screen_days=252,
    )
    resp = find_pairs_signals(req)
    assert isinstance(resp, PairsTradingResponse)
    assert resp.n_pairs_found == 1
    assert len(resp.pairs) == 1
    pair = resp.pairs[0]
    assert pair['symbol_a'] == 'A.SH'
    assert pair['symbol_b'] == 'B.SH'
    assert pair['signal']['spread_zscore'] == 2.5
    assert pair['signal']['action_a'] == 'SELL'


def test_pairs_trading_max_pairs_truncation():
    """若 find_cointegrated_pairs 返回多对,results 只取 max_pairs 个。"""
    dl = MagicMock()
    dl.get_bars.return_value = _price_series()
    many_pairs = [(f'A{i}.SH', f'B{i}.SH') for i in range(10)]
    with patch('core.data_layer.get_data_layer', return_value=dl), \
         patch('core.strategies.pairs_trading.find_cointegrated_pairs',
               return_value=many_pairs), \
         patch('core.strategies.pairs_trading.PairsTradingStrategy') as mock_strat:
        strat = MagicMock()
        strat.latest_signal.return_value = _fake_signal()
        mock_strat.return_value = strat

        req = PairsTradingRequest(
            symbols=[f'X{i}.SH' for i in range(5)], max_pairs=3,
        )
        resp = find_pairs_signals(req)
        # n_pairs_found 是发现的总对数(10);results 只截断到 max_pairs (3)
        assert resp.n_pairs_found == 10
        assert len(resp.pairs) == 3


def test_pairs_trading_skips_failing_pair():
    """单个 pair latest_signal 抛异常 → 该对被跳过。"""
    dl = MagicMock()
    dl.get_bars.return_value = _price_series()

    strat_ok = MagicMock()
    strat_ok.latest_signal.return_value = _fake_signal()
    strat_bad = MagicMock()
    strat_bad.latest_signal.side_effect = RuntimeError('regression failed')

    with patch('core.data_layer.get_data_layer', return_value=dl), \
         patch('core.strategies.pairs_trading.find_cointegrated_pairs',
               return_value=[('A.SH', 'B.SH'), ('C.SH', 'D.SH')]), \
         patch('core.strategies.pairs_trading.PairsTradingStrategy',
               side_effect=[strat_bad, strat_ok]):
        req = PairsTradingRequest(symbols=['A.SH', 'B.SH', 'C.SH', 'D.SH'])
        resp = find_pairs_signals(req)
        # 一对失败,只保留好的那个
        assert resp.n_pairs_found == 2  # 找到 2 对
        assert len(resp.pairs) == 1     # 但只有 1 对生成有效 signal


def test_pairs_trading_response_to_dict():
    resp = PairsTradingResponse(
        pairs=[{'symbol_a': 'A', 'symbol_b': 'B'}], n_pairs_found=1,
    )
    assert resp.to_dict() == {
        'pairs': [{'symbol_a': 'A', 'symbol_b': 'B'}],
        'n_pairs_found': 1,
    }

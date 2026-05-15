"""tests/test_use_cases/test_intraday_signals.py — 盘中信号生成 use case 测试 (P2-3)。"""

from __future__ import annotations


def test_basic_selection():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['sh600519', 'sz000001', 'sh510310'],
        pipeline_scores={
            'sh600519': 0.85,
            'sz000001': 0.45,   # below threshold
            'sh510310': 0.60,
        },
        threshold=0.5,
    ))
    syms = [c.symbol for c in resp.candidates]
    assert syms == ['sh600519', 'sh510310']  # 按 score 降序
    # sz000001 应在 skipped
    skipped_syms = [s['symbol'] for s in resp.skipped]
    assert 'sz000001' in skipped_syms


def test_excluded_symbols_filtered():
    """excluded_symbols(如已持仓)不应进入候选。"""
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['A', 'B'],
        pipeline_scores={'A': 0.9, 'B': 0.7},
        threshold=0.5,
        excluded_symbols={'A'},  # 已持仓
    ))
    syms = [c.symbol for c in resp.candidates]
    assert syms == ['B']


def test_non_positive_score_skipped():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['A', 'B', 'C'],
        pipeline_scores={'A': -0.5, 'B': 0.0, 'C': 0.8},
        threshold=0.5,
    ))
    syms = [c.symbol for c in resp.candidates]
    assert syms == ['C']
    skipped_reasons = {s['symbol']: s['reason'] for s in resp.skipped}
    assert 'non_positive_score' in skipped_reasons['A']
    assert 'non_positive_score' in skipped_reasons['B']


def test_no_score_skipped():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['A', 'B'],
        pipeline_scores={'A': 0.9},   # B 无 score
        threshold=0.5,
    ))
    syms = [c.symbol for c in resp.candidates]
    assert syms == ['A']
    assert any(s['reason'] == 'no_score' for s in resp.skipped)


def test_threshold_used_in_response():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['A'],
        pipeline_scores={'A': 0.9},
        threshold=0.7,
    ))
    assert resp.threshold_used == 0.7


def test_candidate_to_dict_roundtrip():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['sh600519'],
        pipeline_scores={'sh600519': 0.82},
        threshold=0.5,
    ))
    d = resp.candidates[0].to_dict()
    assert d['symbol'] == 'sh600519'
    assert d['direction'] == 'BUY'
    assert d['score'] == 0.82
    assert 'Pipeline score' in d['reason']


def test_empty_watchlist_returns_empty():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=[], pipeline_scores={}, threshold=0.5,
    ))
    assert resp.candidates == []


def test_response_to_dict():
    from core.use_cases.intraday_signals import (
        IntradaySignalRequest, generate_intraday_signals,
    )
    resp = generate_intraday_signals(IntradaySignalRequest(
        watched_symbols=['A'],
        pipeline_scores={'A': 0.9},
        threshold=0.5,
    ))
    d = resp.to_dict()
    assert d['threshold_used'] == 0.5
    assert isinstance(d['candidates'], list)
    assert isinstance(d['skipped'], list)

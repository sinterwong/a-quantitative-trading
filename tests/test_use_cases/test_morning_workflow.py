"""tests/test_use_cases/test_morning_workflow.py — 早盘工作流 use case 测试 (P2-4)。"""

from __future__ import annotations


def _make_request(**overrides):
    from core.use_cases.morning_workflow import MorningWorkflowRequest
    base = dict(
        candidates=[
            {'symbol': 'sh600519', 'name': '茅台', 'score': 85,
             'sector': '白酒', 'pct': 1.5},
            {'symbol': 'sh510310', 'name': '沪深300', 'score': 60,
             'sector': 'ETF', 'pct': 0.5},
        ],
        regime_info={'regime': 'BULL', 'regime_reason': 'MA20 多头', 'atr_ratio': 0.42},
        positions=[{'symbol': 'sh510310'}],
        cash=8000.0,
        equity=20000.0,
    )
    base.update(overrides)
    return MorningWorkflowRequest(**base)


def test_assemble_basic():
    from core.use_cases.morning_workflow import assemble_morning_report
    rep = assemble_morning_report(_make_request())
    assert rep.regime == 'BULL'
    assert rep.atr_ratio == 0.42
    assert rep.positions_count == 1
    assert len(rep.candidates) == 2


def test_candidates_normalized():
    """不同字段名(pct vs change_pct, score vs total_score)归一。"""
    from core.use_cases.morning_workflow import (
        MorningWorkflowRequest, assemble_morning_report,
    )
    rep = assemble_morning_report(MorningWorkflowRequest(
        candidates=[
            {'code': '600519', 'name': '茅台', 'total_score': 90, 'change_pct': 2.0},
            {'symbol': 'sh510310', 'name': 'X', 'score': 70, 'pct': 1.0},
        ],
        regime_info={'regime': 'CALM', 'atr_ratio': 0.2},
        cash=1.0, equity=2.0,
    ))
    assert rep.candidates[0]['symbol'] in ('600519',)   # code 转 symbol
    assert rep.candidates[0]['total_score'] == 90
    assert rep.candidates[1]['change_pct'] == 1.0


def test_notes_format():
    from core.use_cases.morning_workflow import assemble_morning_report
    rep = assemble_morning_report(_make_request())
    notes = rep.notes_for_daily_meta
    assert '[MorningRunner]' in notes
    assert 'regime=BULL' in notes
    assert 'candidates:2' in notes
    assert 'positions:1' in notes
    assert 'equity=20000' in notes


def test_fallback_text_contains_key_info():
    from core.use_cases.morning_workflow import (
        MorningWorkflowRequest, build_fallback_report_text,
    )
    txt = build_fallback_report_text(_make_request())
    assert '早报降级版' in txt
    assert 'BULL' in txt
    assert '20000' in txt or '20000.' in txt
    assert '茅台' in txt
    assert 'IntradayMonitor' in txt


def test_fallback_text_with_empty_candidates():
    from core.use_cases.morning_workflow import (
        MorningWorkflowRequest, build_fallback_report_text,
    )
    txt = build_fallback_report_text(MorningWorkflowRequest(
        candidates=[], regime_info={'regime': 'BEAR', 'atr_ratio': 0.9},
        cash=5000.0, equity=10000.0,
    ))
    assert '今日候选 (0只)' in txt
    assert 'BEAR' in txt


def test_to_dict_serializable():
    from core.use_cases.morning_workflow import assemble_morning_report
    rep = assemble_morning_report(_make_request())
    d = rep.to_dict()
    assert d['regime'] == 'BULL'
    assert isinstance(d['candidates'], list)


def test_regime_reason_optional():
    """regime_info 缺少字段时不崩溃。"""
    from core.use_cases.morning_workflow import (
        MorningWorkflowRequest, assemble_morning_report,
    )
    rep = assemble_morning_report(MorningWorkflowRequest(
        candidates=[], regime_info={},
        cash=0.0, equity=0.0,
    ))
    assert rep.regime == 'CALM'
    assert rep.atr_ratio == 0.0

"""
tests/test_use_cases/test_risk_snapshot.py — risk_snapshot use case 单元测试。

覆盖:
  - happy: 有 monitor + 多板块持仓 → 完整 RiskSnapshot
  - degraded: monitor=None → 板块敞口仍计算,monitor 派生字段保留默认
  - error: monitor.get_status 抛异常 → 不影响 portfolio 字段,仅 monitor 字段空
  - sector exposure 计算正确性 (单元)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.use_cases.risk_snapshot import (
    RiskSnapshot, compute_sector_exposure, get_risk_snapshot,
)


def _fake_svc(positions=None, equity=100000.0):
    svc = MagicMock()
    svc.get_positions.return_value = positions or []
    svc.get_portfolio_summary.return_value = {'total_equity': equity}
    return svc


# ── compute_sector_exposure 单元 ──────────────────────────────

def test_compute_sector_exposure_empty():
    assert compute_sector_exposure([]) == {}


def test_compute_sector_exposure_zero_total():
    """所有持仓 shares=0 或 current_price=0 → 总市值 0,返回空 dict。"""
    positions = [
        {'shares': 0, 'current_price': 100, 'sector': 'TECH'},
        {'shares': 100, 'current_price': 0, 'sector': 'FIN'},
    ]
    assert compute_sector_exposure(positions) == {}


def test_compute_sector_exposure_basic_aggregation():
    positions = [
        {'shares': 100, 'current_price': 10, 'sector': 'TECH'},    # 1000
        {'shares': 200, 'current_price': 5,  'sector': 'TECH'},    # 1000
        {'shares': 100, 'current_price': 20, 'sector': 'FIN'},     # 2000
    ]
    out = compute_sector_exposure(positions)
    assert out['TECH'] == pytest.approx(0.5, abs=1e-4)
    assert out['FIN']  == pytest.approx(0.5, abs=1e-4)


def test_compute_sector_exposure_unknown_sector_label():
    positions = [{'shares': 100, 'current_price': 10}]   # 无 sector 字段
    assert compute_sector_exposure(positions) == {'unknown': 1.0}


# ── get_risk_snapshot ─────────────────────────────────────────

def test_get_risk_snapshot_happy_path_with_monitor():
    svc = _fake_svc(
        positions=[
            {'shares': 100, 'current_price': 10, 'sector': 'TECH'},
            {'shares': 100, 'current_price': 20, 'sector': 'FIN'},
        ],
        equity=50000,
    )
    monitor = MagicMock()
    monitor.get_status.return_value = {
        'peak_equity': 60000,
        'dd_warn': 0.08,
        'dd_stop': 0.12,
        'risk_warn_fired': True,
        'risk_stop_fired': False,
        'kelly_pct': 0.10,
    }
    snap = get_risk_snapshot(svc, monitor=monitor)

    assert snap.total_equity == 50000.0
    assert snap.peak_equity == 60000.0
    assert snap.dd_warn_threshold == 0.08
    assert snap.dd_stop_threshold == 0.12
    assert snap.risk_warn_fired is True
    assert snap.risk_stop_fired is False
    assert snap.kelly_pct == 0.10
    # 当前回撤 = 1 - 50000/60000 ≈ 0.1667
    assert snap.current_drawdown == pytest.approx(1 - 50000/60000, abs=1e-4)
    assert snap.position_count == 2
    assert set(snap.sector_exposure.keys()) == {'TECH', 'FIN'}


def test_get_risk_snapshot_degraded_no_monitor():
    """monitor=None → portfolio 字段算出来,monitor 派生字段保持默认。"""
    svc = _fake_svc(
        positions=[{'shares': 100, 'current_price': 10, 'sector': 'TECH'}],
        equity=1000,
    )
    snap = get_risk_snapshot(svc, monitor=None)
    assert snap.total_equity == 1000.0
    assert snap.position_count == 1
    # monitor 派生默认值
    assert snap.peak_equity == 0.0
    assert snap.dd_warn_threshold == 0.0
    assert snap.kelly_pct is None
    assert snap.risk_warn_fired is False
    assert snap.current_drawdown == 0.0


def test_get_risk_snapshot_monitor_status_error_is_isolated():
    """monitor.get_status 抛异常 → portfolio 字段仍正确,monitor 字段保留默认。"""
    svc = _fake_svc(equity=2000.0)
    monitor = MagicMock()
    monitor.get_status.side_effect = RuntimeError('boom')

    snap = get_risk_snapshot(svc, monitor=monitor)
    assert snap.total_equity == 2000.0
    assert snap.peak_equity == 0.0  # 默认值,未被覆盖
    assert snap.kelly_pct is None


def test_risk_snapshot_to_dict_rounds_floats():
    snap = RiskSnapshot(total_equity=12345.6789, peak_equity=23456.7,
                        current_drawdown=0.12345678, kelly_pct=0.10001)
    d = snap.to_dict()
    assert d['total_equity'] == 12345.68
    assert d['peak_equity'] == 23456.7
    assert d['current_drawdown'] == 0.1235
    assert d['kelly_pct'] == 0.1


def test_risk_snapshot_to_dict_handles_none_kelly():
    d = RiskSnapshot().to_dict()
    assert d['kelly_pct'] is None

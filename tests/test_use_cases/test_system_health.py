"""
tests/test_use_cases/test_system_health.py — system_health use case 单元测试。

覆盖:
  - OK 等级:现金占比正常 + 无大幅亏损
  - WARN 等级:现金 <5% 或浮亏 -5%~-10%
  - CRITICAL 等级:浮亏 < -10%
  - latest_analysis 读取(目录存在/不存在/读异常)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.use_cases.system_health import (
    compute_system_health, SystemHealthReport,
    CASH_RATIO_WARN, PNL_WARN_PCT, PNL_CRIT_PCT,
)


def _fake_svc(equity=100000.0, cash=20000.0, positions=None):
    svc = MagicMock()
    svc.get_portfolio_summary.return_value = {'total_equity': equity, 'cash': cash}
    svc.get_positions.return_value = positions or []
    return svc


def test_system_health_ok_with_balanced_book():
    svc = _fake_svc(
        equity=100000, cash=20000,   # 现金占比 20%
        positions=[{'shares': 100, 'unrealized_pnl': 500}],
    )
    rpt = compute_system_health(svc)
    assert rpt.level == 'OK'
    assert rpt.reasons == []
    assert rpt.n_positions == 1


def test_system_health_warn_low_cash():
    svc = _fake_svc(
        equity=100000, cash=2000,  # 现金占比 2% < 5%
        positions=[{'shares': 100, 'unrealized_pnl': 0}],
    )
    rpt = compute_system_health(svc)
    assert rpt.level == 'WARN'
    assert any('现金占比' in r for r in rpt.reasons)


def test_system_health_warn_moderate_loss():
    """浮亏 -7% → WARN(在 -5% 与 -10% 之间)。"""
    svc = _fake_svc(
        equity=100000, cash=50000,
        positions=[{'unrealized_pnl': -7000}],
    )
    rpt = compute_system_health(svc)
    assert rpt.level == 'WARN'
    assert any('未实现亏损' in r for r in rpt.reasons)


def test_system_health_critical_severe_loss():
    """浮亏 -15% < -10% → CRITICAL。"""
    svc = _fake_svc(
        equity=100000, cash=50000,
        positions=[{'unrealized_pnl': -15000}],
    )
    rpt = compute_system_health(svc)
    assert rpt.level == 'CRITICAL'
    # CRITICAL 也会先经过 WARN 路径,reasons 应包含两条
    assert len(rpt.reasons) >= 2


def test_system_health_no_equity_skips_ratio_checks():
    """equity=0 时,百分比检查应跳过,不应除零崩溃。"""
    svc = _fake_svc(equity=0, cash=0, positions=[])
    rpt = compute_system_health(svc)
    assert rpt.level == 'OK'
    assert rpt.reasons == []
    assert rpt.equity == 0.0


def test_system_health_latest_analysis_picks_lexically_largest(tmp_path):
    """analysis_dir 有文件时,latest_analysis 取字典序最大(即最新日期)。"""
    (tmp_path / 'analysis_2026-05-01.json').write_text('{}')
    (tmp_path / 'analysis_2026-05-15.json').write_text('{}')
    (tmp_path / 'analysis_2026-05-08.json').write_text('{}')

    svc = _fake_svc()
    rpt = compute_system_health(svc, analysis_dir=str(tmp_path))
    assert rpt.latest_analysis == 'analysis_2026-05-15.json'


def test_system_health_missing_analysis_dir_returns_none():
    svc = _fake_svc()
    rpt = compute_system_health(svc, analysis_dir='/nonexistent/path')
    assert rpt.latest_analysis is None


def test_system_health_to_dict_rounds():
    rpt = SystemHealthReport(level='OK', cash=12345.678,
                             total_unrealized_pnl=999.999)
    d = rpt.to_dict()
    assert d['cash'] == 12345.68
    assert d['total_unrealized_pnl'] == 1000.0


def test_thresholds_constants_are_sensible():
    """对常量做轻量 sanity check,避免改动时意外。"""
    assert PNL_WARN_PCT < 0 and PNL_CRIT_PCT < PNL_WARN_PCT
    assert 0 < CASH_RATIO_WARN < 0.5

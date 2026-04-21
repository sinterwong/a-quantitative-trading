"""
Phase 4 Tests — PortfolioRiskChecker

运行方式：
    python tests/test_portfolio_risk.py
    pytest tests/test_portfolio_risk.py -v
"""

from __future__ import annotations
import sys
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_DIR)

import numpy as np
import pandas as pd

from core.portfolio_risk import PortfolioRiskChecker, PortfolioSnapshot
from core.risk_engine import RiskResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(
    positions: dict = None,
    equity: float = 100_000,
    peak_equity: float = 100_000,
    sector_map: dict = None,
    returns: dict = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        positions=positions or {},
        equity=equity,
        peak_equity=peak_equity,
        sector_map=sector_map or {},
        returns=returns or {},
    )


def _make_returns(n: int = 60, mean: float = 0.001, std: float = 0.015, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, std, n))


def _make_correlated_returns(r: float = 0.9, n: int = 60) -> tuple:
    """返回两个相关系数约为 r 的收益率序列。"""
    rng = np.random.default_rng(42)
    base = rng.normal(0, 0.015, n)
    noise = rng.normal(0, 0.005, n)
    a = pd.Series(base)
    b = pd.Series(base * r + noise * (1 - r))
    return a, b


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def _check(cond: bool, msg: str) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        print(f"  FAIL: {msg}")


def _section(name: str) -> None:
    print(f"\n=== {name} ===")


# ===========================================================================
# Test classes
# ===========================================================================

class TestPortfolioSnapshot:

    def test_position_weights_single(self):
        snap = _make_snapshot(positions={'A': 50_000}, equity=100_000)
        assert abs(snap.position_weights['A'] - 0.5) < 1e-9

    def test_position_weights_multi(self):
        snap = _make_snapshot(
            positions={'A': 30_000, 'B': 20_000},
            equity=100_000,
        )
        w = snap.position_weights
        assert abs(w['A'] - 0.3) < 1e-9
        assert abs(w['B'] - 0.2) < 1e-9

    def test_position_weights_zero_equity(self):
        snap = _make_snapshot(positions={'A': 1000}, equity=0)
        assert snap.position_weights['A'] == 0.0

    def test_exposure(self):
        snap = _make_snapshot(positions={'A': 60_000, 'B': 30_000}, equity=100_000)
        assert abs(snap.exposure - 0.9) < 1e-9

    def test_drawdown_no_loss(self):
        snap = _make_snapshot(equity=100_000, peak_equity=100_000)
        assert snap.drawdown == 0.0

    def test_drawdown_with_loss(self):
        snap = _make_snapshot(equity=85_000, peak_equity=100_000)
        assert abs(snap.drawdown - 0.15) < 1e-9

    def test_total_invested(self):
        snap = _make_snapshot(positions={'A': 30_000, 'B': 20_000})
        assert snap.total_invested == 50_000


class TestCheckDrawdown:

    def _checker(self) -> PortfolioRiskChecker:
        return PortfolioRiskChecker(max_drawdown=0.15)

    def test_no_drawdown_ok(self):
        snap = _make_snapshot(equity=100_000, peak_equity=100_000)
        r = self._checker().check_drawdown(snap)
        assert r.level == 'OK'
        assert r.passed

    def test_small_drawdown_ok(self):
        snap = _make_snapshot(equity=95_000, peak_equity=100_000)
        r = self._checker().check_drawdown(snap)
        assert r.level == 'OK'

    def test_drawdown_approaching_warn(self):
        # 75% of limit = 0.1125, use 12%
        snap = _make_snapshot(equity=88_000, peak_equity=100_000)
        r = self._checker().check_drawdown(snap)
        assert r.level == 'WARN'
        assert r.passed   # WARN 不阻断

    def test_drawdown_exceeded_reject(self):
        snap = _make_snapshot(equity=80_000, peak_equity=100_000)
        r = self._checker().check_drawdown(snap)
        assert r.level == 'REJECT'
        assert not r.passed

    def test_drawdown_exact_limit_reject(self):
        snap = _make_snapshot(equity=85_000, peak_equity=100_000)
        r = self._checker().check_drawdown(snap)
        assert not r.passed   # 15% >= 15% limit


class TestCheckSectorConcentration:

    def _checker(self) -> PortfolioRiskChecker:
        return PortfolioRiskChecker(max_sector_weight=0.30)

    def test_no_sector_map_ok(self):
        snap = _make_snapshot(positions={'A': 50_000}, equity=100_000)
        r = self._checker().check_sector_concentration(snap)
        assert r.level == 'OK'

    def test_within_limit_ok(self):
        snap = _make_snapshot(
            positions={'A': 20_000, 'B': 20_000},
            equity=100_000,
            sector_map={'A': '科技', 'B': '消费'},
        )
        r = self._checker().check_sector_concentration(snap)
        assert r.level == 'OK'

    def test_single_sector_within_limit_ok(self):
        snap = _make_snapshot(
            positions={'A': 25_000, 'B': 4_000},
            equity=100_000,
            sector_map={'A': '科技', 'B': '科技'},
        )
        r = self._checker().check_sector_concentration(snap)
        assert r.level == 'OK'   # 29% < 30%

    def test_single_sector_exceeded_warn(self):
        snap = _make_snapshot(
            positions={'A': 20_000, 'B': 15_000},
            equity=100_000,
            sector_map={'A': '科技', 'B': '科技'},
        )
        r = self._checker().check_sector_concentration(snap)
        assert r.level == 'WARN'
        assert '科技' in r.reason

    def test_unknown_sector_grouped_together(self):
        snap = _make_snapshot(
            positions={'A': 20_000, 'B': 15_000},
            equity=100_000,
            sector_map={},   # A/B 都映射到 Unknown
        )
        r = self._checker().check_sector_concentration(snap)
        # 无 sector_map → 直接 OK
        assert r.level == 'OK'


class TestCheckVaR:

    def _checker(self, var_limit: float = 0.03) -> PortfolioRiskChecker:
        return PortfolioRiskChecker(var_limit=var_limit, min_returns_days=20)

    def test_no_returns_ok(self):
        snap = _make_snapshot(positions={'A': 50_000}, equity=100_000)
        r = self._checker().check_var(snap)
        assert r.level == 'OK'

    def test_short_returns_ok(self):
        snap = _make_snapshot(
            positions={'A': 50_000},
            equity=100_000,
            returns={'A': _make_returns(n=10)},   # 少于 min_returns_days
        )
        r = self._checker().check_var(snap)
        assert r.level == 'OK'   # 数据不足，跳过检查

    def test_low_var_ok(self):
        # 低波动率资产 VaR 应该较小
        rng = np.random.default_rng(0)
        low_vol = pd.Series(rng.normal(0.001, 0.003, 60))   # 非常低波动
        snap = _make_snapshot(
            positions={'A': 50_000},
            equity=100_000,
            returns={'A': low_vol},
        )
        r = self._checker(var_limit=0.03).check_var(snap)
        assert r.level == 'OK'

    def test_high_var_reject(self):
        # 高波动 + 大仓位 → VaR 超限
        rng = np.random.default_rng(0)
        high_vol = pd.Series(rng.normal(0, 0.08, 60))   # 8% 日波动
        snap = _make_snapshot(
            positions={'A': 90_000},            # 90% 仓位
            equity=100_000,
            returns={'A': high_vol},
        )
        # 使用很低的 var_limit 强制触发
        r = PortfolioRiskChecker(
            var_limit=0.01, min_returns_days=20
        ).check_var(snap)
        assert not r.passed

    def test_var_details_in_result(self):
        rng = np.random.default_rng(0)
        high_vol = pd.Series(rng.normal(0, 0.08, 60))
        snap = _make_snapshot(
            positions={'A': 90_000},
            equity=100_000,
            returns={'A': high_vol},
        )
        r = PortfolioRiskChecker(var_limit=0.01, min_returns_days=20).check_var(snap)
        assert 'var_pct' in r.details or r.level == 'OK'

    def test_symbol_not_in_returns_skipped(self):
        snap = _make_snapshot(
            positions={'A': 50_000, 'B': 30_000},
            equity=100_000,
            returns={'A': _make_returns(n=60)},   # B 无收益率数据
        )
        # 应该只用 A 计算，不崩溃
        r = self._checker().check_var(snap)
        assert r.level in ('OK', 'WARN', 'REJECT')


class TestCheckCorrelation:

    def _checker(self, max_corr: float = 0.85) -> PortfolioRiskChecker:
        return PortfolioRiskChecker(max_correlation=max_corr)

    def test_no_returns_ok(self):
        snap = _make_snapshot(positions={'A': 50_000, 'B': 30_000}, equity=100_000)
        r = self._checker().check_correlation(snap)
        assert r.level == 'OK'

    def test_single_position_ok(self):
        snap = _make_snapshot(
            positions={'A': 50_000},
            equity=100_000,
            returns={'A': _make_returns(60)},
        )
        r = self._checker().check_correlation(snap)
        assert r.level == 'OK'

    def test_low_correlation_ok(self):
        a = _make_returns(60, seed=1)
        b = _make_returns(60, seed=2)
        snap = _make_snapshot(
            positions={'A': 40_000, 'B': 30_000},
            equity=100_000,
            returns={'A': a, 'B': b},
        )
        r = self._checker(max_corr=0.85).check_correlation(snap)
        # 两个独立随机序列相关系数应该远低于 0.85
        assert r.level == 'OK'

    def test_high_correlation_warn(self):
        a, b = _make_correlated_returns(r=0.95, n=60)
        snap = _make_snapshot(
            positions={'A': 40_000, 'B': 30_000},
            equity=100_000,
            returns={'A': a, 'B': b},
        )
        r = self._checker(max_corr=0.85).check_correlation(snap)
        assert r.level == 'WARN'
        assert 'A' in r.reason or 'B' in r.reason

    def test_high_corr_details(self):
        a, b = _make_correlated_returns(r=0.95, n=60)
        snap = _make_snapshot(
            positions={'A': 40_000, 'B': 30_000},
            equity=100_000,
            returns={'A': a, 'B': b},
        )
        r = self._checker(max_corr=0.85).check_correlation(snap)
        if r.level == 'WARN':
            assert 'pairs' in r.details


class TestCheckBeforeBuy:

    def test_clean_portfolio_ok(self):
        snap = _make_snapshot(
            positions={'A': 20_000},
            equity=100_000,
            peak_equity=100_000,
        )
        r = PortfolioRiskChecker().check_before_buy(snap)
        assert r.passed

    def test_drawdown_exceeded_blocks(self):
        snap = _make_snapshot(equity=80_000, peak_equity=100_000)
        r = PortfolioRiskChecker(max_drawdown=0.15).check_before_buy(snap)
        assert not r.passed

    def test_check_all_returns_list(self):
        snap = _make_snapshot(equity=100_000, peak_equity=100_000)
        results = PortfolioRiskChecker().check_all(snap)
        assert isinstance(results, list)

    def test_check_all_no_false_positives(self):
        snap = _make_snapshot(
            positions={'A': 10_000},
            equity=100_000,
            peak_equity=100_000,
            returns={'A': _make_returns(60, std=0.005)},
        )
        results = PortfolioRiskChecker().check_all(snap)
        rejects = [r for r in results if not r.passed]
        assert len(rejects) == 0


class TestFromPriceSeries:

    def test_returns_computed(self):
        prices = pd.Series([10.0, 10.5, 10.3, 10.8, 11.0])
        result = PortfolioRiskChecker.from_price_series({'A': prices})
        assert 'A' in result
        assert len(result['A']) == len(prices) - 1  # pct_change drops first row

    def test_first_return_correct(self):
        prices = pd.Series([10.0, 11.0])
        result = PortfolioRiskChecker.from_price_series({'A': prices})
        assert abs(result['A'].iloc[0] - 0.1) < 1e-9


# ===========================================================================
# Plain-Python runner
# ===========================================================================

def _run_class(cls):
    global _passed, _failed
    obj = cls()
    for attr in sorted(dir(cls)):
        if not attr.startswith('test_'):
            continue
        method = getattr(obj, attr)
        if not callable(method):
            continue
        ok = False
        try:
            method()
            ok = True
        except AssertionError as e:
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — {e}")
        except Exception as e:
            _failed += 1
            print(f"  FAIL: {cls.__name__}.{attr} — EXCEPTION: {e}")
        if ok:
            _passed += 1
            print(f"  PASS: {cls.__name__}.{attr}")


if __name__ == '__main__':
    _section('PortfolioSnapshot')
    _run_class(TestPortfolioSnapshot)

    _section('CheckDrawdown')
    _run_class(TestCheckDrawdown)

    _section('CheckSectorConcentration')
    _run_class(TestCheckSectorConcentration)

    _section('CheckVaR')
    _run_class(TestCheckVaR)

    _section('CheckCorrelation')
    _run_class(TestCheckCorrelation)

    _section('CheckBeforeBuy')
    _run_class(TestCheckBeforeBuy)

    _section('FromPriceSeries')
    _run_class(TestFromPriceSeries)

    print('\n' + '=' * 60)
    if _failed > 0:
        print(f'FAIL: {_failed} test(s) failed')
        sys.exit(1)
    else:
        print(f'Phase 4 PortfolioRisk: {_passed} passed, 0 failed')

"""
test_daily_risk_report.py — P0-5 每日风险报告测试

验证：
  1. 无持仓时输出空报告，不抛错
  2. 有持仓时调用 PortfolioRiskChecker.check_cvar 和 MonteCarloStressTest
  3. CVaR 超限时 summary['breach'] 包含对应项
  4. JSON 文件被写入 outputs/risk_daily/
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd


def _mock_returns(n: int = 252, seed: int = 42, mu: float = 0.0005,
                  sigma: float = 0.015) -> pd.Series:
    """构造接近真实日收益率分布的随机序列。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    rets = rng.normal(mu, sigma, n)
    return pd.Series(rets, index=dates)


class TestDailyRiskReport(unittest.TestCase):

    def test_empty_portfolio_writes_note(self):
        """无持仓时报告应包含 note=no_positions 且不抛错。"""
        from scripts.daily_risk_report import run_report

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            # patch 持仓 API 返回空
            with patch('scripts.daily_risk_report._fetch_portfolio_snapshot',
                       return_value={'positions': [], 'equity': 0.0,
                                     'cash': 0.0, 'position_value': 0.0}):
                summary = run_report(
                    n_simulations=100, horizon_days=10,
                    output_dir=out_dir, enable_alert=False,
                )

            self.assertEqual(summary['positions_count'], 0)
            self.assertEqual(summary.get('note'), 'no_positions')
            # JSON 文件应存在
            files = list(out_dir.glob('risk_*.json'))
            self.assertEqual(len(files), 1)

    def test_portfolio_with_positions_runs_mc_and_cvar(self):
        """有持仓时应执行 CVaR 与 MC，写入完整字段。"""
        from scripts.daily_risk_report import run_report

        snapshot = {
            'positions': [
                {'symbol': 'A.SH', 'shares': 1000, 'current_price': 10.0},
                {'symbol': 'B.SH', 'shares': 500, 'current_price': 20.0},
            ],
            'equity': 20_000.0,
            'cash': 0.0,
            'position_value': 20_000.0,
        }
        returns = {
            'A.SH': _mock_returns(seed=1),
            'B.SH': _mock_returns(seed=2),
        }

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            with patch('scripts.daily_risk_report._fetch_portfolio_snapshot',
                       return_value=snapshot), \
                 patch('scripts.daily_risk_report._fetch_returns_for_symbols',
                       return_value=returns):
                summary = run_report(
                    n_simulations=500, horizon_days=10,
                    output_dir=out_dir, enable_alert=False,
                )

            self.assertEqual(summary['positions_count'], 2)
            self.assertIsNotNone(summary['cvar'])
            self.assertIsNotNone(summary['monte_carlo'])
            mc = summary['monte_carlo']
            self.assertEqual(mc['n_simulations'], 500)
            self.assertEqual(mc['horizon_days'], 10)
            self.assertGreaterEqual(mc['p5_final'], 0)
            self.assertLessEqual(mc['p5_final'], mc['p95_final'])

            # JSON 文件应存在并可读
            files = list(out_dir.glob('risk_*.json'))
            self.assertEqual(len(files), 1)
            with open(files[0]) as f:
                loaded = json.load(f)
            self.assertEqual(loaded['equity'], summary['equity'])

    def test_cvar_breach_recorded(self):
        """构造极端波动让 CVaR 超限，breach 应被记录。"""
        from scripts.daily_risk_report import run_report

        # 大幅波动 → CVaR 易超 cvar_limit
        snapshot = {
            'positions': [
                {'symbol': 'X.SH', 'shares': 1000, 'current_price': 10.0},
            ],
            'equity': 10_000.0,
            'cash': 0.0,
            'position_value': 10_000.0,
        }
        # σ=0.10 = 10% 日波动，远超正常市场
        returns = {'X.SH': _mock_returns(seed=99, mu=-0.005, sigma=0.10)}

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with patch('scripts.daily_risk_report._fetch_portfolio_snapshot',
                       return_value=snapshot), \
                 patch('scripts.daily_risk_report._fetch_returns_for_symbols',
                       return_value=returns):
                summary = run_report(
                    n_simulations=200, horizon_days=10,
                    output_dir=out_dir, enable_alert=False,
                )

            # 应该至少有 CVaR breach
            self.assertTrue(len(summary['breach']) >= 1)
            self.assertTrue(any('CVaR' in b for b in summary['breach']))

    def test_alert_called_on_breach(self):
        """breach 非空时应调用 AlertManager.send_critical。"""
        from scripts.daily_risk_report import _maybe_alert

        summary_with_breach = {
            'date': date.today().isoformat(),
            'equity': 10_000.0,
            'positions_count': 2,
            'breach': ['CVaR_5%'],
            'cvar': {'reason': 'CVaR(95%) = 6.2% >= limit 5.0%'},
            'monte_carlo': {
                'expected_shortfall': 0.06,
                'max_drawdown_p95': 0.10,
            },
        }

        mock_mgr = MagicMock()
        with patch('core.alerting.get_alert_manager', return_value=mock_mgr):
            _maybe_alert(summary_with_breach)

        mock_mgr.send_critical.assert_called_once()
        msg = mock_mgr.send_critical.call_args[0][0]
        self.assertIn('CVaR_5%', msg)

    def test_no_alert_when_no_breach(self):
        """breach 为空时不应触发告警。"""
        from scripts.daily_risk_report import _maybe_alert

        mock_mgr = MagicMock()
        with patch('core.alerting.get_alert_manager', return_value=mock_mgr):
            _maybe_alert({'breach': []})
        mock_mgr.send_critical.assert_not_called()


if __name__ == '__main__':
    unittest.main()

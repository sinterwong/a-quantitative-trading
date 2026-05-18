"""
test_daily_tca.py — P1-12 每日 TCA 报告测试

验证：
  1. 无成交时输出 note=no_trades_today
  2. 有成交时调用 TCAAnalyzer 并写入 JSON
  3. avg_is_bps > 30 → 告警
  4. _calibrate_impact_coefficients 偏离 1.5x 时返回新系数
  5. ImpactEstimator.load_from_config 优先读取 calibration 文件
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


def _mock_trades(n: int = 5, base_price: float = 10.0,
                 slippage_pct: float = 0.001) -> list:
    """构造 n 笔 BUY 单含正向 IS 偏离（exec > decision）。"""
    today = date.today().isoformat()
    return [
        {
            'id': f't{i}',
            'symbol': f'A{i % 3}.SH',
            'direction': 'BUY',
            'shares': 1000,
            'price': base_price * (1 + slippage_pct),  # 决策价后涨 = 不利
            'decision_price': base_price,
            'commission': 5.0,
            'executed_at': f'{today}T10:{30 + i}:00',
        }
        for i in range(n)
    ]


class TestDailyTCA(unittest.TestCase):

    def test_empty_trades_writes_note(self):
        from scripts.daily_tca import run_report
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with patch('scripts.daily_tca._fetch_trades', return_value=[]):
                summary = run_report(
                    target_date=date.today(),
                    output_dir=out, enable_alert=False,
                    enable_calibration=False,
                )
            self.assertEqual(summary['n_trades'], 0)
            self.assertEqual(summary.get('note'), 'no_trades_today')
            files = list(out.glob('tca_*.json'))
            self.assertEqual(len(files), 1)

    def test_trades_produce_report(self):
        from scripts.daily_tca import run_report
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with patch('scripts.daily_tca._fetch_trades',
                       return_value=_mock_trades(n=6, slippage_pct=0.001)):
                summary = run_report(
                    target_date=date.today(),
                    output_dir=out, enable_alert=False,
                    enable_calibration=False,
                )
            self.assertEqual(summary['n_trades'], 6)
            self.assertGreater(summary['avg_is_bps'], 0,
                               'IS 应为正（买入决策价后涨）')
            self.assertIn('A0.SH', summary['by_symbol'])

    def test_alert_on_high_is(self):
        """avg_is_bps > 30 → send_warning。"""
        from scripts.daily_tca import _maybe_alert
        from core.tca import TCAReport

        bad_report = TCAReport(
            n_trades=10, avg_is_bps=35.0, avg_total_cost_bps=5.0,
            median_is_bps=30.0, p95_is_bps=80.0,
            by_symbol={}, by_direction={}, by_regime={},
            by_hour={}, monthly={}, recommended_slippage_bps=40.0,
        )
        mgr = MagicMock()
        with patch('core.alerting.get_alert_manager', return_value=mgr):
            _maybe_alert(bad_report)
        mgr.send_warning.assert_called_once()
        msg = mgr.send_warning.call_args[0][0]
        self.assertIn('35.00', msg)

    def test_no_alert_on_normal_is(self):
        from scripts.daily_tca import _maybe_alert
        from core.tca import TCAReport

        ok_report = TCAReport(
            n_trades=10, avg_is_bps=8.0, avg_total_cost_bps=3.0,
            median_is_bps=5.0, p95_is_bps=15.0,
            by_symbol={}, by_direction={}, by_regime={},
            by_hour={}, monthly={}, recommended_slippage_bps=10.0,
        )
        mgr = MagicMock()
        with patch('core.alerting.get_alert_manager', return_value=mgr):
            _maybe_alert(ok_report)
        mgr.send_warning.assert_not_called()


class TestCalibration(unittest.TestCase):

    def test_calibrate_within_tolerance_returns_none(self):
        """偏离 < 1.5x 时不调整。"""
        from scripts.daily_tca import _calibrate_impact_coefficients
        # 实际 IS = 1.2x baseline → 在容忍区间
        self.assertIsNone(_calibrate_impact_coefficients(12.0, 10.0))

    def test_calibrate_high_is_scales_up(self):
        """实际 IS 远高于 baseline → 放大系数。"""
        from scripts.daily_tca import _calibrate_impact_coefficients
        from core.execution.impact_estimator import ImpactEstimator
        before_perm = ImpactEstimator.PERMANENT_COEFF

        cal = _calibrate_impact_coefficients(20.0, 10.0)   # 2.0x
        self.assertIsNotNone(cal)
        self.assertGreater(cal['permanent'], before_perm)
        self.assertEqual(cal['scale_ratio'], 2.0)

    def test_calibrate_low_is_scales_down(self):
        """实际 IS 远低于 baseline → 缩小系数。"""
        from scripts.daily_tca import _calibrate_impact_coefficients
        cal = _calibrate_impact_coefficients(5.0, 10.0)   # 0.5x
        self.assertIsNotNone(cal)
        self.assertEqual(cal['scale_ratio'], 0.5)

    def test_calibrate_clamp_floor_ceiling(self):
        """clamp 到 [1.0, 50.0]。"""
        from scripts.daily_tca import _calibrate_impact_coefficients
        # 极端高 IS → 不应让系数超 50
        cal = _calibrate_impact_coefficients(1000.0, 10.0)
        if cal is not None:
            self.assertLessEqual(cal['permanent'], 50.0)
            self.assertLessEqual(cal['temporary'], 50.0)


class TestCalibrationHotReload(unittest.TestCase):

    def test_maybe_calibrate_reloads_impact_estimator(self):
        """_maybe_calibrate 写文件后,应立即热加载到 ImpactEstimator,
        而不是等下次进程重启。"""
        import tempfile, json as _json
        from scripts.daily_tca import _maybe_calibrate
        from core.execution.impact_estimator import ImpactEstimator

        original_perm = ImpactEstimator.PERMANENT_COEFF
        original_temp = ImpactEstimator.TEMPORARY_COEFF
        cal_path = Path(__file__).resolve().parent.parent / 'outputs' / 'tca_calibration.json'
        backup = cal_path.read_text(encoding='utf-8') if cal_path.exists() else None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # 造 5 个 daily TCA 历史记录,IS 均 30bps(远高于 baseline)
            target_date = date(2026, 5, 18)
            for i in range(5):
                d = (target_date - timedelta(days=i + 1)).isoformat()
                (tmp / f'tca_{d}.json').write_text(_json.dumps({
                    'date': d, 'n_trades': 10, 'avg_is_bps': 30.0,
                }), encoding='utf-8')
            try:
                _maybe_calibrate(target_date, output_dir=tmp)
                # baseline=5×sqrt(0.01)+5×0.01≈0.55,rolling 30 → ratio≫1.5 → 触发
                self.assertNotAlmostEqual(
                    ImpactEstimator.PERMANENT_COEFF, original_perm,
                    msg='ImpactEstimator 未被热加载更新',
                )
            finally:
                if backup is not None:
                    cal_path.write_text(backup, encoding='utf-8')
                else:
                    cal_path.unlink(missing_ok=True)
                ImpactEstimator.PERMANENT_COEFF = original_perm
                ImpactEstimator.TEMPORARY_COEFF = original_temp


class TestImpactEstimatorCalibrationFile(unittest.TestCase):

    def test_load_from_config_prefers_calibration(self):
        """outputs/tca_calibration.json 存在时优先读取。"""
        from core.execution.impact_estimator import ImpactEstimator
        # 备份原始默认值
        original_perm = ImpactEstimator.PERMANENT_COEFF
        original_temp = ImpactEstimator.TEMPORARY_COEFF
        # 写一个临时 calibration 文件
        cal_path = Path(__file__).resolve().parent.parent / 'outputs' / 'tca_calibration.json'
        cal_path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if cal_path.exists():
            backup = cal_path.read_text(encoding='utf-8')
        try:
            with open(cal_path, 'w') as f:
                json.dump({
                    'updated_at': datetime.now().isoformat(),
                    'impact_permanent_coeff': 9.5,
                    'impact_temporary_coeff': 11.0,
                    'source': {'test': True},
                }, f)

            ok = ImpactEstimator.load_from_config()
            self.assertTrue(ok)
            self.assertAlmostEqual(ImpactEstimator.PERMANENT_COEFF, 9.5)
            self.assertAlmostEqual(ImpactEstimator.TEMPORARY_COEFF, 11.0)
        finally:
            # 恢复
            if backup is not None:
                cal_path.write_text(backup, encoding='utf-8')
            else:
                cal_path.unlink(missing_ok=True)
            ImpactEstimator.PERMANENT_COEFF = original_perm
            ImpactEstimator.TEMPORARY_COEFF = original_temp


if __name__ == '__main__':
    unittest.main()

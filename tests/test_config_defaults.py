"""R3-4: Tests for core.config_defaults centralization.

Goal: prevent the "5 files have the same magic number" drift that the
review flagged. If someone hardcodes 0.0003 again, this test catches it.
"""
from __future__ import annotations

import unittest

from core import config_defaults


class TestDefaultsContract(unittest.TestCase):
    """The numeric values are the contract — pin them so accidental edits
    blow up a test instead of silently shipping wrong commission rates."""

    def test_commission_rate_is_3bps(self) -> None:
        self.assertEqual(config_defaults.COMMISSION_RATE, 0.0003)

    def test_stamp_tax_is_10bps(self) -> None:
        self.assertEqual(config_defaults.STAMP_TAX_RATE, 0.001)

    def test_slippage_is_5bps(self) -> None:
        self.assertEqual(config_defaults.SLIPPAGE_BPS, 5.0)


class TestConsumersImportSameValue(unittest.TestCase):
    """RiskConfig defaults and SimConfig defaults must mirror config_defaults.
    Any future drift surfaces here, not in a production incident report."""

    def test_riskconfig_uses_centralized_defaults(self) -> None:
        from core.config import RiskConfig
        cfg = RiskConfig()
        self.assertEqual(cfg.commission_rate, config_defaults.COMMISSION_RATE)
        self.assertEqual(cfg.stamp_tax, config_defaults.STAMP_TAX_RATE)
        self.assertEqual(cfg.slippage_bps, config_defaults.SLIPPAGE_BPS)
        self.assertEqual(cfg.max_drawdown, config_defaults.MAX_DRAWDOWN)
        self.assertEqual(cfg.atr_stop_multiplier, config_defaults.ATR_STOP_MULTIPLIER)
        self.assertEqual(cfg.max_net_exposure, config_defaults.MAX_NET_EXPOSURE)
        self.assertEqual(cfg.max_sector_weight, config_defaults.MAX_SECTOR_WEIGHT)

    def test_simconfig_uses_centralized_defaults(self) -> None:
        from core.brokers.simulated import SimConfig
        cfg = SimConfig()
        self.assertEqual(cfg.commission_rate, config_defaults.COMMISSION_RATE)
        self.assertEqual(cfg.stamp_tax_rate, config_defaults.STAMP_TAX_RATE)
        self.assertEqual(cfg.slippage_bps, config_defaults.SLIPPAGE_BPS)


class TestDumpEffectiveCli(unittest.TestCase):
    """The dump-effective CLI is the ops-facing tool. Smoke-test that it
    runs end-to-end and produces parseable JSON containing the expected
    sections."""

    def test_cli_runs_and_produces_json(self) -> None:
        import json
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, '-m', 'core.config', 'dump-effective'],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, f'stderr: {result.stderr}')
        data = json.loads(result.stdout)
        self.assertIn('risk', data)
        self.assertIn('portfolio', data)
        self.assertEqual(data['risk']['commission_rate'],
                         config_defaults.COMMISSION_RATE)


if __name__ == '__main__':
    unittest.main()

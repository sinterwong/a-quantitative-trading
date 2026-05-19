"""R0-4: 回测中静默吞错的关键路径必须计数+日志，不能假装"无信号"。"""
from __future__ import annotations

import logging
import unittest
from typing import List

import numpy as np
import pandas as pd

from core.backtest_engine import BacktestConfig, BacktestEngine
from core.factors.base import Factor, FactorCategory, Signal


def _make_price_series(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0.0, 0.1, n))
    high = close + rng.uniform(0.05, 0.3, n)
    low = close - rng.uniform(0.05, 0.3, n)
    open_ = close + rng.normal(0.0, 0.05, n)
    vol = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol,
    }, index=dates)


class _AlwaysFailFactor(Factor):
    name = 'AlwaysFail'
    category = FactorCategory.PRICE_MOMENTUM

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        # 返回非空 Series，让 signals() 阶段才报错
        return pd.Series(1.0, index=data.index)

    def signals(self, fv: pd.Series, price: float) -> List[Signal]:
        raise RuntimeError('simulated factor bug')


class TestBacktestDegradedSteps(unittest.TestCase):

    def test_factor_signal_failure_recorded_in_degraded_steps(self):
        """因子在 signals() 阶段抛错 → 回测仍跑完, 但 degraded_steps 记录次数。"""
        engine = BacktestEngine(config=BacktestConfig(initial_equity=100_000.0))
        engine.load_data('TEST', _make_price_series(60))
        engine.add_strategy(_AlwaysFailFactor(), signal_threshold=1.0)

        with self.assertLogs('core.backtest_engine', level='WARNING') as cm:
            result = engine.run()

        # 回测不应崩溃
        self.assertIsNotNone(result.equity_curve)
        # 降级计数必须 > 0（因子在大多数 bar 上都失败）
        self.assertIn('factor._AlwaysFailFactor', result.degraded_steps)
        self.assertGreater(result.degraded_steps['factor._AlwaysFailFactor'], 0)
        # 必须有 warning 日志
        self.assertTrue(
            any('AlwaysFail' in m for m in cm.output),
            f"未找到 AlwaysFail 警告日志: {cm.output[:3]}",
        )
        # 末尾汇总日志也必须出现
        self.assertTrue(
            any('degraded steps' in m.lower() for m in cm.output),
            "未找到 'degraded steps' 汇总日志",
        )

    def test_no_degradation_means_empty_dict(self):
        """正常因子跑完无任何降级 → degraded_steps 为空 dict。"""
        # 一个永远不报错、不发信号的因子
        class _SilentFactor(Factor):
            name = 'Silent'
            category = FactorCategory.PRICE_MOMENTUM

            def evaluate(self, data: pd.DataFrame) -> pd.Series:
                return pd.Series(0.0, index=data.index)

            def signals(self, fv, price):
                return []

        engine = BacktestEngine(config=BacktestConfig(initial_equity=100_000.0))
        engine.load_data('TEST', _make_price_series(60))
        engine.add_strategy(_SilentFactor(), signal_threshold=1.0)
        result = engine.run()
        self.assertEqual(result.degraded_steps, {})


if __name__ == '__main__':
    unittest.main()

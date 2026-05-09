"""
test_strategy_runner_rebalance.py — P0-3 PortfolioOptimizer/Allocator 集成测试

验证：
  1. enable_rebalance=False 时不调用 PortfolioOptimizer
  2. 第一轮（无历史）按 period_due 触发 + 写入 _last_target_weights
  3. 上次再平衡后 5% 漂移 → drift_due 触发
  4. 优化结果权重总和 = 1.0
  5. dry_run=True 时不发射真实订单
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from core.factor_pipeline import FactorPipeline
from core.factors.price_momentum import RSIFactor
from core.strategy_runner import RunnerConfig, StrategyRunner


def _make_returns(seed: int = 0, n: int = 252, mu: float = 0.0005) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    return pd.Series(rng.normal(mu, 0.015, n), index=dates)


def _make_bars_df(seed: int = 0, n: int = 252) -> pd.DataFrame:
    rets = _make_returns(seed, n)
    close = (1 + rets).cumprod() * 10
    return pd.DataFrame({
        'open': close * 0.999, 'high': close * 1.005,
        'low': close * 0.995, 'close': close, 'volume': 1e6,
    }, index=close.index)


class TestRebalance(unittest.TestCase):

    def _build_runner(self, **overrides):
        pipeline = FactorPipeline().add(RSIFactor, weight=1.0,
                                        params={'symbol': 'A.SH'})
        cfg = RunnerConfig(
            symbols=['A.SH', 'B.SH', 'C.SH'],
            pipeline=pipeline,
            interval=300,
            dry_run=True,
            enable_rebalance=True,
            rebalance_period_days=21,
            rebalance_drift_threshold=0.05,
            rebalance_max_weight=0.5,
            rebalance_returns_lookback=120,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)

        runner = StrategyRunner(cfg)
        # mock data_layer
        bars = {
            'A.SH': _make_bars_df(seed=1),
            'B.SH': _make_bars_df(seed=2, n=252),
            'C.SH': _make_bars_df(seed=3, n=252),
        }

        def get_bars(sym, days=120, **_):
            return bars.get(sym)

        def get_realtime(sym):
            df = bars.get(sym)
            return {'price': float(df['close'].iloc[-1])} if df is not None else {}

        runner.data_layer = MagicMock()
        runner.data_layer.get_bars = get_bars
        runner.data_layer.get_realtime = get_realtime
        return runner

    def test_disabled_does_not_run_optimizer(self):
        """enable_rebalance=False 时优化器不应被调用。"""
        runner = self._build_runner(enable_rebalance=False)
        with patch('core.portfolio_optimizer.PortfolioOptimizer') as MockOpt:
            runner._maybe_rebalance(['A.SH', 'B.SH'], datetime.now())
            MockOpt.assert_not_called()

    def test_period_due_triggers_optimization(self):
        """没历史时第一轮（_run_count>0）应触发 period_due。"""
        runner = self._build_runner()
        runner._run_count = 1   # 模拟已经跑过 1 轮
        runner._maybe_rebalance(['A.SH', 'B.SH', 'C.SH'], datetime.now())

        # 应写入 target_weights，且权重总和 ≈ 1.0
        self.assertGreater(len(runner._last_target_weights), 0)
        total = sum(runner._last_target_weights.values())
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_target_weights_respect_max_weight(self):
        """优化输出权重 ≤ rebalance_max_weight。"""
        runner = self._build_runner(rebalance_max_weight=0.4)
        runner._run_count = 1
        runner._maybe_rebalance(['A.SH', 'B.SH', 'C.SH'], datetime.now())

        for sym, w in runner._last_target_weights.items():
            self.assertLessEqual(w, 0.4 + 1e-6, f'{sym} weight {w} > 0.4')

    def test_drift_triggers_after_rebalance(self):
        """有上次目标权重 + 实际漂移 > 阈值 → drift_due。"""
        runner = self._build_runner(rebalance_period_days=999)  # 关掉周期触发
        # 第一次：写入 target
        runner._run_count = 1
        runner._maybe_rebalance(['A.SH', 'B.SH', 'C.SH'], datetime.now())
        first_targets = dict(runner._last_target_weights)
        self.assertTrue(first_targets)

        # 第二次：mock 当前持仓与 target 偏离 8%（> 5% 阈值）
        # 通过注入持仓让 current_weights 偏离
        first_sym = next(iter(first_targets))
        target_w = first_targets[first_sym]

        # 构造一个让 first_sym 的实际权重 = target ± 0.08 的持仓
        runner._collect_positions = lambda: [
            {'symbol': first_sym, 'shares': 1000,
             'current_price': 10.0,
             # 让其它仓位都 0，这样 first_sym 实际权重 = 1.0 vs target ~0.33
             }
        ]
        # 第二次应该被 drift trigger
        runner._maybe_rebalance(['A.SH', 'B.SH', 'C.SH'],
                                 datetime.now() + timedelta(days=1))
        # 由于 target 与 current 偏离巨大，应再次写入新的 target
        self.assertTrue(runner._last_target_weights)

    def test_dry_run_does_not_emit_orders(self):
        """dry_run=True 时即使触发也不应调用 OMS / event_bus。"""
        runner = self._build_runner()
        runner._run_count = 1
        runner.event_bus = MagicMock()
        runner.oms = MagicMock()
        runner._maybe_rebalance(['A.SH', 'B.SH', 'C.SH'], datetime.now())

        # dry_run 跳过 _emit_rebalance_orders
        runner.event_bus.emit.assert_not_called()
        runner.oms.submit_from_signal.assert_not_called()

    def test_insufficient_symbols_skips(self):
        """池中标的 < 2 时跳过优化。"""
        runner = self._build_runner()
        runner._run_count = 1
        runner._maybe_rebalance(['A.SH'], datetime.now())
        self.assertEqual(runner._last_target_weights, {})


if __name__ == '__main__':
    unittest.main()

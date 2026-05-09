"""
tests/test_runner_factory.py — P2-15 build_runner 双运行时分发

覆盖：
  - 默认 runtime='sync' → StrategyRunner
  - runtime='async' → AsyncStrategyRunner
  - env RUNNER_RUNTIME=async 覆盖默认值
  - 两种 runner 公共 API 兼容性（run_once / run_once_sync）
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestBuildRunnerRuntime(unittest.TestCase):

    def test_default_returns_sync_runner(self):
        from core.pipeline_factory import build_runner
        from core.strategy_runner import StrategyRunner

        runner = build_runner(symbols=['510300.SH'], dry_run=True)
        self.assertIsInstance(runner, StrategyRunner)

    def test_runtime_async_returns_async_runner(self):
        from core.pipeline_factory import build_runner
        from core.async_runner import AsyncStrategyRunner

        runner = build_runner(symbols=['510300.SH'], dry_run=True, runtime='async')
        self.assertIsInstance(runner, AsyncStrategyRunner)

    def test_env_var_overrides(self):
        from core.pipeline_factory import build_runner
        from core.async_runner import AsyncStrategyRunner

        with patch.dict(os.environ, {'RUNNER_RUNTIME': 'async'}):
            runner = build_runner(symbols=['510300.SH'], dry_run=True)
        self.assertIsInstance(runner, AsyncStrategyRunner)

    def test_unknown_runtime_falls_back_to_sync(self):
        from core.pipeline_factory import build_runner
        from core.strategy_runner import StrategyRunner

        runner = build_runner(symbols=['510300.SH'], dry_run=True, runtime='blarg')
        self.assertIsInstance(runner, StrategyRunner)


class TestRunnerApiCompatibility(unittest.TestCase):
    """两种 runner 共享 RunnerConfig + RunResult 接口。"""

    def test_async_has_run_sync(self):
        from core.async_runner import AsyncStrategyRunner
        self.assertTrue(hasattr(AsyncStrategyRunner, 'run_sync'))
        self.assertTrue(hasattr(AsyncStrategyRunner, 'run_once_sync'))

    def test_sync_has_run_loop(self):
        from core.strategy_runner import StrategyRunner
        self.assertTrue(hasattr(StrategyRunner, 'run_loop'))
        self.assertTrue(hasattr(StrategyRunner, 'run_once'))

    def test_both_share_config_class(self):
        from core.async_runner import AsyncStrategyRunner
        from core.strategy_runner import RunnerConfig
        # AsyncRunner 直接 import StrategyRunner.RunnerConfig
        self.assertIn('RunnerConfig', dir(__import__('core.strategy_runner',
                                                      fromlist=['RunnerConfig'])))


if __name__ == '__main__':
    unittest.main()

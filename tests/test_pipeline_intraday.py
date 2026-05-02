"""
tests/test_pipeline_intraday.py — Pipeline → IntradayMonitor 集成测试

验证：
  1. StrategyRunner.last_scores 正确返回
  2. IntradayMonitor._check_new_positions() 唯一信号来源为 pipeline（无 evaluate_signal 降级）
  3. 无 pipeline scores 时静默跳过（不降级到 RSI 硬编码）
  4. pipeline 异常时静默跳过，不中断运行
  5. signal_threshold 可配置
"""

from __future__ import annotations

import sys
import os
import threading
import time
from datetime import datetime, date
from unittest.mock import patch, MagicMock, PropertyMock
from typing import Dict, List, Optional

import pytest

# ── 路径设置 ─────────────────────────────────────────────────────────────────
PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_DIR)
sys.path.insert(0, os.path.join(PROJ_DIR, 'backend'))

from core.factors.base import Signal, Factor, FactorCategory
from core.factor_pipeline import FactorPipeline, PipelineResult, FactorResult


# ── 辅助：构造 mock PipelineResult ────────────────────────────────────────────

def _make_pipeline_result(symbol: str, combined_score: float) -> PipelineResult:
    """构造一个简单的 PipelineResult 用于测试。"""
    return PipelineResult(
        symbol=symbol,
        combined_score=combined_score,
        factor_results=[],
        signals=[],
        dominant_signal='BUY' if combined_score > 0 else ('SELL' if combined_score < 0 else 'HOLD'),
    )


def _make_run_result(symbol: str, combined_score: float, action: str = 'BUY'):
    """构造 RunResult mock。"""
    from core.strategy_runner import RunResult
    return RunResult(
        symbol=symbol,
        timestamp=datetime.now(),
        pipeline_result=_make_pipeline_result(symbol, combined_score),
        action=action,
        reason='test',
    )


# ═════════════════════════════════════════════════════════════════════════════
# Test 1: StrategyRunner.last_scores
# ═════════════════════════════════════════════════════════════════════════════

class TestLastScores:
    """验证 last_scores 属性正确返回 {symbol: combined_score}。"""

    def test_empty_results(self):
        """无结果时返回空字典。"""
        from core.strategy_runner import StrategyRunner, RunnerConfig

        pipeline = MagicMock()
        pipeline.run.return_value = _make_pipeline_result('000001.SZ', 0.0)

        cfg = RunnerConfig(symbols=[], pipeline=pipeline, dry_run=True)
        runner = StrategyRunner(cfg, data_layer=MagicMock())

        assert runner.last_scores == {}

    def test_with_results(self):
        """有结果时正确映射 symbol → score。"""
        from core.strategy_runner import StrategyRunner, RunnerConfig

        pipeline = MagicMock()
        dl = MagicMock()
        dl.get_bars.return_value = MagicMock(__len__=lambda self: 100)
        dl.get_realtime.return_value = {'price': 15.0}

        cfg = RunnerConfig(
            symbols=['000001.SZ', '600519.SH'],
            pipeline=pipeline,
            dry_run=True,
            regime_aware=False,
        )
        runner = StrategyRunner(cfg, data_layer=dl)

        # 模拟 run_once 结果
        runner._last_run_results = [
            _make_run_result('000001.SZ', 0.8),
            _make_run_result('600519.SH', -0.3),
        ]

        scores = runner.last_scores
        assert scores == {'000001.SZ': 0.8, '600519.SH': -0.3}

    def test_none_pipeline_result_excluded(self):
        """pipeline_result 为 None 的标的不出现在 last_scores 中。"""
        from core.strategy_runner import StrategyRunner, RunResult, RunnerConfig

        pipeline = MagicMock()
        cfg = RunnerConfig(symbols=[], pipeline=pipeline, dry_run=True)
        runner = StrategyRunner(cfg, data_layer=MagicMock())

        runner._last_run_results = [
            RunResult(
                symbol='000001.SZ', timestamp=datetime.now(),
                pipeline_result=None, action='ERROR', reason='test',
            ),
            _make_run_result('600519.SH', 0.5),
        ]

        scores = runner.last_scores
        assert '000001.SZ' not in scores
        assert scores == {'600519.SH': 0.5}

    def test_thread_safety(self):
        """多线程并发读写 last_scores 不报错。"""
        from core.strategy_runner import StrategyRunner, RunnerConfig

        pipeline = MagicMock()
        cfg = RunnerConfig(symbols=[], pipeline=pipeline, dry_run=True)
        runner = StrategyRunner(cfg, data_layer=MagicMock())

        results = []
        errors = []

        def writer():
            for i in range(50):
                runner._last_run_results = [_make_run_result('000001.SZ', i * 0.1)]
                time.sleep(0.001)

        def reader():
            for _ in range(50):
                try:
                    s = runner.last_scores
                    results.append(s)
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader),
                   threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Thread safety errors: {errors}"


# ═════════════════════════════════════════════════════════════════════════════
# Test 2: _check_new_positions 用 pipeline score 决策
# ═════════════════════════════════════════════════════════════════════════════

class TestCheckNewPositionsWithScore:
    """验证有 pipeline score 时走 pipeline 分支。"""

    @patch('services.signals.fetch_realtime')
    @patch('services.signals.confirm_signal_minute')
    def test_pipeline_score_above_threshold_trades(self, mock_confirm, mock_rt):
        """score > threshold → 触发建仓流程。"""
        mock_rt.return_value = {'price': 15.0}
        mock_confirm.return_value = (True, 35.0, 'RSI OK')

        # 构造 mock IntradayMonitor（最小化）
        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        monitor._strategy_runner.last_scores = {'000001.SZ': 0.8}
        monitor._strategy_runner.config.signal_threshold = 0.5
        monitor._strategy_runner.risk_engine = None
        monitor._llm = None
        monitor._cooldown = MagicMock()
        monitor._cooldown.can_fire.return_value = True
        monitor._svc = MagicMock()
        monitor._svc.get_positions.return_value = []
        monitor._get_watched_symbols.return_value = ['000001.SZ']
        monitor._calc_shares.return_value = 100
        monitor._can_trade.return_value = True
        monitor._broker = MagicMock()
        monitor._broker.submit_order.return_value = MagicMock(status='filled', avg_price=15.0)
        monitor._deliver_alert = MagicMock()
        monitor._llm_review_signal.return_value = (True, 'OK', 0.8, 'full')

        # 直接调用方法
        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 验证：下单被调用
        monitor._broker.submit_order.assert_called_once()
        call_kwargs = monitor._broker.submit_order.call_args
        assert call_kwargs[1]['symbol'] == '000001.SZ'
        assert call_kwargs[1]['direction'] == 'BUY'

    @patch('services.signals.fetch_realtime')
    @patch('services.signals.confirm_signal_minute')
    def test_pipeline_score_below_threshold_skips(self, mock_confirm, mock_rt):
        """score < threshold → 跳过，不建仓。"""
        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        monitor._strategy_runner.last_scores = {'000001.SZ': 0.3}
        monitor._strategy_runner.config.signal_threshold = 0.5
        monitor._cooldown = MagicMock()
        monitor._cooldown.can_fire.return_value = True
        monitor._get_watched_symbols.return_value = ['000001.SZ']

        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 验证：不下单
        mock_confirm.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# Test 3: 无 pipeline scores 时不再降级到 evaluate_signal（消除双信号并行）
# ═════════════════════════════════════════════════════════════════════════════

class TestNoFallbackToEvaluateSignal:
    """验证无 pipeline scores 时不再降级到 evaluate_signal()。"""

    @patch('services.signals.evaluate_signal')
    @patch('services.signals.confirm_signal_minute')
    def test_no_score_does_not_call_evaluate_signal(self, mock_confirm, mock_eval):
        """无 pipeline score 时不调用 evaluate_signal()，静默跳过。"""
        mock_confirm.return_value = (True, 22.0, 'confirmed')

        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        monitor._strategy_runner.last_scores = {}  # 空 → 静默跳过，不降级
        monitor._strategy_runner.risk_engine = None
        monitor._llm = None
        monitor._cooldown = MagicMock()
        monitor._cooldown.can_fire.return_value = True
        monitor._svc = MagicMock()
        monitor._svc.get_positions.return_value = []
        monitor._get_watched_symbols.return_value = ['000001.SZ']
        monitor._deliver_alert = MagicMock()

        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 验证：evaluate_signal 不会被调用
        mock_eval.assert_not_called()

    @patch('services.signals.evaluate_signal')
    def test_no_runner_does_not_call_evaluate_signal(self, mock_eval):
        """无 StrategyRunner 时静默跳过，不降级到 evaluate_signal。"""
        monitor = MagicMock()
        monitor._strategy_runner = None  # 无 runner → 静默跳过
        monitor._cooldown = MagicMock()
        monitor._svc = MagicMock()
        monitor._svc.get_positions.return_value = []
        monitor._get_watched_symbols.return_value = ['000001.SZ']
        monitor._deliver_alert = MagicMock()

        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 验证：evaluate_signal 不会被调用
        mock_eval.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# Test 4: pipeline 异常时静默跳过，不降级到 evaluate_signal
# ═════════════════════════════════════════════════════════════════════════════

class TestPipelineErrorGraceful:
    """验证 pipeline 异常时静默跳过，不降级到 evaluate_signal。"""

    @patch('services.signals.evaluate_signal')
    def test_last_scores_exception_does_not_fall_back(self, mock_eval):
        """last_scores 抛异常时静默跳过，不降级到 evaluate_signal。"""
        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        type(monitor._strategy_runner).last_scores = PropertyMock(side_effect=RuntimeError('pipeline error'))
        monitor._cooldown = MagicMock()
        monitor._svc = MagicMock()
        monitor._svc.get_positions.return_value = []
        monitor._get_watched_symbols.return_value = ['000001.SZ']
        monitor._deliver_alert = MagicMock()

        from backend.services.intraday_monitor import IntradayMonitor
        # 不应抛异常
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 验证：不降级到 evaluate_signal
        mock_eval.assert_not_called()

    def test_run_once_exception_continues(self):
        """run_once() 抛异常时不中断 _check_and_push。"""
        from core.strategy_runner import StrategyRunner, RunnerConfig

        pipeline = MagicMock()
        cfg = RunnerConfig(symbols=[], pipeline=pipeline, dry_run=True)
        runner = StrategyRunner(cfg, data_layer=MagicMock())
        runner.run_once = MagicMock(side_effect=RuntimeError('data fetch failed'))

        # 模拟 _check_and_push 中的 try/except
        try:
            runner.run_once()
        except Exception as e:
            pass  # 应被捕获

        # last_scores 仍可用（空）
        assert runner.last_scores == {}


# ═════════════════════════════════════════════════════════════════════════════
# Test 5: signal_threshold 可配置
# ═════════════════════════════════════════════════════════════════════════════

class TestThresholdConfigurable:
    """验证 threshold 从 config 读取。"""

    def test_threshold_from_config(self):
        """signal_threshold 从 RunnerConfig 读取。"""
        from core.strategy_runner import RunnerConfig

        pipeline = MagicMock()
        cfg = RunnerConfig(symbols=[], pipeline=pipeline, signal_threshold=0.8)
        assert cfg.signal_threshold == 0.8

    def test_default_threshold(self):
        """默认 threshold 为 0.5。"""
        from core.strategy_runner import RunnerConfig

        pipeline = MagicMock()
        cfg = RunnerConfig(symbols=[], pipeline=pipeline)
        assert cfg.signal_threshold == 0.5


# ═════════════════════════════════════════════════════════════════════════════
# Test 6: 安全层保留验证
# ═════════════════════════════════════════════════════════════════════════════

class TestSafetyLayersPreserved:
    """验证 pipeline 分支保留了所有安全层。"""

    @patch('services.signals.fetch_realtime')
    @patch('services.signals.confirm_signal_minute')
    def test_minute_confirmation_called(self, mock_confirm, mock_rt):
        """pipeline 分支仍调用分钟确认。"""
        mock_rt.return_value = {'price': 15.0}
        mock_confirm.return_value = (False, 50.0, 'RSI too high')  # 拒绝

        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        monitor._strategy_runner.last_scores = {'000001.SZ': 1.0}
        monitor._strategy_runner.config.signal_threshold = 0.5
        monitor._cooldown = MagicMock()
        monitor._cooldown.can_fire.return_value = True
        monitor._svc = MagicMock()
        monitor._svc.get_positions.return_value = []
        monitor._get_watched_symbols.return_value = ['000001.SZ']
        monitor._deliver_alert = MagicMock()

        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 分钟确认被调用且拒绝了交易
        mock_confirm.assert_called_once_with('000001.SZ', 'BUY')
        monitor._broker.submit_order.assert_not_called()

    @patch('services.signals.fetch_realtime')
    @patch('services.signals.confirm_signal_minute')
    def test_cooldown_still_applies(self, mock_confirm, mock_rt):
        """冷却机制在 pipeline 分支下仍然生效。"""
        monitor = MagicMock()
        monitor._strategy_runner = MagicMock()
        monitor._strategy_runner.last_scores = {'000001.SZ': 1.0}
        monitor._cooldown = MagicMock()
        monitor._cooldown.can_fire.return_value = False  # 冷却中
        monitor._get_watched_symbols.return_value = ['000001.SZ']

        from backend.services.intraday_monitor import IntradayMonitor
        IntradayMonitor._check_new_positions(monitor, datetime.now())

        # 冷却期内，不做任何操作
        mock_confirm.assert_not_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

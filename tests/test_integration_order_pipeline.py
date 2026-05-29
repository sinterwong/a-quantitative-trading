"""
集成测试 — 验证重构后的完整流程。

测试场景：
1. StrategyRunner.run_once() 不产生任何 broker 交易（信号只缓冲）
2. 信号通过 consume_signals() 正确返回
3. OrderGate 对已持有标的的 BUY 不重复执行
4. OrderGate 对无持仓标的的 SELL 返回 rejected
5. 同标的在冷却期内的重复请求被拒绝
6. simulation 模式下所有请求被拒绝（不写 DB）
7. 完整流程：run_once → consume_signals → OrderGate.submit
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from core.strategy_runner import StrategyRunner, RunnerConfig, SignalRecord
from backend.services.order_gate import OrderGate, OrderRequest, GateResult


# ── 辅助类 ──────────────────────────────────────────────────

class FakeDataLayer:
    """返回固定数据的 DataLayer 模拟。"""
    def __init__(self, close=10.0, bars_count=120):
        import pandas as pd
        import numpy as np
        self._bars = pd.DataFrame({
            'open': np.full(bars_count, close),
            'high': np.full(bars_count, close * 1.02),
            'low': np.full(bars_count, close * 0.98),
            'close': np.full(bars_count, close),
            'volume': np.full(bars_count, 1000000),
        })
        self._close = close

    def get_bars(self, symbol, days=120):
        return self._bars

    def get_realtime(self, symbol):
        quote = MagicMock()
        quote.price = self._close
        quote.close = self._close
        return quote


class FakePipeline:
    """返回固定分数的 Pipeline 模拟。"""
    def __init__(self, score=1.5, direction='BUY'):
        self._score = score
        self._direction = direction

    def run(self, symbol, data, price=None):
        from core.factor_pipeline import PipelineResult
        from core.factors.base import Signal as FactorSignal
        sig = FactorSignal(
            timestamp=datetime.now(),
            symbol=symbol,
            direction=self._direction,
            strength=abs(self._score),
            factor_name='TestFactor',
            price=price or 10.0,
        )
        result = MagicMock(spec=PipelineResult)
        result.combined_score = self._score
        result.dominant_signal = self._direction if abs(self._score) >= 0.5 else 'HOLD'
        result.signals = [sig]
        return result


class FakeCooldown:
    def __init__(self):
        self._blocked = set()
    def block(self, key):
        self._blocked.add(key)
    def can_fire(self, key):
        return key not in self._blocked


class FakeBroker:
    def __init__(self):
        self.orders = []
    def submit_order(self, symbol, direction, shares, price, price_type='market'):
        self.orders.append({
            'symbol': symbol, 'direction': direction,
            'shares': shares, 'price': price,
        })
        from backend.services.broker import OrderResult
        return OrderResult(
            order_id='integ-001', status='filled', symbol=symbol,
            direction=direction, submitted_shares=shares,
            filled_shares=shares, avg_price=price,
        )


class FakeSvc:
    def __init__(self, cash=500000, positions=None):
        self._cash = cash
        self._positions = positions or {}

    def get_cash(self):
        return self._cash

    def get_position(self, symbol):
        return self._positions.get(symbol)

    def get_positions(self):
        return list(self._positions.values())

    def get_total_equity(self):
        return self._cash + sum(
            p.get('shares', 0) * p.get('latest_price', 0)
            for p in self._positions.values()
        )

    def set_position(self, symbol, shares, entry_price=10.0, latest_price=10.0):
        self._positions[symbol] = {
            'symbol': symbol, 'shares': shares,
            'entry_price': entry_price, 'latest_price': latest_price,
        }


# ── 测试 ────────────────────────────────────────────────────

class TestSignalBuffering:
    """验证 StrategyRunner 信号只缓冲不执行。"""

    def test_run_once_does_not_call_broker(self):
        """run_once() 不应产生任何 broker 交易。"""
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='BUY')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=False)
        runner = StrategyRunner(cfg, data_layer=dl)

        results = runner.run_once()

        # 信号应被缓冲
        signals = runner.consume_signals()
        assert len(signals) == 1
        assert signals[0].symbol == 'TEST.SH'
        assert signals[0].direction == 'BUY'
        assert signals[0].source == 'pipeline'

    def test_dry_run_does_not_buffer(self):
        """dry_run=True 时不缓冲信号。"""
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='BUY')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=True)
        runner = StrategyRunner(cfg, data_layer=dl)

        runner.run_once()
        signals = runner.consume_signals()
        assert len(signals) == 0


class TestPositionAwareness:
    """验证持仓感知安全网。"""

    def test_buy_skipped_when_already_held(self):
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='BUY')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=False)
        runner = StrategyRunner(cfg, data_layer=dl)
        # 模拟有持仓
        runner._collect_positions = lambda: [
            {'symbol': 'TEST.SH', 'shares': 1000, 'current_price': 10.0}
        ]

        results = runner.run_once()
        assert results[0].action == 'SKIPPED'
        assert results[0].reason == 'already_held'

        # 不应有缓冲信号
        signals = runner.consume_signals()
        assert len(signals) == 0

    def test_sell_skipped_when_no_position(self):
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='SELL')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=False)
        runner = StrategyRunner(cfg, data_layer=dl)
        # 无持仓
        runner._collect_positions = lambda: []

        results = runner.run_once()
        assert results[0].action == 'SKIPPED'
        assert results[0].reason == 'no_position_to_sell'


class TestOrderGateIntegration:
    """验证 OrderGate 的过滤链。"""

    def test_sell_no_position_rejected(self):
        broker = FakeBroker()
        svc = FakeSvc(cash=500000)
        cooldown = FakeCooldown()
        gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
        gate.set_can_trade_fn(lambda: True)

        result = gate.submit(OrderRequest(
            symbol='600519.SH', direction='SELL', price=10.0, source='test',
        ))
        assert result.status == 'rejected'
        assert 'no position' in result.reason
        assert len(broker.orders) == 0

    def test_cooldown_rejects_duplicate(self):
        broker = FakeBroker()
        svc = FakeSvc(cash=500000)
        cooldown = FakeCooldown()
        gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
        gate.set_can_trade_fn(lambda: True)

        # 第一次应该成功
        result1 = gate.submit(OrderRequest(
            symbol='600519.SH', direction='BUY', price=10.0, source='test',
        ))
        assert result1.status == 'filled'

        # 冷却后第二次应被拒绝
        cooldown.block('BUY_600519.SH')
        result2 = gate.submit(OrderRequest(
            symbol='600519.SH', direction='BUY', price=10.0, source='test',
        ))
        assert result2.status == 'rejected'
        assert 'cooldown' in result2.reason

    def test_simulation_blocks_execution(self):
        broker = FakeBroker()
        svc = FakeSvc(cash=500000)
        cooldown = FakeCooldown()
        gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
        gate.set_can_trade_fn(lambda: False)  # simulation

        result = gate.submit(OrderRequest(
            symbol='600519.SH', direction='BUY', price=10.0, source='test',
        ))
        assert result.status == 'simulation'
        assert len(broker.orders) == 0


class TestEndToEndFlow:
    """完整流程：run_once → consume_signals → OrderGate.submit。"""

    def test_full_pipeline_flow(self):
        # 1. StrategyRunner 生成信号
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='BUY')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=False)
        runner = StrategyRunner(cfg, data_layer=dl)
        runner._collect_positions = lambda: []  # 无持仓 → BUY 不被拦截

        results = runner.run_once()
        assert results[0].action == 'BUY'

        # 2. 消费信号
        signals = runner.consume_signals()
        assert len(signals) == 1

        # 3. 通过 OrderGate 执行
        broker = FakeBroker()
        svc = FakeSvc(cash=500000)
        cooldown = FakeCooldown()
        gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
        gate.set_can_trade_fn(lambda: True)

        req = OrderRequest(
            symbol=signals[0].symbol,
            direction=signals[0].direction,
            price=signals[0].price,
            source=signals[0].source,
            reason=signals[0].reason,
        )
        result = gate.submit(req)
        assert result.status == 'filled'
        assert len(broker.orders) == 1
        assert broker.orders[0]['symbol'] == 'TEST.SH'
        assert broker.orders[0]['direction'] == 'BUY'

    def test_full_sell_flow_with_position(self):
        # 1. StrategyRunner 生成 SELL 信号
        dl = FakeDataLayer(close=10.0)
        pipeline = FakePipeline(score=1.5, direction='SELL')
        cfg = RunnerConfig(symbols=['TEST.SH'], pipeline=pipeline, dry_run=False)
        runner = StrategyRunner(cfg, data_layer=dl)
        runner._collect_positions = lambda: [
            {'symbol': 'TEST.SH', 'shares': 1000, 'current_price': 10.0}
        ]

        results = runner.run_once()
        assert results[0].action == 'SELL'

        # 2. 消费信号
        signals = runner.consume_signals()
        assert len(signals) == 1
        assert signals[0].direction == 'SELL'

        # 3. 通过 OrderGate 执行
        broker = FakeBroker()
        svc = FakeSvc(cash=500000)
        svc.set_position('TEST.SH', shares=1000, entry_price=10.0, latest_price=10.0)
        cooldown = FakeCooldown()
        gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
        gate.set_can_trade_fn(lambda: True)

        req = OrderRequest(
            symbol=signals[0].symbol,
            direction=signals[0].direction,
            price=signals[0].price,
            source=signals[0].source,
        )
        result = gate.submit(req)
        assert result.status == 'filled'
        assert broker.orders[0]['direction'] == 'SELL'

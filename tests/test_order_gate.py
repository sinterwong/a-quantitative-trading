"""OrderGate 单元测试 — 统一订单执行入口。"""

import pytest
from unittest.mock import MagicMock, patch
from backend.services.order_gate import OrderGate, OrderRequest, GateResult


class FakeCooldown:
    """可控的冷却模拟。"""
    def __init__(self):
        self._blocked = set()

    def block(self, key):
        self._blocked.add(key)

    def can_fire(self, key):
        return key not in self._blocked


class FakeBroker:
    """可控的 Broker 模拟。"""
    def __init__(self):
        self.orders = []

    def submit_order(self, symbol, direction, shares, price, price_type='market'):
        self.orders.append({
            'symbol': symbol, 'direction': direction,
            'shares': shares, 'price': price, 'price_type': price_type,
        })
        from backend.services.broker import OrderResult
        return OrderResult(
            order_id='test-001', status='filled', symbol=symbol,
            direction=direction, submitted_shares=shares,
            filled_shares=shares, avg_price=price,
        )


class FakeSvc:
    """可控的 PortfolioService 模拟。"""
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


def _make_gate(cash=500000, positions=None, can_trade=True):
    broker = FakeBroker()
    svc = FakeSvc(cash=cash, positions=positions or {})
    cooldown = FakeCooldown()
    gate = OrderGate(broker=broker, svc=svc, cooldown=cooldown)
    gate.set_can_trade_fn(lambda: can_trade)
    return gate, broker, svc, cooldown


class TestOrderGateBasic:

    def test_empty_symbol_rejected(self):
        gate, *_ = _make_gate()
        result = gate.submit(OrderRequest(symbol='', direction='BUY', price=10.0))
        assert result.status == 'rejected'
        assert 'empty symbol' in result.reason

    def test_invalid_direction_rejected(self):
        gate, *_ = _make_gate()
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='HOLD', price=10.0))
        assert result.status == 'rejected'
        assert 'invalid direction' in result.reason

    def test_sell_no_position_rejected(self):
        gate, *_ = _make_gate()
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='SELL', price=10.0))
        assert result.status == 'rejected'
        assert 'no position' in result.reason

    def test_cooldown_rejected(self):
        gate, _, _, cooldown = _make_gate()
        cooldown.block('BUY_600519.SH')
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'rejected'
        assert 'cooldown' in result.reason

    def test_simulation_mode_returns_simulation(self):
        gate, broker, *_ = _make_gate(can_trade=False)
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'simulation'
        assert len(broker.orders) == 0

    def test_buy_filled(self):
        gate, broker, *_ = _make_gate()
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'filled'
        assert len(broker.orders) == 1
        assert broker.orders[0]['symbol'] == '600519.SH'
        assert broker.orders[0]['direction'] == 'BUY'

    def test_sell_with_position_filled(self):
        svc_positions = {'600519.SH': {'symbol': '600519.SH', 'shares': 1000, 'entry_price': 10.0, 'latest_price': 10.0}}
        gate, broker, *_ = _make_gate(positions=svc_positions)
        result = gate.submit(OrderRequest(symbol='600519.SH', direction='SELL', price=10.0, source='exit_engine'))
        assert result.status == 'filled'
        assert len(broker.orders) == 1
        assert broker.orders[0]['direction'] == 'SELL'


class TestOrderGateRiskEngine:

    def test_risk_engine_reject(self):
        gate, broker, _, _ = _make_gate()
        risk_engine = MagicMock()
        risk_result = MagicMock()
        risk_result.passed = False
        risk_result.reason = 'position limit exceeded'
        risk_engine.check.return_value = risk_result
        gate._risk_engine = risk_engine

        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'rejected'
        assert 'risk' in result.reason
        assert len(broker.orders) == 0

    def test_risk_engine_pass(self):
        gate, broker, _, _ = _make_gate()
        risk_engine = MagicMock()
        risk_result = MagicMock()
        risk_result.passed = True
        risk_engine.check.return_value = risk_result
        gate._risk_engine = risk_engine

        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'filled'
        assert len(broker.orders) == 1


class TestOrderGateLLM:

    def test_llm_reject(self):
        gate, broker, _, _ = _make_gate()
        gate.set_llm_review_fn=lambda ctx, direction: (False, 'RSI too high', 0.8, 'full')
        gate._llm_review_fn = lambda ctx, direction: (False, 'RSI too high', 0.8, 'full')

        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'rejected'
        assert 'LLM' in result.reason
        assert len(broker.orders) == 0

    def test_llm_approve(self):
        gate, broker, _, _ = _make_gate()
        gate._llm_review_fn = lambda ctx, direction: (True, 'looks good', 0.9, 'full')

        result = gate.submit(OrderRequest(symbol='600519.SH', direction='BUY', price=10.0, source='pipeline'))
        assert result.status == 'filled'
        assert len(broker.orders) == 1


class TestOrderGateShares:

    def test_explicit_shares(self):
        gate, broker, _, _ = _make_gate()
        result = gate.submit(OrderRequest(
            symbol='600519.SH', direction='BUY', price=10.0,
            shares=500, source='exit_engine',
        ))
        assert result.status == 'filled'
        assert broker.orders[0]['shares'] == 500

    def test_sell_caps_to_held_shares(self):
        svc_positions = {'600519.SH': {'symbol': '600519.SH', 'shares': 300, 'entry_price': 10.0, 'latest_price': 10.0}}
        gate, broker, svc, _ = _make_gate(positions=svc_positions)
        result = gate.submit(OrderRequest(
            symbol='600519.SH', direction='SELL', price=10.0,
            shares=1000, source='exit_engine',
        ))
        assert result.status == 'filled'
        # 应该不超过持仓的 300 股（取整到 100 的倍数）
        assert broker.orders[0]['shares'] <= 300


class TestOrderGateSkipLog:

    def test_skip_recorded(self):
        gate, _, _, _ = _make_gate()
        gate.submit(OrderRequest(symbol='600519.SH', direction='SELL', price=10.0, source='pipeline'))
        skips = gate.skip_log
        assert len(skips) == 1
        assert skips[0].category == 'no_position'
        assert skips[0].symbol == '600519.SH'

    def test_skip_log_capped_at_200(self):
        gate, _, _, _ = _make_gate()
        for i in range(250):
            gate.submit(OrderRequest(symbol='XXXX.SH', direction='SELL', price=10.0, source='test'))
        assert len(gate.skip_log) == 200

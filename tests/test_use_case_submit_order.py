"""R2-1: Direct tests for core.use_cases.submit_order.

These tests exercise the use case without going through Flask, proving the
business logic is independent of the HTTP layer. The endpoint then has only
parsing + dependency injection + exception → status code mapping to test
separately."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from core.use_cases.submit_order import (
    NoReferencePriceError,
    RiskCheckFailedError,
    RiskRejectedError,
    SubmitOrderRequest,
    submit_order,
)


# ─── Fakes ─────────────────────────────────────────────────────────────────


@dataclass
class _FakeOrderResult:
    order_id: str = 'OID-1'
    status: str = 'filled'
    filled_shares: int = 100
    avg_price: float = 10.0
    reason: str = ''
    submitted_at: Optional[str] = '2026-05-19T10:00:00'
    filled_at: Optional[str] = '2026-05-19T10:00:01'


class _FakeBroker:
    """Test fake — captures submit_order calls and yields a configurable
    OrderResult. Optionally returns a market reference price."""

    def __init__(self, market_price: float = 10.0,
                 result: Optional[_FakeOrderResult] = None,
                 raise_on_fetch: bool = False) -> None:
        self._market_price = market_price
        self._raise_on_fetch = raise_on_fetch
        self._result = result or _FakeOrderResult()
        self.calls: List[dict] = []

    def _fetch_market_price(self, symbol: str) -> float:
        if self._raise_on_fetch:
            raise RuntimeError('network down')
        return self._market_price

    def submit_order(self, **kwargs) -> _FakeOrderResult:
        self.calls.append(kwargs)
        return self._result


class _FakeBrokerNoFetcher:
    """Broker without the _fetch_market_price helper — market orders should
    raise NoReferencePriceError because we can't resolve a notional."""

    def submit_order(self, **kwargs):  # pragma: no cover — should not be reached
        raise AssertionError('should have raised before submit_order')


@dataclass
class _RiskResult:
    passed: bool
    reason: str = ''
    details: Optional[dict] = None


class _AcceptingRiskEngine:
    def check(self, signal):
        return _RiskResult(passed=True)


class _RejectingRiskEngine:
    def __init__(self, reason: str = 'position_too_large') -> None:
        self.reason = reason

    def check(self, signal):
        return _RiskResult(passed=False, reason=self.reason,
                           details={'limit': 0.25, 'observed': 0.40})


class _ThrowingRiskEngine:
    def check(self, signal):
        raise RuntimeError('risk_engine broken')


# ─── Test cases ────────────────────────────────────────────────────────────


class TestSubmitOrderHappyPath(unittest.TestCase):

    def test_limit_order_passes_through(self) -> None:
        broker = _FakeBroker()
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=15.5, price_type='limit')
        resp = submit_order(req, broker=broker, risk_engine=None)
        self.assertEqual(resp.status, 'filled')
        self.assertEqual(resp.order_id, 'OID-1')
        self.assertEqual(broker.calls[0]['price'], 15.5)
        self.assertEqual(broker.calls[0]['price_type'], 'limit')

    def test_market_order_uses_broker_ref_price(self) -> None:
        """When price=0, the use case fetches a ref via the broker."""
        broker = _FakeBroker(market_price=20.0)
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=200, price=0.0, price_type='market')
        resp = submit_order(req, broker=broker, risk_engine=_AcceptingRiskEngine())
        self.assertEqual(resp.status, 'filled')
        # Broker was called with price=0 (it owns market-order fill logic)
        self.assertEqual(broker.calls[0]['price'], 0.0)

    def test_response_echoes_request_fields(self) -> None:
        broker = _FakeBroker()
        req = SubmitOrderRequest(symbol='000001.SZ', direction='SELL',
                                 shares=300, price=12.0)
        resp = submit_order(req, broker=broker)
        self.assertEqual(resp.symbol, '000001.SZ')
        self.assertEqual(resp.direction, 'SELL')
        self.assertEqual(resp.shares, 300)


class TestSubmitOrderRiskGate(unittest.TestCase):

    def test_risk_rejected_raises_and_does_not_submit(self) -> None:
        broker = _FakeBroker()
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=1_000_000, price=10.0)
        with self.assertRaises(RiskRejectedError) as cm:
            submit_order(req, broker=broker, risk_engine=_RejectingRiskEngine())
        self.assertEqual(cm.exception.code, 'RISK_REJECTED')
        self.assertIn('position_too_large', cm.exception.message)
        self.assertEqual(cm.exception.details.get('limit'), 0.25)
        # 关键：broker.submit_order 必须没被调用
        self.assertEqual(broker.calls, [])

    def test_risk_engine_raises_converts_to_RiskCheckFailedError(self) -> None:
        broker = _FakeBroker()
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=10.0)
        with self.assertRaises(RiskCheckFailedError) as cm:
            submit_order(req, broker=broker, risk_engine=_ThrowingRiskEngine())
        self.assertEqual(cm.exception.code, 'RISK_ERROR')
        # 保守策略：风控自身异常 → 不下单
        self.assertEqual(broker.calls, [])

    def test_no_risk_engine_skips_check(self) -> None:
        broker = _FakeBroker()
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=10.0)
        resp = submit_order(req, broker=broker, risk_engine=None)
        self.assertEqual(resp.status, 'filled')
        self.assertEqual(len(broker.calls), 1)


class TestSubmitOrderMarketPriceResolution(unittest.TestCase):

    def test_market_order_no_fetcher_raises(self) -> None:
        broker = _FakeBrokerNoFetcher()
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=0.0)
        with self.assertRaises(NoReferencePriceError) as cm:
            submit_order(req, broker=broker)
        self.assertEqual(cm.exception.code, 'NO_REF_PRICE')

    def test_market_order_fetcher_fails_raises(self) -> None:
        broker = _FakeBroker(raise_on_fetch=True)
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=0.0)
        with self.assertRaises(NoReferencePriceError):
            submit_order(req, broker=broker)
        self.assertEqual(broker.calls, [])

    def test_market_order_fetcher_returns_zero_raises(self) -> None:
        broker = _FakeBroker(market_price=0.0)
        req = SubmitOrderRequest(symbol='600000.SH', direction='BUY',
                                 shares=100, price=0.0)
        with self.assertRaises(NoReferencePriceError):
            submit_order(req, broker=broker)


if __name__ == '__main__':
    unittest.main()

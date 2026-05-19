"""``submit_order`` use case — programmatic order submission with risk gate.

R2-1: Previously this logic lived inside ``backend/api.py:/orders/submit`` —
the endpoint constructed ``core.factors.base.Signal`` directly, fetched a
reference price via ``broker._fetch_market_price``, called ``RiskEngine.check``,
then ``broker.submit_order``. That meant scripts / Streamlit / the scheduler
couldn't re-use the same control flow without copy-pasting the endpoint body.

This module is the single source of truth for "submit an order, applying the
PreTrade risk gate first". The HTTP endpoint now thin-wraps it; any future
caller (CLI, internal worker, scheduled job) goes through here.

Inputs / outputs are dataclasses so the call is serializable; broker and
risk_engine are passed as dependencies so tests can inject fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from core.use_cases import UseCaseError

logger = logging.getLogger(__name__)


# ─── Request / Response ────────────────────────────────────────────────────


@dataclass
class SubmitOrderRequest:
    """Programmatic order intent. Validation belongs in the caller (HTTP
    layer does its own field-presence checks); this dataclass only carries
    the data through the use case."""
    symbol: str
    direction: str           # 'BUY' | 'SELL'
    shares: int
    price: float = 0.0       # 0 → market order; broker fetches a ref price
    price_type: str = 'market'


@dataclass
class SubmitOrderResponse:
    """Outcome of an order submission. Fields mirror ``OrderResult`` from
    the broker, plus the request echoes so callers can build their own
    serialization without re-keying the input."""
    order_id: str
    status: str              # broker-reported: 'filled' | 'partial' | 'rejected' | 'pending'
    symbol: str
    direction: str
    shares: int
    filled_shares: int
    avg_price: float
    reason: str
    submitted_at: Optional[str] = None
    filled_at: Optional[str] = None


# ─── Exceptions ────────────────────────────────────────────────────────────


class NoReferencePriceError(UseCaseError):
    """Market order needs a reference price but the broker's ``_fetch_market_price``
    helper returned 0/None. Cannot run risk checks against a zero price."""

    def __init__(self, message: str = 'market order needs a reference price '
                                      '(broker fetch failed)') -> None:
        super().__init__(message, code='NO_REF_PRICE')


class RiskRejectedError(UseCaseError):
    """PreTrade risk check returned ``passed=False``. The order was *not*
    submitted to the broker.

    ``details`` is the structured payload from :class:`core.risk_engine.RiskResult`
    so the HTTP layer can surface it to the client."""

    def __init__(self, reason: str, details: Dict[str, Any]) -> None:
        super().__init__(f'PreTrade rejected: {reason}', code='RISK_REJECTED')
        self.details = details


class RiskCheckFailedError(UseCaseError):
    """The risk engine itself raised. Conservative policy: do NOT submit."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(f'PreTrade check raised: {exc}', code='RISK_ERROR')
        self.cause = exc


# ─── Use case body ─────────────────────────────────────────────────────────


def submit_order(
    req: SubmitOrderRequest,
    *,
    broker: Any,
    risk_engine: Optional[Any] = None,
) -> SubmitOrderResponse:
    """Run the standard order-submission pipeline.

    Steps:

    1. Resolve a reference price.  If ``req.price > 0`` use it directly,
       otherwise call ``broker._fetch_market_price(symbol)``. If neither is
       available, raise :class:`NoReferencePriceError`.
    2. If ``risk_engine`` is given, build a :class:`core.factors.base.Signal`
       and call ``risk_engine.check(signal)``. Reject (raise
       :class:`RiskRejectedError`) on ``not passed``; raise
       :class:`RiskCheckFailedError` if the check itself errors.
    3. Call ``broker.submit_order(symbol, direction, shares, price, price_type)``
       and wrap the result in :class:`SubmitOrderResponse`.

    Parameters
    ----------
    req
        The order intent.
    broker
        Anything that exposes ``submit_order(**kwargs)`` returning an
        OrderResult-shaped object. May also expose ``_fetch_market_price(symbol)``
        for market-order reference pricing.
    risk_engine
        Optional; when ``None``, the PreTrade check is skipped (callers
        that already validated should pass ``None``).
    """
    # 1. 解析参考价
    if req.price > 0:
        risk_price = req.price
    else:
        risk_price = _resolve_market_ref_price(broker, req.symbol)

    # 2. PreTrade 风控（与 IntradayMonitor 走同一道门控）
    if risk_engine is not None:
        _run_risk_check(risk_engine, req, risk_price)

    # 3. 下单 — broker 内部 _lock 保证 "查现金/撮合/写持仓" 原子性
    result = broker.submit_order(
        symbol=req.symbol,
        direction=req.direction,
        shares=req.shares,
        price=req.price,
        price_type=req.price_type,
    )

    return SubmitOrderResponse(
        order_id=result.order_id,
        status=result.status,
        symbol=req.symbol,
        direction=req.direction,
        shares=req.shares,
        filled_shares=result.filled_shares,
        avg_price=result.avg_price,
        reason=result.reason,
        submitted_at=getattr(result, 'submitted_at', None),
        filled_at=getattr(result, 'filled_at', None),
    )


# ─── helpers ───────────────────────────────────────────────────────────────


def _resolve_market_ref_price(broker: Any, symbol: str) -> float:
    """Return a positive ref price for market orders, else raise.

    A market order arrives without a price, but risk rules need a notional
    to estimate single-trade size / portfolio share / CVaR. We ask the broker
    for the latest quote; if that fails we refuse to submit rather than let
    a 0-price slip past risk checks.
    """
    fetch = getattr(broker, '_fetch_market_price', None)
    if fetch is None:
        raise NoReferencePriceError(
            f'broker {type(broker).__name__} has no _fetch_market_price; '
            'cannot resolve market-order reference price'
        )
    try:
        ref = float(fetch(symbol) or 0.0)
    except Exception as exc:
        logger.warning('broker._fetch_market_price(%s) raised: %s', symbol, exc)
        ref = 0.0
    if ref <= 0:
        raise NoReferencePriceError()
    return ref


def _run_risk_check(risk_engine: Any, req: SubmitOrderRequest,
                    risk_price: float) -> None:
    """Build a Signal and run risk_engine.check; raise on rejection / failure."""
    try:
        from typing import cast, Literal
        from core.factors.base import Signal
        signal = Signal(
            timestamp=datetime.now(),
            symbol=req.symbol,
            direction=cast(Literal['BUY', 'SELL'], req.direction),
            strength=1.0,
            factor_name='use_case.submit_order',
            price=risk_price,
        )
        result = risk_engine.check(signal)
    except Exception as exc:
        raise RiskCheckFailedError(exc) from exc

    if not result.passed:
        details = getattr(result, 'details', {}) or {}
        raise RiskRejectedError(result.reason, details)


__all__ = [
    'SubmitOrderRequest', 'SubmitOrderResponse',
    'NoReferencePriceError', 'RiskRejectedError', 'RiskCheckFailedError',
    'submit_order',
]

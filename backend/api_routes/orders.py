"""``/orders/*`` HTTP routes.

R2-4: extracted from backend/api.py (originally part of the 1830-line monolith).

Responsibilities:
- POST /orders/submit         — submit a new order (calls
                                 ``core.use_cases.submit_order``)
- GET  /orders/recent         — list orders with filters
- GET  /orders/pending        — list pending / partial orders
- POST /orders/<id>/cancel    — cancel a pending order

The submit endpoint owns ``Idempotency-Key`` replay protection (R0-1) and
maps :class:`UseCaseError` subclasses to HTTP status codes; everything else
is straight CRUD against ``PortfolioService``.
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

# Shared helpers live in backend.api; importing them here is safe because
# api.py registers this Blueprint at the very bottom of its module body,
# after all helpers + decorators are defined.
#
# We import *names* used at decoration / static dispatch (app / rate_limit
# / get_svc / ok / err / require_json / idempotency singleton). For broker
# and risk-engine getters, we resolve them at *call* time via
# ``sys.modules['backend.api']`` attribute lookup so test patches against
# ``backend.api._get_or_build_broker`` take effect (and we avoid the cycle
# that bites tests loading api.py via importlib as a free-standing module).
from backend.api import (
    _idempotency_store_singleton,
    app,
    err,
    get_svc,
    ok,
    rate_limit,
    require_json,
)


def _api_get_or_build_broker():
    import sys
    return sys.modules['backend.api']._get_or_build_broker()


def _api_get_risk_engine():
    import sys
    return sys.modules['backend.api']._get_risk_engine()

orders_bp = Blueprint('orders', __name__)


@orders_bp.route('/orders/submit', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def submit_order():
    """POST /orders/submit — 通过共享 broker 提交订单(simulation 模式下 PaperBroker 即时撮合)。

    所有外部下单(UI / 脚本 / 第三方)都必须先过 PreTrade 风控，与
    IntradayMonitor 的内部下单链路保持同一道门控。

    R0-1: 支持 ``Idempotency-Key`` header。若同一 key 24h 内被重复提交,
    返回上次的响应而非执行第二次。客户端可用 UUID 防重 / 防网络重试。

    R2-1: 业务逻辑已下沉到 core.use_cases.submit_order。
    """
    if (e := require_json()):
        return e
    body = request.json
    for field in ('symbol', 'direction', 'shares'):
        if field not in body:
            return err(f"missing required field: {field}")

    # ── 幂等键检查（R0-1）─────────────────────────────────────
    # 用 reserve / complete / release 三段式：DB PRIMARY KEY 串行化并发
    # 抢锁，且必须先 reserve 再 submit_order，撮合后 complete；任何错误
    # 路径都 release，避免一个 key 被卡 24h。详见 core/idempotency.py 模块
    # docstring。
    idempotency_key = request.headers.get('Idempotency-Key', '').strip() or None
    idem_store = None
    request_hash = None
    reserved_new = False
    if idempotency_key is not None:
        from core.idempotency import (
            IdempotencyKeyConflict,
            ReserveOutcome,
            compute_request_hash,
        )
        idem_store = _idempotency_store_singleton.get()
        request_hash = compute_request_hash(body)
        try:
            outcome, stored = idem_store.reserve(idempotency_key, request_hash)
        except IdempotencyKeyConflict:
            return jsonify({
                'status': 'error',
                'error': 'idempotency key reused with a different payload',
                'code': 'IDEMPOTENCY_KEY_CONFLICT',
                'timestamp': datetime.now().isoformat(),
            }), 422
        except Exception as exc:
            # 存储层挂了 — 不阻断下单。打 warning，按"无幂等保护"处理。
            app.logger.warning('idempotency.reserve failed (continuing without '
                               'idempotency): %s', exc)
            idem_store = None
            outcome, stored = ReserveOutcome.NEW, None

        if outcome is ReserveOutcome.REPLAY and stored is not None:
            stored.response.setdefault('replayed', True)
            return jsonify(stored.response), 200
        if outcome is ReserveOutcome.IN_FLIGHT:
            # 同 key 还在处理中 — 让客户端短退避后重试，避免重复下单。
            return jsonify({
                'status': 'error',
                'error': 'a request with this idempotency key is already in flight',
                'code': 'IDEMPOTENCY_IN_FLIGHT',
                'timestamp': datetime.now().isoformat(),
            }), 409
        # outcome == NEW：我们持有 reservation，必须 complete 或 release。
        reserved_new = True

    direction = body['direction'].upper()
    if direction not in ('BUY', 'SELL'):
        if reserved_new and idem_store is not None:
            try:
                idem_store.release(idempotency_key, request_hash)
            except Exception as exc:
                app.logger.warning('idempotency.release failed: %s', exc)
        return err("direction must be BUY or SELL")
    try:
        shares = int(body['shares'])
    except (TypeError, ValueError):
        if reserved_new and idem_store is not None:
            try:
                idem_store.release(idempotency_key, request_hash)
            except Exception as exc:
                app.logger.warning('idempotency.release failed: %s', exc)
        return err("shares must be a positive integer")
    if shares <= 0:
        if reserved_new and idem_store is not None:
            try:
                idem_store.release(idempotency_key, request_hash)
            except Exception as exc:
                app.logger.warning('idempotency.release failed: %s', exc)
        return err("shares must be positive")

    symbol = body['symbol']

    from core.use_cases.submit_order import (
        NoReferencePriceError,
        RiskCheckFailedError,
        RiskRejectedError,
        SubmitOrderRequest,
        submit_order as _submit_order_uc,
    )

    req = SubmitOrderRequest(
        symbol=symbol,
        direction=direction,
        shares=shares,
        price=float(body.get('price', 0)),
        price_type=body.get('price_type', 'market'),
    )

    def _release_on_error() -> None:
        if reserved_new and idem_store is not None:
            try:
                idem_store.release(idempotency_key, request_hash)
            except Exception as rel_exc:
                app.logger.warning('idempotency.release failed: %s', rel_exc)

    try:
        result = _submit_order_uc(
            req,
            broker=_api_get_or_build_broker(),
            risk_engine=_api_get_risk_engine(),
        )
    except NoReferencePriceError as exc:
        _release_on_error()
        return jsonify({
            'status': 'error',
            'error': exc.message,
            'code': exc.code,
            'timestamp': datetime.now().isoformat(),
        }), 503
    except RiskRejectedError as exc:
        _release_on_error()
        return jsonify({
            'status': 'error',
            'error': exc.message,
            'code': exc.code,
            'details': exc.details,
            'timestamp': datetime.now().isoformat(),
        }), 403
    except RiskCheckFailedError as exc:
        _release_on_error()
        return jsonify({
            'status': 'error',
            'error': exc.message,
            'code': exc.code,
            'timestamp': datetime.now().isoformat(),
        }), 503
    except Exception:
        # broker / 撮合本身抛了未分类异常 — 释放 reservation 让客户端可重试。
        _release_on_error()
        raise

    response_body = {
        'status': result.status,
        'timestamp': datetime.now().isoformat(),
        'order_id': result.order_id,
        'symbol': result.symbol,
        'direction': result.direction,
        'shares': result.shares,
        'filled_shares': result.filled_shares,
        'avg_price': result.avg_price,
        'reason': result.reason,
        'submitted_at': result.submitted_at,
        'filled_at': result.filled_at,
    }
    if reserved_new and idem_store is not None:
        from core.idempotency import IdempotencyKeyConflict
        try:
            idem_store.complete(idempotency_key, request_hash, response_body)
        except IdempotencyKeyConflict as exc:
            # complete-time hash drift = caller bug. 订单已实际成交，不要回滚。
            app.logger.warning('idempotency.complete conflict (order succeeded): %s',
                               exc)
        except Exception as exc:
            # 存储侧问题不能让已成交订单变成 500 — 客户端拿到 200 + order_id 即可。
            app.logger.warning('idempotency.complete failed (continuing): %s', exc)
    return jsonify(response_body), 200


@orders_bp.route('/orders/recent', methods=['GET'])
def recent_orders():
    """GET /orders/recent?symbol=600036.SH&status=filled&limit=50

    查询订单记录，支持 symbol / status / limit 过滤。
    status 可选: submitted / filled / cancelled / rejected
    """
    symbol = request.args.get('symbol')
    status = request.args.get('status')
    limit = int(request.args.get('limit', 50))
    svc = get_svc()
    orders = svc.get_orders(symbol=symbol, status=status, limit=limit)
    return ok(orders=orders, realized_pnl=svc.get_realized_pnl())


@orders_bp.route('/orders/pending', methods=['GET'])
def pending_orders():
    """GET /orders/pending — 所有挂起/部分成交的订单。"""
    svc = get_svc()
    pending = svc.get_pending_orders()
    return ok(orders=pending, count=len(pending))


@orders_bp.route('/orders/<order_id>/cancel', methods=['POST'])
def cancel_order(order_id):
    """POST /orders/<order_id>/cancel — 撤销挂单。

    触发 PortfolioService.update_order_cancelled()。
    """
    svc = get_svc()
    order = svc.get_order(order_id)
    if not order:
        return err(f'Order not found: {order_id}', 404)
    if order.get('status') not in ('pending', 'partial'):
        return err(f'Cannot cancel order in status "{order.get("status")}"', 422)

    # Use the shared broker instance from main(), not a new one
    from main import get_broker, get_monitor
    broker = get_broker()
    trading_mode = monitor.trading_mode() if (monitor := get_monitor()) else 'simulation'

    if broker is not None:
        cancelled = broker.cancel_order(order_id)
        # Simulation broker always returns False — that is normal, not an error
        if not cancelled and trading_mode == 'live':
            return err('Cancel failed (broker rejected)', 409)

    svc.update_order_cancelled(order_id, reason='user_cancelled')
    updated = svc.get_order(order_id)
    return ok(order_id=order_id, status='cancelled', order=updated)

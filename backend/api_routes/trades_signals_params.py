"""``/trades`` / ``/signals`` / ``/params`` HTTP routes.

R2-4 续集: 三组简单 CRUD 端点合并为单个 Blueprint，因为它们共用
``PortfolioService`` / ``services.signals`` 依赖、没有跨资源逻辑。

- GET  /trades                — recent trades
- POST /trades                — record a completed trade
- GET  /signals               — recent signals
- POST /signals               — record a signal
- GET  /params                — all per-symbol params
- GET  /params/<symbol>       — single symbol params
- PATCH /params/<symbol>      — update params
"""

from __future__ import annotations

from flask import Blueprint, request

from backend.api import err, get_svc, ok, require_json

trades_signals_params_bp = Blueprint('trades_signals_params', __name__)


# ─── Trades ────────────────────────────────────────────────────────────────


@trades_signals_params_bp.route('/trades', methods=['GET'])
def get_trades():
    """GET /trades — recent trades."""
    symbol = request.args.get('symbol')
    limit = int(request.args.get('limit', 50))
    return ok(trades=get_svc().get_trades(symbol=symbol, limit=limit))


@trades_signals_params_bp.route('/trades', methods=['POST'])
def record_trade():
    """POST /trades — record a completed trade."""
    if (e := require_json()):
        return e
    body = request.json
    for field in ('symbol', 'direction', 'shares', 'price'):
        if field not in body:
            return err(f'missing required field: {field}')
    pnl = body.get('pnl')
    if pnl is not None:
        pnl = float(pnl)
    trade_id = get_svc().record_trade(
        body['symbol'], body['direction'],
        int(body['shares']), float(body['price']), pnl,
    )
    return ok(trade_id=trade_id, message='Trade recorded')


# ─── Signals ───────────────────────────────────────────────────────────────


@trades_signals_params_bp.route('/signals', methods=['GET'])
def get_signals():
    """GET /signals — recent signals."""
    symbol = request.args.get('symbol')
    since = request.args.get('since')
    limit = int(request.args.get('limit', 50))
    return ok(signals=get_svc().get_signals(symbol=symbol, since=since, limit=limit))


@trades_signals_params_bp.route('/signals', methods=['POST'])
def record_signal():
    """POST /signals — record a signal."""
    if (e := require_json()):
        return e
    body = request.json
    for field in ('symbol', 'signal'):
        if field not in body:
            return err(f'missing required field: {field}')
    get_svc().record_signal(
        body['symbol'], body['signal'],
        float(body.get('strength', 0.0)),
        body.get('reason', ''),
    )
    return ok(message='Signal recorded')


# ─── Per-symbol params ─────────────────────────────────────────────────────


@trades_signals_params_bp.route('/params/<symbol>', methods=['GET'])
def get_symbol_params(symbol: str):
    """GET /params/<symbol> — WFA + 手工配置合并后的单股参数。"""
    from services.signals import load_symbol_params
    return ok(symbol=symbol, params=load_symbol_params(symbol))


@trades_signals_params_bp.route('/params/<symbol>', methods=['PATCH'])
def update_symbol_params(symbol: str):
    """PATCH /params/<symbol> — 更新单股参数 (写入 params.json)。

    Body: {"rsi_buy": 30, "stop_loss": 0.06, ...}
    """
    if (e := require_json()):
        return e
    from services.signals import (
        PARAM_FIELDS_ALLOWED,
        update_symbol_params as _update,
    )
    updated = _update(symbol, request.json or {})
    if not updated:
        return err(f'No valid fields. Allowed: {sorted(PARAM_FIELDS_ALLOWED)}', 422)
    return ok(symbol=symbol, params=updated)


@trades_signals_params_bp.route('/params', methods=['GET'])
def list_all_params():
    """GET /params — 全量参数（params.json + live_params.json 合并视图）。"""
    from services.signals import list_symbols_with_params, load_symbol_params
    result = {sym: load_symbol_params(sym) for sym in list_symbols_with_params()}
    return ok(params=result, count=len(result))

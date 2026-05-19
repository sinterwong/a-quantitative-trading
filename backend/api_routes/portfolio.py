"""``/positions`` / ``/cash`` / ``/portfolio/*`` HTTP routes.

R2-4 续集: 第二批从 backend/api.py 拆出的 Blueprint。

Resources:
- GET  /positions             — list positions
- POST /portfolio/positions   — upsert a position
- GET  /cash                  — read available cash
- POST /portfolio/cash        — set cash amount
- GET  /portfolio/summary     — combined cash + positions + P&L
- GET  /portfolio/daily       — historical daily metas
- POST /portfolio/daily       — record a daily meta

All routes use ``PortfolioService`` directly; no use_case wrapper yet
because these are simple CRUD/read paths with no risk-gate semantics.
"""

from __future__ import annotations

from flask import Blueprint, request

from backend.api import (
    err,
    get_svc,
    ok,
    require_json,
)

portfolio_bp = Blueprint('portfolio', __name__)


# ─── Positions ─────────────────────────────────────────────────────────────


@portfolio_bp.route('/positions', methods=['GET'])
def get_positions():
    """GET /positions?refresh=1 — positions with unrealized P&L.
    refresh=1 fetches latest prices from Tencent Finance first."""
    refresh = request.args.get('refresh', '0') == '1'
    svc = get_svc()
    if refresh:
        svc.refresh_prices()
    return ok(positions=svc.get_positions())


@portfolio_bp.route('/portfolio/positions', methods=['POST'])
def upsert_position():
    """POST /portfolio/positions — upsert a position."""
    if (e := require_json()):
        return e
    body = request.json
    for field in ('symbol', 'shares', 'entry_price'):
        if field not in body:
            return err(f'missing required field: {field}')
    svc = get_svc()
    svc.upsert_position(
        body['symbol'],
        int(body['shares']),
        float(body['entry_price']),
    )
    return ok(message=f"Position {body['symbol']} updated")


# ─── Cash ──────────────────────────────────────────────────────────────────


@portfolio_bp.route('/cash', methods=['GET'])
def get_cash():
    """GET /cash — available cash."""
    return ok(cash=get_svc().get_cash())


@portfolio_bp.route('/portfolio/cash', methods=['POST'])
def set_cash():
    """POST /portfolio/cash — set cash amount."""
    if (e := require_json()):
        return e
    body = request.json
    if 'amount' not in body:
        return err('missing required field: amount')
    get_svc().set_cash(float(body['amount']))
    return ok(message=f"Cash set to {body['amount']}")


# ─── Summary / Daily ───────────────────────────────────────────────────────


@portfolio_bp.route('/portfolio/summary', methods=['GET'])
def portfolio_summary():
    """GET /portfolio/summary?refresh=1 — cash + positions + realized/unrealized P&L."""
    refresh = request.args.get('refresh', '1') != '0'
    return ok(**get_svc().get_portfolio_summary(refresh_prices_now=refresh))


@portfolio_bp.route('/portfolio/daily', methods=['GET'])
def portfolio_daily():
    """GET /portfolio/daily?limit=30 — recent daily summaries."""
    limit = int(request.args.get('limit', 30))
    return ok(daily=get_svc().get_daily_metas(limit=limit))


@portfolio_bp.route('/portfolio/daily', methods=['POST'])
def record_portfolio_daily():
    """POST /portfolio/daily — record daily meta.

    Body: {equity, cash, n_signals, n_trades, notes}
    """
    body = request.get_json() or {}
    get_svc().record_daily_meta(
        equity=float(body.get('equity', 0)),
        cash=float(body.get('cash', 0)),
        n_signals=int(body.get('n_signals', 0)),
        n_trades=int(body.get('n_trades', 0)),
        note=str(body.get('notes', '')),
    )
    return ok(message='daily meta recorded')

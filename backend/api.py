"""
api.py — HTTP API for Portfolio Service
====================================
Flask HTTP endpoints. All responses are JSON.

Endpoints:
    GET  /health              — health check
    GET  /positions           — all current positions
    GET  /cash                — available cash
    GET  /trades              — recent trades (?symbol=&limit=)
    GET  /signals             — recent signals (?symbol=&since=&limit=)
    GET  /portfolio/summary   — full portfolio snapshot
    GET  /portfolio/daily     — recent daily summaries
    POST /portfolio/positions — upsert a position (JSON body)
    POST /portfolio/cash       — set cash amount
    POST /trades              — record a trade (JSON body)
    POST /signals             — record a signal (JSON body)
    POST /orders/submit       — submit an order intent → triggers broker
    GET  /orders/recent        — recent order results
    POST /analysis/run         — trigger daily analysis manually
    GET  /analysis/status       — last analysis result

Run with: python api.py
"""

import os
import sys
import json
import traceback
from datetime import datetime, date

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(THIS_DIR)
PROJ_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, PROJ_DIR)

from flask import Flask, request, jsonify
from services.portfolio import PortfolioService

app = Flask(__name__)

# Singleton portfolio service
_svc: PortfolioService = None


def get_svc() -> PortfolioService:
    global _svc
    if _svc is None:
        _svc = PortfolioService()
    return _svc


# ============================================================
# Helpers
# ============================================================

def ok(data=None, **kwargs):
    """Return a success JSON response."""
    payload = {'status': 'ok', 'timestamp': datetime.now().isoformat()}
    if data is not None:
        payload['data'] = data
    payload.update(kwargs)
    return jsonify(payload)


def err(message: str, code: int = 400):
    """Return an error JSON response."""
    return jsonify({
        'status': 'error',
        'error': message,
        'timestamp': datetime.now().isoformat(),
    }), code


def require_json():
    """Return error if request has no JSON body."""
    if not request.is_json:
        return err('Content-Type must be application/json', 415)
    return None


# ============================================================
# Health
# ============================================================

@app.route('/health', methods=['GET'])
def health():
    """Liveness probe."""
    try:
        svc = get_svc()
        cash = svc.get_cash()
        return ok(cash=cash, message='healthy')
    except Exception as e:
        return err(str(e), 500)


# ============================================================
# Positions
# ============================================================

@app.route('/positions', methods=['GET'])
def get_positions():
    """GET /positions — all open positions."""
    svc = get_svc()
    return ok(positions=svc.get_positions())


@app.route('/portfolio/positions', methods=['POST'])
def upsert_position():
    """POST /portfolio/positions — upsert a position."""
    if (e := require_json()):
        return e
    body = request.json
    required = ['symbol', 'shares', 'entry_price']
    for field in required:
        if field not in body:
            return err(f"missing required field: {field}")
    svc = get_svc()
    svc.upsert_position(
        body['symbol'],
        int(body['shares']),
        float(body['entry_price']),
    )
    return ok(message=f"Position {body['symbol']} updated")


# ============================================================
# Cash
# ============================================================

@app.route('/cash', methods=['GET'])
def get_cash():
    """GET /cash — available cash."""
    svc = get_svc()
    return ok(cash=svc.get_cash())


@app.route('/portfolio/cash', methods=['POST'])
def set_cash():
    """POST /portfolio/cash — set cash amount."""
    if (e := require_json()):
        return e
    body = request.json
    if 'amount' not in body:
        return err("missing required field: amount")
    svc = get_svc()
    svc.set_cash(float(body['amount']))
    return ok(message=f"Cash set to {body['amount']}")


# ============================================================
# Trades
# ============================================================

@app.route('/trades', methods=['GET'])
def get_trades():
    """GET /trades — recent trades."""
    svc = get_svc()
    symbol = request.args.get('symbol')
    limit = int(request.args.get('limit', 50))
    return ok(trades=svc.get_trades(symbol=symbol, limit=limit))


@app.route('/trades', methods=['POST'])
def record_trade():
    """POST /trades — record a completed trade."""
    if (e := require_json()):
        return e
    body = request.json
    required = ['symbol', 'direction', 'shares', 'price']
    for field in required:
        if field not in body:
            return err(f"missing required field: {field}")
    svc = get_svc()
    pnl = body.get('pnl')
    if pnl is not None:
        pnl = float(pnl)
    trade_id = svc.record_trade(
        body['symbol'], body['direction'],
        int(body['shares']), float(body['price']), pnl
    )
    return ok(trade_id=trade_id, message="Trade recorded")


# ============================================================
# Signals
# ============================================================

@app.route('/signals', methods=['GET'])
def get_signals():
    """GET /signals — recent signals."""
    svc = get_svc()
    symbol = request.args.get('symbol')
    since = request.args.get('since')
    limit = int(request.args.get('limit', 50))
    return ok(signals=svc.get_signals(symbol=symbol, since=since, limit=limit))


@app.route('/signals', methods=['POST'])
def record_signal():
    """POST /signals — record a signal."""
    if (e := require_json()):
        return e
    body = request.json
    required = ['symbol', 'signal']
    for field in required:
        if field not in body:
            return err(f"missing required field: {field}")
    svc = get_svc()
    svc.record_signal(
        body['symbol'], body['signal'],
        float(body.get('strength', 0.0)),
        body.get('reason', ''),
    )
    return ok(message="Signal recorded")


# ============================================================
# Portfolio summary
# ============================================================

@app.route('/portfolio/summary', methods=['GET'])
def portfolio_summary():
    """GET /portfolio/summary — full snapshot."""
    svc = get_svc()
    return ok(**svc.get_portfolio_summary())


@app.route('/portfolio/daily', methods=['GET'])
def portfolio_daily():
    """GET /portfolio/daily — recent daily summaries."""
    svc = get_svc()
    limit = int(request.args.get('limit', 30))
    return ok(daily=svc.get_daily_metas(limit=limit))


# ============================================================
# Order intent (Phase 1: just records intent)
# Phase 2: will call broker service
# ============================================================

@app.route('/orders/submit', methods=['POST'])
def submit_order():
    """
    POST /orders/submit — submit an order intent.
    Phase 1: validates and records the order.
    Phase 2: will execute via broker.
    """
    if (e := require_json()):
        return e
    body = request.json
    required = ['symbol', 'direction', 'shares']
    for field in required:
        if field not in body:
            return err(f"missing required field: {field}")

    direction = body['direction'].upper()
    if direction not in ('BUY', 'SELL'):
        return err("direction must be BUY or SELL")

    shares = int(body['shares'])
    if shares <= 0:
        return err("shares must be positive")

    # Phase 1: just record the intent (Phase 2: execute via broker)
    return ok(
        message="Order intent recorded (Phase 1: no broker execution yet)",
        symbol=body['symbol'],
        direction=direction,
        shares=shares,
        note="Connect broker in Phase 2 to execute",
    )


@app.route('/orders/recent', methods=['GET'])
def recent_orders():
    """GET /orders/recent — placeholder for order history."""
    return ok(orders=[], message="Order history comes in Phase 2 with broker")


# ============================================================
# Analysis trigger
# ============================================================

@app.route('/analysis/run', methods=['POST'])
def run_analysis():
    """
    POST /analysis/run — trigger daily analysis.
    Phase 1: runs the analysis and returns the report.
    """
    import subprocess

    # Import the dynamic selector to run analysis
    try:
        sys.path.insert(0, os.path.join(PROJ_DIR, 'scripts'))
        from dynamic_selector import DynamicStockSelectorV2

        selector = DynamicStockSelectorV2()
        selector.fetch_market_news(20)
        selector.calc_all_scores()
        top_bks = selector.get_top_bk_sectors(5)
        news = selector.get_news_summary(10)
        stocks = selector.get_stock_with_context(5)
        sources = {
            'news': selector._last_news_source,
            'sectors': selector._last_source,
        }

        # Record signals
        svc = get_svc()
        for bk, info in top_bks:
            svc.record_signal(
                bk, 'BUY', info.get('total', 0) / 100.0,
                f"板块:{info.get('name','')} 涨幅:{info.get('change_pct',0):.2f}%"
            )

        return ok(
            sources=sources,
            top_sectors=[
                {'bk': bk, 'name': info.get('name'), 'total': info.get('total'),
                 'change_pct': info.get('change_pct')}
                for bk, info in top_bks
            ],
            news_summary=news,
            selected_stocks=stocks,
        )
    except Exception as e:
        return err(str(e) + '\n' + traceback.format_exc(), 500)


@app.route('/analysis/status', methods=['GET'])
def analysis_status():
    """GET /analysis/status — last known analysis metadata."""
    svc = get_svc()
    metas = svc.get_daily_metas(limit=1)
    if metas:
        return ok(**metas[0])
    return ok(message="No analysis run yet")


# ============================================================
# Error handlers
# ============================================================

@app.errorhandler(404)
def not_found(e):
    return err('Not found: ' + str(e), 404)


@app.errorhandler(500)
def server_error(e):
    return err('Internal server error: ' + str(e), 500)


# ============================================================
# Run
# ============================================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1', help='Bind host')
    parser.add_argument('--port', type=int, default=5555, help='Bind port')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f"Starting Portfolio API on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)

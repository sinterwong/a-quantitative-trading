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
import time
import traceback
from datetime import datetime, date
from functools import wraps

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(THIS_DIR)
PROJ_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, PROJ_DIR)

from flask import Flask, request, jsonify
from services.portfolio import PortfolioService

app = Flask(__name__)

# ─── Rate limiting (simple in-memory token bucket) ───────────────────
_RATE_LIMIT = {}          # client_key -> [timestamp, ...]
_RATE_WINDOW = 60           # seconds
_RATE_MAX    = 10           # max requests per window


def rate_limit(max_per_window: int = None, window_seconds: int = None):
    """Decorator: limits requests per client IP. Applied per-route."""
    mw = max_per_window or _RATE_MAX
    ws = window_seconds or _RATE_WINDOW

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            now = time.time()
            key = request.remote_addr or 'unknown'
            # Prune old entries
            cutoff = now - ws
            if key in _RATE_LIMIT:
                _RATE_LIMIT[key] = [t for t in _RATE_LIMIT[key] if t > cutoff]
            else:
                _RATE_LIMIT[key] = []
            if len(_RATE_LIMIT[key]) >= mw:
                return jsonify({
                    'status': 'error',
                    'code': 429,
                    'message': f'Too many requests (max {mw}/{'{0}s'.format(ws)}). Please retry later.',
                }), 429
            _RATE_LIMIT[key].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

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


def validate_fields(required: dict) -> callable:
    """Decorator: validate required JSON fields with type checking."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            if (e := require_json()):
                return e
            body = request.json or {}
            for field, field_type in required.items():
                if field not in body:
                    return err(f'missing required field: {field}', 422)
                try:
                    field_type(body[field])
                except (ValueError, TypeError):
                    return err(f'field "{field}" must be {field_type.__name__}', 422)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = getattr(fn, '__doc__', '')
        return wrapper
    return decorator


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


@app.route('/docs', methods=['GET'])
def docs():
    """OpenAPI spec at /docs."""
    import json
    spec_path = os.path.join(os.path.dirname(__file__), 'openapi.json')
    try:
        with open(spec_path, 'r', encoding='utf-8') as f:
            spec = json.load(f)
        return jsonify(spec)
    except Exception as e:
        return err('OpenAPI spec not found: ' + str(e), 500)


# ============================================================
# Positions
# ============================================================

@app.route('/positions', methods=['GET'])
def get_positions():
    """
    GET /positions?refresh=1

    Returns positions with unrealized P&L.
    refresh=1 fetches latest prices from Tencent Finance first.
    """
    refresh = request.args.get('refresh', '0') == '1'
    svc = get_svc()
    if refresh:
        svc.refresh_prices()
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
    """
    GET /portfolio/summary?refresh=1

    Query params:
        refresh=1  — fetch latest prices before calculating P&L
    """
    refresh = request.args.get('refresh', '0') == '1'
    svc = get_svc()
    return ok(**svc.get_portfolio_summary(refresh_prices_now=refresh))


@app.route('/portfolio/daily', methods=['GET'])
def portfolio_daily():
    """GET /portfolio/daily — recent daily summaries."""
    svc = get_svc()
    limit = int(request.args.get('limit', 30))
    return ok(daily=svc.get_daily_metas(limit=limit))


@app.route('/portfolio/daily', methods=['POST'])
def record_portfolio_daily():
    """
    POST /portfolio/daily - record daily meta.
    Body: {date, equity, cash, market_value, nav, notes}
    """
    try:
        svc = get_svc()
        body = request.get_json() or {}
        equity     = float(body.get('equity', 0))
        cash       = float(body.get('cash', 0))
        market_val = float(body.get('market_value', 0))
        nav        = float(body.get('nav', 1.0))
        notes      = str(body.get('notes', ''))
        n_signals  = int(body.get('n_signals', 0))
        n_trades   = int(body.get('n_trades', 0))
        svc.record_daily_meta(
            equity=equity, cash=cash,
            n_signals=n_signals, n_trades=n_trades,
            note=notes
        )
        return ok(message='daily meta recorded')
    except Exception as e:
        import traceback
        return err(str(e) + '\n' + traceback.format_exc(), 500)


# ============================================================
# Order intent (Phase 1: just records intent)
# Phase 2: will call broker service
# ============================================================

@app.route('/orders/submit', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def submit_order():
    """
    POST /orders/submit — submit an order → PaperBroker executes it.
    Phase 1: PaperBroker simulates fill and updates portfolio.
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

    symbol = body['symbol']
    price = float(body.get('price', 0))
    price_type = body.get('price_type', 'market')

    # Execute via PaperBroker
    from services.broker import PaperBroker
    svc = get_svc()
    broker = PaperBroker(portfolio_service=svc)
    broker.connect()
    result = broker.submit_order(symbol=symbol, direction=direction,
                                  shares=shares, price=price,
                                  price_type=price_type)

    return ok(
        order_id=result.order_id,
        status=result.status,
        symbol=symbol,
        direction=direction,
        shares=shares,
        filled_shares=result.filled_shares,
        avg_price=result.avg_price,
        reason=result.reason,
        submitted_at=result.submitted_at,
        filled_at=result.filled_at,
    )


@app.route('/orders/recent', methods=['GET'])
def recent_orders():
    """GET /orders/recent — recent filled orders."""
    svc = get_svc()
    trades = svc.get_trades(limit=50)
    return ok(orders=trades, realized_pnl=svc.get_realized_pnl())


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
# Monthly Performance
# ============================================================

@app.route('/analysis/monthly', methods=['GET'])
def monthly_performance():
    """
    GET /analysis/monthly?year=2026&month=4&include_chart=1

    Query params:
        year    — 报告年份（默认今年）
        month   — 报告月份（默认本月）
        include_chart — 是否包含图表（默认1，设为0可省带宽）

    Returns: {
        period, summary, returns, trade_stats,
        max_drawdown, equity_series, chart_base64
    }
    """
    try:
        from services.performance import generate_monthly_report
        year = int(request.args.get('year', date.today().year))
        month = int(request.args.get('month', date.today().month))
        include_chart = bool(int(request.args.get('include_chart', 1)))
        report = generate_monthly_report(year=year, month=month,
                                         include_chart=include_chart)
        return ok(**report)
    except Exception as e:
        import traceback
        return err(str(e) + '\n' + traceback.format_exc(), 500)



@app.route('/analysis/monthly/snapshot', methods=['POST'])
def record_monthly_snapshot():
    """
    POST /analysis/monthly/snapshot
    Body (optional): {"year": 2026, "month": 4}
    写入月度快照到数据库，通常在月末自动由Cron触发。
    """
    try:
        from services.performance import record_monthly_snapshot
        if request.is_json and request.json:
            body = request.json
            year = int(body.get('year', date.today().year))
            month = int(body.get('month', date.today().month))
        else:
            year = date.today().year
            month = date.today().month
        record_monthly_snapshot(year, month)
        return ok(message=f'{year}年{month}月快照已记录')
    except Exception as e:
        import traceback
        return err(str(e) + '\n' + traceback.format_exc(), 500)


@app.route('/analysis/monthly/history', methods=['GET'])
def monthly_history():
    """
    GET /analysis/monthly/history?limit=12
    返回历史月度快照列表。
    """
    try:
        from services.performance import get_monthly_snapshots
        limit = int(request.args.get('limit', 12))
        snapshots = get_monthly_snapshots(limit=limit)
        return ok(snapshots=snapshots, count=len(snapshots))
    except Exception as e:
        import traceback
        return err(str(e) + '\n' + traceback.format_exc(), 500)


# ============================================================
# Watchlist endpoints
# ============================================================

@app.route('/watchlist', methods=['GET'])
def get_watchlist():
    """GET /watchlist — 返回当前自选股列表"""
    from services.watchlist import get_watchlist_all
    items = get_watchlist_all()
    return ok(watchlist=items, count=len(items))


@app.route('/watchlist/add', methods=['POST'])
def add_watchlist():
    """
    POST /watchlist/add
    Body: {"symbol": "600900.SH", "name": "长江电力",
           "reason": "防守型持仓", "alert_pct": 5.0}
    """
    if not request.is_json:
        return err('Content-Type must be application/json', 415)
    body = request.json or {}
    symbol = body.get('symbol', '')
    if not symbol:
        return err('symbol is required', 422)
    from services.watchlist import add_to_watchlist
    ok_ = add_to_watchlist(
        symbol=symbol,
        name=body.get('name', ''),
        reason=body.get('reason', ''),
        alert_pct=float(body.get('alert_pct', 5.0)),
    )
    if ok_:
        return ok(message=f'{symbol} added to watchlist')
    return err('Failed to add to watchlist', 500)


@app.route('/watchlist/<symbol>', methods=['DELETE'])
def remove_watchlist(symbol):
    """DELETE /watchlist/<symbol> — 移除自选股（软删除）"""
    from services.watchlist import remove_from_watchlist
    removed = remove_from_watchlist(symbol)
    if removed:
        return ok(message=f'{symbol} removed from watchlist')
    return err(f'{symbol} not found in watchlist', 404)


@app.route('/watchlist/<symbol>', methods=['PATCH'])
def patch_watchlist(symbol):
    """
    PATCH /watchlist/<symbol>
    Body: {"alert_pct": 7.0} 或 {"enabled": 0}
    """
    if not request.is_json:
        return err('Content-Type must be application/json', 415)
    body = request.json or {}
    from services.watchlist import set_alert_threshold
    if 'alert_pct' in body:
        set_alert_threshold(symbol, float(body['alert_pct']))
    return ok(message=f'{symbol} updated')


# ============================================================
# Alert history endpoints
# ============================================================

@app.route('/alerts/history', methods=['GET'])
def alerts_history():
    """
    GET /alerts/history
    Query params:
        limit      — max rows (default 50)
        type       — filter by type: INDEX/POSITION/WATCHLIST/SECTOR_FLOW
        since_hours — only last N hours (e.g. 24)
        symbol     — filter by symbol (e.g. SH000001)
    """
    from services.alert_history import get_alerts
    limit = int(request.args.get('limit', 50))
    alert_type = request.args.get('type')
    since_hours = int(request.args.get('since_hours', 0)) or None
    symbol = request.args.get('symbol')
    items = get_alerts(
        limit=limit,
        alert_type=alert_type,
        since_hours=since_hours,
        symbol=symbol,
    )
    return ok(alerts=items, count=len(items))


@app.route('/alerts/clear', methods=['POST'])
def clear_alerts():
    """
    POST /alerts/clear
    Body: {"days": 7}  — 删除 N 天之前的预警（默认7天）
    """
    if not request.is_json:
        body = {}
    else:
        body = request.json or {}
    from services.alert_history import clear_old_alerts
    days = int(body.get('days', 7))
    cleared = clear_old_alerts(days)
    return ok(message=f'Cleared {cleared} alerts older than {days} days')


# ============================================================
# Data fetch endpoints (多源兜底路由)
# ============================================================

@app.route('/data/daily/<code>', methods=['GET'])
def data_daily(code):
    """
    GET /data/daily/<code>
    Query params:
        days     — int, number of trading days (default 30, max 2000)
        start    — str, start date YYYY-MM-DD (optional)
        end      — str, end date YYYY-MM-DD (optional)
    
    Returns:
        Standardized OHLCV daily data with MA5/MA10/MA20/volume_ratio.
        Uses multi-source failover: Tencent → Sina → AkShare.
        Circuit breaker protects each source.
    """
    try:
        from services.fetcher_manager import get_fetcher_manager
        days = int(request.args.get('days', 30))
        days = min(days, 2000)
        start = request.args.get('start') or None
        end = request.args.get('end') or None

        fm = get_fetcher_manager()
        df = fm.get_daily_data(code, start_date=start, end_date=end, days=days)

        # Convert to dict records (ISO date string)
        records = []
        for _, row in df.iterrows():
            rec = {}
            for col, val in row.items():
                if col == 'date':
                    rec[col] = str(val)[:10] if val else None
                elif val is not None:
                    rec[col] = round(float(val), 4) if isinstance(val, (int, float)) else val
            records.append(rec)

        return ok(
            code=code,
            rows=len(records),
            columns=list(df.columns),
            data=records,
            fetcher_status=fm.get_fetcher_status(),
        )
    except Exception as e:
        import traceback
        return err(f'数据获取失败: {e}\n{traceback.format_exc()}', 500)


@app.route('/data/status', methods=['GET'])
def data_status():
    """
    GET /data/status
    Returns the current status of all registered fetchers
    (circuit breaker state, failure count, availability).
    """
    from services.fetcher_manager import get_fetcher_manager
    fm = get_fetcher_manager()
    return ok(
        fetchers=[f.name for f in fm.fetchers],
        status=fm.get_fetcher_status(),
    )


@app.route('/data/fund_flow', methods=['GET'])
def data_fund_flow():
    """
    GET /data/fund_flow
    Query params:
        source  — 'market' / 'top' / stock code (e.g. '600900')
        period  — '5日排行' (default) / '3日排行' / '10日排行'
        top     — int, number of top stocks to return (default 20, only for source='top')

    Returns:
        market (source=market): 大盘资金流汇总（两市合计主力净流入）
            - sh_close, sh_change: 上证指数收盘/涨跌幅
            - sz_close, sz_change: 深证成指收盘/涨跌幅
            - main_net: 主力净流入（亿元）
            - main_pct: 主力净流入占成交额百分比

        stock (source=<code>): 个股资金流（来自同花顺5日排行）
            - code, name, date, close, change_pct, turnover_rate
            - main_net: 资金流入净额（元）
            - main_net_yi: 资金流入净额（亿元）
            - signal: strong_inflow / inflow / neutral / outflow / strong_outflow

        top (source=top): 资金流入TOP排名（来自同花顺全市场）
            - List of StockFundFlow records sorted by main_net descending
    """
    source = request.args.get('source', 'market')
    period = request.args.get('period', '5日排行')
    top_n = int(request.args.get('top', 20))

    try:
        from services.fund_flow import FundFlowService
        svc = FundFlowService()

        if source == 'market':
            result = svc.get_market_fund_flow()
            return ok(type='market', **result)

        elif source == 'top':
            tops = svc.get_top_fund_flow_stocks(period=period, top_n=top_n)
            return ok(
                type='top',
                period=period,
                count=len(tops),
                stocks=[t.to_dict() for t in tops],
            )

        else:
            summary = svc.get_main_net_summary(source, period=period)
            return ok(type='stock', source=source, **summary)

    except ImportError:
        return err('FundFlowService not available (AkShare missing)', 500)
    except Exception as e:
        import traceback
        return err(f'资金流获取失败: {e}\n{traceback.format_exc()}', 500)


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

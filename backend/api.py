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
    GET  /trading/mode          — get current trading mode (simulation|live)
    PUT  /trading/mode          — set trading mode {"mode": "simulation"|"live"}

Run with: python api.py
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime, date
from functools import wraps

import pandas as pd

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
                    'message': f'Too many requests (max {mw}/{ws}s). Please retry later.',
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
    refresh = request.args.get('refresh', '1') != '0'
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
    """
    GET /orders/recent?symbol=600036.SH&status=filled&limit=50

    查询订单记录，支持 symbol / status / limit 过滤。
    status 可选: submitted / filled / cancelled / rejected
    """
    symbol = request.args.get('symbol')
    status = request.args.get('status')
    limit  = int(request.args.get('limit', 50))
    svc = get_svc()
    orders = svc.get_orders(symbol=symbol, status=status, limit=limit)
    return ok(orders=orders, realized_pnl=svc.get_realized_pnl())


@app.route('/orders/pending', methods=['GET'])
def pending_orders():
    """
    GET /orders/pending — 所有挂起/部分成交的订单。
    """
    svc = get_svc()
    pending = svc.get_pending_orders()
    return ok(orders=pending, count=len(pending))


@app.route('/orders/<order_id>/cancel', methods=['POST'])
def cancel_order(order_id):
    """
    POST /orders/<order_id>/cancel — 撤销挂单。
    触发 PortfolioService.update_order_cancelled()。
    """
    svc = get_svc()
    order = svc.get_order(order_id)
    if not order:
        return err(f'Order not found: {order_id}', 404)
    if order.get('status') not in ('pending', 'partial'):
        return err(f'Cannot cancel order in status "{order.get("status")}"', 422)

    # Use the shared broker instance from main(), not a new one
    from main import get_broker
    broker = get_broker()
    trading_mode = monitor.trading_mode() if (monitor := get_monitor()) else 'simulation'

    if broker is not None:
        cancelled = broker.cancel_order(order_id)
        # Simulation broker always returns False — that is normal, not an error
        if not cancelled and trading_mode == 'live':
            return err('Cancel failed (broker rejected)', 409)
    else:
        # No broker initialised yet; skip broker-level cancel
        pass

    svc.update_order_cancelled(order_id, reason='user_cancelled')
    updated = svc.get_order(order_id)
    return ok(order_id=order_id, status='cancelled', order=updated)


# ============================================================
# Symbol params (P1)
# ============================================================

@app.route('/params/<symbol>', methods=['GET'])
def get_symbol_params(symbol):
    """
    GET /params/<symbol> — 查询单股参数（WFA + 手工配置合并后）。
    返回 rsi_buy, rsi_sell, stop_loss, take_profit, atr_threshold 等。
    """
    from services.signals import load_symbol_params
    params = load_symbol_params(symbol)
    return ok(symbol=symbol, params=params)


@app.route('/params/<symbol>', methods=['PATCH'])
def update_symbol_params(symbol):
    """
    PATCH /params/<symbol> — 更新单股参数（写入 params.json）。
    Body: {"rsi_buy": 30, "stop_loss": 0.06, ...}
    支持字段: rsi_period, rsi_buy, rsi_sell, stop_loss, take_profit,
              min_hold_days, atr_threshold, atr_period, atr_multiplier
    """
    if (e := require_json()):
        return e
    import json as _json
    body = request.json
    allowed = {'rsi_period', 'rsi_buy', 'rsi_sell', 'stop_loss', 'take_profit',
               'min_hold_days', 'atr_threshold', 'atr_period', 'atr_multiplier'}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return err(f'No valid fields. Allowed: {sorted(allowed)}', 422)

    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params_file = os.path.join(proj, 'params.json')
    params_all = {}
    if os.path.exists(params_file):
        with open(params_file, 'r', encoding='utf-8') as f:
            params_all = _json.load(f)

    # 查找或创建对应 symbol 的策略条目
    strategies = params_all.setdefault('strategies', {})
    target_key = None
    for name, conf in strategies.items():
        if conf.get('symbol', '').upper() == symbol.upper():
            target_key = name
            break
    if target_key is None:
        target_key = f'Custom_{symbol}'
        strategies[target_key] = {'symbol': symbol.upper(), 'params': {}}

    p = strategies[target_key].setdefault('params', {})
    for k, v in updates.items():
        p[k] = v
    params_all['updated'] = datetime.now().strftime('%Y-%m-%d')

    with open(params_file, 'w', encoding='utf-8') as f:
        _json.dump(params_all, f, ensure_ascii=False, indent=4)

    return ok(symbol=symbol, updated=updates, params=p)


@app.route('/params', methods=['GET'])
def list_all_params():
    """
    GET /params — 全量参数列表（params.json + live_params.json 合并视图）。
    """
    import json as _json
    from services.signals import load_symbol_params

    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params_file = os.path.join(proj, 'params.json')
    all_symbols = set()
    if os.path.exists(params_file):
        with open(params_file, 'r', encoding='utf-8') as f:
            params_all = _json.load(f)
        for conf in params_all.get('strategies', {}).values():
            sym = conf.get('symbol')
            if sym:
                all_symbols.add(sym.upper())

    # 也包含 live_params.json 中的 key
    live_file = os.path.join(proj, 'backend', 'services', 'live_params.json')
    if os.path.exists(live_file):
        with open(live_file, 'r', encoding='utf-8') as f:
            live = _json.load(f)
        for k in live:
            if '_' in k:
                all_symbols.add(k.rsplit('_', 1)[0])

    result = {}
    for sym in sorted(all_symbols):
        result[sym] = load_symbol_params(sym)

    return ok(params=result, count=len(result))


# ============================================================
# Analysis trigger
# ============================================================

@app.route('/analysis/run', methods=['POST'])
def run_analysis():
    """
    POST /analysis/run — trigger daily analysis.

    运行 DynamicStockSelectorV2 选股 + 记录信号，并将完整结果持久化到
    outputs/analysis/analysis_{date}.json，供 /analysis/health 和
    DailyOpsReporter 读取。
    """
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

        result = {
            'sources': sources,
            'top_sectors': [
                {'bk': bk, 'name': info.get('name'), 'total': info.get('total'),
                 'change_pct': info.get('change_pct')}
                for bk, info in top_bks
            ],
            'news_summary': news,
            'selected_stocks': stocks,
        }

        # ── 持久化到 outputs/analysis/ ──────────────────────────────
        try:
            out_dir = os.path.join(BACKEND_DIR, 'outputs', 'analysis')
            os.makedirs(out_dir, exist_ok=True)
            today_str = datetime.now().strftime('%Y-%m-%d')
            out_path = os.path.join(out_dir, f'analysis_{today_str}.json')
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    **result,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响 API 响应

        # ── 记录 daily_meta（供 StrategyHealthMonitor 读取）─────────
        try:
            summary = svc.get_portfolio_summary()
            trades_today = svc.get_trades(limit=200)
            today_str_iso = datetime.now().strftime('%Y-%m-%d')
            n_trades = sum(1 for t in trades_today
                          if str(t.get('timestamp', ''))[:10] == today_str_iso)
            svc.record_daily_meta(
                equity=float(summary.get('total_equity', 0) or 0),
                cash=float(summary.get('cash', 0) or 0),
                n_signals=len(top_bks),
                n_trades=n_trades,
            )
        except Exception:
            pass

        return ok(**result)
    except Exception as e:
        return err(str(e) + '\n' + traceback.format_exc(), 500)


@app.route('/analysis/health', methods=['GET'])
def analysis_health():
    """GET /analysis/health — 系统健康状态汇总。

    综合持仓、最近分析、信号等数据，返回 OK / WARN / CRITICAL 健康等级。
    """
    try:
        svc = get_svc()
        summary = svc.get_portfolio_summary()
        positions = svc.get_positions()
        n_positions = len(positions)
        total_pnl = sum(float(p.get('unrealized_pnl', 0) or 0) for p in positions)
        cash = float(summary.get('cash', 0) or 0)
        equity = float(summary.get('total_equity', 0) or 0)

        # 简单健康规则
        level = 'OK'
        reasons = []

        # 1. 现金占比过低 → WARN
        if equity > 0 and cash / equity < 0.05:
            level = 'WARN'
            reasons.append(f'现金占比仅 {cash/equity*100:.1f}%，低于 5%')

        # 2. 未实现亏损超过总权益 5% → WARN
        if equity > 0 and total_pnl < -0.05 * equity:
            level = 'WARN'
            reasons.append(f'未实现亏损 {total_pnl:.0f} 超过总权益 5%')

        # 3. 未实现亏损超过总权益 10% → CRITICAL
        if equity > 0 and total_pnl < -0.10 * equity:
            level = 'CRITICAL'
            reasons.append(f'未实现亏损 {total_pnl:.0f} 超过总权益 10%')

        # 4. 最近分析时间检查
        latest_analysis = None
        try:
            analysis_dir = os.path.join(BACKEND_DIR, 'outputs', 'analysis')
            if os.path.isdir(analysis_dir):
                files = sorted(os.listdir(analysis_dir), reverse=True)
                if files:
                    latest_analysis = files[0]
        except Exception:
            pass

        return ok(
            level=level,
            reasons=reasons,
            n_positions=n_positions,
            total_unrealized_pnl=round(total_pnl, 2),
            cash=round(cash, 2),
            equity=round(equity, 2),
            latest_analysis=latest_analysis,
        )
    except Exception as e:
        return err(str(e), 500)


@app.route('/analysis/status', methods=['GET'])
def analysis_status():
    """GET /analysis/status — last known analysis metadata."""
    svc = get_svc()
    metas = svc.get_daily_metas(limit=1)
    if metas:
        return ok(**metas[0])
    return ok(message="No analysis run yet")


# ============================================================
# Pipeline 工厂（DynamicWeightPipeline + 全量因子）
# ============================================================

def build_pipeline(symbol: str = ''):
    """构建生产用因子流水线（委托给 core.pipeline_factory）。"""
    from core.pipeline_factory import build_pipeline as _build
    return _build(symbol=symbol)


# ============================================================
# 行业轮动信号
# ============================================================

@app.route('/analysis/sector_rotation', methods=['POST'])
def sector_rotation_signal():
    """
    POST /analysis/sector_rotation

    基于价格动量对行业 ETF 排名，返回本周期换仓建议。

    Body (JSON, 可选):
        {
          "top_n": 3,
          "lookback_days": 60,
          "rebalance_days": 21,
          "momentum_method": "return",   // "return" | "sharpe"
          "current_holdings": ["510170.SH"]
        }

    Returns:
        {
          "rebalance_date": "2026-04-29",
          "buy":  ["516950.SH", "512660.SH"],
          "sell": ["510170.SH"],
          "hold": [],
          "scores": {"516950.SH": 0.123, ...},
          "avg_turnover_pct": 0.33
        }
    """
    try:
        body = request.get_json(silent=True) or {}
        top_n            = int(body.get('top_n', 3))
        lookback_days    = int(body.get('lookback_days', 60))
        rebalance_days   = int(body.get('rebalance_days', 21))
        momentum_method  = str(body.get('momentum_method', 'return'))
        current_holdings = list(body.get('current_holdings', []))

        from core.strategies.sector_rotation import SectorRotationStrategy, DEFAULT_SECTOR_ETFS
        from core.data_layer import get_data_layer

        strategy = SectorRotationStrategy(
            top_n=top_n,
            lookback_days=lookback_days,
            rebalance_days=rebalance_days,
            momentum_method=momentum_method,
        )

        dl = get_data_layer()
        price_data = {}
        for sym in DEFAULT_SECTOR_ETFS:
            df = dl.get_bars(sym, days=max(lookback_days + 20, 90))
            if df is not None and not df.empty:
                price_data[sym] = df

        if not price_data:
            return err('无法获取行业 ETF 行情数据', 503)

        signal = strategy.latest_signal(price_data, current_holdings=current_holdings)

        # 记录到 signals 表（SECTOR_FLOW 类型告警）
        svc = get_svc()
        for sym in signal.buy:
            svc.record_signal(sym, 'BUY', signal.scores.get(sym, 0),
                              f'行业轮动买入: 动量分 {signal.scores.get(sym, 0):.4f}')

        return ok(
            rebalance_date=signal.rebalance_date,
            buy=signal.buy,
            sell=signal.sell,
            hold=signal.hold,
            scores=signal.scores,
            top_n=signal.top_n,
            universe_size=len(price_data),
        )
    except Exception as e:
        return err(str(e) + '\n' + traceback.format_exc(), 500)


# ============================================================
# 配对交易信号
# ============================================================

@app.route('/analysis/pairs_trading', methods=['POST'])
def pairs_trading_signal():
    """
    POST /analysis/pairs_trading

    在指定标的池中筛选协整配对，并返回当前信号。

    Body (JSON, 可选):
        {
          "symbols": ["600519.SH", "000858.SZ", "000568.SZ"],
          "entry_z": 2.0,
          "exit_z":  0.5,
          "stop_z":  4.0,
          "lookback_days": 60,
          "screen_days":   252
        }

    Returns:
        {
          "pairs": [
            {
              "symbol_a": "600519.SH",
              "symbol_b": "000858.SZ",
              "signal": { "spread_zscore": 2.3, "action_a": "BUY", "action_b": "SELL", ... }
            }
          ],
          "n_pairs_found": 1
        }
    """
    try:
        body = request.get_json(silent=True) or {}
        symbols      = list(body.get('symbols', []))
        entry_z      = float(body.get('entry_z', 2.0))
        exit_z       = float(body.get('exit_z',  0.5))
        stop_z       = float(body.get('stop_z',  4.0))
        lookback_days= int(body.get('lookback_days', 60))
        screen_days  = int(body.get('screen_days', 252))

        if len(symbols) < 2:
            return err('至少提供 2 个标的用于配对筛选', 400)

        from core.strategies.pairs_trading import find_cointegrated_pairs, PairsTradingStrategy
        from core.data_layer import get_data_layer

        dl = get_data_layer()
        # 加载价格矩阵（close 列）
        price_dict = {}
        for sym in symbols:
            df = dl.get_bars(sym, days=screen_days + 30)
            if df is not None and not df.empty and 'close' in df.columns:
                price_dict[sym] = df['close']

        if len(price_dict) < 2:
            return err('有效行情数据不足 2 个标的', 503)

        price_df = pd.DataFrame(price_dict).dropna()

        # 筛选协整配对
        pairs = find_cointegrated_pairs(price_df, lookback_days=screen_days)

        results = []
        for sym_a, sym_b in pairs[:5]:   # 最多返回 5 对
            try:
                strat = PairsTradingStrategy(
                    symbol_a=sym_a, symbol_b=sym_b,
                    entry_z=entry_z, exit_z=exit_z, stop_z=stop_z,
                    lookback_days=lookback_days,
                )
                signal = strat.latest_signal(price_df)
                if signal:
                    results.append({
                        'symbol_a': sym_a,
                        'symbol_b': sym_b,
                        'signal': {
                            'date':          signal.date,
                            'spread_zscore': round(signal.spread_zscore, 4),
                            'action_a':      signal.action_a,
                            'action_b':      signal.action_b,
                            'spread':        round(signal.spread, 6),
                        }
                    })
            except Exception:
                continue

        return ok(pairs=results, n_pairs_found=len(pairs))
    except Exception as e:
        return err(str(e) + '\n' + traceback.format_exc(), 500)


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
    Body: {"alert_pct": 7.0} 和/或 {"enabled": 0}
    """
    if not request.is_json:
        return err('Content-Type must be application/json', 415)
    body = request.json or {}
    from services.watchlist import set_alert_threshold, set_watchlist_enabled
    if 'alert_pct' in body:
        set_alert_threshold(symbol, float(body['alert_pct']))
    if 'enabled' in body:
        set_watchlist_enabled(symbol, int(body['enabled']))
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


@app.route('/data/realtime/<symbol>', methods=['GET'])
def data_realtime(symbol):
    """
    GET /data/realtime/<symbol> — 轻量实时行情接口。
    包一层 fetch_realtime()，返回最新价、涨跌幅、成交量等。
    """
    from services.signals import fetch_realtime
    quote = fetch_realtime(symbol)
    if not quote:
        return err(f'Realtime data unavailable for {symbol}', 502)
    return ok(symbol=symbol, quote=quote)


# ============================================================
# Trading Mode
# ============================================================

_MODE_FILE = os.path.join(os.path.dirname(__file__), 'trading_mode.json')
_VALID_MODES = {'simulation', 'live'}


def _load_trading_mode() -> str:
    try:
        with open(_MODE_FILE, 'r') as f:
            data = json.load(f)
        mode = data.get('mode', 'simulation')
        return mode if mode in _VALID_MODES else 'simulation'
    except (FileNotFoundError, json.JSONDecodeError):
        return 'simulation'


def _save_trading_mode(mode: str) -> None:
    with open(_MODE_FILE, 'w') as f:
        json.dump({'mode': mode, 'updated_at': datetime.now().isoformat()}, f)


@app.route('/trading/mode', methods=['GET'])
def get_trading_mode():
    """Return current trading mode (simulation or live)."""
    mode = _load_trading_mode()
    return ok(mode=mode)


@app.route('/trading/mode', methods=['PUT'])
def set_trading_mode():
    """Set trading mode. Body: {"mode": "simulation"|"live"}"""
    if (e := require_json()):
        return e
    body = request.json or {}
    mode = body.get('mode', '')
    if mode not in _VALID_MODES:
        return err(f'invalid mode "{mode}", must be one of: {sorted(_VALID_MODES)}', 422)
    _save_trading_mode(mode)
    return ok(mode=mode, message=f'Trading mode set to {mode}')


# ============================================================
# Monitor status
# ============================================================

@app.route('/monitor/status', methods=['GET'])
def monitor_status():
    """
    GET /monitor/status
    返回 IntradayMonitor 的实时运行状态：
      - 线程状态、交易模式、扫描计数
      - 最近 10 条信号触发记录
      - 最近 10 条跳过记录（含原因分类）
      - 最近 5 条 LLM 审核记录
      - 风控状态（Kelly 仓位、回撤熔断）
    """
    from main import get_monitor
    monitor = get_monitor()
    if monitor is None:
        return err('Monitor not initialized', 503)
    return ok(monitor.get_status())


@app.route('/risk/status', methods=['GET'])
def risk_status():
    """
    GET /risk/status — 风控状态查询。
    返回：组合敞口、板块集中度、ATR 止损触发状态、回撤水平。
    """
    svc = get_svc()
    from main import get_monitor
    monitor = get_monitor()

    positions = svc.get_positions()
    summary = svc.get_portfolio_summary(refresh_prices_now=True)

    # 板块集中度
    sector_exposure = {}
    total_market_value = 0
    for p in positions:
        mv = p.get('shares', 0) * p.get('current_price', 0)
        total_market_value += mv
        sector = p.get('sector', 'unknown')
        sector_exposure[sector] = sector_exposure.get(sector, 0) + mv
    if total_market_value > 0:
        sector_exposure = {k: round(v / total_market_value, 4)
                           for k, v in sector_exposure.items()}

    # 回撤水平
    dd_warn = 0.0
    dd_stop = 0.0
    peak_equity = 0.0
    current_equity = summary.get('total_equity', 0)
    if monitor:
        peak_equity = monitor._peak_equity
        dd_warn = monitor._dd_warn
        dd_stop = monitor._dd_stop
        if peak_equity > 0:
            current_dd = 1 - (current_equity / peak_equity)
        else:
            current_dd = 0.0
    else:
        current_dd = 0.0

    return ok(
        total_equity=round(current_equity, 2),
        peak_equity=round(peak_equity, 2),
        current_drawdown=round(current_dd, 4),
        dd_warn_threshold=dd_warn,
        dd_stop_threshold=dd_stop,
        risk_warn_fired=monitor._risk_warn_fired if monitor else False,
        risk_stop_fired=monitor._risk_stop_fired if monitor else False,
        kelly_pct=round(monitor._kelly_pct, 4) if monitor else None,
        position_count=len(positions),
        sector_exposure=sector_exposure,
    )


# ============================================================
# Prometheus 监控指标端点
# ============================================================

@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """
    暴露 Prometheus 格式监控指标。

    指标包含：
      trading_net_value         — 组合净值
      trading_total_pnl_yuan    — 累计浮动盈亏
      trading_n_positions       — 持仓数量
      trading_cash_yuan         — 可用现金
      trading_health_status     — 策略健康状态（0=OK,1=WARN,2=CRITICAL）
      trading_api_requests_total — API 请求计数
      trading_api_errors_total  — API 错误计数
      trading_factor_ic         — 各因子最新 IC 值

    Prometheus 配置示例（prometheus.yml）：
      scrape_configs:
        - job_name: 'trading'
          static_configs:
            - targets: ['localhost:5555']
          metrics_path: /metrics
    """
    try:
        from core.metrics import get_registry
        reg = get_registry()

        # 从本地 portfolio 数据刷新指标
        try:
            positions = _svc.get_positions()
            cash = _svc.get_cash()
            total_pnl = sum(
                p.get('unrealized_pnl', 0.0)
                for p in (positions if isinstance(positions, list) else [])
            )
            n_pos = len([p for p in (positions if isinstance(positions, list) else [])
                         if p.get('shares', 0) > 0])
            total_val = cash + sum(
                float(p.get('shares', 0)) * float(p.get('current_price', 0.0))
                for p in (positions if isinstance(positions, list) else [])
            )
            net_val = total_val / max(_svc.get_initial_capital() if hasattr(_svc, 'get_initial_capital') else total_val, 1.0)
            reg.update_from_portfolio(
                net_value=net_val,
                total_pnl=float(total_pnl),
                n_positions=n_pos,
                cash=float(cash),
            )
        except Exception:
            pass   # 静默降级，仍返回已有指标

        output = reg.generate()
        return output, 200, {'Content-Type': reg.content_type}
    except Exception as e:
        return f'# metrics error: {e}\n', 500, {'Content-Type': 'text/plain'}


# ============================================================
# P1: Northbound (北向资金)
# ============================================================

@app.route('/northbound/flow', methods=['GET'])
def northbound_flow():
    """
    GET /northbound/flow?refresh=1

    北向资金实时流量（沪深港通）。
    refresh=1 强制跳过 60s cache 拉取最新数据。
    """
    refresh = request.args.get('refresh', '0') == '1'
    from services.northbound import fetch_kamt, get_north_flow_direction
    from services.data_cache import cached_kamt

    kamt = cached_kamt(force_refresh=refresh) if refresh else fetch_kamt()
    if not kamt:
        return err('Failed to fetch northbound data', 502)

    direction = get_north_flow_direction()
    net_yi = kamt.get('net_north_cny', 0) / 1e8

    # 格式化摘要
    from services.northbound import format_kamt_summary
    summary_text = format_kamt_summary(kamt)

    # 近10日历史
    from services.northbound import get_north_history
    history = get_north_history()
    history_yi = {k: round(v / 1e8, 2) for k, v in history.items()}

    return ok(
        summary=summary_text,
        net_north_yi=round(net_yi, 2),
        direction=direction.get('direction'),
        strength=direction.get('strength'),
        trend_yi=direction.get('trend_yi'),
        reason=direction.get('reason'),
        history=history_yi,
        updated_at=kamt.get('timestamp', ''),
    )


# ============================================================
# P1: Performance Summary (绩效聚合)
# ============================================================

@app.route('/performance/summary', methods=['GET'])
def performance_summary():
    """
    GET /performance/summary?year=2026&month=4&include_chart=1

    聚合三大绩效函数，统一返回账户表现。
    """
    year  = request.args.get('year', type=int) or date.today().year
    month = request.args.get('month', type=int) or date.today().month
    incl_chart = request.args.get('include_chart', '1') == '1'

    from services.performance import (
        generate_monthly_report,
        compute_trade_stats,
        compute_max_drawdown,
    )
    from services.portfolio import PortfolioService

    # 聚合三个计算函数的结果
    report = generate_monthly_report(year=year, month=month, include_chart=incl_chart)

    svc = get_svc()
    trades = svc.get_orders(status='filled', limit=500)

    # 只取当月交易
    month_str = f"{year}-{month:02d}"
    month_trades = [t for t in trades if (t.get('filled_at') or '').startswith(month_str)]

    trade_stats = compute_trade_stats(trades)         # 全量
    trade_stats_month = compute_trade_stats(month_trades)  # 当月

    equity_series = report.get('equity_series', [])
    max_dd = compute_max_drawdown(equity_series) if equity_series else {
        'max_drawdown_pct': 0.0, 'peak_equity': 0,
        'trough_equity': 0, 'peak_date': '', 'trough_date': '',
    }

    return ok(
        period=f"{year}年{month}月",
        year=year,
        month=month,
        returns=report.get('returns', {}),
        summary=report.get('summary', {}),
        trade_stats=trade_stats,
        trade_stats_month=trade_stats_month,
        max_drawdown=max_dd,
        equity_curve=equity_series[-30:],
        benchmark_curve=report.get('benchmark_curve', [])[-30:],
        chart_base64=report.get('chart_base64') if incl_chart else None,
        generated_at=report.get('generated_at'),
    )


# ============================================================
# P1: Fundamentals (基本面)
# ============================================================

@app.route('/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol):
    """
    GET /fundamentals/600036.SH

    返回 PE、PB、股息率、总市值等基本面指标。
    """
    from services.fundamentals import fetch_fundamentals
    data = fetch_fundamentals(symbol)
    if data is None:
        return err(f'Fundamentals unavailable for {symbol}', 404)
    return ok(**data)


# ============================================================
# P1: Market Status (市场状态)
# ============================================================

@app.route('/market/status', methods=['GET'])
def market_status():
    """
    GET /market/status

    返回当前市场是否开盘、交易时段、下次开/收时间。
    """
    from services.intraday_monitor import is_market_open
    from datetime import datetime, timedelta

    now = datetime.now()
    open_now = is_market_open(now)

    # 判断当前时段
    from datetime import time as dtime
    t = now.time()
    if open_now:
        if dtime(9, 30) <= t < dtime(11, 30):
            session = 'morning'
            next_change = now.replace(hour=11, minute=30, second=0, microsecond=0)
        elif dtime(13, 0) <= t < dtime(15, 0):
            session = 'afternoon'
            next_change = now.replace(hour=15, minute=0, second=0, microsecond=0)
        else:
            session = 'closed'  # 盘后
            next_change = None
    else:
        session = 'closed'
        # 下一个工作日 9:15（集合竞价）
        days_ahead = 1 if t >= dtime(15, 0) else 0
        from datetime import date as ddate
        next_open = (datetime.combine(ddate.today(), dtime(9, 15))
                     + timedelta(days=days_ahead))
        next_change = next_open.isoformat()

    return ok(
        is_open=open_now,
        session=session,
        next_change=next_change,
        server_time=now.isoformat(),
    )


# ============================================================
# P1: Orders with Filters (订单过滤查询) — modifies existing
# ============================================================

# ============================================================
# P2: LLM Signal Review (独立信号审核)
# ============================================================

@app.route('/llm/analyze', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def llm_analyze():
    """
    POST /llm/analyze — LLM 独立信号审核

    Body (required):
        symbol, direction, signal, price, alert_reason
    Body (optional):
        entry_price, position_shares, position_pnl,
        rsi_value, atr_ratio, market_regime, north_flow_yi,
        cash, equity, other_positions, recent_trades, news_sentiment

    返回: {approved, decision, reason, confidence, size_rec}
    """
    if (e := require_json()):
        return e
    body = request.json
    required = ['symbol', 'direction', 'signal', 'price', 'alert_reason']
    for f in required:
        if f not in body:
            return err(f'missing required field: {f}', 422)

    # 尝试初始化 LLM provider
    provider = None
    try:
        from services.llm.providers import MiniMaxProvider
        provider = MiniMaxProvider()
        provider.chat([{"role": "user", "content": "hi"}], max_tokens=5)
    except Exception:
        pass

    from services.llm.service import signal_review
    result = signal_review(
        symbol=body['symbol'],
        direction=body['direction'],
        signal=body['signal'],
        price=float(body['price']),
        alert_reason=body['alert_reason'],
        entry_price=body.get('entry_price'),
        position_shares=int(body.get('position_shares', 0)),
        position_pnl=float(body.get('position_pnl', 0)),
        rsi_value=body.get('rsi_value'),
        atr_ratio=body.get('atr_ratio'),
        market_regime=body.get('market_regime', 'UNKNOWN'),
        north_flow_yi=float(body.get('north_flow_yi', 0)),
        cash=float(body.get('cash', 0)),
        equity=float(body.get('equity', 0)),
        other_positions=body.get('other_positions'),
        recent_trades=body.get('recent_trades'),
        news_sentiment=body.get('news_sentiment', ''),
        provider=provider,
    )
    return ok(
        approved=result.approved,
        decision=result.decision,
        reason=result.reason,
        confidence=result.confidence,
        size_rec=result.size_rec,
        llm_available=(provider is not None),
    )


# ============================================================
# P2: WFA History (WFA 历史查询)
# ============================================================

@app.route('/wfa/history', methods=['GET'])
def wfa_history():
    """
    GET /wfa/history?symbol=600036.SH&strategy=RSI&limit=30

    查询 WFA 运行历史记录。
    """
    symbol   = request.args.get('symbol')
    strategy = request.args.get('strategy')
    limit    = int(request.args.get('limit', 30))

    from services.wfa_history import get_wfa_history
    try:
        records = get_wfa_history(symbol=symbol, strategy=strategy, limit=limit)
        return ok(records=records, count=len(records))
    except Exception as e:
        return err(str(e), 500)


@app.route('/wfa/summary', methods=['GET'])
def wfa_summary():
    """
    GET /wfa/summary?symbol=600036.SH

    查询某标的最新 WFA 结果（regime ATR 两条策略的最新记录）。
    """
    symbol = request.args.get('symbol')
    if not symbol:
        return err('symbol is required', 422)

    from services.wfa_history import get_latest_wfa
    rsi_result = get_latest_wfa(symbol, 'RSI')
    atr_result = get_latest_wfa(symbol, 'ATR')
    return ok(
        symbol=symbol,
        rsi=rsi_result,
        atr=atr_result,
    )


# ============================================================
# IPO Analysis endpoints  (feature/ipo-stars)
# ============================================================

@app.route('/ipo/analyze', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def analyze_ipo():
    """
    POST /ipo/analyze?stock_code=01810

    单只新股深度分析报告。
    触发完整分析流程：多源数据获取 → 交叉验证 → 分析引擎 → 报告生成。

    Query params:
        stock_code  — 股票代码（港股如 01810，需包含前导零）

    Returns:
        IPOAnalysisReport JSON（见 core.ipo_report.IPOAnalysisReport.to_dict()）
        404 if stock_code not found or analysis fails.
    """
    stock_code = request.args.get('stock_code', '').strip()
    if not stock_code:
        return err('stock_code is required', 422)

    try:
        from core.ipo_data_source import IPODataSource
        from core.ipo_cross_validator import DataCrossValidator
        from core.ipo_analyst_engine import IPOAnalystEngine

        # Step 1: 多源数据获取
        ds = IPODataSource()
        ipo_info = ds.get_ipo_info(stock_code)
        if ipo_info is None:
            return err(f'IPO data not found for stock_code={stock_code}', 404)

        multi_source = ds.get_all_sources(stock_code)

        # Step 2: 交叉验证
        validator = DataCrossValidator()
        validated = validator.merge_with_confidence(multi_source)

        # Step 3: 分析引擎
        engine = IPOAnalystEngine()
        report = engine.analyze(
            stock_code=stock_code,
            multi_source_data=multi_source,
            validated_data=validated,
            market_sentiment={},
        )

        # Step 4: 报告生成并持久化
        try:
            report_dict = report.to_dict()
        except Exception:
            # Fallback: manual dict conversion
            report_dict = {
                'stock_code': stock_code,
                'report': str(report),
            }

        # 持久化到 outputs/ipo/
        try:
            out_dir = os.path.join(BACKEND_DIR, 'outputs', 'ipo')
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f'ipo_analysis_{stock_code}.json')
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    'stock_code': stock_code,
                    'report': report_dict,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响 API 响应

        return ok(stock_code=stock_code, report=report_dict)

    except Exception as e:
        return err(f'IPO analysis failed: {e}\n{traceback.format_exc()}', 500)


@app.route('/ipo/upcoming', methods=['GET'])
def upcoming_ipos():
    """
    GET /ipo/upcoming

    获取即将上市的港股新股列表（来自东方财富）。
    数据经 IPODataSource 缓存，TTL 30s。

    Returns:
        List[IPOInfo]  — 每个元素包含 stock_code, stock_name,
                         listing_date, issue_price, proceeds 等字段。
    """
    try:
        from core.ipo_data_source import IPODataSource

        ds = IPODataSource()
        force_refresh = request.args.get('refresh', '0') == '1'
        ipos = ds.get_upcoming_ipos(force_refresh=force_refresh)

        # 转为 dict 列表
        result = []
        for ipo in ipos:
            try:
                result.append({
                    'stock_code': ipo.stock_code,
                    'stock_name': ipo.stock_name,
                    'listing_date': ipo.listing_date,
                    'issue_price': ipo.issue_price,
                    'issue_price_high': ipo.issue_price_high,
                    'issue_price_low': ipo.issue_price_low,
                    'shares': ipo.shares,
                    'proceeds': ipo.proceeds,
                    'lot_size': ipo.lot_size,
                    'application_ratio': ipo.application_ratio,
                    'application_ratio_乙组': ipo.application_ratio_乙组,
                    'application_deadline': ipo.application_deadline,
                    'listing_board': ipo.listing_board,
                    'industry': ipo.industry,
                    'sponsor': ipo.sponsor,
                    'cornerstone_investors': ipo.cornerstone_investors,
                    'source': ipo.source,
                    'fetched_at': ipo.fetched_at,
                })
            except Exception:
                continue

        return ok(ipos=result, count=len(result))
    except Exception as e:
        return err(f'Failed to fetch upcoming IPOs: {e}\n{traceback.format_exc()}', 500)


@app.route('/ipo/history/<stock_code>', methods=['GET'])
def ipo_history(stock_code):
    """
    GET /ipo/history/601888

    从 IPOHistoryStore（A 股历史新股 Parquet 缓存）查询历史分析记录。
    支持 A 股股票代码（如 601888、001270.SZ）。

    Query params:
        start  — 开始日期 YYYY-MM-DD（默认 2020-01-01）
        end    — 结束日期 YYYY-MM-DD（默认今日）

    Returns:
        DataFrame records as dict list, with columns:
        symbol / name / ipo_date / issue_price / shares /
        pe_ratio / industry / market_type
    """
    try:
        from core.ipo_store import IPOHistoryStore

        start = request.args.get('start') or '2020-01-01'
        end = request.args.get('end') or datetime.now().strftime('%Y-%m-%d')

        store = IPOHistoryStore()
        df = store.get_ipo_history(start=start, end=end, symbol=stock_code)

        if df is None or df.empty:
            return err(f'No IPO history found for stock_code={stock_code}', 404)

        # DataFrame → list of dict
        records = []
        for idx, row in df.iterrows():
            rec = {}
            for col, val in row.items():
                if val is not None and not (isinstance(val, float) and np.isnan(val)):
                    rec[col] = val
            records.append(rec)

        return ok(stock_code=stock_code, records=records, count=len(records))
    except Exception as e:
        return err(f'Failed to fetch IPO history: {e}\n{traceback.format_exc()}', 500)


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

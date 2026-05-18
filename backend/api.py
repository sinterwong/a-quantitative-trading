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
import threading
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
from core.data_gateway.capabilities import MacroIndicator

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


# ─── P2-20: Global API Key Auth + Per-IP Rate Limit ──────────────────
# 通过 before_request 钩子覆盖 50+ 端点，未 decorate 的端点也受保护。
# 配置：
#   TRADING_API_KEY     — 设置后启用 X-API-Key 校验；未设置则放行（dev 默认）
#   TRADING_RL_PER_MIN  — 全局每分钟限流上限，默认 120；设为 0 关闭
#
# 公共端点（始终免认证、免限流）：/health, /docs, /metrics
_PUBLIC_PATHS = frozenset({'/health', '/docs', '/metrics'})

_GLOBAL_RATE_LIMIT: dict = {}    # ip -> [timestamps...]


def _global_rl_max() -> int:
    try:
        return max(0, int(os.environ.get('TRADING_RL_PER_MIN', '120')))
    except ValueError:
        return 120


def _api_key_required() -> str:
    return os.environ.get('TRADING_API_KEY', '').strip()


_LOOPBACK_IPS = frozenset({'127.0.0.1', '::1', 'localhost'})


def _is_loopback_request() -> bool:
    """识别本地回环请求（Streamlit / 本机脚本）。"""
    addr = (request.remote_addr or '').strip()
    return addr in _LOOPBACK_IPS


@app.before_request
def _check_auth_and_rate_limit():
    path = (request.path or '').rstrip('/') or '/'
    # OPTIONS（CORS preflight）与公共端点放行
    if request.method == 'OPTIONS' or path in _PUBLIC_PATHS:
        return None

    # 本地回环豁免（保留 Streamlit / 本机调度脚本零摩擦），可用 env
    # TRADING_API_REQUIRE_LOCALHOST=1 关闭以模拟生产
    require_local = os.environ.get('TRADING_API_REQUIRE_LOCALHOST', '0').strip()
    if _is_loopback_request() and require_local != '1':
        return None

    # API Key 认证（仅在 TRADING_API_KEY 设置时启用）
    expected = _api_key_required()
    if expected:
        provided = request.headers.get('X-API-Key', '').strip()
        if not provided or provided != expected:
            return jsonify({
                'status': 'error',
                'error': 'unauthorized: invalid or missing X-API-Key',
                'timestamp': datetime.now().isoformat(),
            }), 401

    # 全局每分钟 per-IP 限流
    rl_max = _global_rl_max()
    if rl_max > 0:
        now = time.time()
        cutoff = now - 60.0
        key = request.remote_addr or 'unknown'
        bucket = _GLOBAL_RATE_LIMIT.get(key, [])
        bucket = [t for t in bucket if t > cutoff]
        if len(bucket) >= rl_max:
            _GLOBAL_RATE_LIMIT[key] = bucket
            return jsonify({
                'status': 'error',
                'code': 429,
                'message': f'global rate limit exceeded (>{rl_max}/min)',
                'timestamp': datetime.now().isoformat(),
            }), 429
        bucket.append(now)
        _GLOBAL_RATE_LIMIT[key] = bucket

    return None

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


# ============================================================
# Order intent (Phase 1: just records intent)
# Phase 2: will call broker service
# ============================================================

def _get_or_build_broker():
    """复用 main.get_broker() 的共享实例；测试/无 monitor 场景回退到新建 PaperBroker。"""
    try:
        from main import get_broker
        b = get_broker()
        if b is not None:
            return b
    except Exception:
        pass
    from services.broker import PaperBroker
    b = PaperBroker(portfolio_service=get_svc())
    b.connect()
    return b


_RISK_ENGINE = None
# Flask 在多线程 WSGI 下两个请求会并发进入 _get_risk_engine 的懒建分支;
# 没锁会创建两份 RiskEngine,如果其 __init__ 有副作用(打开 sqlite 句柄、
# 注册回调) 就会泄漏。锁只保护懒建,共享 StrategyRunner 实例的路径无副作用。
_RISK_ENGINE_LOCK = threading.Lock()


def _get_risk_engine():
    """共享 RiskEngine：优先复用 StrategyRunner 的实例，否则懒建一个本地 singleton。"""
    global _RISK_ENGINE
    try:
        from main import get_monitor
        m = get_monitor()
        if m is not None and getattr(m, '_strategy_runner', None) is not None:
            re = getattr(m._strategy_runner, 'risk_engine', None)
            if re is not None:
                return re
    except Exception:
        pass
    if _RISK_ENGINE is None:
        with _RISK_ENGINE_LOCK:
            if _RISK_ENGINE is None:
                try:
                    from core.risk_engine import RiskEngine
                    _RISK_ENGINE = RiskEngine()
                except Exception:
                    return None
    return _RISK_ENGINE


@app.route('/orders/submit', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def submit_order():
    """POST /orders/submit — 通过共享 broker 提交订单(simulation 模式下 PaperBroker 即时撮合)。

    所有外部下单(UI / 脚本 / 第三方)都必须先过 PreTrade 风控，与
    IntradayMonitor 的内部下单链路保持同一道门控。
    """
    if (e := require_json()):
        return e
    body = request.json
    for field in ('symbol', 'direction', 'shares'):
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

    # 市价单调用方通常不带 price,但 RiskEngine 的多数规则(单笔金额、组合
    # 占比、CVaR 估算)需要金额才能算。price=0 等于绕过这些规则 ⇒ 必须先
    # 拿到一个参考价。复用 broker 的 _fetch_market_price(失败返回 0),
    # 失败时直接拒单而不是用 0 通过风控。
    broker = _get_or_build_broker()
    if price <= 0:
        ref = 0.0
        try:
            fetch = getattr(broker, '_fetch_market_price', None)
            if fetch is not None:
                ref = float(fetch(symbol) or 0.0)
        except Exception:
            ref = 0.0
        if ref <= 0:
            return jsonify({
                'status': 'error',
                'error': 'market order needs a reference price (broker fetch failed)',
                'code': 'NO_REF_PRICE',
                'timestamp': datetime.now().isoformat(),
            }), 503
        risk_price = ref
    else:
        risk_price = price

    # ── PreTrade 风控（与 IntradayMonitor 同一道门控）────────────
    re = _get_risk_engine()
    if re is not None:
        try:
            from core.factors.base import Signal as _Sig
            sig = _Sig(
                timestamp=datetime.now(), symbol=symbol, direction=direction,
                strength=1.0, factor_name='API', price=risk_price,
            )
            rr = re.check(sig)
            if not rr.passed:
                return jsonify({
                    'status': 'error',
                    'error': f'PreTrade rejected: {rr.reason}',
                    'code': 'RISK_REJECTED',
                    'details': rr.details,
                    'timestamp': datetime.now().isoformat(),
                }), 403
        except Exception as exc:
            # 风控自身异常不应放行：宁可保守拒单
            return jsonify({
                'status': 'error',
                'error': f'PreTrade check raised: {exc}',
                'code': 'RISK_ERROR',
                'timestamp': datetime.now().isoformat(),
            }), 503

    result = broker.submit_order(
        symbol=symbol, direction=direction, shares=shares,
        price=price,
        price_type=price_type,
    )
    return ok(
        order_id=result.order_id, status=result.status,
        symbol=symbol, direction=direction, shares=shares,
        filled_shares=result.filled_shares, avg_price=result.avg_price,
        reason=result.reason,
        submitted_at=result.submitted_at, filled_at=result.filled_at,
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
    支持字段见 services.signals.PARAM_FIELDS_ALLOWED。
    """
    if (e := require_json()):
        return e
    from services.signals import update_symbol_params as _update, PARAM_FIELDS_ALLOWED
    updated = _update(symbol, request.json or {})
    if not updated:
        return err(f'No valid fields. Allowed: {sorted(PARAM_FIELDS_ALLOWED)}', 422)
    return ok(symbol=symbol, params=updated)


@app.route('/params', methods=['GET'])
def list_all_params():
    """GET /params — 全量参数列表（params.json + live_params.json 合并视图）。"""
    from services.signals import list_symbols_with_params, load_symbol_params
    result = {sym: load_symbol_params(sym) for sym in list_symbols_with_params()}
    return ok(params=result, count=len(result))


# ============================================================
# Analysis trigger
# ============================================================

@app.route('/analysis/run', methods=['POST'])
def run_analysis():
    """POST /analysis/run — 触发每日分析 (use case: daily_analysis)。"""
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis
    response = run_daily_analysis(
        DailyAnalysisRequest(output_dir=os.path.join(BACKEND_DIR, 'outputs', 'analysis')),
        portfolio_svc=get_svc(),
    )
    return ok(**response.to_dict())


@app.route('/analysis/health', methods=['GET'])
def analysis_health():
    """GET /analysis/health — 系统健康状态 (use case: system_health)。"""
    from core.use_cases.system_health import compute_system_health
    report = compute_system_health(
        get_svc(),
        analysis_dir=os.path.join(BACKEND_DIR, 'outputs', 'analysis'),
    )
    return ok(**report.to_dict())


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
    from core.use_cases.sector_rotation_signal import (
        SectorRotationRequest, run_sector_rotation,
    )
    from core.use_cases import UseCaseError
    body = request.get_json(silent=True) or {}
    req = SectorRotationRequest(
        top_n=int(body.get('top_n', 3)),
        lookback_days=int(body.get('lookback_days', 60)),
        rebalance_days=int(body.get('rebalance_days', 21)),
        momentum_method=str(body.get('momentum_method', 'return')),
        current_holdings=list(body.get('current_holdings', [])),
    )
    try:
        response = run_sector_rotation(req, portfolio_svc=get_svc())
    except UseCaseError as exc:
        return err(exc.message, 503 if exc.code == 'DATA_UNAVAILABLE' else 422)
    return ok(**response.to_dict())


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
    from core.use_cases.pairs_trading_signal import (
        PairsTradingRequest, find_pairs_signals,
    )
    from core.use_cases import UseCaseError
    body = request.get_json(silent=True) or {}
    req = PairsTradingRequest(
        symbols=list(body.get('symbols', [])),
        entry_z=float(body.get('entry_z', 2.0)),
        exit_z=float(body.get('exit_z', 0.5)),
        stop_z=float(body.get('stop_z', 4.0)),
        lookback_days=int(body.get('lookback_days', 60)),
        screen_days=int(body.get('screen_days', 252)),
    )
    try:
        response = find_pairs_signals(req)
    except UseCaseError as exc:
        code = 503 if exc.code == 'DATA_UNAVAILABLE' else 400
        return err(exc.message, code)
    return ok(**response.to_dict())


# ============================================================
# 单股票综合分析（A 股 / 港股）
# ============================================================

@app.route('/analysis/stock/a', methods=['POST'])
def analyze_a_stock_endpoint():
    """
    POST /analysis/stock/a

    A 股单标的综合分析。整合：
      - 行情快照 + 实时报价
      - 因子流水线（technical + fundamental + macro，DynamicWeightPipeline）
      - 基本面快照（PE / PB / ROE / 营收增速等，AKShare）
      - 大盘 Regime（BULL / BEAR / VOLATILE / CALM）
      - 单股票风险（ATR / VaR-95 / 年化波动率 / 建议止损止盈）
      - 可选：ML 方向预测、新闻情感、LLM 综合解读
      - 规则化投资建议（基于综合得分 × Regime × 基本面）

    Body:
      {
        "symbol": "603369.SH",        // 必填，'NNNNNN.SH' 或 'NNNNNN.SZ'
        "lookback_days": 250,          // 可选，默认 250
        "include_regime": true,        // 可选，默认 true
        "include_news": false,         // 可选，默认 false（依赖 NLP 缓存）
        "include_ml": false,           // 可选，默认 false（依赖已训练模型）
        "include_llm": false           // 可选，默认 false（产生 LLM 调用费用）
      }

    Returns:
      与 services.single_stock_analysis.AnalysisReport 的 to_dict() 一致，
      详见模块 docstring。失败字段以 warnings + 字段为 None 表达。
    """
    from services.single_stock_analysis import (
        AnalysisRequest, analyze_a_share, detect_market,
    )
    try:
        req = AnalysisRequest.from_body(request.get_json(silent=True) or {})
    except ValueError as exc:
        return err(str(exc), 422)
    if detect_market(req.symbol) != 'A':
        return err(
            f'symbol {req.symbol!r} 不是 A 股代码（应为 NNNNNN.SH/SZ）；港股请用 /analysis/stock/hk',
            422,
        )
    return ok(**analyze_a_share(req).to_dict())


@app.route('/analysis/stock/hk', methods=['POST'])
def analyze_hk_stock_endpoint():
    """
    POST /analysis/stock/hk

    港股单标的综合分析。整合：
      - 港股快照（新浪 HK：last / 52w / 涨跌幅 / 市值）
      - 技术因子（RSI / MACD / Bollinger / ATR；港股不接入 A 股 fundamental / macro）
      - 风险（基于历史 K 线 ATR / VaR；不可用时回退 52w range 估算）
      - 可选 LLM 综合解读（ML / 新闻港股暂未支持）

    Body:
      {
        "symbol": "HK:00700",          // 必填，支持 'HK:NNNNN' / 'NNNNN.HK' / 'hkNNNNN'
        "lookback_days": 250,
        "include_regime": false,        // 港股忽略；返回 N/A
        "include_news": false,          // 港股 NLP 因子未对接，返回 unavailable
        "include_ml": false,            // 港股 ML 模型未注册，返回 unavailable
        "include_llm": false            // 可用，调用配置的 LLM provider
      }

    Returns:
      AnalysisReport.to_dict() 结构，market='HK'，缺失能力以 warnings 列出。
    """
    from services.single_stock_analysis import (
        AnalysisRequest, analyze_hk_share, detect_market,
    )
    try:
        req = AnalysisRequest.from_body(request.get_json(silent=True) or {})
    except ValueError as exc:
        return err(str(exc), 422)
    if detect_market(req.symbol) != 'HK':
        return err(
            f'symbol {req.symbol!r} 不是港股代码（应为 HK:NNNNN / NNNNN.HK / hkNNNNN）；A 股请用 /analysis/stock/a',
            422,
        )
    return ok(**analyze_hk_share(req).to_dict())


# ============================================================
# Sector Comparison
# ============================================================

@app.route('/analysis/sector/compare', methods=['POST'])
def sector_compare():
    """
    POST /analysis/sector/compare

    行业板块横向对比：给定行业名称或股票列表，返回同行业个股的估值对比。

    Body（两种模式）:
      行业模式:
        {
          "sector": "白酒",           // 必填，行业名称
          "base_symbol": "603369.SH"   // 可选，基准股票
        }

      自定义模式:
        {
          "symbols": ["603369.SH","000858.SZ","600519.SH"],  // 必填，股票列表
          "sector_name": "白酒",       // 可选，板块名称（用于展示）
          "base_symbol": "603369.SH"    // 可选，基准股票
        }

    支持的行业: 白酒、银行、房地产、医药、电力设备、电子、计算机、
               国防军工、食品饮料、非银金融、煤炭、有色金属、化工、建筑、交通运输

    Returns:
      {
        "sector_name": "白酒",
        "stock_count": 4,
        "avg_pe": 22.5,
        "avg_pb": 4.2,
        "stocks": [
          {
            "symbol": "603369.SH", "name": "今世缘",
            "price": 28.13, "pct_change": 2.11,
            "pe": 14.96, "pb": 3.47,
            "pe_percentile": 15.2, "pb_percentile": 8.1,
            "is_base": true
          },
          ...
        ],
        "warnings": []
      }
    """
    from services.sector_comparison import compare_sector, compare_symbols
    body = request.get_json(silent=True) or {}
    sector = body.get('sector')
    symbols = body.get('symbols')
    sector_name = body.get('sector_name', sector or '自定义')
    base_symbol = body.get('base_symbol')

    try:
        if symbols:
            result = compare_symbols(symbols, sector_name, base_symbol)
        elif sector:
            result = compare_sector(sector, base_symbol)
        else:
            return err('body 必须包含 sector 或 symbols 字段', 422)
    except ValueError as exc:
        return err(str(exc), 422)
    return ok(**result.to_dict())


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
        app.logger.exception('monthly_report failed')
        return err(f'月度报告生成失败: {e}', 500)



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
        app.logger.exception('record_monthly_snapshot failed')
        return err(f'月度快照记录失败: {e}', 500)


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
        app.logger.exception('monthly_history failed')
        return err(f'月度历史查询失败: {e}', 500)


# ============================================================
# 回测
# ============================================================

@app.route('/backtest/run', methods=['POST'])
def backtest_run():
    """
    POST /backtest/run

    单标的回测,返回绩效 KPI(不含 equity curve 序列)。

    Body (JSON):
        {
          "symbol": "sh600519",
          "start": "2024-01-01",            // 可选
          "end":   "2024-12-31",            // 可选
          "days":  252,                      // start/end 缺省时用
          "initial_equity":  100000,
          "commission_rate": 0.0003,
          "slippage_bps":    5.0,
          "strategies": [
            {"factor_name": "RSI", "threshold": 1.0, "params": {"window": 14}}
          ]
        }

    Returns:
        {
          "symbol": "sh600519", "n_bars": 120, "n_trades": 8,
          "total_return": 0.12, "annual_return": 0.25, "sharpe": 1.4,
          "max_drawdown_pct": 0.08, "win_rate": 0.62, "profit_factor": 1.7,
          "factor_ic": 0.03, "factor_ir": 0.6, "summary": "..."
        }
    """
    from core.use_cases.backtest import (
        BacktestRequest, StrategySpec, run_backtest,
    )
    from core.use_cases import UseCaseError
    body = request.get_json(silent=True) or {}
    try:
        symbol = body.get('symbol')
        if not symbol:
            return err('symbol is required', 422)
        req = BacktestRequest(
            symbol=str(symbol),
            start=body.get('start'),
            end=body.get('end'),
            days=int(body.get('days', 252)),
            initial_equity=float(body.get('initial_equity', 100_000)),
            commission_rate=float(body.get('commission_rate', 0.0003)),
            slippage_bps=float(body.get('slippage_bps', 5.0)),
            strategies=[
                StrategySpec(
                    factor_name=str(s['factor_name']),
                    threshold=float(s.get('threshold', 1.0)),
                    params=dict(s.get('params', {})),
                )
                for s in body.get('strategies', [])
            ],
        )
    except (KeyError, ValueError, TypeError) as exc:
        return err(f'invalid request: {exc}', 422)
    try:
        response = run_backtest(req)
    except UseCaseError as exc:
        return err(exc.message, 503 if exc.code == 'DATA_UNAVAILABLE' else 422)
    return ok(**response.to_dict())


# ============================================================
# 组合优化
# ============================================================

@app.route('/portfolio/compose', methods=['POST'])
def portfolio_compose():
    """
    POST /portfolio/compose

    基于 universe 的历史日 K 收益,产出建议权重(不下单)。

    Body (JSON):
        {
          "universe":     ["600519.SH", "000858.SZ", "601318.SH"],
          "method":       "min_variance",  // min_variance | max_sharpe |
                                           // risk_parity | max_diversification |
                                           // equal_weight
          "history_days": 252,
          "max_weight":   0.25,
          "min_weight":   0.0,
          "cov_method":   "ledoit_wolf",
          "rf_annual":    0.02
        }

    Returns:
        {
          "method": "min_variance",
          "weights": {"600519.SH": 0.40, ...},
          "n_assets": 3,
          "expected_return": 0.08, "expected_vol": 0.18, "sharpe": 0.33,
          "diagnostics": {"cov_method": "ledoit_wolf", "history_bars": "250", ...}
        }
    """
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest, compose_portfolio,
    )
    from core.use_cases import UseCaseError
    body = request.get_json(silent=True) or {}
    try:
        req = ComposePortfolioRequest(
            universe=list(body.get('universe', [])),
            method=str(body.get('method', 'min_variance')),
            history_days=int(body.get('history_days', 252)),
            max_weight=float(body.get('max_weight', 0.25)),
            min_weight=float(body.get('min_weight', 0.0)),
            cov_method=str(body.get('cov_method', 'ledoit_wolf')),
            rf_annual=float(body.get('rf_annual', 0.02)),
        )
    except (ValueError, TypeError) as exc:
        return err(f'invalid request: {exc}', 422)
    try:
        advice = compose_portfolio(req)
    except UseCaseError as exc:
        return err(exc.message, 503 if exc.code == 'DATA_UNAVAILABLE' else 422)
    return ok(**advice.to_dict())


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

_DATA_NOT_FOUND_MARKERS = (
    '所有数据源均失败',   # FetcherManager 所有 fetcher 都失败
    '未获取到数据',
    '空数据',
    'no data',
)


def _is_symbol_not_found(err_msg: str) -> bool:
    """根据 fetcher 错误消息判断是否属于"无此 symbol",而非内部异常。"""
    s = str(err_msg)
    return any(m in s for m in _DATA_NOT_FOUND_MARKERS)


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
        Multi-source failover: Tencent → Sina → AkShare(熔断器保护)。

    Status:
        200 - 数据正常返回
        404 - 全部 fetcher 都报"无该 symbol 数据",视为标的不存在
        500 - 内部错误(网络全断 / fetcher_manager 加载失败 / 等)
    """
    try:
        from services.fetcher_manager import get_fetcher_manager
        days = int(request.args.get('days', 30))
        days = min(days, 2000)
        start = request.args.get('start') or None
        end = request.args.get('end') or None

        fm = get_fetcher_manager()
        df = fm.get_daily_data(code, start_date=start, end_date=end, days=days)
    except Exception as exc:
        app.logger.exception('data_daily(%s) failed', code)
        if _is_symbol_not_found(exc):
            return err(f'无该标的的行情数据: {code}', 404)
        return err(f'数据获取失败: {exc}', 500)

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
        app.logger.exception('fund_flow failed')
        return err(f'资金流获取失败: {e}', 500)


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
    """GET /risk/status — 风控快照（组合敞口、板块集中度、回撤、Kelly）。"""
    from core.use_cases.risk_snapshot import get_risk_snapshot
    from main import get_monitor
    snap = get_risk_snapshot(get_svc(), monitor=get_monitor())
    return ok(**snap.to_dict())


# ============================================================
# Prometheus 监控指标端点
# ============================================================

@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """GET /metrics — Prometheus 格式监控指标（in-process 刷新，无自调 HTTP）。"""
    try:
        from core.metrics import get_registry
        reg = get_registry()
        reg.refresh_from_service(get_svc())
        return reg.generate(), 200, {'Content-Type': reg.content_type}
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
    """GET /performance/summary?year=2026&month=4&include_chart=1 — 月度绩效聚合。"""
    from core.use_cases.performance_summary import (
        PerformanceSummaryRequest, compute_performance_summary,
    )
    req = PerformanceSummaryRequest(
        year=request.args.get('year', type=int) or 0,
        month=request.args.get('month', type=int) or 0,
        include_chart=request.args.get('include_chart', '1') == '1',
    )
    return ok(**compute_performance_summary(req, get_svc()).to_dict())


# ============================================================
# P1: Macro Data (宏观数据)
# ============================================================

@app.route('/data/macro/<indicator>', methods=['GET'])
def get_macro_data(indicator):
    """
    GET /data/macro/PMI
    GET /data/macro/M2
    GET /data/macro/CREDIT

    返回宏观指标的最新值和日期。
    """
    try:
        macro_ind = MacroIndicator(indicator)
    except ValueError:
        valid = {i.value for i in MacroIndicator}
        return err(f'Unknown macro indicator: {indicator}. Valid: {valid}', 400)

    try:
        from core.data_gateway import get_gateway
        result = get_gateway().macro(macro_ind)
        if result is None or result.empty:
            return err(f'No data for {indicator}', 404)
        latest = result.iloc[-1]
        val_col = result.columns[0]
        return ok(
            indicator=indicator,
            value=float(latest[val_col]) if pd.notna(latest[val_col]) else None,
            date=str(latest.name.date()) if hasattr(latest.name, 'date') else str(latest.name),
            unit='%' if indicator == 'M2' else '点',
        )
    except Exception as exc:
        return err(f'macro data error: {exc}', 500)


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
    """GET /market/status — 当前 A 股是否开盘、当前时段、下次切换时间。"""
    from services.intraday_monitor import is_market_open
    from datetime import datetime, timedelta, time as dtime, date as ddate

    now = datetime.now()
    open_now = is_market_open(now)
    t = now.time()

    if open_now and dtime(9, 30) <= t < dtime(11, 30):
        session, next_change = 'morning', now.replace(hour=11, minute=30, second=0, microsecond=0)
    elif open_now and dtime(13, 0) <= t < dtime(15, 0):
        session, next_change = 'afternoon', now.replace(hour=15, minute=0, second=0, microsecond=0)
    elif open_now:
        session, next_change = 'closed', None
    else:
        session = 'closed'
        days_ahead = 1 if t >= dtime(15, 0) else 0
        next_change = (datetime.combine(ddate.today(), dtime(9, 15))
                       + timedelta(days=days_ahead)).isoformat()

    return ok(is_open=open_now, session=session, next_change=next_change,
              server_time=now.isoformat())


# ============================================================
# P1: Orders with Filters (订单过滤查询) — modifies existing
# ============================================================

# ============================================================
# P4-2: News headlines (供 streamlit / UI 替代 core.factors.nlp 直连)
# ============================================================

@app.route('/data/news/<symbol>', methods=['GET'])
def data_news(symbol):
    """GET /data/news/<symbol>?n=5 — 标的最新新闻标题列表(东方财富)。"""
    n = int(request.args.get('n', 5))
    try:
        from core.factors.nlp import _fetch_news_eastmoney
        headlines = _fetch_news_eastmoney(symbol, n=n) or []
    except Exception as e:
        return err(f'news fetch failed: {e}', 503)
    return ok(symbol=symbol, headlines=headlines, count=len(headlines))


# ============================================================
# P2: LLM Signal Review (独立信号审核)
# ============================================================

def _probe_llm_provider():
    """尝试初始化 LLM provider；不可用返回 None。"""
    try:
        from services.llm.providers import MiniMaxProvider
        provider = MiniMaxProvider()
        provider.chat([{"role": "user", "content": "hi"}], max_tokens=5)
        return provider
    except Exception:
        return None


@app.route('/llm/analyze', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def llm_analyze():
    """POST /llm/analyze — LLM 独立信号审核 (services.llm.service.signal_review 入口)。"""
    if (e := require_json()):
        return e
    body = request.json
    if 'symbol' not in body:
        return err('missing required field: symbol', 422)
    if 'price' not in body:
        return err('missing required field: price', 422)
    # Provide sensible defaults for optional fields the UI may not fill
    body.setdefault('direction', 'UNKNOWN')
    body.setdefault('signal', 'NEUTRAL')
    body.setdefault('alert_reason', '')

    provider = _probe_llm_provider()
    from services.llm.service import signal_review
    result = signal_review(
        symbol=body['symbol'], direction=body['direction'],
        signal=body['signal'], price=float(body['price']),
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
        approved=result.approved, decision=result.decision,
        reason=result.reason, confidence=result.confidence,
        size_rec=result.size_rec, llm_available=(provider is not None),
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

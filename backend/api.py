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

from typing import Optional

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

# Singleton portfolio service — Flask WSGI 多线程下两个并发请求曾各建一个实例，
# 导致 DB 句柄分裂。改用 LockedSingleton 走双检锁。
from core.singleton import LockedSingleton

_svc_singleton: LockedSingleton[PortfolioService] = LockedSingleton(
    PortfolioService, name="api.portfolio_service"
)


def get_svc() -> PortfolioService:
    return _svc_singleton.get()


def reset_svc(instance: Optional[PortfolioService] = None) -> None:
    """重置 PortfolioService 单例（测试用，替代历史上的 ``api._svc = ...`` 直接赋值）。"""
    _svc_singleton.reset(instance)


# R0-1: 订单提交幂等性存储——同 Idempotency-Key 24h 内重试直接回放上次响应。
def _make_idempotency_store():
    from core.idempotency import IdempotencyStore
    return IdempotencyStore()


_idempotency_store_singleton = LockedSingleton(
    _make_idempotency_store, name="api.idempotency_store"
)


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

# R2-4 续集：/positions, /portfolio/positions, /cash, /portfolio/cash,
# /portfolio/summary, /portfolio/daily 6 个端点已拆到
# backend/api_routes/portfolio.py。


# R2-4 续集：/trades, /signals 已拆到 backend/api_routes/trades_signals_params.py


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


def _make_risk_engine():
    from core.risk_engine import RiskEngine
    return RiskEngine()


# Flask 多线程 WSGI 下两个请求并发进入懒建分支会创建两份 RiskEngine，
# 其 __init__ 有副作用(打开 sqlite 句柄、注册回调)，所以必须加锁。
_risk_engine_singleton: LockedSingleton = LockedSingleton(
    _make_risk_engine, name="api.risk_engine"
)


def _get_risk_engine():
    """共享 RiskEngine：优先复用 StrategyRunner 的实例，否则懒建一个本地 singleton。"""
    try:
        from main import get_monitor
        m = get_monitor()
        if m is not None and getattr(m, '_strategy_runner', None) is not None:
            re = getattr(m._strategy_runner, 'risk_engine', None)
            if re is not None:
                return re
    except Exception:
        pass
    try:
        return _risk_engine_singleton.get()
    except Exception:
        # RiskEngine 初始化失败（如配置缺失）维持旧行为：返回 None 让上层决定。
        return None


# R2-4: /orders/* 4 个端点已拆到 backend/api_routes/orders.py（Flask Blueprint）。
# 注册放在文件末尾，确保所有 helper（rate_limit / ok / err / get_svc /
# _get_or_build_broker / _get_risk_engine / _idempotency_store_singleton）
# 都已定义。


# ============================================================
# Symbol params (P1)
# ============================================================

# R2-4 续集：/params/* 3 个端点已拆到
# backend/api_routes/trades_signals_params.py


# ============================================================
# Analysis trigger
# ============================================================

# R2-4 续集：所有 /analysis/* (11 个) 已拆到 backend/api_routes/analysis.py


# ============================================================
# Pipeline 工厂（DynamicWeightPipeline + 全量因子）
# ============================================================

def build_pipeline(symbol: str = ''):
    """构建生产用因子流水线（委托给 core.pipeline_factory）。"""
    from core.pipeline_factory import build_pipeline as _build
    return _build(symbol=symbol)


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


# R2-4 续集：/watchlist/* + /alerts/* 6 个端点已拆到
# backend/api_routes/watchlist_alerts.py


# ============================================================
# Data fetch endpoints (多源兜底路由)
# ============================================================

# R2-4 续集：/data/* 4 个端点已拆到 backend/api_routes/data.py


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

# R2-4 续集：/northbound, /performance, /data/macro, /fundamentals,
# /market/status, /data/news 6 个端点已拆到 backend/api_routes/market.py


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

    from services.walkforward_persistence import get_wfa_history
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

# R2-4: Blueprint 注册。必须放在所有 helper 定义之后，否则 blueprint 模块
# `from backend.api import ...` 拿不到符号。未来新增 blueprint 都在这一段
# 集中注册，方便审计 URL 命名空间冲突。
from backend.api_routes.analysis import analysis_bp  # noqa: E402
from backend.api_routes.data import data_bp  # noqa: E402
from backend.api_routes.market import market_bp  # noqa: E402
from backend.api_routes.orders import orders_bp  # noqa: E402
from backend.api_routes.portfolio import portfolio_bp  # noqa: E402
from backend.api_routes.trades_signals_params import trades_signals_params_bp  # noqa: E402
from backend.api_routes.watchlist_alerts import watchlist_alerts_bp  # noqa: E402
app.register_blueprint(analysis_bp)
app.register_blueprint(data_bp)
app.register_blueprint(market_bp)
app.register_blueprint(orders_bp)
app.register_blueprint(portfolio_bp)
app.register_blueprint(trades_signals_params_bp)
app.register_blueprint(watchlist_alerts_bp)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1', help='Bind host')
    parser.add_argument('--port', type=int, default=5555, help='Bind port')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f"Starting Portfolio API on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)

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
BACKEND_DIR = THIS_DIR
PROJ_ROOT = os.path.dirname(THIS_DIR)
sys.path.insert(0, PROJ_ROOT)

from typing import Optional

from flask import Flask, request, jsonify
from backend.services.portfolio import PortfolioService
from core.data_gateway.capabilities import MacroIndicator

app = Flask(__name__)

# ─── Rate limiting (simple in-memory token bucket) ───────────────────
# R0-2 收尾: Flask WSGI 多线程下，两个并发请求同时读-改-写 _RATE_LIMIT[key]
# 列表会丢失 timestamp 或踩到"dictionary changed size during iteration"。
# 用 _RATE_LIMIT_LOCK 序列化 bucket 维护。
_RATE_LIMIT = {}          # client_key -> [timestamp, ...]
_RATE_LIMIT_LOCK = threading.Lock()
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
            cutoff = now - ws
            with _RATE_LIMIT_LOCK:
                bucket = [t for t in _RATE_LIMIT.get(key, []) if t > cutoff]
                if len(bucket) >= mw:
                    _RATE_LIMIT[key] = bucket
                    return jsonify({
                        'status': 'error',
                        'code': 429,
                        'message': f'Too many requests (max {mw}/{ws}s). Please retry later.',
                    }), 429
                bucket.append(now)
                _RATE_LIMIT[key] = bucket
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
_GLOBAL_RATE_LIMIT_LOCK = threading.Lock()  # R0-2 收尾：同 _RATE_LIMIT


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
        with _GLOBAL_RATE_LIMIT_LOCK:
            bucket = [t for t in _GLOBAL_RATE_LIMIT.get(key, []) if t > cutoff]
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


# R0-1 / R2-4 review-fix: broker / risk_engine / idempotency-store 三个
# getter 现集中在 backend/api_deps.py。重新导出以保持向后兼容（已有测试
# 用 patch.object(backend.api, '_get_or_build_broker', ...) 的方式 mock）。
from backend.api_deps import (
    _get_or_build_broker,
    _get_risk_engine,
    _idempotency_store_singleton,
    _risk_engine_singleton,
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

# R2-4: /orders/* 4 个端点已拆到 backend/api_routes/orders.py（Flask Blueprint）。
# 注册放在文件末尾,确保所有 helper(rate_limit / ok / err / get_svc /
# api_deps._get_or_build_broker / _get_risk_engine / _idempotency_store_singleton)
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

# R2-4 续集：/backtest, /portfolio/compose, /wfa/* 已拆到
# backend/api_routes/research.py


# R2-4 续集：/watchlist/* + /alerts/* 6 个端点已拆到
# backend/api_routes/watchlist_alerts.py


# ============================================================
# Data fetch endpoints (多源兜底路由)
# ============================================================

# R2-4 续集：/data/* 4 个端点已拆到 backend/api_routes/data.py


# R2-4 续集：/trading/mode, /monitor, /risk, /metrics, /llm/analyze 已拆到
# backend/api_routes/ops.py


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
from backend.api_routes.ops import ops_bp  # noqa: E402
from backend.api_routes.orders import orders_bp  # noqa: E402
from backend.api_routes.portfolio import portfolio_bp  # noqa: E402
from backend.api_routes.research import research_bp  # noqa: E402
from backend.api_routes.trades_signals_params import trades_signals_params_bp  # noqa: E402
from backend.api_routes.watchlist_alerts import watchlist_alerts_bp  # noqa: E402
from backend.api_routes.test import test_bp  # noqa: E402
app.register_blueprint(analysis_bp)
app.register_blueprint(data_bp)
app.register_blueprint(market_bp)
app.register_blueprint(ops_bp)
app.register_blueprint(orders_bp)
app.register_blueprint(portfolio_bp)
app.register_blueprint(research_bp)
app.register_blueprint(trades_signals_params_bp)
app.register_blueprint(watchlist_alerts_bp)
app.register_blueprint(test_bp)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1', help='Bind host')
    parser.add_argument('--port', type=int, default=5555, help='Bind port')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f"Starting Portfolio API on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)

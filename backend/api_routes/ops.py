"""``/trading/mode`` / ``/monitor/status`` / ``/risk/status`` / ``/metrics`` /
``/llm/analyze`` HTTP routes — 运维 + 监控类端点。

R2-4 续集: 6 个 ops/monitoring 端点 (3 系统 + 2 trading mode toggle + 1 LLM 审核)。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from flask import Blueprint, request

from backend.api import (
    BACKEND_DIR,
    err,
    get_svc,
    ok,
    rate_limit,
    require_json,
)

ops_bp = Blueprint('ops', __name__)


# ─── Trading mode toggle ───────────────────────────────────────────────────

_MODE_FILE = os.path.join(BACKEND_DIR, 'trading_mode.json')
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


@ops_bp.route('/trading/mode', methods=['GET'])
def get_trading_mode():
    """GET /trading/mode — current trading mode (simulation | live)."""
    return ok(mode=_load_trading_mode())


@ops_bp.route('/trading/mode', methods=['PUT'])
def set_trading_mode():
    """PUT /trading/mode — Body: {"mode": "simulation"|"live"}"""
    if (e := require_json()):
        return e
    body = request.json or {}
    mode = body.get('mode', '')
    if mode not in _VALID_MODES:
        return err(f'invalid mode "{mode}", must be one of: {sorted(_VALID_MODES)}', 422)
    _save_trading_mode(mode)
    return ok(mode=mode, message=f'Trading mode set to {mode}')


# ─── Monitor / Risk / Metrics ──────────────────────────────────────────────


@ops_bp.route('/monitor/status', methods=['GET'])
def monitor_status():
    """GET /monitor/status — IntradayMonitor 实时运行状态。"""
    from main import get_monitor
    monitor = get_monitor()
    if monitor is None:
        return err('Monitor not initialized', 503)
    return ok(monitor.get_status())


@ops_bp.route('/risk/status', methods=['GET'])
def risk_status():
    """GET /risk/status — 风控快照（组合敞口、板块集中度、回撤、Kelly）。"""
    from main import get_monitor
    from core.use_cases.risk_snapshot import get_risk_snapshot
    snap = get_risk_snapshot(get_svc(), monitor=get_monitor())
    return ok(**snap.to_dict())


@ops_bp.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """GET /metrics — Prometheus 格式监控指标（in-process 刷新）。"""
    try:
        from core.metrics import get_registry
        reg = get_registry()
        reg.refresh_from_service(get_svc())
        return reg.generate(), 200, {'Content-Type': reg.content_type}
    except Exception as e:  # noqa: BLE001 — Prometheus 抓取端点，永远不能 500 阻塞抓取
        return f'# metrics error: {e}\n', 500, {'Content-Type': 'text/plain'}


# ─── LLM signal review ────────────────────────────────────────────────────


def _probe_llm_provider():
    """尝试初始化 LLM provider；不可用返回 None。"""
    try:
        from services.llm.providers import MiniMaxProvider
        provider = MiniMaxProvider()
        provider.chat([{"role": "user", "content": "hi"}], max_tokens=5)
        return provider
    except Exception:  # noqa: BLE001 — provider 任意子层异常都视作"不可用"
        return None


@ops_bp.route('/llm/analyze', methods=['POST'])
@rate_limit(max_per_window=10, window_seconds=60)
def llm_analyze():
    """POST /llm/analyze — LLM 独立信号审核 (services.llm.service.signal_review)。"""
    if (e := require_json()):
        return e
    body = request.json
    if 'symbol' not in body:
        return err('missing required field: symbol', 422)
    if 'price' not in body:
        return err('missing required field: price', 422)
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

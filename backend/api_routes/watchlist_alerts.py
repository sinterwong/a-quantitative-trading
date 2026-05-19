"""``/watchlist/*`` 和 ``/alerts/*`` HTTP routes.

R2-4 续集: 6 个端点 (4 watchlist + 2 alerts) 合并到单个 Blueprint。
两组都是 services.* 上的 CRUD，没有 PortfolioService 依赖，逻辑独立。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend.api import err, ok

watchlist_alerts_bp = Blueprint('watchlist_alerts', __name__)


# ─── Watchlist ─────────────────────────────────────────────────────────────


@watchlist_alerts_bp.route('/watchlist', methods=['GET'])
def get_watchlist():
    """GET /watchlist — 返回当前自选股列表。"""
    from services.watchlist import get_watchlist_all
    items = get_watchlist_all()
    return ok(watchlist=items, count=len(items))


@watchlist_alerts_bp.route('/watchlist/add', methods=['POST'])
def add_watchlist():
    """POST /watchlist/add

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


@watchlist_alerts_bp.route('/watchlist/<symbol>', methods=['DELETE'])
def remove_watchlist(symbol: str):
    """DELETE /watchlist/<symbol> — 移除自选股（软删除）。"""
    from services.watchlist import remove_from_watchlist
    if remove_from_watchlist(symbol):
        return ok(message=f'{symbol} removed from watchlist')
    return err(f'{symbol} not found in watchlist', 404)


@watchlist_alerts_bp.route('/watchlist/<symbol>', methods=['PATCH'])
def patch_watchlist(symbol: str):
    """PATCH /watchlist/<symbol>

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


# ─── Alert history ─────────────────────────────────────────────────────────


@watchlist_alerts_bp.route('/alerts/history', methods=['GET'])
def alerts_history():
    """GET /alerts/history

    Query params:
        limit       — max rows (default 50)
        type        — filter by type: INDEX/POSITION/WATCHLIST/SECTOR_FLOW
        since_hours — only last N hours (e.g. 24)
        symbol      — filter by symbol (e.g. SH000001)
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


@watchlist_alerts_bp.route('/alerts/clear', methods=['POST'])
def clear_alerts():
    """POST /alerts/clear

    Body: {"days": 7}  — 删除 N 天之前的预警（默认 7 天）
    """
    body = request.json or {} if request.is_json else {}
    from services.alert_history import clear_old_alerts
    days = int(body.get('days', 7))
    cleared = clear_old_alerts(days)
    return ok(message=f'Cleared {cleared} alerts older than {days} days')

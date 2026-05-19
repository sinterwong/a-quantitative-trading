"""``/northbound`` / ``/performance`` / ``/fundamentals`` / ``/market`` /
``/data/macro`` / ``/data/news`` HTTP routes.

R2-4 续集: market-data 类的查询端点 (6 个)，统一拆到 market blueprint。
"""

from __future__ import annotations

from datetime import date as ddate, datetime, time as dtime, timedelta

import pandas as pd
from flask import Blueprint, request

from backend.api import err, get_svc, ok
from core.data_gateway.capabilities import MacroIndicator

market_bp = Blueprint('market', __name__)


@market_bp.route('/northbound/flow', methods=['GET'])
def northbound_flow():
    """GET /northbound/flow?refresh=1 — 沪深港通北向资金实时流量。

    refresh=1 跳过 60s cache 强制重拉。
    """
    refresh = request.args.get('refresh', '0') == '1'
    from services.data_cache import cached_kamt
    from services.northbound import (
        fetch_kamt,
        format_kamt_summary,
        get_north_flow_direction,
        get_north_history,
    )

    kamt = cached_kamt(force_refresh=refresh) if refresh else fetch_kamt()
    if not kamt:
        return err('Failed to fetch northbound data', 502)

    direction = get_north_flow_direction()
    net_yi = kamt.get('net_north_cny', 0) / 1e8
    summary_text = format_kamt_summary(kamt)
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


@market_bp.route('/performance/summary', methods=['GET'])
def performance_summary():
    """GET /performance/summary?year=2026&month=4&include_chart=1
    — 月度绩效聚合 (use case)。"""
    from core.use_cases.performance_summary import (
        PerformanceSummaryRequest,
        compute_performance_summary,
    )
    req = PerformanceSummaryRequest(
        year=request.args.get('year', type=int) or 0,
        month=request.args.get('month', type=int) or 0,
        include_chart=request.args.get('include_chart', '1') == '1',
    )
    return ok(**compute_performance_summary(req, get_svc()).to_dict())


@market_bp.route('/data/macro/<indicator>', methods=['GET'])
def get_macro_data(indicator: str):
    """GET /data/macro/{PMI|M2|CREDIT} — 宏观指标最新值。"""
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
    except Exception as exc:  # noqa: BLE001 — HTTP transport boundary
        return err(f'macro data error: {exc}', 500)


@market_bp.route('/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol: str):
    """GET /fundamentals/<symbol> — PE / PB / 股息率 / 总市值 等。"""
    from services.fundamentals import fetch_fundamentals
    data = fetch_fundamentals(symbol)
    if data is None:
        return err(f'Fundamentals unavailable for {symbol}', 404)
    return ok(**data)


@market_bp.route('/market/status', methods=['GET'])
def market_status():
    """GET /market/status — A 股是否开盘、当前时段、下次切换时间。"""
    from services.intraday_monitor import is_market_open

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


@market_bp.route('/data/news/<symbol>', methods=['GET'])
def data_news(symbol: str):
    """GET /data/news/<symbol>?n=5 — 最新新闻标题（东方财富）。"""
    n = int(request.args.get('n', 5))
    try:
        from core.factors.nlp import _fetch_news_eastmoney
        headlines = _fetch_news_eastmoney(symbol, n=n) or []
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        return err(f'news fetch failed: {e}', 503)
    return ok(symbol=symbol, headlines=headlines, count=len(headlines))

"""``/backtest`` / ``/portfolio/compose`` / ``/wfa/*`` HTTP routes.

R2-4 续集: 4 个研究类端点 (回测 + 组合优化 + WFA 历史)。
都是 use_case / services 触发器，HTTP transport 层薄壳。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend.api import err, ok

research_bp = Blueprint('research', __name__)


@research_bp.route('/backtest/run', methods=['POST'])
def backtest_run():
    """POST /backtest/run — 单标的回测，返回绩效 KPI（不含 equity 序列）。

    Body: {"symbol", "start"?, "end"?, "days"?, "initial_equity"?,
    "commission_rate"?, "slippage_bps"?, "strategies": [{"factor_name", ...}]}
    """
    from core.use_cases import UseCaseError
    from core.use_cases.backtest import (
        BacktestRequest,
        StrategySpec,
        run_backtest,
    )
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


@research_bp.route('/portfolio/compose', methods=['POST'])
def portfolio_compose():
    """POST /portfolio/compose — 基于 universe 收益的组合权重建议（不下单）。

    Body: {"universe": [...], "method": "min_variance|max_sharpe|risk_parity|
    max_diversification|equal_weight", "history_days"?, "max_weight"?,
    "min_weight"?, "cov_method"?, "rf_annual"?}
    """
    from core.use_cases import UseCaseError
    from core.use_cases.compose_portfolio import (
        ComposePortfolioRequest,
        compose_portfolio,
    )
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


@research_bp.route('/wfa/history', methods=['GET'])
def wfa_history():
    """GET /wfa/history?symbol=600036.SH&strategy=RSI&limit=30 — WFA 历史。"""
    symbol = request.args.get('symbol')
    strategy = request.args.get('strategy')
    limit = int(request.args.get('limit', 30))

    from services.walkforward_persistence import get_wfa_history
    try:
        records = get_wfa_history(symbol=symbol, strategy=strategy, limit=limit)
        return ok(records=records, count=len(records))
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        return err(str(e), 500)


@research_bp.route('/wfa/summary', methods=['GET'])
def wfa_summary():
    """GET /wfa/summary?symbol=600036.SH — 最新 RSI / ATR WFA 结果。"""
    symbol = request.args.get('symbol')
    if not symbol:
        return err('symbol is required', 422)

    from services.wfa_history import get_latest_wfa
    rsi_result = get_latest_wfa(symbol, 'RSI')
    atr_result = get_latest_wfa(symbol, 'ATR')
    return ok(symbol=symbol, rsi=rsi_result, atr=atr_result)

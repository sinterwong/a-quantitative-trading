"""``/analysis/*`` HTTP routes.

R2-4 续集: 11 个 analysis use_case 触发器端点。绝大多数都是 thin wrapper:
解析 JSON body → 构造 *Request dataclass → 调 use case → 返回 .to_dict()。
"""

from __future__ import annotations

import json
import os
from datetime import date

from flask import Blueprint, request

from backend.api import BACKEND_DIR, app, err, get_svc, ok

analysis_bp = Blueprint('analysis', __name__)


# ─── 每日分析 / 健康 / 状态 ─────────────────────────────────────────────────


@analysis_bp.route('/analysis/run', methods=['POST'])
def run_analysis():
    """POST /analysis/run — 触发每日分析 (use case: daily_analysis)。"""
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis
    response = run_daily_analysis(
        DailyAnalysisRequest(output_dir=os.path.join(BACKEND_DIR, 'outputs', 'analysis')),
        portfolio_svc=get_svc(),
    )
    return ok(**response.to_dict())


@analysis_bp.route('/analysis/health', methods=['GET'])
def analysis_health():
    """GET /analysis/health — 系统健康状态 (use case: system_health)。"""
    from core.use_cases.system_health import compute_system_health
    report = compute_system_health(
        get_svc(),
        analysis_dir=os.path.join(BACKEND_DIR, 'outputs', 'analysis'),
    )
    return ok(**report.to_dict())


@analysis_bp.route('/analysis/status', methods=['GET'])
def analysis_status():
    """GET /analysis/status — 最近一次每日分析: daily_meta + 持久化 JSON 内容。"""
    svc = get_svc()
    payload: dict = {}

    metas = svc.get_daily_metas(limit=1)
    if metas:
        payload.update(metas[0])

    analysis_dir = os.path.join(BACKEND_DIR, 'outputs', 'analysis')
    try:
        files = sorted(
            f for f in os.listdir(analysis_dir)
            if f.startswith('analysis_') and f.endswith('.json')
        )
    except FileNotFoundError:
        files = []
    if files:
        latest = os.path.join(analysis_dir, files[-1])
        try:
            with open(latest, encoding='utf-8') as f:
                content = json.load(f)
            for k in ('timestamp', 'sources', 'top_sectors', 'news_summary',
                      'selected_stocks', 'warnings'):
                if k in content:
                    payload[k] = content[k]
            payload['source_file'] = os.path.basename(latest)
        except (OSError, ValueError):
            pass

    if not payload:
        return ok(message="No analysis run yet")
    return ok(**payload)


# ─── 行业轮动 ──────────────────────────────────────────────────────────────


@analysis_bp.route('/analysis/sector_rotation', methods=['POST'])
def sector_rotation_signal():
    """POST /analysis/sector_rotation — 基于动量的行业 ETF 轮动。

    Body (JSON, 可选): top_n / lookback_days / rebalance_days /
    momentum_method(return|sharpe) / current_holdings"""
    from core.use_cases import UseCaseError
    from core.use_cases.sector_rotation_signal import (
        SectorRotationRequest,
        run_sector_rotation,
    )
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


# ─── 配对交易 ──────────────────────────────────────────────────────────────


@analysis_bp.route('/analysis/pairs_trading', methods=['POST'])
def pairs_trading_signal():
    """POST /analysis/pairs_trading — 协整配对筛选 + 当前信号。

    Body: symbols[] / entry_z / exit_z / stop_z / lookback_days / screen_days"""
    from core.use_cases import UseCaseError
    from core.use_cases.pairs_trading_signal import (
        PairsTradingRequest,
        find_pairs_signals,
    )
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


# ─── 单股综合分析 ──────────────────────────────────────────────────────────


@analysis_bp.route('/analysis/stock/a', methods=['POST'])
def analyze_a_stock_endpoint():
    """POST /analysis/stock/a — A 股综合分析（行情 + 因子 + 风险 + 可选 ML/NLP/LLM）。

    Body: {"symbol": "603369.SH", "lookback_days": 250, "include_regime/news/ml/llm": bool}"""
    from services.single_stock_analysis import (
        AnalysisRequest,
        analyze_a_share,
        detect_market,
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


@analysis_bp.route('/analysis/stock/hk', methods=['POST'])
def analyze_hk_stock_endpoint():
    """POST /analysis/stock/hk — 港股综合分析（行情 + 因子 + 风险 + 可选 LLM）。

    Body: {"symbol": "HK:00700", ...} — 支持 HK:N / N.HK / hkN"""
    from services.single_stock_analysis import (
        AnalysisRequest,
        analyze_hk_share,
        detect_market,
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


# ─── 行业横向对比 ──────────────────────────────────────────────────────────


@analysis_bp.route('/analysis/sector/compare', methods=['POST'])
def sector_compare():
    """POST /analysis/sector/compare — 行业板块横向对比。

    Body (两种模式):
      行业模式:   {"sector": "白酒", "base_symbol": "603369.SH"}
      自定义模式: {"symbols": [...], "sector_name": "白酒", "base_symbol": ...}
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


# ─── 月度绩效（与 /performance/summary 不同，这一组在 services.performance） ─


@analysis_bp.route('/analysis/monthly', methods=['GET'])
def monthly_performance():
    """GET /analysis/monthly?year=2026&month=4&include_chart=1 — 月度报告。"""
    try:
        from services.performance import generate_monthly_report
        year = int(request.args.get('year', date.today().year))
        month = int(request.args.get('month', date.today().month))
        include_chart = bool(int(request.args.get('include_chart', 1)))
        report = generate_monthly_report(
            year=year, month=month, include_chart=include_chart,
        )
        return ok(**report)
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        app.logger.exception('monthly_report failed')
        return err(f'月度报告生成失败: {e}', 500)


@analysis_bp.route('/analysis/monthly/snapshot', methods=['POST'])
def record_monthly_snapshot():
    """POST /analysis/monthly/snapshot — 写入月度快照 (Cron 月末调用)。"""
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
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        app.logger.exception('record_monthly_snapshot failed')
        return err(f'月度快照记录失败: {e}', 500)


@analysis_bp.route('/analysis/monthly/history', methods=['GET'])
def monthly_history():
    """GET /analysis/monthly/history?limit=12 — 历史月度快照。"""
    try:
        from services.performance import get_monthly_snapshots
        limit = int(request.args.get('limit', 12))
        snapshots = get_monthly_snapshots(limit=limit)
        return ok(snapshots=snapshots, count=len(snapshots))
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        app.logger.exception('monthly_history failed')
        return err(f'月度历史查询失败: {e}', 500)

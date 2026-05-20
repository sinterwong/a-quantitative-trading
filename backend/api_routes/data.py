"""``/data/*`` HTTP routes.

R2-4 续集: 4 个数据查询端点 (daily / status / fund_flow / realtime)。
都调 ``services.fetcher_manager`` / ``services.fund_flow`` / ``services.signals``
做多源 failover 取数，无写入语义。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend.api import app, err, ok

data_bp = Blueprint('data', __name__)


# 与 backend/api.py 同步的 not-found 标记串（fetcher 报错 msg 含这些 → 404）
_DATA_NOT_FOUND_MARKERS = (
    '所有数据源均失败',
    '未获取到数据',
    '空数据',
    'no data',
)


def _is_symbol_not_found(err_msg: object) -> bool:
    s = str(err_msg)
    return any(m in s for m in _DATA_NOT_FOUND_MARKERS)


@data_bp.route('/data/daily/<code>', methods=['GET'])
def data_daily(code: str):
    """GET /data/daily/<code>?days=30&start=YYYY-MM-DD&end=YYYY-MM-DD

    Multi-source failover: Tencent → Sina → AkShare (熔断器保护)。
    返回字段: OHLCV + MA5/MA10/MA20/volume_ratio。

    Status:
        200 — 数据正常
        404 — 全部 fetcher 报"无该 symbol" → 标的不存在
        500 — 内部错误（网络全断 / fetcher_manager 加载失败）
    """
    try:
        from services.fetcher_manager import get_fetcher_manager
        days = min(int(request.args.get('days', 30)), 2000)
        start = request.args.get('start') or None
        end = request.args.get('end') or None

        fm = get_fetcher_manager()
        df = fm.get_daily_data(code, start_date=start, end_date=end, days=days)
    except Exception as exc:  # noqa: BLE001 — HTTP transport boundary
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


@data_bp.route('/data/status', methods=['GET'])
def data_status():
    """GET /data/status — registered fetchers + circuit breaker state."""
    from services.fetcher_manager import get_fetcher_manager
    fm = get_fetcher_manager()
    return ok(
        fetchers=[f.name for f in fm.fetchers],
        status=fm.get_fetcher_status(),
    )


@data_bp.route('/data/fund_flow', methods=['GET'])
def data_fund_flow():
    """GET /data/fund_flow?source=market|top|<code>&period=5日排行&top=20

    source=market: 大盘资金流汇总（sh/sz 收盘 + 主力净流入）
    source=top:    资金流入 TOP 排名
    source=<code>: 个股资金流摘要
    """
    source = request.args.get('source', 'market')
    period = request.args.get('period', '5日排行')
    top_n = int(request.args.get('top', 20))

    try:
        from services.fund_flow import FundFlowService
        svc = FundFlowService()
        if source == 'market':
            return ok(type='market', **svc.get_market_fund_flow())
        if source == 'top':
            tops = svc.get_top_fund_flow_stocks(period=period, top_n=top_n)
            return ok(
                type='top', period=period, count=len(tops),
                stocks=[t.to_dict() for t in tops],
            )
        summary = svc.get_main_net_summary(source, period=period)
        return ok(type='stock', source=source, **summary)
    except ImportError:
        return err('FundFlowService not available (AkShare missing)', 500)
    except Exception as e:  # noqa: BLE001 — HTTP transport boundary
        app.logger.exception('fund_flow failed')
        return err(f'资金流获取失败: {e}', 500)


@data_bp.route('/data/realtime/<symbol>', methods=['GET'])
def data_realtime(symbol: str):
    """GET /data/realtime/<symbol> — 轻量实时行情。"""
    from services.signals import fetch_realtime
    quote = fetch_realtime(symbol)
    if not quote:
        return err(f'Realtime data unavailable for {symbol}', 502)
    return ok(symbol=symbol, quote=quote)

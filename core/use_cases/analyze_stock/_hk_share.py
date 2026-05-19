"""
analyze_stock._hk_share — 港股综合分析(更受限:无 PE / 无 Regime / 仅技术因子)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Type, cast

from core.factors.base import Factor

from ._deps import resolve_gateway
from ._llm_summary import try_llm_summary
from ._recommend import make_recommendation
from ._risk_metrics import compute_risk_metrics
from ._symbols import normalize_hk_symbol
from ._types import AnalysisReport, AnalysisRequest
from ._utils import safe_float

logger = logging.getLogger('core.use_cases.analyze_stock')


def analyze_hk_share(req: AnalysisRequest) -> AnalysisReport:
    sym = normalize_hk_symbol(req.symbol)
    report = AnalysisReport(
        symbol=sym, market='HK',
        as_of=datetime.now().isoformat(timespec='seconds'),
    )

    # 1) 港股实时快照(QuoteSourceManager 路由:腾讯主 → 新浪备)
    snap = None
    try:
        mgr = resolve_gateway(req)
        q = mgr.quote(sym)
        if q is not None and q.price > 0:
            snap = q
            report.snapshot = {
                'name': q.name,
                'last': q.price,
                'open': q.open,
                'high': q.high,
                'low': q.low,
                'prev_close': q.prev_close,
                'change': q.change,
                'change_pct': q.pct_change,
                'volume': q.volume,
                'amount': q.amount,
                'high_52w': q.high_52w,
                'low_52w': q.low_52w,
                'mkt_cap': q.market_cap,
                'pe_ttm': q.pe_ttm or None,
                'pb': q.pb or None,
                'turnover_rate': q.turnover_rate or None,
                'float_cap': q.float_cap or None,
                'volume_ratio': q.volume_ratio or None,
                'currency': q.currency or 'HKD',
            }
        else:
            report.warnings.append('hk_snapshot_unavailable')
    except Exception as exc:
        report.warnings.append(f'hk_data_error: {exc}')

    # 2) 历史日K(data_gateway 自动路由 + failover)
    df = None
    try:
        df = resolve_gateway(req).kline(
            sym, interval="daily",
            days=max(req.lookback_days, 60), adjust='qfq',
        )
        if df is None or df.empty:
            report.warnings.append('hk_history_unavailable')
    except Exception as exc:
        report.warnings.append(f'hk_history_error: {exc}')

    current_price = (snap.price if snap else 0.0) or 0.0

    # 3) 基本面(营收/净利/ROE/Akshare 港股财报)
    try:
        fund = resolve_gateway(req).fundamentals(sym)

        # 区分"有 YoY 增长数据"和"只有绝对值"两种情况
        has_yoy_data = (
            (fund.revenue_yoy or 0) != 0
            or (fund.profit_yoy or 0) != 0
        )
        has_absolute_data = (
            fund.eps_ttm > 0 or fund.roe_ttm > 0
            or fund.pe_ttm > 0 or fund.pb > 0
        )

        if fund and (has_yoy_data or has_absolute_data):
            note = 'PE/PB 来自 AkShare 财报(腾讯 88 字段已在 snapshot 中)'
            if has_yoy_data and not has_absolute_data:
                note += ' | 仅 YoY 数据,绝对估值指标为空'
            elif has_absolute_data and not has_yoy_data:
                note += ' | 仅绝对估值指标,YoY 增长数据为空'

            report.fundamentals = {
                'pe_ttm': fund.pe_ttm or safe_float(None),
                'pb': fund.pb or safe_float(None),
                'roe_ttm': fund.roe_ttm or safe_float(None),
                'eps_ttm': fund.eps_ttm or safe_float(None),
                'revenue_yoy': fund.revenue_yoy or safe_float(None),
                'profit_yoy': fund.profit_yoy or safe_float(None),
                'dividend_yield': fund.dividend_yield or safe_float(None),
                'as_of_date': str(fund.timestamp.date()) if fund.timestamp else None,
                'note': note,
            }
        elif fund:
            report.warnings.append('hk_fundamentals_limited: only_zero_metrics available')
        else:
            report.warnings.append('hk_fundamentals_unavailable')
    except Exception as exc:
        report.warnings.append(f'hk_fundamentals_error: {exc}')

    # 4) 技术因子(仅技术层,HK 无 Regime / 宏观)
    if df is not None and not df.empty:
        try:
            from core.factor_pipeline import FactorPipeline
            from core.factors.price_momentum import (
                RSIFactor, ATRFactor, BollingerFactor,
            )
            from core.strategies.macd_trend import MACDTrendFactor

            pipeline = FactorPipeline(min_bars=20)
            sym_param = {'symbol': sym}
            for cls, w in [
                (RSIFactor, 0.30),
                (MACDTrendFactor, 0.30),
                (BollingerFactor, 0.20),
                (ATRFactor, 0.20),
            ]:
                try:
                    # mypy 对 ABCMeta 子类的 type[Factor] 推断不完美，cast 让契约清晰
                    pipeline.add(cast(Type[Factor], cls), weight=w, params=sym_param)
                except Exception as exc:  # noqa: BLE001 — 单因子失败不阻断 pipeline
                    logger.debug('HK pipeline.add %s failed: %s', cls.__name__, exc)

            pr = pipeline.run(symbol=sym, data=df, price=current_price)
            report.factor_pipeline = {
                'combined_score': pr.combined_score,
                'dominant_signal': pr.dominant_signal,
                'buy_strength': round(pr.buy_strength, 4),
                'sell_strength': round(pr.sell_strength, 4),
                'factors_ok': pr.metadata.get('factors_ok', 0),
                'factors_total': pr.metadata.get('factors_total', 0),
                'breakdown': [
                    {
                        'name': fr.name,
                        'score': round(fr.latest_value or 0.0, 4),
                        'signals': [
                            {'direction': s.direction, 'strength': round(s.strength, 4),
                             'reason': getattr(s, 'reason', '')}
                            for s in (fr.signals or [])
                        ],
                        'error': fr.error,
                    }
                    for fr in pr.factor_results
                ],
                'errors': pr.errors(),
            }
        except Exception as exc:
            report.warnings.append(f'pipeline_error: {exc}')
    else:
        report.warnings.append('pipeline_skipped: no_history')

    # 5) 风险指标
    if df is not None and not df.empty and current_price > 0:
        report.risk = compute_risk_metrics(df, current_price)
    elif snap is not None and snap.high_52w and snap.low_52w:
        # 历史不可用时退化使用 52 周高低估算波幅
        spread = snap.high_52w - snap.low_52w
        if spread > 0 and current_price > 0:
            report.risk = {
                'note': 'estimated_from_52w_range',
                'range_52w_pct': round(spread / current_price * 100, 3),
                'distance_to_52w_low_pct': round(
                    (current_price - snap.low_52w) / current_price * 100, 3),
                'distance_to_52w_high_pct': round(
                    (snap.high_52w - current_price) / current_price * 100, 3),
            }

    # 港股没有 A 股 Regime 概念
    if req.include_regime:
        report.regime = {
            'regime': 'N/A',
            'note': '港股市场未接入 Regime 检测;A 股 SH000001 上下文不适用',
        }

    # ML / 新闻:港股暂不支持
    if req.include_ml:
        report.ml_prediction = {
            'available': False,
            'reason': 'no_hk_ml_model_registered',
        }
    if req.include_news:
        report.news_sentiment = {
            'available': False,
            'reason': 'nlp_news_factor_a_share_only',
        }

    # LLM 解读对港股仍可用(基于已有快照 + 因子)
    if req.include_llm:
        report.llm_summary = try_llm_summary(report, llm_provider=req.llm_provider)

    # 投资建议(无 regime 时只用 combined_score + 风险)
    report.recommendation = make_recommendation(
        combined_score=report.factor_pipeline.get('combined_score', 0.0),
        dominant=report.factor_pipeline.get('dominant_signal', 'HOLD'),
        regime=None,
        fundamentals=report.fundamentals,
        risk=report.risk,
    )

    report.data_quality = {
        'bars_used': int(len(df)) if df is not None else 0,
        'factors_failed': list(report.factor_pipeline.get('errors', {}).keys())
        if report.factor_pipeline else [],
        'has_snapshot': bool(report.snapshot),
        'has_history': df is not None and not df.empty,
    }
    return report

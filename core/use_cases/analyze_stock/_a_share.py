"""
analyze_stock._a_share — A 股综合分析(全量因子 + 基本面 + Regime + 风险)。
"""

from __future__ import annotations

import logging
from datetime import datetime

from ._deps import resolve_data_layer, resolve_gateway
from ._llm_summary import try_llm_summary
from ._ml import try_ml_prediction
from ._news import try_news_sentiment
from ._recommend import make_recommendation
from ._risk_metrics import compute_risk_metrics
from ._symbols import normalize_a_share_symbol
from ._types import AnalysisReport, AnalysisRequest
from ._utils import safe_float

logger = logging.getLogger('core.use_cases.analyze_stock')


def analyze_a_share(req: AnalysisRequest) -> AnalysisReport:
    """A 股综合分析(全量因子 + 基本面 + Regime + 风险)。"""
    sym = normalize_a_share_symbol(req.symbol)
    report = AnalysisReport(
        symbol=sym, market='A',
        as_of=datetime.now().isoformat(timespec='seconds'),
    )

    # 1) 行情数据
    df = None
    try:
        dl = resolve_data_layer(req)
        df = dl.get_bars(sym, days=max(req.lookback_days, 60))
        if df is None or df.empty:
            report.warnings.append('bars_unavailable')
        else:
            last = df.iloc[-1]
            report.snapshot = {
                'last': float(last['close']),
                'open': float(last['open']),
                'high': float(last['high']),
                'low': float(last['low']),
                'volume': int(last['volume']),
                'bar_date': str(last.get('date', '')) if hasattr(last, 'get') else '',
                'bars_returned': int(len(df)),
            }
            # 实时报价(涨跌幅 / 量比,会比日 K 新)
            try:
                quote = dl.get_realtime(sym)
                if quote is not None:
                    report.snapshot.update({
                        'realtime_price': float(getattr(quote, 'price', 0.0) or 0.0),
                        'pct_change': float(getattr(quote, 'pct_change', 0.0) or 0.0),
                        'vol_ratio': getattr(quote, 'vol_ratio', None),
                        # ── 腾讯 88 扩展字段 ──
                        'pe_ttm': getattr(quote, 'pe_ttm', None),
                        'pb': getattr(quote, 'pb', None),
                        'turnover_rate': getattr(quote, 'turnover_rate', None),
                        'market_cap': getattr(quote, 'market_cap', None),
                        'float_cap': getattr(quote, 'float_cap', None),
                        'high_52w': getattr(quote, 'high_52w', None),
                        'low_52w': getattr(quote, 'low_52w', None),
                        'limit_up': getattr(quote, 'limit_up', None),
                        'limit_down': getattr(quote, 'limit_down', None),
                    })
            except Exception as exc:
                logger.debug('realtime quote failed: %s', exc)
    except Exception as exc:
        report.warnings.append(f'data_layer_error: {exc}')

    if df is None or df.empty:
        # 数据完全不可用 → 提前返回最小化报告
        report.recommendation = {
            'action': 'HOLD', 'confidence': 0.0,
            'reasoning': '行情数据不可用,无法分析',
        }
        return report

    current_price = report.snapshot.get('realtime_price') or report.snapshot.get('last') or 0.0

    # 2) 因子流水线
    try:
        from core.pipeline_factory import build_pipeline
        # strict=False → 允许部分因子缺失,靠 PipelineResult.factors_ok 判断
        pipeline = build_pipeline(symbol=sym, strict=False)
        pipeline_result = pipeline.run(symbol=sym, data=df, price=current_price)

        report.factor_pipeline = {
            'combined_score': pipeline_result.combined_score,
            'dominant_signal': pipeline_result.dominant_signal,
            'buy_strength': round(pipeline_result.buy_strength, 4),
            'sell_strength': round(pipeline_result.sell_strength, 4),
            'factors_ok': pipeline_result.metadata.get('factors_ok', 0),
            'factors_total': pipeline_result.metadata.get('factors_total', 0),
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
                for fr in pipeline_result.factor_results
            ],
            'errors': pipeline_result.errors(),
        }
    except Exception as exc:
        report.warnings.append(f'pipeline_error: {exc}')

    # 3) 基本面(统一走 gateway,内部包含腾讯实时 PE/PB 补充)
    try:
        fund = resolve_gateway(req).fundamentals(sym)
        if fund and (fund.eps_ttm > 0 or fund.roe_ttm > 0 or fund.pe_ttm > 0):
            report.fundamentals = {
                'pe_ttm': fund.pe_ttm or safe_float(None),
                'pb': fund.pb or safe_float(None),
                'roe_ttm': fund.roe_ttm or safe_float(None),
                'eps_ttm': fund.eps_ttm or safe_float(None),
                'revenue_yoy': fund.revenue_yoy or safe_float(None),
                'profit_yoy': fund.profit_yoy or safe_float(None),
                'ocf_to_profit': fund.ocf_to_profit or safe_float(None),
                'as_of_date': str(fund.timestamp.date()) if fund.timestamp else None,
            }
        else:
            report.warnings.append('fundamentals_unavailable')
    except Exception as exc:
        report.warnings.append(f'fundamentals_error: {exc}')

    # 4) Regime(A 股大盘上下文,可选)
    if req.include_regime:
        try:
            from core.regime import get_regime
            r = get_regime()
            report.regime = {
                'regime': r.regime,
                'date': r.date_str,
                'close': r.close,
                'ma20': r.ma20,
                'ma60': r.ma60,
                'atr_ratio': round(r.atr_ratio, 4),
                'atr_threshold_dynamic': round(r.atr_threshold_dynamic, 4),
                'ma60_slope': round(r.ma60_slope, 6),
                'position_cap': r.position_cap,
                'signal_threshold_multiplier': r.signal_threshold_multiplier,
                'allow_new_buys': r.allow_new_buys,
                'should_reduce_positions': r.should_reduce_positions,
                'reason': r.reason,
                'source': r.source,
            }
        except Exception as exc:
            report.warnings.append(f'regime_error: {exc}')

    # 5) 风险指标
    report.risk = compute_risk_metrics(df, current_price)

    # 6) ML 预测(可选)
    if req.include_ml:
        report.ml_prediction = try_ml_prediction(sym, df)

    # 7) 新闻情感(可选)
    if req.include_news:
        report.news_sentiment = try_news_sentiment(sym)

    # 8) LLM 综合解读(可选)
    if req.include_llm:
        report.llm_summary = try_llm_summary(report, llm_provider=req.llm_provider)

    # 9) 投资建议(融合 combined_score + Regime + 基本面)
    report.recommendation = make_recommendation(
        combined_score=report.factor_pipeline.get('combined_score', 0.0),
        dominant=report.factor_pipeline.get('dominant_signal', 'HOLD'),
        regime=report.regime,
        fundamentals=report.fundamentals,
        risk=report.risk,
    )

    # 10) 数据质量
    report.data_quality = {
        'bars_used': int(len(df)),
        'factors_failed': list(report.factor_pipeline.get('errors', {}).keys()),
        'has_fundamentals': bool(report.fundamentals),
        'has_regime': report.regime is not None,
    }

    # 11) 行业横向对比(sector 作为可选输入)
    if req.sector:
        try:
            from services.sector_comparison import compare_sector
            comp = compare_sector(req.sector, base_symbol=sym)
            report.sector_comparison = comp.to_dict()
        except ValueError:
            # 未知行业,静默跳过
            pass
        except Exception as exc:
            report.warnings.append(f'sector_comparison_error: {exc}')

    return report

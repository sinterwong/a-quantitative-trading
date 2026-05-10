"""
backend/services/single_stock_analysis.py — 单股票综合分析

整合系统现有能力对单只股票生成结构化分析报告：
  - 行情快照（实时价 + OHLC + 涨跌幅）
  - 因子流水线评分（technical + fundamental + macro，A 股全量；港股技术层）
  - 基本面快照（PE / PB / ROE / 营收增速等，仅 A 股）
  - 市场环境（Regime，BULL/BEAR/VOLATILE/CALM，仅 A 股市场上下文）
  - 单股票风险指标（ATR / VaR-95 / 年化波动率 / 最大回撤 / 建议止损止盈）
  - ML 价格方向预测（XGBoost，可选）
  - 新闻情感（Claude Haiku，A 股可选）
  - LLM 综合解读（DeepSeek/Kimi/MiniMax，可选）
  - 投资建议（基于综合得分 + Regime 的规则化决策）

两套分析路径：
  - analyze_a_share(): 调用全量因子流水线 + FundamentalDataManager + Regime
  - analyze_hk_share(): 仅技术因子 + 港股快照（PE 留空，无 Regime）

入口约定：每个分析步骤独立 try-except，单步失败不影响整体报告——
返回字段为 None / 空 dict + warnings 列表说明降级原因。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger('backend.single_stock_analysis')


# ---------------------------------------------------------------------------
# 输入 / 输出
# ---------------------------------------------------------------------------

@dataclass
class AnalysisRequest:
    symbol: str
    lookback_days: int = 250
    include_llm: bool = False
    include_regime: bool = True
    include_news: bool = False
    include_ml: bool = False
    sector: Optional[str] = None        # 行业名称，如"白酒"，用于横向对比

    @classmethod
    def from_body(cls, body: Dict[str, Any]) -> 'AnalysisRequest':
        sym = str(body.get('symbol', '')).strip()
        if not sym:
            raise ValueError('missing required field: symbol')
        return cls(
            symbol=sym,
            lookback_days=int(body.get('lookback_days', 250)),
            include_llm=bool(body.get('include_llm', False)),
            include_regime=bool(body.get('include_regime', True)),
            include_news=bool(body.get('include_news', False)),
            include_ml=bool(body.get('include_ml', False)),
            sector=body.get('sector') or None,
        )


@dataclass
class AnalysisReport:
    symbol: str
    market: str            # 'A' | 'HK'
    as_of: str
    snapshot: Dict[str, Any] = field(default_factory=dict)
    factor_pipeline: Dict[str, Any] = field(default_factory=dict)
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    regime: Optional[Dict[str, Any]] = None
    risk: Dict[str, Any] = field(default_factory=dict)
    ml_prediction: Optional[Dict[str, Any]] = None
    news_sentiment: Optional[Dict[str, Any]] = None
    llm_summary: Optional[Dict[str, Any]] = None
    recommendation: Dict[str, Any] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)
    sector_comparison: Optional[Dict[str, Any]] = None  # 行业横向对比
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'market': self.market,
            'as_of': self.as_of,
            'snapshot': self.snapshot,
            'factor_pipeline': self.factor_pipeline,
            'fundamentals': self.fundamentals,
            'regime': self.regime,
            'risk': self.risk,
            'ml_prediction': self.ml_prediction,
            'news_sentiment': self.news_sentiment,
            'llm_summary': self.llm_summary,
            'recommendation': self.recommendation,
            'data_quality': self.data_quality,
            'sector_comparison': self.sector_comparison,
            'warnings': self.warnings,
        }


# ---------------------------------------------------------------------------
# 符号识别 / 校验
# ---------------------------------------------------------------------------

_A_SHARE_PATTERN = re.compile(r'^\d{6}\.(SH|SZ)$', re.IGNORECASE)
_HK_PATTERNS = [
    re.compile(r'^HK:?(\d{3,5})$', re.IGNORECASE),
    re.compile(r'^hk(\d{3,5})$', re.IGNORECASE),
    re.compile(r'^(\d{3,5})\.HK$', re.IGNORECASE),
]


def detect_market(symbol: str) -> str:
    """识别股票市场：'A' / 'HK' / 'unknown'。"""
    s = symbol.strip()
    if _A_SHARE_PATTERN.match(s):
        return 'A'
    for p in _HK_PATTERNS:
        if p.match(s):
            return 'HK'
    return 'unknown'


def normalize_a_share_symbol(symbol: str) -> str:
    """统一 A 股代码大小写为 '600519.SH' 形式。"""
    s = symbol.strip().upper()
    if not _A_SHARE_PATTERN.match(s):
        raise ValueError(f'invalid A-share symbol: {symbol!r} (expected NNNNNN.SH/SZ)')
    return s


def normalize_hk_symbol(symbol: str) -> str:
    """统一港股代码为 'hkNNNNN' 形式（new sina API 用）。"""
    s = symbol.strip()
    for p in _HK_PATTERNS:
        m = p.match(s)
        if m:
            num = m.group(1).zfill(5)
            return f'hk{num}'
    raise ValueError(f'invalid HK symbol: {symbol!r} (expected HK:NNNNN / NNNNN.HK / hkNNNNN)')


# ---------------------------------------------------------------------------
# 共享子分析 — 风险 / 趋势统计
# ---------------------------------------------------------------------------

def _compute_risk_metrics(df, current_price: float) -> Dict[str, Any]:
    """从日 K 计算 ATR / VaR-95 / 年化波动率 / 最大回撤 / 建议止损止盈。"""
    try:
        import numpy as np
        import pandas as pd

        if df is None or len(df) < 20:
            return {'error': 'insufficient_bars'}

        df = df.copy()
        # ATR(14) — Wilder
        high = df['high'].astype(float)
        low = df['low'].astype(float)
        close = df['close'].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_14 = float(tr.tail(14).mean())

        # 收益率序列 → VaR / 波动 / 回撤
        rets = close.pct_change().dropna()
        if len(rets) < 20:
            return {
                'atr_14': round(atr_14, 4),
                'atr_pct': round(atr_14 / current_price * 100, 3) if current_price > 0 else None,
                'error': 'insufficient_returns',
            }

        var_95 = float(np.percentile(rets, 5))    # 1 日 VaR（负值表示亏损）
        ann_vol = float(rets.std() * math.sqrt(252))

        # 滚动峰值最大回撤（以闭盘价口径）
        equity_curve = (1 + rets).cumprod()
        peak = equity_curve.cummax()
        dd = (equity_curve / peak - 1)
        max_dd = float(dd.min())

        suggested_stop = round(current_price - 3.0 * atr_14, 4) if current_price > 0 else None
        # 1.5R 止盈：3R 风险下浮 → 4.5×ATR 上浮（保留可调）
        suggested_tp = round(current_price + 4.5 * atr_14, 4) if current_price > 0 else None

        return {
            'atr_14': round(atr_14, 4),
            'atr_pct': round(atr_14 / current_price * 100, 3) if current_price > 0 else None,
            'var_95_1d': round(var_95, 6),
            'annualized_vol': round(ann_vol, 4),
            'max_drawdown_window': round(max_dd, 4),
            'suggested_stop_loss': suggested_stop,
            'suggested_take_profit': suggested_tp,
            'returns_window_days': int(len(rets)),
        }
    except Exception as exc:
        logger.warning('_compute_risk_metrics failed: %s', exc)
        return {'error': str(exc)}


# ---------------------------------------------------------------------------
# A 股分析
# ---------------------------------------------------------------------------

def analyze_a_share(req: AnalysisRequest) -> AnalysisReport:
    """A 股综合分析（全量因子 + 基本面 + Regime + 风险）。"""
    sym = normalize_a_share_symbol(req.symbol)
    report = AnalysisReport(
        symbol=sym, market='A',
        as_of=datetime.now().isoformat(timespec='seconds'),
    )

    # 1) 行情数据
    df = None
    try:
        from core.data_layer import get_data_layer
        dl = get_data_layer()
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
            # 实时报价（涨跌幅 / 量比，会比日 K 新）
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
            'reasoning': '行情数据不可用，无法分析',
        }
        return report

    current_price = report.snapshot.get('realtime_price') or report.snapshot.get('last') or 0.0

    # 2) 因子流水线
    pipeline_result = None
    try:
        from core.pipeline_factory import build_pipeline
        # strict=False → 允许部分因子缺失，靠 PipelineResult.factors_ok 判断
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

    # 3) 基本面
    try:
        from core.fundamental_data import FundamentalDataManager
        fm = FundamentalDataManager()
        fdf = fm.get_fundamentals(sym)
        if fdf is not None and not fdf.empty:
            row = fdf.iloc[-1]
            report.fundamentals = {
                'pe_ttm': _safe_float(row.get('pe_ttm')),
                'pb': _safe_float(row.get('pb')),
                'roe_ttm': _safe_float(row.get('roe_ttm')),
                'eps_ttm': _safe_float(row.get('eps_ttm')),
                'revenue_yoy': _safe_float(row.get('revenue_yoy')),
                'profit_yoy': _safe_float(row.get('profit_yoy')),
                'ocf_to_profit': _safe_float(row.get('ocf_to_profit')),
                'as_of_date': str(fdf.index[-1].date() if hasattr(fdf.index[-1], 'date') else fdf.index[-1]),
            }
        else:
            report.warnings.append('fundamentals_unavailable')
    except Exception as exc:
        report.warnings.append(f'fundamentals_error: {exc}')

    # 4) Regime（A 股大盘上下文，可选）
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
    report.risk = _compute_risk_metrics(df, current_price)

    # 6) ML 预测（可选）
    if req.include_ml:
        report.ml_prediction = _try_ml_prediction(sym, df)

    # 7) 新闻情感（可选）
    if req.include_news:
        report.news_sentiment = _try_news_sentiment(sym)

    # 8) LLM 综合解读（可选）
    if req.include_llm:
        report.llm_summary = _try_llm_summary(report)

    # 9) 投资建议（融合 combined_score + Regime + 基本面）
    report.recommendation = _make_recommendation(
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

    # 11) 行业横向对比（sector 作为可选输入）
    if req.sector:
        try:
            from services.sector_comparison import compare_sector
            comp = compare_sector(req.sector, base_symbol=sym)
            report.sector_comparison = comp.to_dict()
        except ValueError:
            # 未知行业，静默跳过
            pass
        except Exception as exc:
            report.warnings.append(f'sector_comparison_error: {exc}')

    return report


# ---------------------------------------------------------------------------
# 港股分析（更受限：无 PE / 无 Regime / 仅技术因子）
# ---------------------------------------------------------------------------

def analyze_hk_share(req: AnalysisRequest) -> AnalysisReport:
    sym = normalize_hk_symbol(req.symbol)
    report = AnalysisReport(
        symbol=sym, market='HK',
        as_of=datetime.now().isoformat(timespec='seconds'),
    )

    # 1) 港股实时快照（QuoteSourceManager 路由：腾讯主 → 新浪备）
    snap = None
    try:
        from core.quote_source_manager import get_quote_manager
        mgr = get_quote_manager()
        q = mgr.fetch_quote(sym)
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

    # 2) 历史日K（QuoteSourceManager 路由：腾讯主 → 新浪备）
    df = None
    try:
        from core.quote_source_manager import get_quote_manager
        mgr = get_quote_manager()
        df = mgr.fetch_daily_kline(sym, days=max(req.lookback_days, 60), adjust='qfq')
        if df is None or df.empty:
            report.warnings.append('hk_history_unavailable')
    except Exception as exc:
        report.warnings.append(f'hk_history_error: {exc}')

    current_price = (snap.price if snap else 0.0) or 0.0

    # 3) 技术因子（仅技术层，HK 无基本面/宏观对接）
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
                    pipeline.add(cls, weight=w, params=sym_param)
                except Exception as exc:
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

    # 4) 风险指标
    if df is not None and not df.empty and current_price > 0:
        report.risk = _compute_risk_metrics(df, current_price)
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
            'note': '港股市场未接入 Regime 检测；A 股 SH000001 上下文不适用',
        }

    # ML / 新闻：港股暂不支持
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

    # LLM 解读对港股仍可用（基于已有快照 + 因子）
    if req.include_llm:
        report.llm_summary = _try_llm_summary(report)

    # 投资建议（无 regime 时只用 combined_score + 风险）
    report.recommendation = _make_recommendation(
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


# ---------------------------------------------------------------------------
# 子分析助手 — ML / 新闻 / LLM
# ---------------------------------------------------------------------------

def _try_ml_prediction(symbol: str, df) -> Dict[str, Any]:
    """尝试加载已训练的 XGBoost 模型并预测下一日方向。"""
    try:
        from core.ml.model_registry import ModelRegistry
        registry = ModelRegistry()
        try:
            model, meta = registry.load(symbol, 'xgboost')
        except Exception as exc:
            return {'available': False, 'reason': f'model_not_found: {exc}'}

        from core.ml.price_predictor import MLPredictionFactor
        factor = MLPredictionFactor(symbol=symbol)
        # 直接评估因子（XGBoost 模型存活则返回方向 z-score）
        z = factor.evaluate(df)
        latest_z = float(z.dropna().iloc[-1]) if hasattr(z, 'dropna') and len(z) else 0.0

        return {
            'available': True,
            'model': 'xgboost',
            'latest_score': round(latest_z, 4),
            'direction': 'BUY' if latest_z > 0.3 else ('SELL' if latest_z < -0.3 else 'HOLD'),
            'metrics': meta.get('metrics', {}),
            'trained_at': meta.get('trained_at', ''),
        }
    except Exception as exc:
        logger.debug('_try_ml_prediction failed: %s', exc)
        return {'available': False, 'reason': str(exc)}


def _try_news_sentiment(symbol: str) -> Dict[str, Any]:
    """尝试读取 Parquet 缓存的新闻情感。不调网络。"""
    try:
        from core.factors.nlp import NewsSentimentFactor
        factor = NewsSentimentFactor(symbol=symbol)
        # 使用 _load_parquet_cache（如有）；不存在则返回 unavailable
        if hasattr(factor, '_load_parquet_cache'):
            cache = factor._load_parquet_cache()
            if cache is None or len(cache) == 0:
                return {'available': False, 'reason': 'no_cached_sentiment'}
            latest = cache.iloc[-1]
            return {
                'available': True,
                'score': round(float(latest.get('score', 0.0) or 0.0), 4),
                'as_of': str(latest.get('date') or cache.index[-1]),
                'source': 'parquet_cache',
            }
        return {'available': False, 'reason': 'cache_api_unavailable'}
    except Exception as exc:
        logger.debug('_try_news_sentiment failed: %s', exc)
        return {'available': False, 'reason': str(exc)}


_LLM_SYSTEM_PROMPT = (
    '你是一名专业的量化分析师。请根据下面提供的结构化分析数据，'
    '给出投资角度的综合解读。要求：\n'
    '1. 仅基于提供的数据；不要编造\n'
    '2. 输出严格 JSON：{"overall_view":str,"bullish_points":[str],"bearish_points":[str],'
    '"key_risks":[str],"action_bias":"BUY"|"SELL"|"HOLD"}\n'
    '3. overall_view 不超过 80 字；每个 list 最多 3 条，每条不超过 30 字\n'
)


def _try_llm_summary(report: AnalysisReport) -> Dict[str, Any]:
    """调用 LLM 对结构化分析做一次综合解读。"""
    try:
        from backend.services.llm.factory import create_provider
        provider = create_provider()
    except Exception as exc:
        return {'available': False, 'reason': f'llm_provider_unavailable: {exc}'}

    try:
        import json
        # 喂给 LLM 的负载——只发关键字段以控制 token
        payload = {
            'symbol': report.symbol,
            'market': report.market,
            'snapshot': report.snapshot,
            'factor_pipeline': {
                k: v for k, v in report.factor_pipeline.items()
                if k in ('combined_score', 'dominant_signal',
                         'buy_strength', 'sell_strength')
            },
            'top_factors': [
                f for f in report.factor_pipeline.get('breakdown', [])
                if f.get('error') is None
            ][:6],
            'fundamentals': report.fundamentals,
            'regime': report.regime,
            'risk': report.risk,
        }
        user_msg = (
            '【分析数据】\n'
            + json.dumps(payload, ensure_ascii=False, default=str)
        )
        resp = provider.chat([
            {'role': 'system', 'content': _LLM_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_msg},
        ], max_tokens=400, temperature=0.2)

        content = (resp.content or '').strip()
        # 尝试解析为 JSON
        parsed = _safe_json_extract(content)
        return {
            'available': True,
            'model': resp.model,
            'latency_ms': resp.latency_ms,
            'usage': resp.usage,
            'parsed': parsed,
            'raw': content if not parsed else None,
        }
    except Exception as exc:
        logger.warning('_try_llm_summary failed: %s', exc)
        return {'available': False, 'reason': str(exc)}


# ---------------------------------------------------------------------------
# 推荐决策
# ---------------------------------------------------------------------------

def _make_recommendation(combined_score: float, dominant: str,
                         regime: Optional[Dict[str, Any]],
                         fundamentals: Optional[Dict[str, Any]],
                         risk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规则化决策：综合得分 × Regime 阻尼 × 基本面健康度。

    基本面硬红线（任一条触发直接 SELL，不受综合得分覆盖）：
      - 营收 YoY 连续为负（本期 < 0）
      - 净利 YoY 连续为负（本期 < 0）
      - 任一 YoY 跌幅超过 20%（趋势性恶化）

    返回 {action, confidence, reasoning}.
    """
    score = float(combined_score or 0.0)
    reasons: List[str] = []

    # 0) 基本面硬红线 — 任一触发则强制 SELL，不受综合得分左右
    fundamental_red = False
    if fundamentals:
        rev = fundamentals.get('revenue_yoy')
        profit = fundamentals.get('profit_yoy')
        roe = fundamentals.get('roe_ttm')

        if roe is not None and roe < 0:
            fundamental_red = True
            reasons.append(f'ROE 为负({roe:.1f}%)')

        if rev is not None:
            if rev < -20:
                fundamental_red = True
                reasons.append(f'营收同比大幅下滑({rev:+.1f}%)')
            elif rev < 0:
                fundamental_red = True
                reasons.append(f'营收同比下滑({rev:+.1f}%)')

        if profit is not None:
            if profit < -20:
                fundamental_red = True
                reasons.append(f'净利同比大幅下滑({profit:+.1f}%)')
            elif profit < 0:
                fundamental_red = True
                reasons.append(f'净利同比下滑({profit:+.1f}%)')

    # 1) Regime 调整
    multiplier = 1.0
    blocked = False
    if regime:
        mult = float(regime.get('signal_threshold_multiplier', 1.0) or 1.0)
        if mult > 1.0:
            multiplier = mult
            reasons.append(f"Regime={regime.get('regime')} 阈值×{mult:.2f}")
        if regime.get('regime') == 'BEAR' and not regime.get('allow_new_buys', True):
            blocked = True
            reasons.append('BEAR 禁止新开多仓')

    # 2) 基本面软修正（仅在未触发红线时适用）
    # 营收/净利本身的 YoY 已在红线处理；这里只处理无 YoY 风险但 ROE/营收质量偏弱的情况
    if fundamentals and not fundamental_red:
        pass  # 暂不需要软修正，当前因子已足够严格

    # 3) 风险约束
    if risk and isinstance(risk, dict):
        ann_vol = risk.get('annualized_vol')
        if ann_vol is not None and ann_vol > 0.60:
            score *= 0.85
            reasons.append(f'年化波动 {ann_vol:.1%} 偏高，置信折扣')

    # 4) 决策
    threshold = 0.5 * multiplier
    if blocked:
        action = 'HOLD'
    elif fundamental_red:
        # 基本面红线：强制 SELL，reasoning 已在上面说明具体原因
        action = 'SELL'
    elif score >= threshold:
        action = 'BUY'
    elif score <= -threshold:
        action = 'SELL'
    else:
        action = 'HOLD'

    confidence = min(1.0, abs(score) / max(1.0, threshold))
    if dominant in ('BUY', 'SELL') and dominant == action:
        confidence = min(1.0, confidence + 0.10)

    return {
        'action': action,
        'confidence': round(confidence, 4),
        'adjusted_score': round(score, 4),
        'threshold': round(threshold, 4),
        'reasoning': '; '.join(reasons) or '基于综合得分',
    }


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 6)
    except (TypeError, ValueError):
        return None


def _safe_json_extract(text: str) -> Optional[Dict[str, Any]]:
    """尝试从 LLM 输出中提取 JSON。LLM 经常在 JSON 周围包裹解释或代码块。"""
    if not text:
        return None
    import json
    # 优先尝试三反引号代码块
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        # 否则取首个 { 到末尾 }
        i = text.find('{')
        j = text.rfind('}')
        if i >= 0 and j > i:
            candidate = text[i:j + 1]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


__all__ = [
    'AnalysisRequest',
    'AnalysisReport',
    'detect_market',
    'analyze_a_share',
    'analyze_hk_share',
    'normalize_a_share_symbol',
    'normalize_hk_symbol',
]

"""
backend/services/single_stock_analysis.py — 单股票分析(兼容 shim)

⚠️ P2-2 重构起,业务逻辑已下沉到 ``core.use_cases.analyze_stock``。
   本文件保留为兼容 shim,转发所有公开符号。

新代码请改用:

    from core.use_cases.analyze_stock import (
        AnalysisRequest, AnalysisReport, analyze_stock,
    )
"""

from core.use_cases.analyze_stock import (
    AnalysisRequest,
    AnalysisReport,
    analyze_stock,
    analyze_a_share,
    analyze_hk_share,
    detect_market,
    normalize_a_share_symbol,
    normalize_hk_symbol,
)

# 内部辅助符号(测试和 backend 内部使用)
from core.use_cases.analyze_stock import (  # noqa: F401
    _compute_risk_metrics,
    _try_ml_prediction,
    _try_news_sentiment,
    _try_llm_summary,
    _make_recommendation,
    _safe_float,
    _safe_json_extract,
)

__all__ = [
    'AnalysisRequest',
    'AnalysisReport',
    'analyze_stock',
    'analyze_a_share',
    'analyze_hk_share',
    'detect_market',
    'normalize_a_share_symbol',
    'normalize_hk_symbol',
]

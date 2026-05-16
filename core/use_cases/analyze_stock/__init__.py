"""
core/use_cases/analyze_stock — 单股票综合分析 use case (P2-2)

> 历史路径:本 use case 原位于 ``backend/services/single_stock_analysis.py``,
> P2-2 重构起业务下沉到 use case 层。最初是单一 ``analyze_stock.py`` 文件
> (近 900 行),后续按职责拆为子模块:
>
>     _types          AnalysisRequest / AnalysisReport
>     _symbols        市场识别 + 代码规范化
>     _deps           gateway / data_layer 注入解析
>     _utils          通用小工具
>     _risk_metrics   ATR / VaR / 波动 / 回撤
>     _ml             ML 价格方向预测
>     _news           新闻情感
>     _llm_summary    LLM 综合解读
>     _recommend      投资建议决策
>     _a_share        A 股分析流程
>     _hk_share       港股分析流程
>
> 本 __init__ 只负责 public API 与 dispatch,以及保留原文件曾导出的
> 内部 helper 别名(下划线前缀)以兼容老代码与测试。
"""

from __future__ import annotations

from core.use_cases import UseCaseError

# ── 公共类型 ─────────────────────────────────────────────────
from ._types import AnalysisRequest, AnalysisReport

# ── 公共函数 ─────────────────────────────────────────────────
from ._symbols import (
    detect_market,
    normalize_a_share_symbol,
    normalize_hk_symbol,
)
from ._a_share import analyze_a_share
from ._hk_share import analyze_hk_share

# ── 内部 helper(导出供 backend.services.single_stock_analysis shim 与测试用) ─
# 旧名称(带下划线)→ 新名称(无下划线)的转发,保持向后兼容
from ._deps import resolve_data_layer as _resolve_data_layer
from ._deps import resolve_gateway as _resolve_gateway
from ._llm_summary import LLM_SYSTEM_PROMPT as _LLM_SYSTEM_PROMPT
from ._llm_summary import try_llm_summary as _try_llm_summary
from ._ml import try_ml_prediction as _try_ml_prediction
from ._news import try_news_sentiment as _try_news_sentiment
from ._recommend import make_recommendation as _make_recommendation
from ._risk_metrics import compute_risk_metrics as _compute_risk_metrics
from ._utils import safe_float as _safe_float
from ._utils import safe_json_extract as _safe_json_extract


def analyze_stock(req: AnalysisRequest) -> AnalysisReport:
    """统一入口:按 symbol 识别市场,dispatch 到对应分析函数。

    所有 caller (REST API / Streamlit UI / CLI) 应优先调本函数,
    ``analyze_a_share`` / ``analyze_hk_share`` 保留作为旧 caller 兼容。

    Raises
    ------
    UseCaseError(code='INVALID_SYMBOL')
        无法识别市场。
    """
    market = detect_market(req.symbol)
    if market == 'A':
        return analyze_a_share(req)
    if market == 'HK':
        return analyze_hk_share(req)
    raise UseCaseError(
        f'unrecognized symbol market: {req.symbol}',
        code='INVALID_SYMBOL',
    )


__all__ = [
    'AnalysisRequest',
    'AnalysisReport',
    'detect_market',
    'analyze_stock',
    'analyze_a_share',
    'analyze_hk_share',
    'normalize_a_share_symbol',
    'normalize_hk_symbol',
]

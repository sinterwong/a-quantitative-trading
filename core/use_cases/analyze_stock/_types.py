"""
analyze_stock._types — AnalysisRequest / AnalysisReport 数据类。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger('core.use_cases.analyze_stock')


@dataclass
class AnalysisRequest:
    symbol: str
    lookback_days: int = 250
    include_llm: bool = False
    include_regime: bool = True
    include_news: bool = False
    include_ml: bool = False
    sector: Optional[str] = None        # 行业名称，如"白酒"，用于横向对比
    # 可选：调用方直接注入的 LLM provider；为 None 时走 core.llm_provider 服务定位器。
    # 不在 from_body() 中读取——只用于 in-process 调用 / 测试注入。
    llm_provider: Optional[Any] = field(default=None, repr=False, compare=False)
    # 可选：注入 DataGateway / DataLayer，None 时走 get_gateway / get_data_layer。
    # 用于测试或多源切换；同样不进 from_body。
    gateway: Optional[Any] = field(default=None, repr=False, compare=False)
    data_layer: Optional[Any] = field(default=None, repr=False, compare=False)

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

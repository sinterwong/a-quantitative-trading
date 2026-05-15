"""
analyze_stock._llm_summary — 用 LLM 对结构化分析做综合解读。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ._types import AnalysisReport
from ._utils import safe_json_extract

logger = logging.getLogger('core.use_cases.analyze_stock')


LLM_SYSTEM_PROMPT = (
    '你是一名专业的量化分析师。请根据下面提供的结构化分析数据，'
    '给出投资角度的综合解读。要求：\n'
    '1. 仅基于提供的数据；不要编造\n'
    '2. 输出严格 JSON：{"overall_view":str,"bullish_points":[str],"bearish_points":[str],'
    '"key_risks":[str],"action_bias":"BUY"|"SELL"|"HOLD"}\n'
    '3. overall_view 不超过 80 字；每个 list 最多 3 条，每条不超过 30 字\n'
)


def try_llm_summary(
    report: AnalysisReport,
    llm_provider: Optional[Any] = None,
) -> Dict[str, Any]:
    """调用 LLM 对结构化分析做一次综合解读。

    Args:
        report: 已经填好其它字段的分析报告。
        llm_provider: 可选——直接注入一个 provider(测试 / 多 provider 场景)。
            为 ``None`` 时回退到 :func:`core.llm_provider.create_provider`,
            该工厂由 backend 启动时注册。
    """
    if llm_provider is None:
        try:
            from core.llm_provider import create_provider
            llm_provider = create_provider()
        except Exception as exc:
            return {'available': False, 'reason': f'llm_provider_unavailable: {exc}'}
    provider = llm_provider

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
            {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_msg},
        ], max_tokens=4096, temperature=0.2)

        content = (resp.content or '').strip()
        # 尝试解析为 JSON
        parsed = safe_json_extract(content)
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

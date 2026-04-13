"""
prompts/__init__.py — Prompt 模板导出
"""

from backend.services.llm.prompts import news_sentiment, policy_analysis

SYSTEM_PROMPTS = {
    'news_sentiment': news_sentiment.SYSTEM_PROMPT,
    'policy_analysis': policy_analysis.SYSTEM_PROMPT,
}

USER_TEMPLATES = {
    'news_sentiment': news_sentiment.USER_TEMPLATE,
    'policy_analysis': policy_analysis.USER_TEMPLATE,
}

__all__ = ['SYSTEM_PROMPTS', 'USER_TEMPLATES']

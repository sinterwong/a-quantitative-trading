"""
providers/__init__.py — Provider 抽象接口 + 公开导出
=====================================================

所有 Provider 均继承自 backend.services.llm.providers.base.LLMProvider。
"""

from backend.services.llm.providers.base import LLMProvider, LLMResponse

# Re-export concrete providers for convenience
from backend.services.llm.providers.deepseek import DeepSeekProvider
from backend.services.llm.providers.kimi import KimiProvider

__all__ = ['LLMProvider', 'LLMResponse', 'DeepSeekProvider', 'KimiProvider']

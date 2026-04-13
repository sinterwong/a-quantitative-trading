"""
backend/services/llm/
=====================
统一 LLM 调用服务，支持多 Provider 插拔和缓存。

使用方式:
    from backend.services.llm import LLMService, DeepSeekProvider

    provider = DeepSeekProvider()
    llm = LLMService(provider)
    result = llm.analyze_news("央行宣布降准...")
    print(result.sentiment, result.confidence)
"""

from backend.services.llm.service import LLMService

__all__ = ['LLMService']

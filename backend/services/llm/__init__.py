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
from backend.services.llm.factory import create_provider, create_llm_service

# 把 backend 的 provider 工厂注册到 core 层的服务定位器，
# 这样 core.use_cases 就不必再 import backend.services.llm 了。
from core.llm_provider import set_provider_factory as _set_provider_factory

_set_provider_factory(create_provider)

__all__ = ['LLMService', 'create_provider', 'create_llm_service']

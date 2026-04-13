"""
factory.py — Provider 工厂
==========================
从环境变量或配置自动选择并初始化 LLM Provider。
"""

import os
import logging
from typing import Optional

from backend.services.llm.providers.base import LLMProvider
from backend.services.llm.providers.deepseek import DeepSeekProvider
from backend.services.llm.providers.kimi import KimiProvider

logger = logging.getLogger(__name__)


def create_provider(provider_name: Optional[str] = None, **kwargs) -> LLMProvider:
    """
    根据名称或环境变量创建 LLM Provider。

    Args:
        provider_name: "deepseek" | "kimi"，None 表示从 LLM_PROVIDER 环境变量读取
        **kwargs: 传给 Provider 构造函数的额外参数

    Returns:
        LLMProvider 实例

    Raises:
        ValueError: provider_name 不合法或 Provider 不可用（未配置 API key）
    """
    name = (provider_name or os.environ.get('LLM_PROVIDER', 'deepseek')).lower()

    if name == 'deepseek':
        prov = DeepSeekProvider(**kwargs)
    elif name == 'kimi':
        prov = KimiProvider(**kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {name}. Use 'deepseek' or 'kimi'.")

    if not prov.is_available:
        raise ValueError(
            f"Provider '{name}' is not available: API key not configured. "
            f"Set {name.upper()}_API_KEY in .env."
        )

    logger.info("LLM Provider created: %s (model=%s)", name, getattr(prov, 'model', '?'))
    return prov


def create_llm_service(provider_name: Optional[str] = None, **kwargs):
    """
    一步创建 LLMService（包含 Provider）。

    Args:
        provider_name: 同 create_provider
        **kwargs: 传给 LLMService 的参数

    Returns:
        LLMService 实例
    """
    from backend.services.llm import LLMService

    prov = create_provider(provider_name)
    return LLMService(provider=prov, **kwargs)

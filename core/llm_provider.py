"""
core.llm_provider — LLM Provider 服务定位器
==============================================

use_cases 层不能依赖 backend；当它需要 LLM 时只能通过本模块拿 provider。
具体的 provider 工厂由 backend 在启动时调用 :func:`set_provider_factory`
注册（通常发生在 ``backend.services.llm`` 第一次被导入时）。

测试代码可以直接调用 :func:`set_provider_factory` 注入 mock，
或者把 provider 实例作为 ``llm_provider`` 参数显式传入 use case。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class LLMProviderNotConfigured(RuntimeError):
    """LLM provider 工厂未注册。"""


_factory: Optional[Callable[[], Any]] = None


def set_provider_factory(factory: Optional[Callable[[], Any]]) -> None:
    """注册（或重置）LLM provider 工厂。传入 ``None`` 表示清空。"""
    global _factory
    _factory = factory


def get_provider_factory() -> Optional[Callable[[], Any]]:
    """返回当前已注册的工厂，未注册返回 ``None``。"""
    return _factory


def create_provider() -> Any:
    """调用已注册工厂创建一个新 provider。

    Raises:
        LLMProviderNotConfigured: 工厂未注册。
        其它任意异常: 由工厂本身抛出（如 API key 缺失）。
    """
    if _factory is None:
        raise LLMProviderNotConfigured(
            "LLM provider factory not registered. "
            "Import backend.services.llm at startup or call "
            "core.llm_provider.set_provider_factory()."
        )
    return _factory()

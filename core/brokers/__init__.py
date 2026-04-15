# core.brokers — 券商适配器包
"""
所有券商适配器实现 BrokerAdapter 接口。
当前：PaperBroker（生产可用）
其他：FutuBroker / TigerBroker / IBBroker（Stub，禁止在 Paper 期间调用）
"""

from core.brokers.paper import PaperBroker
from core.brokers.facade import BrokerFactory, SafetyMode

__all__ = ['PaperBroker', 'BrokerFactory', 'SafetyMode']

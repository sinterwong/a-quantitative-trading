# -*- coding: utf-8 -*-
"""
data_gateway.providers — 各家数据源实现

每个 provider 实现 Provider ABC,在 declare() 中声明能力,
通过 gateway.register_provider() 注册到全局 gateway。
"""

from .akshare import AkshareProvider
from .base import Provider, ProviderError
from .eastmoney import EastmoneyProvider
from .sina import SinaProvider
from .tencent import TencentProvider
from .yfinance import YfinanceProvider

__all__ = [
    "Provider",
    "ProviderError",
    "TencentProvider",
    "SinaProvider",
    "EastmoneyProvider",
    "YfinanceProvider",
    "AkshareProvider",
]

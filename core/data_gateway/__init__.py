# -*- coding: utf-8 -*-
"""
data_gateway — 统一数据网关

整个系统对外网数据的唯一出口。设计要点:
  - 业务定义 schema(schemas.py),与具体 provider 解耦
  - provider 平级,通过 capability 声明能力,健康度动态打分
  - 字段级聚合:同字段可由不同 provider 互补提供
  - HTTP / 缓存 / 熔断 / 监控等横切关注点全部在网关层
  - 单一公开入口: get_gateway()
"""

from .cache import MemoryCache, ParquetDiskCache, TieredCache
from .capabilities import Capability, Market, ProviderCapability
from .gateway import DataGateway, get_gateway, reset_gateway
from .schemas import (
    BalanceSheet,
    Fundamentals,
    MarketIndexSnapshot,
    NorthFlow,
    Quote,
    SectorConstituent,
    SectorRanking,
)
from .symbols import (
    detect_market,
    normalize_to_sina,
    normalize_to_tencent,
    safe_float,
    safe_int,
)
from .frames import normalize_kline_index

__all__ = [
    # gateway
    "DataGateway",
    "get_gateway",
    "reset_gateway",
    # cache
    "MemoryCache",
    "ParquetDiskCache",
    "TieredCache",
    # schemas
    "Quote",
    "Fundamentals",
    "BalanceSheet",
    "SectorRanking",
    "SectorConstituent",
    "NorthFlow",
    "MarketIndexSnapshot",
    # capabilities
    "Capability",
    "Market",
    "ProviderCapability",
    # symbols
    "detect_market",
    "normalize_to_sina",
    "normalize_to_tencent",
    "safe_float",
    "safe_int",
    # frames
    "normalize_kline_index",
]

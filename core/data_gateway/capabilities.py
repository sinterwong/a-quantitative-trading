# -*- coding: utf-8 -*-
"""
data_gateway.capabilities — 能力声明

Provider 通过 capabilities() / markets() 声明自身能力,gateway 据此选源。
此处枚举即"系统能从网络拿哪些数据"的完整闭包,新增数据类型时同步扩展。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet


class Capability(str, Enum):
    """数据类型能力。

    字符串值用于 field_authority dict 的 key,便于配置和日志可读。
    """

    QUOTE = "quote"                          # Quote (实时行情)
    KLINE_DAILY = "kline_daily"              # 日 K(也涵盖周/月/年,通过 interval 区分)
    KLINE_MINUTE = "kline_minute"            # 分钟 K
    FUNDAMENTALS = "fundamentals"            # Fundamentals
    SECTOR_RANKING = "sector_ranking"        # List[SectorRanking]
    SECTOR_CONSTITUENTS = "sector_constituents"  # List[SectorConstituent]
    NORTH_FLOW = "north_flow"                # NorthFlow
    MARKET_INDEX = "market_index"            # MarketIndexSnapshot
    MACRO = "macro"                          # 宏观时序 DataFrame (PMI/M2/社融等)
    FUNDAMENTALS_HISTORY = "fundamentals_history"  # 基本面历史时序 DataFrame（日频，前向填充）
    BALANCE_SHEET = "balance_sheet"               # BalanceSheet（资产负债表）
    MARGIN_FLOW = "margin_flow"                   # 融资融券日频时序 DataFrame
    NEWS_HEADLINES = "news_headlines"             # 新闻标题列表 List[str]


class MacroIndicator(str, Enum):
    """宏观指标枚举。扩展此处时同步更新 AkShareProvider.fetch_macro。"""
    PMI = "PMI"           # 制造业采购经理指数
    M2 = "M2"             # 货币供应量 M2 同比
    CREDIT = "CREDIT"     # 社融存量同比


class Market(str, Enum):
    """市场类别。"""

    A = "A"
    INDEX = "INDEX"
    HK = "HK"
    US = "US"
    GLOBAL = "GLOBAL"   # 跨市场聚合数据(板块/北向/VIX/期货)


@dataclass(frozen=True)
class ProviderCapability:
    """Provider 的能力声明 snapshot,由 Provider.declare() 返回。

    Attributes:
        capabilities: 该 provider 支持的数据类型集合
        markets: 该 provider 覆盖的市场集合
        priority_hint: 冷启动评分(尚无健康度统计时使用),范围 [0, 1]。
            默认 0.5。腾讯/新浪等稳定源建议 0.8+,东方财富建议 0.5,
            akshare 等不稳定源建议 0.2。
    """

    capabilities: FrozenSet[Capability] = field(default_factory=frozenset)
    markets: FrozenSet[Market] = field(default_factory=frozenset)
    priority_hint: float = 0.5


__all__ = [
    "Capability",
    "MacroIndicator",
    "Market",
    "ProviderCapability",
]

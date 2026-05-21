# -*- coding: utf-8 -*-
"""
data_gateway.capabilities — 能力声明

Provider 通过 capabilities() / markets() 声明自身能力,gateway 据此选源。
此处枚举即"系统能从网络拿哪些数据"的完整闭包,新增数据类型时同步扩展。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Tuple


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
    DUPONT = "dupont"                            # DupontMetrics（杜邦分析）
    OPERATION = "operation"                      # OperationMetrics（运营能力）
    DIVIDEND = "dividend"                       # DividendRecord（分红记录）
    MARGIN_FLOW = "margin_flow"                 # 融资融券日频时序 DataFrame
    FUND_FLOW = "fund_flow"                       # 个股资金流日频 DataFrame（主力/超大/大单净流入）
    NEWS_HEADLINES = "news_headlines"             # 新闻标题列表 List[str]


class MacroIndicator(str, Enum):
    """宏观指标枚举。扩展此处时同步更新 AkShareProvider.fetch_macro。"""
    PMI = "PMI"           # 制造业采购经理指数
    M2 = "M2"             # 货币供应量 M2 同比
    CREDIT = "CREDIT"     # 社融存量同比
    CPI = "CPI"           # 居民消费价格指数（同比）
    PPI = "PPI"           # 工业生产者出厂价格指数（同比）


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


# ─── 路由策略元数据 (G4) ─────────────────────────────────────────────────────


class RoutingStrategy(str, Enum):
    """单次 (capability, provider 方法) 的多源聚合策略。

    gateway._route() 据此分派到 _sequential_fetch / _merged_fetch /
    _merged_history_fetch / _merged_list_fetch，新增数据类型时只需在
    ROUTING_POLICY 里登记一行，无需在 gateway 公开方法里重写分派代码。
    """

    FAILOVER = "failover"             # 按健康度逐个尝试，首个非空胜出
    MERGE_FIELDS = "merge_fields"     # 并发 top-K 家 dataclass 字段级合并
    MERGE_FRAMES = "merge_frames"     # 并发 top-K 家 DataFrame 列级合并
    MERGE_LISTS = "merge_lists"       # 并发 top-K 家 list 归一去重 (G5)


@dataclass(frozen=True)
class CapabilityPolicy:
    """一条路由声明。

    Attributes:
        strategy: 多源聚合策略
        skip_fields: 仅 MERGE_FIELDS 用——dataclass 中不参与字段级胜出的"标识列"
            (如 symbol/name)，由 score 最高的源直接给定。
        ffill: 仅 MERGE_FRAMES 用——合并后是否对结果做日频前向填充。
            季报类稀疏时序 → True；K 线/资金流缺失即真缺失 → False。
    """

    strategy: RoutingStrategy
    skip_fields: Tuple[str, ...] = ()
    ffill: bool = False


# (Capability, provider 方法名) → CapabilityPolicy
#
# 用二元键而非单 Capability 是因为同一 capability 下不同 op 策略可能不同：
# NORTH_FLOW 的 fetch_north_flow（实时单点）走 FAILOVER，
# fetch_north_flow_history（时序）走 MERGE_FRAMES。
ROUTING_POLICY: Dict[Tuple[Capability, str], CapabilityPolicy] = {
    (Capability.QUOTE, "fetch_quote"): CapabilityPolicy(
        RoutingStrategy.MERGE_FIELDS,
        skip_fields=("symbol", "code", "market", "name", "currency"),
    ),
    (Capability.QUOTE, "fetch_quotes"): CapabilityPolicy(
        RoutingStrategy.MERGE_FIELDS,
        skip_fields=("symbol", "code", "market", "name", "currency"),
    ),
    (Capability.KLINE_DAILY, "fetch_kline_daily"): CapabilityPolicy(
        RoutingStrategy.MERGE_FRAMES, ffill=False,
    ),
    (Capability.KLINE_MINUTE, "fetch_kline_minute"): CapabilityPolicy(
        RoutingStrategy.MERGE_FRAMES, ffill=False,
    ),
    (Capability.FUNDAMENTALS, "fetch_fundamentals"): CapabilityPolicy(
        RoutingStrategy.MERGE_FIELDS,
        skip_fields=("symbol", "name", "industry", "sector"),
    ),
    (Capability.SECTOR_RANKING, "fetch_sectors"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.SECTOR_CONSTITUENTS, "fetch_sector_constituents"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.NORTH_FLOW, "fetch_north_flow"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.NORTH_FLOW, "fetch_north_flow_history"): CapabilityPolicy(
        RoutingStrategy.MERGE_FRAMES, ffill=False,
    ),
    (Capability.MARKET_INDEX, "fetch_market_index"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.MACRO, "fetch_macro"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.FUNDAMENTALS_HISTORY, "fetch_fundamentals_history"): CapabilityPolicy(
        RoutingStrategy.MERGE_FRAMES, ffill=True,
    ),
    (Capability.BALANCE_SHEET, "fetch_balance_sheet"): CapabilityPolicy(
        RoutingStrategy.MERGE_FIELDS, skip_fields=("symbol",),
    ),
    (Capability.DUPONT, "fetch_dupont_metrics"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.OPERATION, "fetch_operation_metrics"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.DIVIDEND, "fetch_dividend"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.MARGIN_FLOW, "fetch_margin_flow"): CapabilityPolicy(
        RoutingStrategy.FAILOVER,
    ),
    (Capability.FUND_FLOW, "fetch_fund_flow"): CapabilityPolicy(
        RoutingStrategy.MERGE_FRAMES, ffill=False,
    ),
    (Capability.NEWS_HEADLINES, "fetch_news_headlines"): CapabilityPolicy(
        RoutingStrategy.MERGE_LISTS,   # G5: EM kuaixun + AkShare 财联社电报
    ),
}


def get_policy(capability: Capability, fn_name: str) -> CapabilityPolicy:
    """查找 (capability, fn_name) 的路由策略；未登记则 KeyError。

    强制每个公开方法都对应一条 policy，避免静默走默认分支导致行为不一致。
    """
    try:
        return ROUTING_POLICY[(capability, fn_name)]
    except KeyError as exc:
        raise KeyError(
            f"未登记的路由策略: ({capability.value}, {fn_name})。"
            f" 请在 capabilities.ROUTING_POLICY 中补一行。"
        ) from exc


__all__ = [
    "Capability",
    "CapabilityPolicy",
    "MacroIndicator",
    "Market",
    "ProviderCapability",
    "ROUTING_POLICY",
    "RoutingStrategy",
    "get_policy",
]

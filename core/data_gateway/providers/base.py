# -*- coding: utf-8 -*-
"""
data_gateway.providers.base — Provider 抽象基类

每个 provider 通过 declare() 声明能力,gateway 只调用已声明的 fetch_* 方法。
未声明的方法默认返回 None / 空,避免每个 provider 都得 raise NotImplementedError。

Provider 不持有 HTTP 客户端实例,而是接受 HttpClient 注入(Stage 2 实现),
这样横切关注点(超时/重试/熔断/监控)都在传输层统一处理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import pandas as pd

from ..capabilities import Capability, MacroIndicator, Market, ProviderCapability
from ..schemas import (
    BalanceSheet,
    Fundamentals,
    MarketIndexSnapshot,
    NorthFlow,
    Quote,
    SectorConstituent,
    SectorRanking,
)


class ProviderError(Exception):
    """Provider 调用失败的统一异常类型。

    gateway 用此异常区分"网络/解析失败"和"业务空结果":
      - raise ProviderError → 触发健康度记录失败 + 熔断计数
      - return None / 空集 → 视为"本源无此数据"(非失败)
    """


class Provider(ABC):
    """数据源 provider 抽象基类。

    所有 provider 平级。能力声明决定它能被路由到的请求类型,
    实际选源由 gateway 基于 (capability_match × health_score × field_authority) 完成。
    """

    name: str = "provider"

    @abstractmethod
    def declare(self) -> ProviderCapability:
        """声明本 provider 支持的 capability / market / 冷启动评分。"""

    def supports(self, capability: Capability, market) -> bool:
        """是否支持(capability, market)组合。

        默认仅检查粗粒度的 capabilities() / markets() 是否都命中。
        provider 可重写以表达更细的能力(如腾讯 KLINE_MINUTE 只支持 HK,
        不支持 A/INDEX/US)。返回 False 时 gateway 不会调用对应 fetch_*。
        """
        decl = self.declare()
        return capability in decl.capabilities and market in decl.markets

    def field_authority(self) -> Dict[Capability, Dict[str, float]]:
        """声明各字段的权威度权重(0-1)。

        默认不声明任何权重,gateway 视为统一 1.0。覆盖此方法可针对
        特定数据类型(如 Quote)声明"我对 pe_ttm/pb 比别家更权威"。

        Returns:
            {Capability.QUOTE: {"pe_ttm": 1.2, "pb": 1.2, ...}, ...}
            权重 > 1 表示比基准更权威,< 1 表示更弱
        """
        return {}

    # ── Capability-specific fetch hooks ────────────────────────────────────────
    # 子类只实现自己在 declare() 中声明的 capability 对应方法。
    # gateway 会根据 capability 声明分发调用,未声明能力不会被调用。

    def fetch_quote(self, symbol: str) -> Optional[Quote]:
        return None

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return {}

    def fetch_kline_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        """日 K 线（涵盖周/月/年，通过 adjust 参数区分）。

        interval 参数从此方法中移除——日 K 和分钟 K 是不同的 Capability，
        分别由 fetch_kline_daily / fetch_kline_minute 提供。"""
        return pd.DataFrame()

    def fetch_kline_minute(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """分钟 K 线。interval: '1m' / '5m' / '15m' / '30m' / '60m'。

        注意：目前仅腾讯 HK 行情支持分钟 K，其他 Provider 返回空 DataFrame。"""
        return pd.DataFrame()

    def fetch_fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        return None

    def fetch_sectors(self, limit: int = 100) -> List[SectorRanking]:
        return []

    def fetch_sector_constituents(
        self,
        code: str,
        limit: int = 20,
    ) -> List[SectorConstituent]:
        return []

    def fetch_north_flow(self) -> Optional[NorthFlow]:
        return None

    def fetch_north_flow_history(self, days: int = 252) -> pd.DataFrame:
        """北向资金日频历史时序。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex,列 north_flow(亿元/天) 与可选 south_flow。
            空 DataFrame 表示本源无数据。
        """
        return pd.DataFrame()

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        return None

    def fetch_macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        """indicator: MacroIndicator enum (PMI / M2 / CREDIT)。

        返回 DataFrame(列约定: date, value)，空 DataFrame 表示无数据。"""
        return pd.DataFrame()

    def fetch_fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """基本面历史时序（日频，前向填充季报）。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列：roe_ttm / eps_ttm / revenue_yoy / profit_yoy /
            ocf_to_profit 等。若无数据返回空 DataFrame。
        """
        return pd.DataFrame()

    def fetch_balance_sheet(self, symbol: str) -> Optional[BalanceSheet]:
        """资产负债表快照（最新一期）。

        Returns
        -------
        BalanceSheet | None
            含 debt_to_equity / current_ratio / quick_ratio 等。
            None 表示本源不支持或无数据。
        """
        return None

    def fetch_margin_flow(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """个股融资融券日频时序。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列 margin_balance（融资余额，元）/ short_balance（融券余额，元）。
            可选列 net_buy（融资净买入额，元）当源支持时返回。
            空 DataFrame 表示本源无数据。
        """
        return pd.DataFrame()

    def fetch_fund_flow(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """个股资金流日频时序。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列 main_net_inflow / super_net_inflow / large_net_inflow
            （元）及对应 _ratio（%），可选 close / change_pct。
            空 DataFrame 表示本源无数据。
        """
        return pd.DataFrame()

    def fetch_news_headlines(self, symbol: str, n: int = 20) -> list:
        """新闻标题列表。

        语义约定：通常按 symbol 返回个股相关新闻；若 provider 文档明示
        "全市场快讯"，调用方应理解为宽泛财经舆情。

        Returns
        -------
        List[str]
            最多 n 条标题（最新在前），空列表表示本源无数据。
        """
        return []


__all__ = ["Provider", "ProviderError"]

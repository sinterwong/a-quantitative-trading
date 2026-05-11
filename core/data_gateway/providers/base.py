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

from ..capabilities import Capability, ProviderCapability
from ..schemas import (
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

    def fetch_kline(
        self,
        symbol: str,
        interval: str = "daily",
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        """interval: 'daily' / 'weekly' / 'monthly' / '1m' / '5m' / '15m' / '30m' / '60m'"""
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

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        return None

    def fetch_macro(self, indicator: str) -> pd.DataFrame:
        """indicator: 'PMI' / 'M2' / 'SHRZGM' ...

        返回 DataFrame(列约定: date, value),空 DataFrame 表示无数据。
        """
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


__all__ = ["Provider", "ProviderError"]

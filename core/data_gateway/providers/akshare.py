# -*- coding: utf-8 -*-
"""
data_gateway.providers.akshare — akshare 隔离区(仅宏观数据)

akshare 实测稳定性差,只在无替代方案的数据类型上保留:
  - MACRO: PMI / M2 / 社融存量(月度时序)

其他能力(实时行情、K 线、北向)走腾讯/新浪/东方财富,不再用 akshare。
本 provider 是 akshare import 在仓库内的"唯一合法出口"。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.akshare")


class AkshareProvider(Provider):
    """akshare 宏观数据隔离 provider。"""

    name = "akshare"

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({Capability.MACRO}),
            markets=frozenset({Market.GLOBAL}),
            priority_hint=0.30,  # 实测不稳定,健康度低
        )

    def fetch_macro(self, indicator: str) -> pd.DataFrame:
        """支持 indicator: PMI / M2 / CREDIT。"""
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 macro 请求")
            return pd.DataFrame()

        try:
            if indicator == "PMI":
                return self._fetch_pmi(ak)
            if indicator == "M2":
                return self._fetch_m2(ak)
            if indicator == "CREDIT":
                return self._fetch_credit(ak)
        except Exception as exc:
            raise ProviderError(f"akshare.fetch_macro({indicator}): {exc}") from exc

        return pd.DataFrame()

    @staticmethod
    def _normalize(raw: pd.DataFrame, date_col: str, value_col: str, out_col: str) -> pd.DataFrame:
        """通用归一: 取 [date_col, value_col] → DataFrame(index=date, value)。"""
        df = raw[[date_col, value_col]].copy()
        df.columns = ["date", out_col]
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date")
        df[out_col] = pd.to_numeric(df[out_col], errors="coerce")
        return df.sort_index()

    @classmethod
    def _fetch_pmi(cls, ak) -> pd.DataFrame:
        raw = ak.macro_china_pmi_monthly()
        raw.columns = [c.strip() for c in raw.columns]
        date_col = raw.columns[0]
        pmi_col = next(
            (c for c in raw.columns if "PMI" in c or "pmi" in c.lower()),
            raw.columns[1],
        )
        return cls._normalize(raw, date_col, pmi_col, "pmi")

    @classmethod
    def _fetch_m2(cls, ak) -> pd.DataFrame:
        raw = ak.macro_china_money_supply_bal()
        raw.columns = [c.strip() for c in raw.columns]
        date_col = raw.columns[0]
        m2_col = (
            next((c for c in raw.columns if "m2" in c.lower() and "yoy" in c.lower()), None)
            or next((c for c in raw.columns if "m2" in c.lower()), raw.columns[1])
        )
        return cls._normalize(raw, date_col, m2_col, "m2_yoy")

    @classmethod
    def _fetch_credit(cls, ak) -> pd.DataFrame:
        raw = ak.macro_china_shrzgm()
        raw.columns = [c.strip() for c in raw.columns]
        date_col = raw.columns[0]
        val_col = (
            next((c for c in raw.columns if "yoy" in c.lower()), None)
            or next((c for c in raw.columns if "同比" in c), raw.columns[1])
        )
        return cls._normalize(raw, date_col, val_col, "credit_yoy")


__all__ = ["AkshareProvider"]

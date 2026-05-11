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
import re

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..schemas import Fundamentals
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.akshare")


def _parse_chinese_date(raw: str) -> pd.Timestamp:
    """解析 '2026年04月份' → pd.Timestamp。失败返回 NaT。"""
    m = re.match(r"(\d{4})年(\d{2})月份", str(raw).strip())
    if m:
        return pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1)
    try:
        return pd.to_datetime(raw, errors="coerce")
    except Exception:
        return pd.NaT


class AkshareProvider(Provider):
    """akshare 宏观数据隔离 provider。"""

    name = "akshare"

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.MACRO,
                Capability.FUNDAMENTALS,
                Capability.FUNDAMENTALS_HISTORY,
            }),
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

    def fetch_fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        """从 stock_financial_abstract 提取基本面快照。

        akshare 1.18.60 财报摘要接口不含 PE/PB，pe_ttm/pb 留 0，
        由 gateway 层通过腾讯实时行情补充（见 gateway.fundamentals()）。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 fundamentals 请求")
            return None

        try:
            code = symbol.replace(".SH", "").replace(".SZ", "").replace(".", "")
            raw = ak.stock_financial_abstract(symbol=code)
            if raw is None or raw.empty:
                return None
        except Exception as exc:
            raise ProviderError(f"akshare.fetch_fundamentals({symbol}): {exc}") from exc

        # 最新季度列（索引2，即 df.columns[2]）
        if len(raw.columns) < 3:
            return None
        latest_col = raw.columns[2]

        # 按 [选项, 指标] 双键查找值
        def get_val(option: str, indicator: str, default: float = 0.0) -> float:
            rows = raw[(raw["选项"] == option) & (raw["指标"] == indicator)]
            if rows.empty:
                return default
            val = rows.iloc[0][latest_col]
            try:
                return float(val)
            except (TypeError, ValueError):
                return default

        # 提取关键指标
        eps_ttm = get_val("常用指标", "基本每股收益", 0.0)
        roe_ttm = get_val("盈利能力", "净资产收益率(ROE)", 0.0)
        profit_ttm = get_val("常用指标", "归母净利润", 0.0)
        revenue_ttm = get_val("常用指标", "营业总收入", 0.0)
        revenue_yoy = get_val("成长能力", "营业总收入增长率", 0.0)
        profit_yoy = get_val("成长能力", "归属母公司净利润增长率", 0.0)

        # OCF/净利润（现金流质量）= 经营现金流量净额 / 归母净利润
        ocf_net = get_val("常用指标", "经营现金流量净额", 0.0)
        if profit_ttm > 0:
            ocf_to_profit = ocf_net / profit_ttm
        else:
            ocf_to_profit = 0.0

        # 报告期
        try:
            period_str = str(latest_col).strip()  # e.g. "20260331"
            report_ts = pd.Timestamp(
                year=int(period_str[:4]), month=int(period_str[4:6]), day=1
            )
        except Exception:
            report_ts = pd.Timestamp.now()

        if eps_ttm <= 0 and roe_ttm <= 0:
            # 无可用财务数据
            return None

        return Fundamentals(
            symbol=symbol,
            name="",
            eps_ttm=eps_ttm,
            roe_ttm=roe_ttm,
            profit_ttm=profit_ttm,
            revenue_ttm=revenue_ttm,
            revenue_yoy=revenue_yoy,
            profit_yoy=profit_yoy,
            ocf_to_profit=ocf_to_profit,
            pe_ttm=0.0,   # 腾讯实时行情补充，见 gateway.fundamentals()
            pb=0.0,       # 腾讯实时行情补充，见 gateway.fundamentals()
            timestamp=report_ts,
        )

    @staticmethod
    def _normalize(raw: pd.DataFrame, date_col: str, value_col: str, out_col: str) -> pd.DataFrame:
        """通用归一: 取 [date_col, value_col] → DataFrame(index=date, value)。"""
        df = raw[[date_col, value_col]].copy()
        df.columns = ["date", out_col]
        df["date"] = df["date"].apply(_parse_chinese_date)
        df = df.dropna(subset=["date"]).set_index("date")
        df[out_col] = pd.to_numeric(df[out_col], errors="coerce")
        return df.sort_index()

    @classmethod
    def _fetch_pmi(cls, ak) -> pd.DataFrame:
        # akshare 1.18.60: 函数名为 macro_china_pmi（不是 macro_china_pmi_monthly）
        raw = ak.macro_china_pmi()
        raw.columns = [c.strip() for c in raw.columns]
        date_col = next((c for c in raw.columns if "月" in c or "date" in c.lower()), raw.columns[0])
        pmi_col = next(
            (c for c in raw.columns if "制造业" in c and "指数" in c),
            raw.columns[1],
        )
        return cls._normalize(raw, date_col, pmi_col, "pmi")

    @classmethod
    def _fetch_m2(cls, ak) -> pd.DataFrame:
        # akshare 1.18.60: 函数名为 macro_china_money_supply（不是 macro_china_money_supply_bal）
        raw = ak.macro_china_money_supply()
        raw.columns = [c.strip() for c in raw.columns]
        date_col = next((c for c in raw.columns if "月" in c or "date" in c.lower()), raw.columns[0])
        m2_col = next(
            (c for c in raw.columns if "M2" in c and "同比" in c),
            next((c for c in raw.columns if "m2" in c.lower() and "yoy" in c.lower()), None),
        )
        if m2_col is None:
            m2_col = raw.columns[1]
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

    def fetch_fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """从 stock_financial_analysis_indicator_em 获取财务历史，转换为日频 DataFrame。

        列映射：
          ROEJQ            → roe_ttm      （加权 ROE，%）
          EPSJB            → eps_ttm      （每股收益 TTM，元/股）
          NETPROFITRPHBZC  → profit_yoy   （净利润同比增长，%）
          TOTALOPERATEREVE → _revenue     （营业总收入，用于自算营收 YoY）

        注意：pe_ttm / pb / ocf_to_profit / holder_num 在此数据源中不可得，
        对应因子将降级为零，这是已知数据层限制。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 fundamentals_history 请求")
            return pd.DataFrame()

        code_raw = symbol.upper()
        if not code_raw.endswith((".SH", ".SZ")):
            code_raw = code_raw + ".SH"

        try:
            df = ak.stock_financial_analysis_indicator_em(symbol=code_raw)
        except Exception as exc:
            raise ProviderError(
                f"akshare.fetch_fundamentals_history({symbol}): {exc}"
            ) from exc

        if df is None or df.empty:
            return pd.DataFrame()

        return self._normalize_indicator_em(df, start, end)

    @staticmethod
    def _normalize_indicator_em(
        df: pd.DataFrame, start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """将 stock_financial_analysis_indicator_em DataFrame 标准化为日频时序。"""
        import numpy as np

        df = df.copy()

        if "REPORT_DATE" not in df.columns:
            return pd.DataFrame()
        df.index = pd.to_datetime(df["REPORT_DATE"], errors="coerce")
        df = df[~df.index.isna()].sort_index()
        if df.empty:
            return pd.DataFrame()

        result = {}

        # ROE（%，直接可用）
        if "ROEJQ" in df.columns:
            result["roe_ttm"] = pd.to_numeric(df["ROEJQ"], errors="coerce")

        # EPS（TTM，元/股）
        if "EPSJB" in df.columns:
            result["eps_ttm"] = pd.to_numeric(df["EPSJB"], errors="coerce")

        # 净利润 YoY（AkShare 直接提供）
        if "NETPROFITRPHBZC" in df.columns:
            result["profit_yoy"] = pd.to_numeric(df["NETPROFITRPHBZC"], errors="coerce")

        # 营收 YoY（AkShare 无直接字段，从 TOTALOPERATEREVE 自算）
        # 注意：营收为 0 或 NaN 时不做填充，让 NaN 传播，因子会正确降级
        if "TOTALOPERATEREVE" in df.columns:
            rev = pd.to_numeric(df["TOTALOPERATEREVE"], errors="coerce")
            rev_prev = rev.shift(1)  # 上一年同期
            yoy = ((rev / rev_prev.replace(0, np.nan)) - 1) * 100
            result["revenue_yoy"] = yoy.replace([np.inf, -np.inf], np.nan)

        # 以下字段在此数据源中不可得：
        #   pe_ttm, pb, ocf_to_profit, holder_num
        # 其对应因子在 financial_data 缺失这些字段时返回零值，这是预期行为。

        if not result:
            return pd.DataFrame()

        quarterly = pd.DataFrame(result).sort_index()
        quarterly = quarterly[~quarterly.index.duplicated(keep="last")]

        # 季频 → 日频（前向填充）
        start_dt = pd.Timestamp(start) if start else quarterly.index.min()
        end_dt = pd.Timestamp(end) if end else pd.Timestamp.now()
        daily_idx = pd.bdate_range(start=start_dt, end=end_dt)  # 工作日

        daily = quarterly.reindex(daily_idx)
        daily = daily.ffill()

        return daily


__all__ = ["AkshareProvider"]

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

from ..capabilities import Capability, MacroIndicator, Market, ProviderCapability
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

    def supports(self, capability: Capability, market) -> bool:
        """AkShare 的 FUNDAMENTALS / FUNDAMENTALS_HISTORY / MACRO 对所有市场均可用。

        AkShare 的财报和宏观数据跨 A/H 股，声明 Market.GLOBAL 是诚实声明，
        但基类默认的精确匹配不认 GLOBAL × 具体市场（如 HK），
        所以这里显式 override——只影响 AkShareProvider，不影响其他 Provider。
        """
        if not super().supports(capability, market):
            return False
        # GLOBAL provider 额外放行所有具体市场（财报/宏观不区分交易所）
        if Market.GLOBAL in self.declare().markets:
            return True
        return market in self.declare().markets

    def fetch_macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        """支持 indicator: MacroIndicator.PMI / M2 / CREDIT。"""
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 macro 请求")
            return pd.DataFrame()

        try:
            if indicator == MacroIndicator.PMI:
                return self._fetch_pmi(ak)
            if indicator == MacroIndicator.M2:
                return self._fetch_m2(ak)
            if indicator == MacroIndicator.CREDIT:
                return self._fetch_credit(ak)
        except Exception as exc:
            raise ProviderError(f"akshare.fetch_macro({indicator}): {exc}") from exc

        return pd.DataFrame()

    def fetch_fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        """从 stock_financial_abstract（A股）或 stock_hk_financial_indicator_em（港股）提取基本面快照。

        pe_ttm/pb 由 gateway 层通过腾讯实时行情补充（见 gateway.fundamentals()）。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 fundamentals 请求")
            return None

        # 区分 A 股和港股
        if self._is_hk_symbol(symbol):
            return self._fetch_hk_fundamentals(symbol, ak)

        # A 股路径（原逻辑）
        return self._fetch_a_share_fundamentals(symbol, ak)

    def _is_hk_symbol(self, symbol: str) -> bool:
        """判断是否为港股代码。"""
        s = symbol.strip().upper()
        return (
            s.startswith("HK")
            or s.endswith(".HK")
            or s.startswith("HK:")
            or (s.isdigit() and len(s) <= 5)
        )

    def _fetch_hk_fundamentals(self, symbol: str, ak) -> Optional[Fundamentals]:
        """通过 stock_hk_financial_indicator_em 获取港股基本面快照。"""
        # 提取纯数字代码（akshare HK 接口不接受 HK:/hk 前缀）
        code = self._normalize_hk_code(symbol)

        try:
            raw = ak.stock_hk_financial_indicator_em(symbol=code)
            if raw is None or raw.empty:
                return None
        except Exception as exc:
            raise ProviderError(f"akshare._fetch_hk_fundamentals({symbol}): {exc}") from exc

        if len(raw) == 0:
            return None

        row = raw.iloc[0]

        def gv(col: str, default: float = 0.0) -> float:
            try:
                v = row[col]
                if v is None:
                    return default
                f = float(v)
                return f if f == f else default
            except (TypeError, ValueError):
                return default

        # 字段映射（均来自 stock_hk_financial_indicator_em）
        eps_ttm = gv("基本每股收益(元)")
        bps = gv("每股净资产(元)")
        roe_ttm = gv("股东权益回报率(%)")
        revenue_ttm = gv("营业总收入", 0.0)
        profit_ttm = gv("净利润", 0.0)
        revenue_qoq = gv("营业总收入滚动环比增长(%)")
        profit_qoq = gv("净利润滚动环比增长(%)")
        pe_ttm = gv("市盈率")
        pb = gv("市净率")
        dividend_yield = gv("股息率TTM(%)", 0.0)

        # 有意义的数据才返回
        if eps_ttm <= 0 and roe_ttm <= 0 and profit_ttm == 0:
            return None

        return Fundamentals(
            symbol=symbol,
            name=str(row.get("简称", row.get("SECURITY_NAME_ABBR", ""))),
            eps_ttm=eps_ttm,
            bps=bps,
            roe_ttm=roe_ttm,
            revenue_ttm=revenue_ttm,
            profit_ttm=profit_ttm,
            revenue_yoy=revenue_qoq,
            profit_yoy=profit_qoq,
            pe_ttm=pe_ttm,
            pb=pb,
            dividend_yield=dividend_yield,
            # 以下字段本接口不可得
            pe_static=0.0,
            ps_ttm=0.0,
            ocf_to_profit=0.0,
            market_cap=0.0,
            float_cap=0.0,
            industry="",
            sector="",
            timestamp=pd.Timestamp.now(),
        )

    @staticmethod
    def _normalize_hk_code(symbol: str) -> str:
        """将各类港股代码格式统一为 5 位带前导零的纯数字（akshare HK 接口格式）。

        akshare stock_hk_* 接口只认 5 位带前导零的代码（如 "00700"），
        不接受 hk00700 / HK:00700 / 00700.HK / 700 等格式。
        """
        s = symbol.strip().upper()
        # 剥掉市场前缀
        for prefix in ("HK:", "HK"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        # 剥掉 .HK 后缀
        if s.endswith(".HK"):
            s = s[:-3]
        # 补前导零到 5 位（港股代码固定 5 位）
        return s.zfill(5)

    def _fetch_a_share_fundamentals(self, symbol: str, ak) -> Optional[Fundamentals]:
        """通过 stock_financial_abstract 获取 A 股基本面快照。"""
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

        # OCF/净利润（现金流质量）
        ocf_net = get_val("常用指标", "经营现金流量净额", 0.0)
        if profit_ttm > 0:
            ocf_to_profit = ocf_net / profit_ttm
        else:
            ocf_to_profit = 0.0

        # 报告期
        try:
            period_str = str(latest_col).strip()
            report_ts = pd.Timestamp(
                year=int(period_str[:4]), month=int(period_str[4:6]), day=1
            )
        except Exception:
            report_ts = pd.Timestamp.now()

        if eps_ttm <= 0 and roe_ttm <= 0:
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
            pe_static=0.0,
            ps_ttm=0.0,
            bps=0.0,
            dividend_yield=0.0,
            market_cap=0.0,
            float_cap=0.0,
            industry="",
            sector="",
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
        """从 stock_financial_analysis_indicator_em（A 股）或
        stock_financial_hk_analysis_indicator_em（港股）获取财务历史时序。

        列映射（A股）：
          ROEJQ            → roe_ttm
          EPSJB            → eps_ttm
          NETPROFITRPHBZC  → profit_yoy
          TOTALOPERATEREVE → _revenue（自算营收 YoY）

        列映射（港股）：
          ROE_AVG          → roe_ttm
          EPS_TTM          → eps_ttm
          HOLDER_PROFIT_YOY → profit_yoy
          OPERATE_INCOME_YOY → revenue_yoy

        注意：pe_ttm / pb / ocf_to_profit / holder_num 在此数据源中不可得，
        对应因子在 financial_data 缺失这些字段时返回零值，这是已知数据层限制。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装,跳过 fundamentals_history 请求")
            return pd.DataFrame()

        if self._is_hk_symbol(symbol):
            return self._fetch_hk_fundamentals_history(symbol, ak, start, end)
        return self._fetch_a_share_fundamentals_history(symbol, ak, start, end)

    def _fetch_hk_fundamentals_history(
        self, symbol: str, ak, start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """通过 stock_financial_hk_analysis_indicator_em 获取港股财务历史（年频）。"""
        code_raw = symbol.upper()
        code = self._normalize_hk_code(code_raw)

        try:
            df = ak.stock_financial_hk_analysis_indicator_em(symbol=code)
        except Exception as exc:
            raise ProviderError(
                f"akshare._fetch_hk_fundamentals_history({symbol}): {exc}"
            ) from exc

        if df is None or df.empty:
            return pd.DataFrame()

        return self._normalize_hk_indicator_em(df, start, end)

    @staticmethod
    def _normalize_hk_indicator_em(
        df: pd.DataFrame, start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """将 stock_financial_hk_analysis_indicator_em DataFrame 标准化为日频时序。"""
        import numpy as np

        df = df.copy()

        # 优先使用 REPORT_DATE，否则尝试 FISCAL_YEAR
        date_col = None
        for col in ("REPORT_DATE", "FISCAL_YEAR", "START_DATE"):
            if col in df.columns:
                date_col = col
                break
        if date_col is None:
            return pd.DataFrame()

        df.index = pd.to_datetime(df[date_col], errors="coerce")
        df = df[~df.index.isna()].sort_index()
        if df.empty:
            return pd.DataFrame()

        result = {}

        # ROE（%，年频加权）
        if "ROE_AVG" in df.columns:
            result["roe_ttm"] = pd.to_numeric(df["ROE_AVG"], errors="coerce")

        # EPS TTM（元/股）
        if "EPS_TTM" in df.columns:
            result["eps_ttm"] = pd.to_numeric(df["EPS_TTM"], errors="coerce")

        # 归母净利润 YoY（AkShare 直接提供）
        if "HOLDER_PROFIT_YOY" in df.columns:
            result["profit_yoy"] = pd.to_numeric(df["HOLDER_PROFIT_YOY"], errors="coerce")

        # 营收 YoY（AkShare 直接提供）
        if "OPERATE_INCOME_YOY" in df.columns:
            result["revenue_yoy"] = pd.to_numeric(df["OPERATE_INCOME_YOY"], errors="coerce")

        if not result:
            return pd.DataFrame()

        quarterly = pd.DataFrame(result).sort_index()
        quarterly = quarterly[~quarterly.index.duplicated(keep="last")]

        # 年频 → 日频（前向填充）
        #
        # 经济假设：最新一期年报覆盖整个自然年，1 个数据点复制 252 个交易日。
        # 这适合趋势类因子（ROE/EPS 趋势），但不适合事件驱动类因子——
        # 真实场景中年报发布后数据应立即更新，这是当前实现的已知局限。
        start_dt = pd.Timestamp(start) if start else quarterly.index.min()
        end_dt = pd.Timestamp(end) if end else pd.Timestamp.now()
        daily_idx = pd.bdate_range(start=start_dt, end=end_dt)

        daily = quarterly.reindex(daily_idx)
        daily = daily.ffill()

        return daily

    def _fetch_a_share_fundamentals_history(
        self, symbol: str, ak, start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """从 stock_financial_analysis_indicator_em 获取 A 股财务历史，转换为日频 DataFrame。"""
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

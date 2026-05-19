# -*- coding: utf-8 -*-
"""
data_gateway.providers.baostock — baostock A股基本面 + 日K数据源

baostock (api.baostock.com) 是免费无需 Token 的A股数据API，特点：
  - 基本面数据完整：利润表 / 现金流 / 运营能力 / 杜邦分析
  - A股日K线稳定，作为腾讯/新浪的第三备源
  - 不支持港股/美股

能力矩阵：
  ┌────────────────┬──────┬───────┬──────┬──────┐
  │ 数据类型        │ A 股 │ INDEX │ HK   │ US   │
  ├────────────────┼──────┼───────┼──────┼──────┤
  │ KLINE_DAILY    │ ✓    │ ✗     │ ✗    │ ✗    │
  │ FUNDAMENTALS   │ ✓    │ ✗     │ ✗    │ ✗    │
  │ FUNDAMENTALS_HISTORY │ ✓ │ ✗    │ ✗    │ ✗    │
  └────────────────┴──────┴───────┴──────┴──────┘

字段覆盖：
  - KLINE_DAILY: date, open, high, low, close, volume, amount, adjustflag
  - FUNDAMENTALS: roe_ttm, eps_ttm, revenue_ttm, profit_ttm, roe (杜邦),
                  现金流指标, 运营指标
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Dict, Optional

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..schemas import BalanceSheet, Fundamentals, Quote
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.baostock")

# ─── Baostock 全局会话管理 ──────────────────────────────────────────
_bs_lock = threading.Lock()
_bs_session: Optional["_BaostockSession"] = None


class _BaostockSession:
    """baostock API 会话封装，处理 login/logout 生命周期。"""

    def __init__(self):
        import baostock as bs
        self._bs = bs
        self._logged_in = False
        self.login()

    def login(self):
        if self._logged_in:
            return
        result = self._bs.login()
        if result.error_msg != "success":
            raise ProviderError(f"baostock login failed: {result.error_msg}")
        self._logged_in = True
        logger.debug("baostock session opened")

    def logout(self):
        if not self._logged_in:
            return
        try:
            self._bs.logout()
        except Exception:
            pass
        self._logged_in = False
        logger.debug("baostock session closed")

    def is_login(self) -> bool:
        return self._logged_in

    def ensure_login(self):
        """必要时重连（baostock 会话有时限）。"""
        if not self._logged_in:
            self.login()


def _get_session() -> _BaostockSession:
    """获取全局 baostock 会话，单例模式。"""
    global _bs_session
    with _bs_lock:
        if _bs_session is None:
            _bs_session = _BaostockSession()
        else:
            try:
                _bs_session.ensure_login()
            except Exception:
                # 登录失败，重新创建会话
                try:
                    _bs_session.logout()
                except Exception:
                    pass
                _bs_session = _BaostockSession()
        return _bs_session


def _symbol_to_bs(code: str) -> str:
    """将系统标准化代码转为 baostock 格式。

    baostock 使用 'sh.600519' / 'sz.000001' 格式。
    支持 A股（sh/sz），不支持港股/美股/指数。
    """
    code = code.strip().lower()
    # 已带 sh./sz. 前缀，直接返回
    if code.startswith("sh."):
        return code
    if code.startswith("sz."):
        return code
    if code.startswith("sh"):
        return f"sh.{code[2:]}"
    if code.startswith("sz"):
        return f"sz.{code[2:]}"
    if code.startswith("60") or code.startswith("68"):
        return f"sh.{code}"
    if code.startswith("00") or code.startswith("30"):
        return f"sz.{code}"
    # 无法识别，返回原格式碰运气
    return code


class BaostockProvider(Provider):
    """baostock A股基本面 + 日K provider。"""

    name = "baostock"

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.KLINE_DAILY,
                Capability.FUNDAMENTALS,
                Capability.FUNDAMENTALS_HISTORY,
                Capability.BALANCE_SHEET,
            }),
            markets=frozenset({Market.A}),
            priority_hint=0.75,  # 稳定免费源，冷启动评分较高
        )

    def supports(self, capability: Capability, market) -> bool:
        if not super().supports(capability, market):
            return False
        # baostock 仅支持 A股
        return market in (Market.A, Market.INDEX) or market == Market.A

    def field_authority(self) -> Dict[Capability, Dict[str, float]]:
        # Baostock 是 A 股基本面主源(priority_hint=0.75)，权威高于 AkShare(备灾源)。
        # industry 是 Baostock 独家字段，声明 1.0 让其他源不会覆盖。
        return {
            Capability.FUNDAMENTALS: {
                "roe_ttm": 1.0, "eps_ttm": 1.0,
                "profit_yoy": 0.9, "industry": 1.0,
            },
        }

    # ─── KLINE_DAILY ─────────────────────────────────────────────────

    def fetch_kline_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
        **kwargs,
    ) -> pd.DataFrame:
        """获取A股日K线。

        Args:
            symbol: 标准化代码，如 'sh600519'
            days: 历史天数
            adjust: 复权类型（baostock 不支持，忽略）
            limit: 最大行数（baostock 不支持，忽略）

        Returns:
            DataFrame，列: date, open, high, low, close, volume, amount
        """
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = _offset_date(end_date, days)

        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_kline_daily %s (%s -> %s)", symbol, start_date, end_date)

        try:
            rs = session._bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",  # 不复权
            )
            if rs.error_msg != "success":
                raise ProviderError(f"baostock kline query failed: {rs.error_msg}")

            # rs.get_data() 直接返回完整 DataFrame，无需循环
            df = rs.get_data()
            if df is None or df.empty:
                return pd.DataFrame()
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            return df.sort_values("timestamp").reset_index(drop=True)

        except Exception as exc:
            if "login" in str(exc).lower():
                try:
                    session.login()
                    return self.fetch_kline_daily(symbol, days, end_date)
                except Exception:
                    pass
            raise ProviderError(f"baostock kline fetch failed: {exc}") from exc

    # ─── FUNDAMENTALS ───────────────────────────────────────────────

    def fetch_fundamentals(self, symbol: str) -> Fundamentals:
        """获取A股基本面快照（最新一期财报）。

        策略：优先取最新一期数据（通常是当年Q1，ROE/epsTTM为TTM值），
        若营收(单季为空)则往后找最近有值期。
        baostock quarterly profit 表中 Q1 单季营收通常为空（年报才有完整数据），
        所以 revenue 从最近有值期取。
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_fundamentals %s", symbol)

        try:
            profit_df = self._fetch_profit_all(session, bs_code)
            cashflow = self._fetch_cashflow(session, bs_code)
            operation = self._fetch_operation(session, bs_code)
            dupont = self._fetch_dupont(session, bs_code)
            growth = self._fetch_growth(session, bs_code)
            industry = self._fetch_industry(session, bs_code)
        except Exception as exc:
            raise ProviderError(f"baostock fundamentals fetch failed: {exc}") from exc

        if profit_df is None or profit_df.empty:
            return Fundamentals(symbol=symbol)

        # 取最新一期（第一行即最新）
        row = profit_df.iloc[0]
        name = self._fetch_stock_name(session, bs_code)

        # ROE：始终为小数（0.105687 = 10.57%），×100 转百分比
        roe_val = _safe_float(row.get("roeAvg")) * 100

        # revenue：Q1 通常为空，跳过找最近有值期
        revenue_val = 0.0
        for _, r in profit_df.iterrows():
            mb = _safe_float(r.get("MBRevenue"))
            if mb > 0:
                revenue_val = mb  # baostock MBRevenue 已是元，无需转换
                break

        # epsTTM：baostock 的 epsTTM 字段已经是 TTM 值（年报口径）
        eps_ttm = _safe_float(row.get("epsTTM"))
        # 若 epsTTM 为空（偶发），用 netProfit / totalShare 近似
        if eps_ttm == 0:
            net_profit = _safe_float(row.get("netProfit"))
            total_share = _safe_float(row.get("totalShare"))
            if total_share > 0:
                eps_ttm = net_profit / total_share

        fundamentals = Fundamentals(
            symbol=symbol,
            name=name,
            eps_ttm=eps_ttm,
            roe_ttm=roe_val,
            profit_ttm=_safe_float(row.get("netProfit")),
            revenue_ttm=revenue_val,
            industry=industry,
        )

        # 现金流
        if cashflow is not None and not cashflow.empty:
            cf_row = cashflow.iloc[0]
            fundamentals.ocf_to_profit = _safe_float(cf_row.get("CFOToNP"))

        # YoY 增速（来自 growth_data，小数格式如 -0.045049 = -4.5%，无需 ×100）
        if growth is not None and not growth.empty:
            g_row = growth.iloc[0]
            fundamentals.profit_yoy = _safe_float(g_row.get("YOYNI"))
            fundamentals.eps_yoy = _safe_float(g_row.get("YOYEPSBasic"))
            fundamentals.asset_yoy = _safe_float(g_row.get("YOYAsset"))

        # 营收 YoY：找最近两个同季度对比（通常用年报 Q4 vs Q4）
        if len(profit_df) >= 2:
            cur_row = profit_df.iloc[0]
            for _, prev_row in profit_df.iloc[1:].iterrows():
                # 只对比同季度（如 Q4 vs Q4）
                if prev_row.get("statDate", "")[5:7] == cur_row.get("statDate", "")[5:7]:
                    mb_cur = _safe_float(cur_row.get("MBRevenue"))
                    mb_prev = _safe_float(prev_row.get("MBRevenue"))
                    if mb_cur > 0 and mb_prev > 0:
                        fundamentals.revenue_yoy = (mb_cur - mb_prev) / mb_prev * 100
                        break

        return fundamentals

    def _fetch_profit_all(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        """拉取近4年全部利润表（用于找最近有值期+计算YoY）。返回按时间降序排列的 DataFrame。"""
        all_rows = []
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_profit_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        all_rows.append(df)
        if not all_rows:
            return pd.DataFrame()
        result = pd.concat(all_rows, ignore_index=True)
        # 按 statDate 降序排列（最新期在前）
        result["_sort_date"] = pd.to_datetime(result["statDate"], errors="coerce")
        result = result.sort_values("_sort_date", ascending=False).drop(columns=["_sort_date"])
        return result.reset_index(drop=True)

    def _fetch_cashflow(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_cash_flow_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        return df
        return pd.DataFrame()

    def _fetch_operation(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_operation_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        return df
        return pd.DataFrame()

    def _fetch_dupont(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_dupont_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        return df
        return pd.DataFrame()

    def _fetch_growth(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        """拉取最新一期 growth_data（YoY 增速）。"""
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_growth_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        return df
        return pd.DataFrame()

    def _fetch_balance(self, session: _BaostockSession, bs_code: str) -> pd.DataFrame:
        """拉取最新一期 balance_data（资产负债）。"""
        year = datetime.now().year
        for offset in range(4):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_balance_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        return df
        return pd.DataFrame()

    def _fetch_balance_history(
        self, session: _BaostockSession, bs_code: str, years: int = 4,
    ) -> pd.DataFrame:
        """拉取近 N 年全部 balance_data 季度数据(用于构建日频时序)。"""
        all_rows = []
        year = datetime.now().year
        for offset in range(years):
            y = year - offset
            for q in [4, 3, 2, 1]:
                rs = session._bs.query_balance_data(bs_code, year=y, quarter=q)
                if rs.error_msg == "success":
                    df = rs.get_data()
                    if df is not None and not df.empty:
                        all_rows.append(df)
        if not all_rows:
            return pd.DataFrame()
        return pd.concat(all_rows, ignore_index=True)

    def _fetch_industry(self, session: _BaostockSession, bs_code: str) -> str:
        """通过 stock_industry 查询行业分类。"""
        try:
            rs = session._bs.query_stock_industry(code=bs_code)
            if rs.error_msg == "success":
                df = rs.get_data()
                if df is not None and not df.empty:
                    return str(df.iloc[0].get("industry", "") or "")
        except Exception:
            pass
        return ""

    def fetch_balance_sheet(self, symbol: str) -> BalanceSheet:
        """获取 A股资产负债表快照。"""
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_balance_sheet %s", symbol)

        try:
            df = self._fetch_balance(session, bs_code)
        except Exception as exc:
            raise ProviderError(f"baostock balance_sheet fetch failed: {exc}") from exc

        if df is None or df.empty:
            return BalanceSheet(symbol=symbol)

        row = df.iloc[0]
        # balance_data 只有比率字段，无绝对值
        return BalanceSheet(
            symbol=symbol,
            total_asset=0.0,  # balance_data 不提供绝对值
            total_liability=0.0,
            debt_to_equity=_safe_float(row.get("liabilityToAsset")) * 100,  # 小数→百分比
            current_ratio=_safe_float(row.get("currentRatio")),
            quick_ratio=_safe_float(row.get("quickRatio")),
            equity=0.0,  # assetToEquity 是杠杆倍数，不是股东权益金额
        )

    # ─── FUNDAMENTALS_HISTORY ────────────────────────────────────────────────

    def fetch_fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """A股财务历史时序(日频,前向填充季报)。

        当前仅输出 balance sheet 衍生字段(W1-2),与 AkshareProvider 提供的
        利润表字段(roe_ttm/eps_ttm/...)互为字段级互补。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex(工作日),列:
              debt_to_equity (%)   - liabilityToAsset × 100
              current_ratio        - 流动比率
              quick_ratio          - 速动比率
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        try:
            raw = self._fetch_balance_history(session, bs_code)
        except Exception as exc:
            raise ProviderError(
                f"baostock fetch_fundamentals_history({symbol}): {exc}"
            ) from exc

        if raw is None or raw.empty:
            return pd.DataFrame()

        return self._normalize_balance_history(raw, start, end)

    @staticmethod
    def _normalize_balance_history(
        raw: pd.DataFrame, start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """把 baostock balance_data 多季度 DataFrame 标准化为日频时序。"""
        df = raw.copy()
        if "statDate" not in df.columns:
            return pd.DataFrame()
        df["_dt"] = pd.to_datetime(df["statDate"], errors="coerce")
        df = df.dropna(subset=["_dt"]).sort_values("_dt")
        if df.empty:
            return pd.DataFrame()
        df = df.drop_duplicates(subset=["_dt"], keep="last").set_index("_dt")

        result = {}
        if "liabilityToAsset" in df.columns:
            # baostock 给出小数(0.5 = 50%),统一转 %
            result["debt_to_equity"] = pd.to_numeric(
                df["liabilityToAsset"], errors="coerce",
            ) * 100
        if "currentRatio" in df.columns:
            result["current_ratio"] = pd.to_numeric(
                df["currentRatio"], errors="coerce",
            )
        if "quickRatio" in df.columns:
            result["quick_ratio"] = pd.to_numeric(
                df["quickRatio"], errors="coerce",
            )

        if not result:
            return pd.DataFrame()

        quarterly = pd.DataFrame(result).sort_index()

        # 季频 → 日频(union-reindex-ffill,避免季末是周末时丢值)
        start_dt = pd.Timestamp(start) if start else quarterly.index.min()
        end_dt = pd.Timestamp(end) if end else pd.Timestamp.now()
        daily_idx = pd.bdate_range(start=start_dt, end=end_dt)

        union_idx = quarterly.index.union(daily_idx).sort_values()
        daily = quarterly.reindex(union_idx).ffill().reindex(daily_idx)
        return daily

    def _fetch_stock_name(self, session: _BaostockSession, bs_code: str) -> str:
        """通过 stock_basic 查询股票名称。"""
        try:
            rs = session._bs.query_stock_basic(code=bs_code)
            if rs.error_msg == "success":
                df = rs.get_data()
                if df is not None and not df.empty:
                    return df.iloc[0].get("code_name", "") or ""
        except Exception:
            pass
        return ""


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _offset_date(end_date: str, offset_days: int) -> str:
    """从 end_date 往前推 offset_days 天，返回 YYYY-MM-DD。"""
    try:
        end = pd.Timestamp(end_date)
        start = end - pd.Timedelta(days=offset_days)
        return start.strftime("%Y-%m-%d")
    except Exception:
        # 回退逻辑
        from datetime import timedelta
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        dt -= timedelta(days=offset_days)
        return dt.strftime("%Y-%m-%d")


__all__ = ["BaostockProvider"]

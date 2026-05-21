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
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from datetime import timedelta

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..schemas import BalanceSheet, DividendRecord, DupontMetrics, Fundamentals, IndustryClassification, IndexConstituent, OperationMetrics, Quote
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.baostock")

# baostock adjustflag: "1"=后复权, "2"=前复权, "3"=不复权
_ADJUST_TO_FLAG: Dict[str, str] = {
    "qfq": "2",
    "hfq": "1",
    "none": "3",
    "no": "3",
}

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
                Capability.DUPONT,
                Capability.OPERATION,
                Capability.DIVIDEND,
                Capability.INDUSTRY_CLASSIFICATION,
                Capability.INDEX_CONSTITUENT,
                Capability.TRADE_CALENDAR,
            }),
            markets=frozenset({Market.A}),
            priority_hint=0.75,  # 稳定免费源，冷启动评分较高
        )

    def supports(self, capability: Capability, market) -> bool:
        if not super().supports(capability, market):
            return False
        # baostock 仅支持 A股
        return market in (Market.A, Market.INDEX)

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
    ) -> pd.DataFrame:
        """获取A股日K线。

        Args:
            symbol: 标准化代码，如 'sh600519'
            days: 历史天数
            adjust: 复权类型，qfq/hfq/none，未知值按 qfq 处理
            limit: 不生效（baostock 用 days 控制范围）

        Returns:
            DataFrame，列: date, open, high, low, close, volume, amount
        """
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = _offset_date(end_date, days)
        adjustflag = _ADJUST_TO_FLAG.get(adjust, "2")
        if adjust not in _ADJUST_TO_FLAG:
            logger.debug("未知 adjust=%s，按 qfq 处理", adjust)

        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug(
            "fetch_kline_daily %s (%s -> %s, adjust=%s)",
            symbol, start_date, end_date, adjust,
        )

        retried = False
        while True:
            try:
                rs = session._bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount,peTTM,pbMRQ,psTTM,pcfNcfTTM",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag=adjustflag,
                )
                if rs.error_msg != "success":
                    raise ProviderError(
                        f"baostock kline query failed: {rs.error_msg}"
                    )

                # rs.get_data() 直接返回完整 DataFrame，无需循环
                df = rs.get_data()
                if df is None or df.empty:
                    return pd.DataFrame()
                for col in ["open", "high", "low", "close", "volume", "amount",
                             "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.rename(columns={"date": "timestamp"})
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                return df.sort_values("timestamp").reset_index(drop=True)

            except Exception as exc:
                if not retried and "login" in str(exc).lower():
                    retried = True
                    try:
                        session.login()
                        continue
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

        # BPS = 归属母公司股东权益 / 总股本
        total_share = _safe_float(row.get("totalShare"))
        equity_attr = _safe_float(row.get("nIncomeAttrP"))  # 归属净利润，临时用
        # totalShare 单位是股（整数），MBRevenue/roeAvg 可间接算 BPS
        # 已知 roeAvg = 归属净利润 / 平均净资产 => 净资产 ≈ 归属净利润 / roeAvg
        roe_avg = _safe_float(row.get("roeAvg"))
        net_profit_attr = _safe_float(row.get("nIncomeAttrP"))
        if roe_avg > 0 and total_share > 0:
            equity_attr = net_profit_attr / roe_avg  # 估算平均净资产
            bps_val = equity_attr / total_share
        else:
            bps_val = 0.0

        fundamentals = Fundamentals(
            symbol=symbol,
            name=name,
            eps_ttm=eps_ttm,
            roe_ttm=roe_val,
            profit_ttm=_safe_float(row.get("netProfit")),
            revenue_ttm=revenue_val,
            industry=industry,
            net_margin=_safe_float(row.get("npMargin")) * 100,   # 小数→%
            gross_margin=_safe_float(row.get("gpMargin")) * 100,  # 小数→%
            bps=bps_val,
        )

        # 股息率：近12个月每股股利之和 / 当前股价（从 K 线 peTTM 反推）
        self._fill_dividend_yield(fundamentals, session, bs_code, eps_ttm)

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
            fundamentals.equity_yoy = _safe_float(g_row.get("YOYEquity"))
            fundamentals.pni_yoy = _safe_float(g_row.get("YOYPNI"))

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

    def fetch_dupont_metrics(self, symbol: str) -> "DupontMetrics":
        """获取 A股杜邦分析指标快照（ROE 三拆解）。

        Baostock query_dupont_data 字段：
          dupontROE(dupontNetMargin × dupontAssetTurn × dupontAssetStoEquity)
          dupontNetMargin（净利率，%）
          dupontAssetTurn（总资产周转率，次）
          dupontAssetStoEquity（权益乘数，倍）
          dupontTaxBurden（税负，%）
          dupontIntburden（利息负担，%）
          dupontEbittogr（EBIT/营收，%）
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_dupont_metrics %s", symbol)

        try:
            df = self._fetch_dupont(session, bs_code)
        except Exception as exc:
            raise ProviderError(f"baostock dupont fetch failed: {exc}") from exc

        if df is None or df.empty:
            return DupontMetrics(symbol=symbol)

        row = df.iloc[0]
        return DupontMetrics(
            symbol=symbol,
            roe=_safe_float(row.get("dupontROE")),
            net_margin=_safe_float(row.get("dupontNetMargin")),
            asset_turn=_safe_float(row.get("dupontAssetTurn")),
            equity_multiplier=_safe_float(row.get("dupontAssetStoEquity")),
            tax_burden=_safe_float(row.get("dupontTaxBurden")),
            int_burden=_safe_float(row.get("dupontIntburden")),
            ebit_to_revenue=_safe_float(row.get("dupontEbittogr")),
        )

    def fetch_operation_metrics(self, symbol: str) -> "OperationMetrics":
        """获取 A股运营能力指标快照。

        Baostock query_operation_data 字段：
          invTurnDays（存货周转天数）
          nrTurnDays（应收账款周转天数）
          assetTurnRatio（总资产周转率，次）
          caTurnRatio（流动资产周转率，次）
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_operation_metrics %s", symbol)

        try:
            df = self._fetch_operation(session, bs_code)
        except Exception as exc:
            raise ProviderError(f"baostock operation fetch failed: {exc}") from exc

        if df is None or df.empty:
            return OperationMetrics(symbol=symbol)

        row = df.iloc[0]
        return OperationMetrics(
            symbol=symbol,
            nr_turn_days=_safe_float(row.get("nrTurnDays")),
            inv_turn_days=_safe_float(row.get("invTurnDays")),
            asset_turn=_safe_float(row.get("assetTurnRatio")),
            ca_turn=_safe_float(row.get("caTurnRatio")),
        )

    # ─── FUNDAMENTALS_HISTORY ────────────────────────────────────────────────

    def _fetch_all_financials(
        self, session: _BaostockSession, bs_code: str, years: int = 4,
    ) -> Dict[str, pd.DataFrame]:
        """批量拉取近 N 年 6 张季频财务报表，返回 dict[表名, DataFrame]。"""
        tables: Dict[str, pd.DataFrame] = {}
        year = datetime.now().year

        fetchers = {
            "profit": session._bs.query_profit_data,
            "cashflow": session._bs.query_cash_flow_data,
            "operation": session._bs.query_operation_data,
            "dupont": session._bs.query_dupont_data,
            "growth": session._bs.query_growth_data,
            "balance": session._bs.query_balance_data,
        }

        for name, fetcher in fetchers.items():
            all_rows = []
            for offset in range(years):
                y = year - offset
                for q in [4, 3, 2, 1]:
                    rs = fetcher(bs_code, year=y, quarter=q)
                    if rs.error_msg == "success":
                        df = rs.get_data()
                        if df is not None and not df.empty:
                            all_rows.append(df)
            if all_rows:
                tables[name] = pd.concat(all_rows, ignore_index=True)
        return tables

    def _normalize_financial_history(
        self, tables: Dict[str, pd.DataFrame], start: str | None, end: str | None,
    ) -> pd.DataFrame:
        """将多张季频财报 DataFrame 合并归一化为日频前向填充序列。

        输出列（来自 Baostock 六表）:
          gross_margin   %   销售毛利率     profit.gpMargin
          net_margin     %   销售净利率     profit.npMargin
          eps_ttm              每股收益TTM  profit.epsTTM
          roe_ttm        %   ROE(平均)     profit.roeAvg
          revenue_ttm     元  主营收入      profit.MBRevenue
          profit_ttm      元  净利润        profit.netProfit
          debt_to_equity  %   资产负债率    balance.liabilityToAsset
          current_ratio         流动比率      balance.currentRatio
          quick_ratio           速动比率      balance.quickRatio
          cfo_to_profit         CFO/净利润   cashflow.CFOToNP
          cfo_to_revenue        CFO/营收     cashflow.CFOToOR
          asset_turn      次   总资产周转率  operation.assetTurnRatio
          inv_turn_days   天   存货周转天数  operation.INVTurnDays
          nr_turn_days    天   应收周转天数  operation.NRTurnDays
          equity_yoy      %   净资产同比    growth.YOYEquity
          profit_yoy      %   净利润同比    growth.YOYNI
          revenue_yoy     %   营收同比      (自算)
          dupont_roe      %   杜邦ROE       dupont.dupontROE
          equity_multiplier     权益乘数     dupont.dupontAssetStoEquity
        """
        dfs = {}
        for name, raw in tables.items():
            if raw is None or raw.empty or "statDate" not in raw.columns:
                continue
            df = raw.copy()
            df["_dt"] = pd.to_datetime(df["statDate"], errors="coerce")
            df = df.dropna(subset=["_dt"]).sort_values("_dt")
            df = df.drop_duplicates(subset=["_dt"], keep="last").set_index("_dt")
            dfs[name] = df

        if not dfs:
            return pd.DataFrame()

        # 逐表提取目标字段
        result: Dict[str, pd.Series] = {}

        if "profit" in dfs:
            p = dfs["profit"]
            for col, out in [
                ("gpMargin", "gross_margin"),
                ("npMargin", "net_margin"),
                ("epsTTM", "eps_ttm"),
                ("roeAvg", "roe_ttm"),
                ("MBRevenue", "revenue_ttm"),
                ("netProfit", "profit_ttm"),
            ]:
                if col in p.columns:
                    s = pd.to_numeric(p[col], errors="coerce")
                    if out in ("gross_margin", "net_margin", "roe_ttm"):
                        s = s * 100
                    result[out] = s

        if "balance" in dfs:
            b = dfs["balance"]
            for col, out in [
                ("liabilityToAsset", "debt_to_equity"),
                ("currentRatio", "current_ratio"),
                ("quickRatio", "quick_ratio"),
            ]:
                if col in b.columns:
                    s = pd.to_numeric(b[col], errors="coerce")
                    if out == "debt_to_equity":
                        s = s * 100
                    result[out] = s

        if "cashflow" in dfs:
            c = dfs["cashflow"]
            for col, out in [
                ("CFOToNP", "cfo_to_profit"),
                ("CFOToOR", "cfo_to_revenue"),
            ]:
                if col in c.columns:
                    result[out] = pd.to_numeric(c[col], errors="coerce")

        if "operation" in dfs:
            o = dfs["operation"]
            for col, out in [
                ("AssetTurnRatio", "asset_turn"),
                ("INVTurnDays", "inv_turn_days"),
                ("NRTurnDays", "nr_turn_days"),
            ]:
                if col in o.columns:
                    result[out] = pd.to_numeric(o[col], errors="coerce")

        if "growth" in dfs:
            g = dfs["growth"]
            for col, out in [
                ("YOYEquity", "equity_yoy"),
                ("YOYNI", "profit_yoy"),
                ("YOYPNI", "pni_yoy"),
            ]:
                if col in g.columns:
                    result[out] = pd.to_numeric(g[col], errors="coerce") * 100

        if "dupont" in dfs:
            d = dfs["dupont"]
            for col, out in [
                ("dupontROE", "dupont_roe"),
                ("dupontAssetStoEquity", "equity_multiplier"),
                ("dupontTaxBurden", "tax_burden"),
                ("dupontIntburden", "int_burden"),
                ("dupontEbittogr", "ebit_to_revenue"),
            ]:
                if col in d.columns:
                    s = pd.to_numeric(d[col], errors="coerce")
                    if out in ("dupont_roe", "tax_burden", "int_burden", "ebit_to_revenue"):
                        s = s * 100
                    result[out] = s

        # revenue_yoy：自算同期比（Q4 vs Q4）
        if "profit" in dfs:
            p = dfs["profit"].copy()
            p = p[p["MBRevenue"].notna()]
            p["MBRevenue"] = pd.to_numeric(p["MBRevenue"], errors="coerce")
            p = p.dropna(subset=["MBRevenue"])
            p = p.sort_index()
            if not p.empty and len(p) >= 2:
                p["rev_yoy"] = p["MBRevenue"].pct_change(periods=4) * 100
                p = p.dropna(subset=["rev_yoy"])
                if not p.empty:
                    result["revenue_yoy"] = p["rev_yoy"]

        if not result:
            return pd.DataFrame()

        quarterly = pd.DataFrame(result).sort_index()
        quarterly = quarterly.drop_duplicates(keep="last")

        # 季频 → 日频
        start_dt = pd.Timestamp(start) if start else quarterly.index.min()
        end_dt = pd.Timestamp(end) if end else pd.Timestamp.now()
        daily_idx = pd.bdate_range(start=start_dt, end=end_dt)
        union_idx = quarterly.index.union(daily_idx).sort_values()
        daily = quarterly.reindex(union_idx).ffill().reindex(daily_idx)
        return daily

    def fetch_fundamentals_history(
        self, symbol: str, start: str | None = None, end: str | None = None,
    ) -> pd.DataFrame:
        """A股财务历史时序（日频，前向填充季报）。

        Baostock 六表全量输出：profit / cashflow / operation / dupont / growth / balance。
        与 AkshareProvider 的 roe_ttm/eps_ttm/... 字段级互补。

        Returns
        -------
        pd.DataFrame
            DatetimeIndex（工作日），列: gross_margin, net_margin, eps_ttm, roe_ttm,
            revenue_ttm, profit_ttm, debt_to_equity, current_ratio, quick_ratio,
            cfo_to_profit, cfo_to_revenue, asset_turn, inv_turn_days, nr_turn_days,
            equity_yoy, profit_yoy, revenue_yoy, dupont_roe, equity_multiplier
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        try:
            tables = self._fetch_all_financials(session, bs_code)
        except Exception as exc:
            raise ProviderError(
                f"baostock fetch_fundamentals_history({symbol}): {exc}"
            ) from exc

        if not tables:
            return pd.DataFrame()

        return self._normalize_financial_history(tables, start, end)

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

    def _fill_dividend_yield(
        self, fundamentals: Fundamentals, session: _BaostockSession,
        bs_code: str, eps_ttm: float,
    ):
        """根据近4期除权除息记录计算股息率，写入 fundamentals.dividend_yield。

        股息率 = 近4期每股税前股利之和 / 当前股价 × 100
        当前股价 = pe_ttm × eps_ttm（pe_ttm 从最新日 K 线取）。
        """
        try:
            # 取最近1天 K 线拿到 peTTM，然后反推股价
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = _offset_date(end_date, 5)
            rs = session._bs.query_history_k_data_plus(
                bs_code,
                "date,close,peTTM",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",  # 不复权
            )
            if rs.error_msg != "success":
                return
            df_kline = rs.get_data()
            if df_kline is None or df_kline.empty:
                return
            latest = df_kline.iloc[0]
            pe_ttm = _safe_float(latest.get("peTTM"))
            if pe_ttm <= 0:
                pe_ttm = _safe_float(latest.get("close"))
                if pe_ttm <= 0 or eps_ttm <= 0:
                    return
                # 没有 PE，用 eps 反推作罢（此时 pe = price/eps）
                price = pe_ttm
            else:
                price = pe_ttm * eps_ttm
            if price <= 0:
                return

            # 拉取近4年除权除息数据（每期年报/中报最多一条）
            total_cash_per_share = 0.0
            count = 0
            year = datetime.now().year
            for offset in range(4):
                y = str(year - offset)
                rs_div = session._bs.query_dividend_data(bs_code, year=y, yearType="operate")
                if rs_div.error_msg == "success":
                    df_div = rs_div.get_data()
                    if df_div is not None and not df_div.empty:
                        for _, div_row in df_div.iterrows():
                            cps = _safe_float(div_row.get("dividCashPsBeforeTax"))
                            if cps > 0:
                                total_cash_per_share += cps
                                count += 1
                                if count >= 4:
                                    break
                if count >= 4:
                    break

            if total_cash_per_share > 0 and price > 0:
                fundamentals.dividend_yield = total_cash_per_share / price * 100
        except Exception:
            pass

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

    def fetch_dividend(self, symbol: str, year: int | None = None) -> List[DividendRecord]:
        """获取A股股票指定年份的分红记录列表。

        Args:
            symbol: 标准化代码，如 'sh600519'
            year: 分红年份，默认为最近4年。None 表示最近4年。

        Returns:
            List[DividendRecord]，按除权除息日倒序。
            空列表表示无分红记录或查询失败。
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_dividend %s year=%s", symbol, year)

        records: List[DividendRecord] = []
        years_to_query = [year] if year else list(range(datetime.now().year, datetime.now().year - 4, -1))

        for y in years_to_query:
            try:
                rs = session._bs.query_dividend_data(bs_code, year=str(y), yearType="operate")
                if rs.error_msg != "success":
                    continue
                df = rs.get_data()
                if df is None or df.empty:
                    continue

                for _, row in df.iterrows():
                    # 实际 baostock query_dividend_data 字段（已验证 sh.600519 2023/2024）
                    cash = _safe_float(row.get("dividCashPsBeforeTax"))
                    stock = _safe_float(row.get("dividStocksPs"))
                    reserve = _safe_float(row.get("dividReserveToStockPs"))

                    # 至少有一项分红才算有效记录
                    if cash <= 0 and stock <= 0 and reserve <= 0:
                        continue

                    record = DividendRecord(
                        symbol=symbol,
                        plan_announce_date=_parse_date(row.get("dividPlanAnnounceDate")),
                        operate_date=_parse_date(row.get("dividOperateDate")),
                        pay_date=_parse_date(row.get("dividPayDate")),
                        stock_market_date=_parse_date(row.get("dividStockMarketDate")),
                        cash_per_share=cash,
                        stock_per_share=stock,
                        reserve_to_stock=reserve,
                    )
                    records.append(record)

            except Exception as exc:
                logger.debug("fetch_dividend %s year=%s failed: %s", symbol, y, exc)
                continue

        # 按除权除息日倒序（datetime 类型可直接比较）
        records.sort(key=lambda r: r.operate_date or datetime.min, reverse=True)
        return records

    def fetch_industry_classification(self, symbol: str) -> Optional[IndustryClassification]:
        """获取A股股票的行业分类信息。

        调用 Baostock query_stock_industry（全市场接口，一次返回所有股票，
        内部按 code 过滤目标股票）。

        Returns
        -------
        IndustryClassification | None
            无行业数据时返回 None（如股票代码不在 baostock 数据库中）。
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        bs_code = _symbol_to_bs(symbol)
        logger.debug("fetch_industry_classification %s", symbol)

        try:
            rs = session._bs.query_stock_industry()
            if rs.error_msg != "success":
                return None

            df = rs.get_data()
            if df is None or df.empty:
                return None

            # 按 code 过滤目标股票
            matched = df[df["code"] == bs_code]
            if matched.empty:
                return None

            row = matched.iloc[0]
            return IndustryClassification(
                symbol=symbol,
                code_name=str(row.get("code_name") or ""),
                industry=str(row.get("industry") or ""),
                classification=str(row.get("industryClassification") or ""),
                update_date=str(row.get("updateDate") or ""),
            )

        except Exception as exc:
            logger.debug("fetch_industry_classification %s failed: %s", symbol, exc)
            return None

    def fetch_index_constituents(self, index_code: str) -> List[IndexConstituent]:
        """获取指数成分股列表。

        Args:
            index_code: 指数代码，选项: 'hs300' / 'sz50' / 'zz500'

        Returns
        -------
        List[IndexConstituent]，按 code 排序。
        空列表表示无数据或查询失败。
        """
        _VALID_INDEX_CODES = ("hs300", "sz50", "zz500")
        if index_code not in _VALID_INDEX_CODES:
            logger.debug("fetch_index_constituents: invalid index_code=%s", index_code)
            return []

        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        logger.debug("fetch_index_constituents %s", index_code)

        query_method = {
            "hs300": session._bs.query_hs300_stocks,
            "sz50": session._bs.query_sz50_stocks,
            "zz500": session._bs.query_zz500_stocks,
        }[index_code]

        try:
            rs = query_method()
            if rs.error_msg != "success":
                return []
            df = rs.get_data()
            if df is None or df.empty:
                return []

            records: List[IndexConstituent] = []
            for _, row in df.iterrows():
                code = str(row.get("code") or "")
                if not code:
                    continue
                # baostock code 格式: 'sh.600519' → 标准化为 'sh600519'
                symbol = code.replace(".", "")
                records.append(IndexConstituent(
                    index_code=index_code,
                    symbol=symbol,
                    code_name=str(row.get("code_name") or ""),
                    update_date=str(row.get("updateDate") or ""),
                ))
            return sorted(records, key=lambda r: r.symbol)

        except Exception as exc:
            logger.debug("fetch_index_constituents %s failed: %s", index_code, exc)
            return []

    def fetch_trade_calendar(
        self, start_date: str, end_date: str,
    ) -> pd.DataFrame:
        """获取交易日历。

        调用 Baostock query_trade_dates，返回指定日期范围内的交易日信息。

        Args:
            start_date: 起始日期，格式 'YYYY-MM-DD'
            end_date: 结束日期，格式 'YYYY-MM-DD'

        Returns
        -------
        pd.DataFrame
            列: calendar_date（日期）, is_trading_day（'1'=交易日 '0'=非交易日）
            空 DataFrame 表示查询失败。
        """
        try:
            session = _get_session()
        except Exception as exc:
            raise ProviderError(f"baostock 会话获取失败: {exc}") from exc

        logger.debug("fetch_trade_calendar %s ~ %s", start_date, end_date)

        try:
            rs = session._bs.query_trade_dates(
                start_date=start_date, end_date=end_date,
            )
            if rs.error_msg != "success":
                return pd.DataFrame(columns=["calendar_date", "is_trading_day"])

            df = rs.get_data()
            if df is None or df.empty:
                return pd.DataFrame(columns=["calendar_date", "is_trading_day"])

            # 只保留必要列并排序
            df = df[["calendar_date", "is_trading_day"]].copy()
            df = df.sort_values("calendar_date").reset_index(drop=True)
            return df

        except Exception as exc:
            logger.debug("fetch_trade_calendar %s~%s failed: %s", start_date, end_date, exc)
            return pd.DataFrame(columns=["calendar_date", "is_trading_day"])


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_date(date_str) -> Optional[datetime]:
    """将 baostock 返回的日期字符串解析为 datetime（支持多种格式）。"""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _offset_date(end_date: str, offset_days: int) -> str:
    """从 end_date 往前推 offset_days 天，返回 YYYY-MM-DD。"""
    try:
        end = pd.Timestamp(end_date)
        start = end - pd.Timedelta(days=offset_days)
        return start.strftime("%Y-%m-%d")
    except Exception:
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        dt -= timedelta(days=offset_days)
        return dt.strftime("%Y-%m-%d")


__all__ = ["BaostockProvider"]

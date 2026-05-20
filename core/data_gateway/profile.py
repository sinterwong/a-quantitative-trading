# -*- coding: utf-8 -*-
"""data_gateway.profile — StockProfile 聚合视图构建器

抽出来的原因:
  - profile() + 5 个 helper 在 gateway.py 里占了 ~230 行,占主类 1/6 体积
    却是单一职责的"信息包组装",其它公开方法(quote / kline / fundamentals
    / ...)都不依赖它。
  - 把这块剥离让 gateway.py 回归"路由 + 公开 API"主线,不再混淆"如何
    并发拉所有 capability + 把 DataFrame 末行映射成 dataclass"的细节。

调用模式:
    from core.data_gateway.profile import build_profile
    prof = build_profile(self, symbol, headlines_n=10)

DataGateway.profile() 是这个函数的薄壳。helpers (\\_safe_call /
\\_df_to_margin_snapshot / ...) 都做成模块级函数,DataGateway 不再
背着它们做静态方法。

R2-4 review-fix: 拆 core/data_gateway/gateway.py(1370 行)的第一步。
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import as_completed
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

import pandas as pd

from .capabilities import MacroIndicator
from .schemas import (
    FundFlowSnapshot,
    MacroSnapshot,
    MarginSnapshot,
    StockProfile,
)

if TYPE_CHECKING:
    from .gateway import DataGateway


logger = logging.getLogger("data_gateway.profile")


def build_profile(
    gw: "DataGateway",
    symbol: str,
    *,
    headlines_n: int = 10,
) -> StockProfile:
    """聚合所有 capability 的"信息包":一次调用拿到该标的当前已知全部信息。

    并发触发以下切片拉取,任意切片失败不阻塞主流程:
      - quote / fundamentals / balance_sheet(dataclass)
      - margin_flow / fund_flow 时序末行 → MarginSnapshot / FundFlowSnapshot
      - news_headlines 列表(全市场快讯, n 条)
      - macro PMI/M2/CREDIT 末值 → MacroSnapshot

    Args:
        gw: DataGateway 实例(走它的公开 API + _get_profile_executor +
            _last_provenance)。
        symbol: 标的代码(如 'sh600519')
        headlines_n: 快讯条数上限

    Returns:
        StockProfile,含 completeness(0-1)和 provenance(每切片主源)。
    """
    # 并发触发所有切片,复用 gw._get_profile_executor()(与 gw._executor
    # 物理隔离,避免外层切片任务占满槽位、内层 _merged_fetch fan-out
    # 永远等不到 worker → deadlock)。
    ind_pmi = MacroIndicator.PMI
    ind_m2 = MacroIndicator.M2
    ind_credit = MacroIndicator.CREDIT

    pool = gw._get_profile_executor()
    results: Dict[str, Any] = {}
    futures: Dict[Any, str] = {
        pool.submit(_safe_call, gw.quote, symbol): "quote",
        pool.submit(_safe_call, gw.fundamentals, symbol): "fundamentals",
        pool.submit(_safe_call, gw.balance_sheet, symbol): "balance_sheet",
        pool.submit(_safe_call, gw.margin_flow, symbol): "margin_df",
        pool.submit(_safe_call, gw.fund_flow, symbol): "fund_df",
        pool.submit(_safe_call, gw.news_headlines, symbol, headlines_n): "headlines",
        pool.submit(_safe_call, gw.macro, ind_pmi): "macro_pmi",
        pool.submit(_safe_call, gw.macro, ind_m2): "macro_m2",
        pool.submit(_safe_call, gw.macro, ind_credit): "macro_credit",
    }
    for fut in as_completed(futures):
        slot = futures[fut]
        try:
            results[slot] = fut.result()
        except Exception as exc:
            logger.debug("profile slot %s 失败: %s", slot, exc)
            results[slot] = None

    prof = StockProfile(symbol=symbol)
    prof.quote = results.get("quote")
    prof.fundamentals = results.get("fundamentals")
    prof.balance_sheet = results.get("balance_sheet")
    prof.headlines = results.get("headlines") or []

    margin_df = results.get("margin_df")
    if isinstance(margin_df, pd.DataFrame) and not margin_df.empty:
        prof.margin = _df_to_margin_snapshot(margin_df)

    fund_df = results.get("fund_df")
    if isinstance(fund_df, pd.DataFrame) and not fund_df.empty:
        prof.fund_flow_latest = _df_to_fund_flow_snapshot(fund_df)

    pmi_df = results.get("macro_pmi")
    m2_df = results.get("macro_m2")
    credit_df = results.get("macro_credit")
    macro_snapshot = _build_macro_snapshot(pmi_df, m2_df, credit_df)
    if macro_snapshot is not None:
        prof.macro = macro_snapshot

    # provenance: 从 _last_provenance 中各切片缓存键提取首要源
    prof.provenance = _collect_profile_provenance(gw, symbol, prof)

    # completeness: 7 个切片(headlines 用是否非空计) 平均
    slots = [
        ("quote", prof.quote is not None),
        ("fundamentals", prof.fundamentals is not None),
        ("balance_sheet", prof.balance_sheet is not None),
        ("margin", prof.margin is not None),
        ("fund_flow", prof.fund_flow_latest is not None),
        ("headlines", bool(prof.headlines)),
        ("macro", prof.macro is not None),
    ]
    prof.completeness = sum(1 for _, ok in slots if ok) / len(slots)
    prof.missing_capabilities = [name for name, ok in slots if not ok]

    return prof


def _safe_call(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """profile 内并发切片包装:单切片异常不阻塞其他切片。"""
    try:
        return fn(*args, **kw)
    except Exception as exc:
        logger.debug("profile slice %s 失败: %s", getattr(fn, "__name__", fn), exc)
        return None


def _df_to_margin_snapshot(df: pd.DataFrame) -> MarginSnapshot:
    last = df.iloc[-1]
    idx = df.index[-1]
    ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.now()

    def f(col: str) -> float:
        if col not in df.columns:
            return 0.0
        try:
            v = float(last[col])
            return v if v == v else 0.0
        except (TypeError, ValueError):
            return 0.0

    return MarginSnapshot(
        date=ts,
        margin_balance=f("margin_balance"),
        net_buy=f("net_buy"),
        short_balance=f("short_balance"),
    )


def _df_to_fund_flow_snapshot(df: pd.DataFrame) -> FundFlowSnapshot:
    last = df.iloc[-1]
    idx = df.index[-1]
    ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.now()

    def f(col: str) -> float:
        if col not in df.columns:
            return 0.0
        try:
            v = float(last[col])
            return v if v == v else 0.0
        except (TypeError, ValueError):
            return 0.0

    return FundFlowSnapshot(
        date=ts,
        main_net_inflow=f("main_net_inflow"),
        super_net_inflow=f("super_net_inflow"),
        large_net_inflow=f("large_net_inflow"),
        medium_net_inflow=f("medium_net_inflow"),
        small_net_inflow=f("small_net_inflow"),
        main_net_ratio=f("main_net_ratio"),
    )


def _build_macro_snapshot(
    pmi_df: Optional[pd.DataFrame],
    m2_df: Optional[pd.DataFrame],
    credit_df: Optional[pd.DataFrame],
) -> Optional[MacroSnapshot]:
    def _last_numeric(df: Optional[pd.DataFrame], preferred_col: str) -> float:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0.0
        col = preferred_col if preferred_col in df.columns else (
            df.columns[0] if len(df.columns) else None
        )
        if col is None:
            return 0.0
        try:
            v = float(df[col].iloc[-1])
            return v if v == v else 0.0
        except (TypeError, ValueError, IndexError):
            return 0.0

    pmi = _last_numeric(pmi_df, "pmi")
    m2 = _last_numeric(m2_df, "m2_yoy")
    credit = _last_numeric(credit_df, "credit_yoy")
    if pmi == 0 and m2 == 0 and credit == 0:
        return None
    return MacroSnapshot(pmi=pmi, m2_yoy=m2, credit_yoy=credit)


def _collect_profile_provenance(
    gw: "DataGateway",
    symbol: str,
    prof: StockProfile,
) -> Dict[str, str]:
    """从 gw._last_provenance 抽取每个切片的主源(出现频率最高的 provider)。

    仅返回成功查到源的切片:未命中 / 无 provenance 的不出现,调用方
    可以放心地以 `provenance.get(slot)` 判断"是否知道这个切片来自哪"。
    """
    def primary(prov_dict: Dict[str, str]) -> str:
        if not prov_dict:
            return ""
        # 过滤掉 `<field>__divergence` 这类元数据键，避免把差异度值误当成 provider 名计数
        from .merge import DIVERGENCE_SUFFIX
        sources = [v for k, v in prov_dict.items() if not k.endswith(DIVERGENCE_SUFFIX)]
        if not sources:
            return ""
        counts = Counter(sources)
        return counts.most_common(1)[0][0]

    candidates: Dict[str, str] = {}
    if prof.quote is not None:
        candidates["quote"] = primary(gw.provenance(f"quote:{symbol}"))
    if prof.fundamentals is not None:
        candidates["fundamentals"] = primary(
            gw.provenance(f"fundamentals:{symbol}")
        )
    if prof.balance_sheet is not None:
        candidates["balance_sheet"] = primary(
            gw.provenance(f"balance_sheet:{symbol}")
        )
    if prof.margin is not None:
        # G4 起 FAILOVER 也写 {"_provider": name},primary 可正确返回源
        candidates["margin"] = primary(
            gw.provenance(f"margin_flow:{symbol}:None:None")
        )
    if prof.fund_flow_latest is not None:
        candidates["fund_flow"] = primary(gw.provenance(f"fund_flow:{symbol}"))
    if prof.headlines:
        # G3 后 news_headlines cache_key 不再含 n,可直接定位
        candidates["headlines"] = primary(
            gw.provenance(f"news_headlines:{symbol}")
        )
    if prof.macro is not None:
        # macro 一次调用按 indicator 分桶(macro:PMI/M2/CREDIT 各一条),
        # 把三条 provenance 的源汇总取众数作为主源
        macro_keys = ("macro:PMI", "macro:M2", "macro:CREDIT")
        combined: Dict[str, str] = {}
        for k in macro_keys:
            combined.update(gw.provenance(k))
        candidates["macro"] = primary(combined)
    return {k: v for k, v in candidates.items() if v}


__all__ = ["build_profile"]

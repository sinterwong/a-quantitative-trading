#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/investigate_provider_capabilities.py

调研已有 providers 在能力矩阵上其它维度的潜力。
验证维度：
  1. 市场覆盖：US/HK/GLOBAL 在已有 provider 中是否还有扩展空间
  2. Capability 扩展：现有 provider 是否还有未声明但实际上可以支持的能力
  3. 字段权威：各 provider 的 field_authority 是否还有补全空间
  4. 路由策略：ROUTING_POLICY 是否与实际 provider 能力匹配

验证方式：通过实际调用各 provider 的 fetch_* 方法，检查返回数据的质量和完整性。
"""

import sys
import os

# ── 环境设置 ────────────────────────────────────────────────────────────────
CONDA_PYTHON = "/home/sinter/softwares/miniconda3/envs/quant-trading/bin/python"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

# ── 依赖导入 ────────────────────────────────────────────────────────────────
import json
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

# ── 加载 provider 模块 ──────────────────────────────────────────────────────
os.environ.setdefault("PYTHONPATH", PROJECT_ROOT)

from core.data_gateway.capabilities import (
    Capability,
    Market,
    ProviderCapability,
    ROUTING_POLICY,
    RoutingStrategy,
    get_policy,
)
from core.data_gateway.providers.akshare import AkshareProvider
from core.data_gateway.providers.baostock import BaostockProvider
from core.data_gateway.providers.eastmoney import EastmoneyProvider
from core.data_gateway.providers.sina import SinaProvider
from core.data_gateway.providers.tencent import TencentProvider
from core.data_gateway.providers.yfinance import YfinanceProvider
from core.data_gateway.providers.base import Provider, ProviderError


# ─────────────────────────────────────────────────────────────────────────────
# 测试工具函数
# ─────────────────────────────────────────────────────────────────────────────

def banner(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print('='*70)


def result_row(ok: bool, msg: str):
    icon = "✓" if ok else "✗"
    print(f"  [{icon}] {msg}")


def safe_call(fn, *args, **kwargs) -> tuple[bool, Any, Optional[str]]:
    """安全调用，返回 (成功, 结果, 错误信息)"""
    try:
        result = fn(*args, **kwargs)
        return True, result, None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def summarise_df(label: str, df: pd.DataFrame):
    """打印 DataFrame 摘要。"""
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        print(f"    {label}: empty")
        return
    rows = len(df)
    cols = list(df.columns)
    print(f"    {label}: {rows} rows, columns: {cols}")


def summarise_quote(label: str, q):
    """打印 Quote 对象摘要。"""
    if q is None:
        print(f"    {label}: None")
        return
    non_default = {k: v for k, v in q._asdict().items()
                   if v not in (0, 0.0, "", None, [])}
    print(f"    {label}: symbol={q.symbol}, price={q.price}, "
          f"pe_ttm={q.pe_ttm}, pb={q.pb}, "
          f"market_cap={q.market_cap}, float_cap={q.float_cap}, "
          f"dividend_yield={q.dividend_yield}, "
          f"high_52w={q.high_52w}, low_52w={q.low_52w}, "
          f"turnover_rate={q.turnover_rate}, "
          f"extra_fields={list(non_default.keys())}")


# ─────────────────────────────────────────────────────────────────────────────
# 维度一：市场覆盖分析
# ─────────────────────────────────────────────────────────────────────────────

def investigate_market_coverage() -> Dict[str, Any]:
    banner("维度一：市场覆盖 — 各 provider 声明 vs 实际验证")
    results = {}

    providers = [
        ("TencentProvider", TencentProvider()),
        ("SinaProvider",    SinaProvider()),
        ("EastmoneyProvider", EastmoneyProvider()),
        ("AkshareProvider", AkshareProvider()),
        ("BaostockProvider", BaostockProvider()),
        ("YfinanceProvider", YfinanceProvider()),
    ]

    test_cases = [
        # (symbol, market, expected_capabilities)
        ("sh600519", Market.A,     [Capability.QUOTE, Capability.KLINE_DAILY]),
        ("hk00700",  Market.HK,    [Capability.QUOTE, Capability.KLINE_DAILY]),
        ("usAAPL",   Market.US,    [Capability.QUOTE, Capability.KLINE_DAILY]),
        ("sh000001", Market.INDEX, [Capability.QUOTE, Capability.MARKET_INDEX]),
    ]

    for name, provider in providers:
        decl = provider.declare()
        results[name] = {
            "declared_markets": sorted(m.value for m in decl.markets),
            "declared_caps":    sorted(c.value for c in decl.capabilities),
            "tests": {},
        }
        print(f"\n  [{name}]")
        print(f"    声明市场: {results[name]['declared_markets']}")
        print(f"    声明能力: {results[name]['declared_caps']}")

        for symbol, market, caps in test_cases:
            test_key = f"{symbol}/{market.value}"
            row = {"symbol": symbol, "market": market.value, "caps": [c.value for c in caps], "results": {}}
            for cap in caps:
                supports = provider.supports(cap, market)
                row["results"][cap.value] = {"supports": supports}
                if supports:
                    try:
                        if cap == Capability.QUOTE:
                            ok, q, err = safe_call(provider.fetch_quote, symbol)
                            if ok and q is not None:
                                row["results"][cap.value]["quote_valid"] = q.is_valid
                                row["results"][cap.value]["price"] = q.price if q else 0
                            else:
                                row["results"][cap.value]["error"] = str(err)[:80]
                        elif cap == Capability.KLINE_DAILY:
                            ok, df, err = safe_call(provider.fetch_kline_daily, symbol, days=5)
                            if ok:
                                row["results"][cap.value]["rows"] = len(df) if df is not None else 0
                            else:
                                row["results"][cap.value]["error"] = str(err)[:80]
                        elif cap == Capability.MARKET_INDEX:
                            ok, snap, err = safe_call(provider.fetch_market_index, symbol)
                            if ok and snap:
                                row["results"][cap.value]["price"] = snap.price
                            else:
                                row["results"][cap.value]["error"] = str(err)[:80]
                    except Exception as e:
                        row["results"][cap.value]["error"] = f"EXCEPTION: {e}"
            results[name]["tests"][test_key] = row

            # 打印简洁结果
            for cap_val, res in row["results"].items():
                if "error" in res:
                    result_row(False, f"{symbol}/{market.value}/{cap_val} → ERROR: {res['error']}")
                elif "supports" in res and not res["supports"]:
                    result_row(False, f"{symbol}/{market.value}/{cap_val} → 未声明支持（但实际可能可用）")
                elif "quote_valid" in res:
                    result_row(True, f"{symbol}/{market.value}/{cap_val} → valid={res['quote_valid']}, price={res['price']}")
                elif "rows" in res:
                    result_row(True, f"{symbol}/{market.value}/{cap_val} → {res['rows']} rows")
                elif "price" in res:
                    result_row(True, f"{symbol}/{market.value}/{cap_val} → price={res['price']}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 维度二：已有能力扩展（未被 declare 但实际可能可用的能力）
# ─────────────────────────────────────────────────────────────────────────────

def investigate_capability_extensions() -> Dict[str, Any]:
    banner("维度二：能力扩展 — 已有接口未被声明的能力")

    # 探索性测试用例：(provider_name, symbol, capability, method_name)
    # 这些是代码审查中发现"有实现但未在 declare() 中加入"的能力
    exploration_cases = []

    # Tencent: 88-field 包含 dividend_yield（茅台 600519 有值）
    exploration_cases.append(
        ("TencentProvider", "sh600519", Capability.QUOTE, "fetch_quote",
         "腾讯 88-field 包含 dividend_yield / turnover_rate / high_52w / low_52w 等，"
         "field_authority 已声明但未验证 dividend_yield 是否真实可读")
    )
    # Sina: 5档行情字段（bid1/ask1）
    exploration_cases.append(
        ("SinaProvider", "sh600519", Capability.QUOTE, "fetch_quote",
         "新浪 5 档 bid1/ask1 字段权威 1.2，需验证 bid1_price/vol / ask1_price/vol 是否真实有值")
    )
    # Eastmoney: fetch_north_flow_history 是否可用
    exploration_cases.append(
        ("EastmoneyProvider", None, Capability.NORTH_FLOW, "fetch_north_flow_history",
         "EastmoneyProvider.declare() 没有 NORTH_FLOW_HISTORY，但有 _fetch_kamt_daily/history 方法，"
         "需验证 fetch_north_flow_history 是否可调用且有数据")
    )
    # Baostock: fetch_fundamentals_history 实际输出哪些字段
    exploration_cases.append(
        ("BaostockProvider", "sh600519", Capability.FUNDAMENTALS_HISTORY, "fetch_fundamentals_history",
         "Baostock fetch_fundamentals_history 声明输出 debt_to_equity / current_ratio / quick_ratio，"
         "与 Akshare 的 roe_ttm/eps_ttm/profit_yoy 互补，验证实际输出")
    )
    # Yfinance: US KLINE + MARKET_INDEX 实际数据质量
    exploration_cases.append(
        ("YfinanceProvider", "AAPL", Capability.KLINE_DAILY, "fetch_kline_daily",
         "Yfinance US K 线延迟验证（是否盘中无数据）")
    )
    exploration_cases.append(
        ("YfinanceProvider", "^VIX", Capability.MARKET_INDEX, "fetch_market_index",
         "Yfinance VIX 行情验证")
    )

    results = {}
    for (provider_name, symbol, cap, method, note) in exploration_cases:
        print(f"\n  测试 → {provider_name}.{method}({symbol or 'N/A'})")
        print(f"    说明: {note}")

        provider_map = {
            "TencentProvider":    TencentProvider(),
            "SinaProvider":       SinaProvider(),
            "EastmoneyProvider": EastmoneyProvider(),
            "AkshareProvider":   AkshareProvider(),
            "BaostockProvider":  BaostockProvider(),
            "YfinanceProvider":  YfinanceProvider(),
        }
        provider = provider_map.get(provider_name)
        if provider is None:
            result_row(False, f"Provider {provider_name} 未找到")
            results[f"{provider_name}.{method}"] = {"error": "provider not found"}
            continue

        try:
            fn = getattr(provider, method)
        except AttributeError:
            result_row(False, f"方法 {method} 不存在")
            results[f"{provider_name}.{method}"] = {"error": "method not exists"}
            continue

        ok, result, err = safe_call(fn, symbol) if symbol else safe_call(fn)
        if not ok:
            result_row(False, f"调用失败: {err}")
            results[f"{provider_name}.{method}"] = {"error": str(err)}
        elif isinstance(result, pd.DataFrame):
            if result.empty:
                result_row(False, f"返回空 DataFrame（{method}）")
                results[f"{provider_name}.{method}"] = {"status": "empty_df"}
            else:
                result_row(True, f"DataFrame: {len(result)} rows × {list(result.columns)}")
                results[f"{provider_name}.{method}"] = {
                    "status": "ok",
                    "rows": len(result),
                    "columns": list(result.columns),
                    "head": result.head(3).to_dict(orient="records") if len(result) > 0 else [],
                }
        elif isinstance(result, list):
            result_row(True, f"List: {len(result)} items")
            results[f"{provider_name}.{method}"] = {"status": "ok", "count": len(result)}
        elif hasattr(result, "_asdict"):
            d = result._asdict()
            non_default = {k: v for k, v in d.items()
                          if v not in (0, 0.0, "", None, [], {})}
            result_row(True, f"Object: {non_default}")
            results[f"{provider_name}.{method}"] = {"status": "ok", "fields": list(non_default.keys())}
        elif hasattr(result, "__dataclass_fields__"):
            # dataclass: use dataclasses.asdict
            from dataclasses import asdict
            d = asdict(result)
            non_default = {k: v for k, v in d.items()
                          if v not in (0, 0.0, "", None, [], {})}
            result_row(True, f"Dataclass: {non_default}")
            results[f"{provider_name}.{method}"] = {"status": "ok", "fields": list(non_default.keys())}
        elif result is None:
            result_row(False, f"返回 None（可能正常，也可能无数据）")
            results[f"{provider_name}.{method}"] = {"status": "null"}
        else:
            result_row(True, f"返回: {str(result)[:100]}")
            results[f"{provider_name}.{method}"] = {"status": "ok", "value": str(result)[:80]}

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 维度三：字段权威度补全验证
# ─────────────────────────────────────────────────────────────────────────────

def investigate_field_authority() -> Dict[str, Any]:
    banner("维度三：字段权威度 — field_authority 补全空间分析")

    providers = [
        ("TencentProvider",    TencentProvider()),
        ("SinaProvider",       SinaProvider()),
        ("EastmoneyProvider",  EastmoneyProvider()),
        ("AkshareProvider",    AkshareProvider()),
        ("BaostockProvider",   BaostockProvider()),
        ("YfinanceProvider",   YfinanceProvider()),
    ]

    test_symbols = {
        "TencentProvider":    "sh600519",
        "SinaProvider":       "sh600519",
        "EastmoneyProvider": "sh600519",
        "AkshareProvider":   "sh600519",
        "BaostockProvider":   "sh600519",
        "YfinanceProvider":   "AAPL",
    }

    results = {}

    for name, provider in providers:
        print(f"\n  [{name}]")
        fa = provider.field_authority()
        decl = provider.declare()

        print(f"    field_authority 声明: {fa}")
        print(f"    声明的 capabilities: {sorted(c.value for c in decl.capabilities)}")

        # 检查 Quote 字段权威度
        if Capability.QUOTE in decl.capabilities:
            symbol = test_symbols.get(name, "sh600519")
            ok, q, err = safe_call(provider.fetch_quote, symbol)
            if ok and q is not None:
                print(f"    fetch_quote({symbol}) → price={q.price}, pe_ttm={q.pe_ttm}, "
                      f"pb={q.pb}, dividend_yield={q.dividend_yield}, "
                      f"high_52w={q.high_52w}, low_52w={q.low_52w}, "
                      f"turnover_rate={q.turnover_rate}, bid1={q.bid1_price}, "
                      f"ask1={q.ask1_price}")

                # 检查字段权威度声明完整性
                quote_fa = fa.get(Capability.QUOTE, {})
                high_authority_fields = [
                    ("pe_ttm", q.pe_ttm), ("pb", q.pb), ("market_cap", q.market_cap),
                    ("float_cap", q.float_cap), ("high_52w", q.high_52w), ("low_52w", q.low_52w),
                    ("turnover_rate", q.turnover_rate), ("dividend_yield", q.dividend_yield),
                    ("bid1_price", q.bid1_price), ("ask1_price", q.ask1_price),
                    ("amplitude", q.amplitude), ("volume_ratio", q.volume_ratio),
                ]
                missing_auth = []
                for fname, fval in high_authority_fields:
                    if fval not in (0, 0.0, None) and fname not in quote_fa:
                        missing_auth.append((fname, fval))
                if missing_auth:
                    result_row(False, f"Quote 字段有值但未声明权威: {missing_auth}")
                    results[f"{name}/quote_field_gaps"] = {
                        "status": "has_gaps",
                        "gaps": [(f, v) for f, v in missing_auth],
                    }
                else:
                    result_row(True, "Quote 字段权威度声明完整")

                results[f"{name}/quote"] = {"status": "ok", "price": q.price}
            else:
                result_row(False, f"fetch_quote 失败: {err}")
                results[f"{name}/quote"] = {"error": str(err)}
        else:
            print(f"    不支持 QUOTE，跳过")

        # 检查 Fundametnals 字段权威度
        if Capability.FUNDAMENTALS in decl.capabilities:
            symbol = test_symbols.get(name, "sh600519")
            ok, f, err = safe_call(provider.fetch_fundamentals, symbol)
            if ok and f is not None:
                from dataclasses import asdict as _asdict
                d = _asdict(f)
                non_default = {k: v for k, v in d.items() if v not in (0, 0.0, "", None)}
                result_row(True, f"Fundamentals: {non_default}")

                fa_fund = fa.get(Capability.FUNDAMENTALS, {})
                important_fields = [
                    ("roe_ttm", f.roe_ttm), ("eps_ttm", f.eps_ttm),
                    ("revenue_yoy", f.revenue_yoy), ("profit_yoy", f.profit_yoy),
                    ("dividend_yield", f.dividend_yield), ("pe_ttm", f.pe_ttm),
                    ("pb", f.pb),
                ]
                missing_auth = []
                for fname, fval in important_fields:
                    if fval not in (0, 0.0, None) and fname not in fa_fund:
                        missing_auth.append((fname, fval))
                if missing_auth:
                    result_row(False, f"Fundamentals 字段有值但未声明权威: {missing_auth}")
                    results[f"{name}/fundamentals_field_gaps"] = {
                        "status": "has_gaps",
                        "gaps": [(f, v) for f, v in missing_auth],
                    }
                else:
                    result_row(True, "Fundamentals 字段权威度声明完整")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 维度四：路由策略完整性
# ─────────────────────────────────────────────────────────────────────────────

def investigate_routing_policy() -> Dict[str, Any]:
    banner("维度四：路由策略完整性 — ROUTING_POLICY vs 实际 provider 能力")

    # 已知 capability × method → 实际 providers 映射
    known_routes = [
        # (capability, method_name, expected_providers_with_this_method)
        (Capability.QUOTE, "fetch_quote",         ["TencentProvider", "SinaProvider", "EastmoneyProvider"]),
        (Capability.QUOTE, "fetch_quotes",        ["TencentProvider", "SinaProvider", "EastmoneyProvider"]),
        (Capability.KLINE_DAILY, "fetch_kline_daily",
         ["TencentProvider", "SinaProvider", "BaostockProvider", "YfinanceProvider"]),
        (Capability.KLINE_MINUTE, "fetch_kline_minute",
         ["TencentProvider", "SinaProvider"]),
        (Capability.FUNDAMENTALS, "fetch_fundamentals",
         ["AkshareProvider", "BaostockProvider"]),
        (Capability.SECTOR_RANKING, "fetch_sectors",
         ["EastmoneyProvider", "SinaProvider"]),
        (Capability.SECTOR_CONSTITUENTS, "fetch_sector_constituents",
         ["EastmoneyProvider", "SinaProvider"]),
        (Capability.NORTH_FLOW, "fetch_north_flow",
         ["EastmoneyProvider", "AkshareProvider"]),
        (Capability.NORTH_FLOW, "fetch_north_flow_history",
         ["AkshareProvider"]),  # Eastmoney 没有公开 fetch_north_flow_history 方法
        (Capability.MARKET_INDEX, "fetch_market_index",
         ["TencentProvider", "SinaProvider", "EastmoneyProvider", "YfinanceProvider"]),
        (Capability.MACRO, "fetch_macro",
         ["AkshareProvider"]),
        (Capability.FUNDAMENTALS_HISTORY, "fetch_fundamentals_history",
         ["AkshareProvider", "BaostockProvider"]),
        (Capability.BALANCE_SHEET, "fetch_balance_sheet",
         ["BaostockProvider"]),
        (Capability.MARGIN_FLOW, "fetch_margin_flow",
         ["AkshareProvider"]),  # EM 无个股融资融券时序
        (Capability.FUND_FLOW, "fetch_fund_flow",
         ["AkshareProvider"]),
        (Capability.NEWS_HEADLINES, "fetch_news_headlines",
         ["EastmoneyProvider", "AkshareProvider"]),
    ]

    results = {}

    provider_map = {
        "TencentProvider":    TencentProvider(),
        "SinaProvider":       SinaProvider(),
        "EastmoneyProvider": EastmoneyProvider(),
        "AkshareProvider":   AkshareProvider(),
        "BaostockProvider":  BaostockProvider(),
        "YfinanceProvider":  YfinanceProvider(),
    }

    for cap, method, expected_providers in known_routes:
        row_key = f"{cap.value}/{method}"
        row = {
            "capability": cap.value,
            "method": method,
            "expected_providers": expected_providers,
            "routing_policy": None,
            "issues": [],
        }

        # 检查 ROUTING_POLICY 是否登记
        try:
            policy = get_policy(cap, method)
            row["routing_policy"] = {
                "strategy": policy.strategy.value,
                "skip_fields": policy.skip_fields,
                "ffill": policy.ffill,
            }
            result_row(True, f"ROUTING_POLICY[{cap.value}/{method}] → {policy.strategy.value}")
        except KeyError:
            row["issues"].append("ROUTING_POLICY 未登记")
            result_row(False, f"ROUTING_POLICY[{cap.value}/{method}] → 未登记！")

        # 检查每个期望的 provider 是否真的有这个方法
        for pname in expected_providers:
            provider = provider_map.get(pname)
            if provider is None:
                row["issues"].append(f"provider {pname} 未找到")
                continue
            decl = provider.declare()
            if not hasattr(provider, method):
                row["issues"].append(f"{pname} 没有方法 {method}")
                result_row(False, f"  {pname}.{method} 不存在")
            elif cap not in decl.capabilities:
                row["issues"].append(f"{pname} 声明了 {cap.value} 但没有 {method}")
                result_row(False, f"  {pname}.{method} 存在，但不在 declare() 中")
            else:
                result_row(True, f"  {pname}.{method} ✓ (declared)")

        results[row_key] = row

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 维度五：AkShare 港股基本面数据验证（已声明但之前测试未覆盖）
# ─────────────────────────────────────────────────────────────────────────────

def investigate_akshare_hk_fundamentals() -> Dict[str, Any]:
    banner("维度五：AkShare 港股基本面 — fetch_fundamentals / fetch_fundamentals_history 港股覆盖验证")

    provider = AkshareProvider()
    results = {}

    # 港股代码测试用例
    hk_symbols = ["hk00700", "00700", "HK:00700", "hk00001", "hk00005"]
    for sym in hk_symbols:
        print(f"\n  测试 AkShareProvider.fetch_fundamentals({sym})")
        ok, result, err = safe_call(provider.fetch_fundamentals, sym)
        if not ok:
            result_row(False, f"调用失败: {err}")
            results[sym] = {"status": "error", "error": str(err)}
        elif result is None:
            result_row(False, "返回 None（无数据或解析失败）")
            results[sym] = {"status": "null"}
        else:
            from dataclasses import asdict as _asdict
            d = _asdict(result)
            non_default = {k: v for k, v in d.items() if v not in (0, 0.0, "", None)}
            result_row(True, f"Fundamentals: {non_default}")
            results[sym] = {"status": "ok", "fields": list(non_default.keys())}

        # fetch_fundamentals_history
        print(f"  测试 AkShareProvider.fetch_fundamentals_history({sym})")
        ok2, df2, err2 = safe_call(provider.fetch_fundamentals_history, sym)
        if not ok2:
            result_row(False, f"fetch_fundamentals_history 失败: {err2}")
            results[f"{sym}_history"] = {"status": "error", "error": str(err2)}
        elif df2 is None or (isinstance(df2, pd.DataFrame) and df2.empty):
            result_row(False, "fetch_fundamentals_history 返回空 DataFrame")
            results[f"{sym}_history"] = {"status": "empty"}
        else:
            print(f"    → {len(df2)} rows, columns: {list(df2.columns)}")
            results[f"{sym}_history"] = {
                "status": "ok",
                "rows": len(df2),
                "columns": list(df2.columns),
            }

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────

def main():
    banner("Provider 能力矩阵潜力调研")

    all_results = {
        "timestamp": datetime.now().isoformat(),
        "project_root": PROJECT_ROOT,
        "conda_python": CONDA_PYTHON,
    }

    print("\n" + "="*70)
    print("  调研维度说明")
    print("="*70)
    print("""
  维度一：市场覆盖 — 各 provider 声明 vs 实际验证
  维度二：能力扩展 — 已有接口未被声明的能力
  维度三：字段权威度 — field_authority 补全空间分析
  维度四：路由策略完整性 — ROUTING_POLICY vs 实际 provider 能力
  维度五：AkShare 港股基本面 — fetch_fundamentals/history 港股覆盖验证
    """)

    # 维度一
    all_results["market_coverage"] = investigate_market_coverage()

    # 维度二
    all_results["capability_extensions"] = investigate_capability_extensions()

    # 维度三
    all_results["field_authority"] = investigate_field_authority()

    # 维度四
    all_results["routing_policy"] = investigate_routing_policy()

    # 维度五
    all_results["akshare_hk"] = investigate_akshare_hk_fundamentals()

    # 输出汇总
    banner("调研结果汇总")
    print(f"\n  完整结果已保存到: {SCRIPT_DIR}/provider_capability_investigation_results.json")

    # 打印关键发现
    findings = []
    for category, data in all_results.items():
        if category == "timestamp":
            continue
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, dict) and val.get("issues"):
                    findings.append(f"  ⚠ [{category}] {key}: {val['issues']}")
                if isinstance(val, dict) and val.get("status") == "has_gaps":
                    findings.append(f"  ⚠ [{category}] {key}: 字段权威度有缺口 → {val.get('gaps')}")

    if findings:
        print("\n  关键发现：")
        for f in findings:
            print(f)
    else:
        print("\n  未发现明显问题")

    # 保存 JSON
    output_path = os.path.join(SCRIPT_DIR, "provider_capability_investigation_results.json")
    with open(output_path, "w", encoding="utf-8") as fp:
        json.dump(all_results, fp, ensure_ascii=False, indent=2, default=str)
    print(f"\n  JSON 结果已保存: {output_path}")

    banner("调研完成")


if __name__ == "__main__":
    main()

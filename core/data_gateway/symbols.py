# -*- coding: utf-8 -*-
"""
data_gateway.symbols — 标的代码工具

集中市场检测和代码格式归一化。从 core.symbol_utils 迁入,
后续 Stage 6 会删除 core.symbol_utils。
"""

from typing import Any

from .capabilities import Market


# ─── 安全转换 ──────────────────────────────────────────────────────────────────


def safe_float(val: Any, default: float = 0.0) -> float:
    """容错 float 转换:None/'-'/'--'/NaN/解析失败 → default。"""
    if val is None:
        return default
    try:
        s = str(val).strip()
        if s in ("", "-", "--"):
            return default
        f = float(s)
        return f if f == f else default
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    f = safe_float(val, float(default))
    return int(f)


# ─── 市场检测 ──────────────────────────────────────────────────────────────────


def detect_market(symbol: str) -> Market:
    """检测标的市场类型。

    支持格式:
        sh600519 / sz000001 / 600519.SH / 000001.SZ  → Market.A
        sh000001 / sz399006 / 000001.SH             → Market.INDEX
        hk00700 / HK:00700 / 00700.HK               → Market.HK
        usAAPL / US:AAPL                            → Market.US
    """
    s = symbol.strip()
    upper = s.upper()

    if upper.startswith("HK:") or upper.endswith(".HK"):
        return Market.HK
    if upper.startswith("US:"):
        return Market.US

    lower = s.lower()
    if lower.startswith("hk"):
        return Market.HK
    if lower.startswith("us"):
        return Market.US
    if lower.startswith(("sh000", "sz399")):
        return Market.INDEX
    if lower.startswith(("sh", "sz")):
        return Market.A

    if upper.endswith(".SH") or upper.endswith(".SZ"):
        code = s[:-3].strip()
        if code.startswith("000") and upper.endswith(".SH"):
            return Market.INDEX
        if code.startswith("399") and upper.endswith(".SZ"):
            return Market.INDEX
        return Market.A

    if s.isdigit():
        if s.startswith(("000", "399")):
            return Market.INDEX
        return Market.A

    if s.isalpha():
        return Market.US

    return Market.A


# ─── 格式归一化 ────────────────────────────────────────────────────────────────


def a_share_exchange(symbol: str) -> str:
    """识别 A 股 / A 股 ETF 标的属于哪个交易所。

    返回 "sh"（上交所）或 "sz"（深交所）。

    规则：
      个股: 6/9 开头 → SH（沪 A / 科创板）；0/3 开头 → SZ（深 A / 创业板）
      ETF: 51x / 56x / 58x → SH（上交所基金）；15x / 16x / 18x → SZ（深交所基金）
            注意：159xxx 是深交所 ETF（曾被误归 SH，已修正）。

    输入支持各类常见格式（sh600519 / 600519.SH / 600519），
    无法识别时默认返回 "sz"。
    """
    s = symbol.strip().upper()
    # 剥掉常见前后缀
    for prefix in ("SH:", "SZ:", "SH", "SZ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.endswith(".SH") or s.endswith(".SZ"):
        s = s[:-3]
    s = s.lstrip(".")
    if not s:
        return "sz"

    # SH 前缀：A 股 6/9 + ETF 51/56/58
    if s.startswith(("6", "9", "51", "56", "58")):
        return "sh"
    # SZ 前缀：A 股 0/3（含创业板 30、新三板北交所 8）+ ETF 15/16/18
    return "sz"


def normalize_to_sina(symbol: str) -> str:
    """转换为新浪格式: sh600519 / sz000001 / hk00700 / gb_aapl"""
    s = symbol.strip()
    upper = s.upper()

    if upper.startswith("HK:"):
        code = s[3:].strip()
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    if upper.startswith("US:"):
        return f"gb_{s[3:].strip().lower()}"
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    lower = s.lower()
    if lower.startswith(("sh", "sz", "hk")):
        return lower
    if lower.startswith("us"):
        return f"gb_{s[2:].lower()}"
    if s.isdigit():
        return f"sh{s}" if s.startswith(("60", "68", "51", "58")) else f"sz{s}"
    if s.isalpha():
        return f"gb_{s.lower()}"
    return lower


def normalize_to_tencent(symbol: str) -> str:
    """转换为腾讯格式: sh600519 / sz000001 / hk00700 / usAAPL"""
    s = symbol.strip()
    upper = s.upper()

    if upper.startswith("HK:"):
        code = s[3:].strip()
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    if upper.startswith("US:"):
        return f"us{s[3:].strip()}"
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    if s.lower().startswith(("us", "hk")):
        return s
    lower = s.lower()
    if lower.startswith(("sh", "sz")):
        return lower
    if s.isdigit():
        return f"sh{s}" if s.startswith(("60", "68", "51", "58")) else f"sz{s}"
    if s.isalpha():
        return f"us{s.upper()}"
    return lower


__all__ = [
    "safe_float",
    "safe_int",
    "detect_market",
    "a_share_exchange",
    "normalize_to_sina",
    "normalize_to_tencent",
]

# -*- coding: utf-8 -*-
"""
symbol_utils.py — 标的代码工具函数（唯一真实来源）
===================================================

集中管理：
  - _safe_float / _safe_int: 安全数值转换
  - detect_market: 市场类型检测
  - normalize_to_sina / normalize_to_tencent: 代码格式转换

所有其他模块应从这里导入，不要自行实现。
"""

from typing import Any


# ─── 安全转换 ──────────────────────────────────────────────────────────────────


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换为 float"""
    if val is None:
        return default
    try:
        s = str(val).strip()
        if s in ("", "-", "--"):
            return default
        f = float(s)
        return f if f == f else default  # NaN check
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    """安全转换为 int"""
    f = _safe_float(val, float(default))
    return int(f)


# ─── 市场检测 ──────────────────────────────────────────────────────────────────


def detect_market(symbol: str) -> str:
    """
    检测标的市场类型。

    支持格式：
      sh600519, sz000001, 600519.SH, 000001.SZ → A
      sh000001, sz399006, 000001.SH             → INDEX
      hk00700, HK:00700, 00700.HK              → HK
      usAAPL, US:AAPL                           → US

    Returns: 'A' | 'INDEX' | 'HK' | 'US'
    """
    s = symbol.strip()

    # HK:xxx / US:xxx 格式
    if s.upper().startswith("HK:"):
        return "HK"
    if s.upper().startswith("US:"):
        return "US"

    # xxx.HK 格式
    if s.upper().endswith(".HK"):
        return "HK"

    # sh/sz 前缀
    lower = s.lower()
    if lower.startswith("hk"):
        return "HK"
    if lower.startswith("us"):
        return "US"
    if lower.startswith(("sh000", "sz399")):
        return "INDEX"
    if lower.startswith(("sh", "sz")):
        return "A"

    # xxx.SH / xxx.SZ 后缀
    upper = s.upper()
    if upper.endswith(".SH") or upper.endswith(".SZ"):
        code = s[:-3].strip()
        if code.startswith("000") and upper.endswith(".SH"):
            return "INDEX"
        if code.startswith("399") and upper.endswith(".SZ"):
            return "INDEX"
        return "A"

    # 纯数字
    if s.isdigit():
        if s.startswith(("000", "399")):
            return "INDEX"
        return "A"

    # 纯字母 → 美股
    if s.isalpha():
        return "US"

    return "A"


# ─── 代码格式转换 ──────────────────────────────────────────────────────────────


def normalize_to_sina(symbol: str) -> str:
    """
    将任意格式的标的代码转换为新浪格式。

    新浪 A 股: sh600519 / sz000001
    新浪港股: hk00700
    新浪美股: gb_aapl

    Examples:
        '600519.SH' → 'sh600519'
        '000001.SZ' → 'sz000001'
        'HK:00700'  → 'hk00700'
        'US:AAPL'   → 'gb_aapl'
        'sh600519'  → 'sh600519'
    """
    s = symbol.strip()
    upper = s.upper()

    # HK:xxx 格式
    if upper.startswith("HK:"):
        code = s[3:].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # US:xxx 格式
    if upper.startswith("US:"):
        return f"gb_{s[3:].strip().lower()}"

    # xxx.HK 格式
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # xxx.SH / xxx.SZ 格式
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    # 已经是 sh/sz 格式
    lower = s.lower()
    if lower.startswith(("sh", "sz")):
        return lower

    # hk 前缀（港股）
    if lower.startswith("hk"):
        return lower

    # us 前缀（美股）→ gb_ 格式
    if lower.startswith("us"):
        code = s[2:]
        return f"gb_{code.lower()}"

    # 纯数字 → A 股
    if s.isdigit():
        if s.startswith(("60", "68", "5")):
            return f"sh{s}"
        return f"sz{s}"

    # 纯字母 → 美股
    if s.isalpha():
        return f"gb_{s.lower()}"

    return lower


def normalize_to_tencent(symbol: str) -> str:
    """
    将任意格式的标的代码转换为腾讯格式。

    腾讯 A 股: sh600519 / sz000001
    腾讯港股: hk00700
    腾讯美股: usAAPL（区分大小写）

    Examples:
        '600519.SH' → 'sh600519'
        '000001.SZ' → 'sz000001'
        'HK:00700'  → 'hk00700'
        'US:AAPL'   → 'usAAPL'
        'sh600519'  → 'sh600519'
    """
    s = symbol.strip()
    upper = s.upper()

    # HK:xxx 格式
    if upper.startswith("HK:"):
        code = s[3:].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # US:xxx 格式
    if upper.startswith("US:"):
        return f"us{s[3:].strip()}"

    # xxx.HK 格式
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # xxx.SH / xxx.SZ 格式
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    # 已经是 us/hk 格式（保留大小写）
    if s.lower().startswith(("us", "hk")):
        return s

    # 已经是 sh/sz 格式（A 股不区分大小写）
    lower = s.lower()
    if lower.startswith(("sh", "sz")):
        return lower

    # 纯数字
    if s.isdigit():
        if s.startswith(("60", "68", "5")):
            return f"sh{s}"
        return f"sz{s}"

    # 纯字母 → 美股（保留大小写）
    if s.isalpha():
        return f"us{s.upper()}"

    return lower

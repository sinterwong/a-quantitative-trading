"""ui/format.py — 渲染层 helper。

无任何 st.* / requests / pandas 依赖,纯函数,易测。
"""
from __future__ import annotations

from typing import Any


def fmt_money(value: Any, *, currency: str = '¥', decimals: int = 2) -> str:
    """格式化金额: ¥12,345.67;None/NaN → '—'。"""
    if value is None:
        return '—'
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:  # NaN
        return '—'
    return f'{currency}{v:,.{decimals}f}'


def fmt_pct(value: Any, *, decimals: int = 2, signed: bool = False) -> str:
    """value 假设是小数(0.1234 → 12.34%);None/NaN → '—'。"""
    if value is None:
        return '—'
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:
        return '—'
    sign = '+' if (signed and v > 0) else ''
    return f'{sign}{v * 100:.{decimals}f}%'


def fmt_num(value: Any, *, decimals: int = 2) -> str:
    if value is None:
        return '—'
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:
        return '—'
    return f'{v:,.{decimals}f}'


def fmt_int(value: Any) -> str:
    if value is None:
        return '—'
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return str(value)


def color_for_change(value: Any) -> str:
    """涨绿跌红(A 股惯例,红涨绿跌 — 这里用 A 股配色)。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ''
    if v > 0:
        return '#d9534f'  # 红
    if v < 0:
        return '#5cb85c'  # 绿
    return '#6c757d'


def truncate(text: Any, n: int = 80) -> str:
    s = str(text or '')
    return s if len(s) <= n else s[: n - 1] + '…'

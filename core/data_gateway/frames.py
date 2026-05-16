# -*- coding: utf-8 -*-
"""
frames.py — kline DataFrame 通用归一化辅助。

跨 provider 的 kline 返回字段不统一(``date`` vs ``timestamp``、
字符串日期 vs datetime),use case 端复用同一段 rename + to_datetime +
set_index 代码。本模块抽出这段,避免在多个 use case 中复制粘贴。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def normalize_kline_index(df: "pd.DataFrame") -> "pd.DataFrame":
    """规范化 kline DataFrame 的时间索引。

    - 若 ``date`` 列存在,重命名为 ``timestamp``。
    - 若 ``timestamp`` 列存在,转为 datetime,丢弃无法解析的行,设为 index。
    - 其它列原样保留。

    传入 ``None`` 或空 DataFrame 时原样返回(让调用方按自己的语义判空)。
    """
    import pandas as pd

    if df is None or df.empty:
        return df

    if 'date' in df.columns:
        df = df.rename(columns={'date': 'timestamp'})
    if 'timestamp' in df.columns:
        df = df.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp']).set_index('timestamp')
    return df

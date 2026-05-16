"""ui/widgets/forms.py — 跨页面表单组件。"""
from __future__ import annotations

from typing import Optional, Sequence

import streamlit as st


def symbol_input(label: str = '标的代码', *, key: str = 'symbol',
                 default: str = '', placeholder: str = '600519.SH / 00700.HK') -> str:
    return st.text_input(label, key=key, value=default, placeholder=placeholder).strip()


def universe_input(label: str = '资产池 (每行一个代码)', *,
                   key: str = 'universe', default: Sequence[str] = ()) -> list:
    raw = st.text_area(label, key=key, value='\n'.join(default), height=140,
                       placeholder='600519.SH\n000858.SZ\n601318.SH')
    return [s.strip() for s in raw.splitlines() if s.strip()]


def date_window(*, key_prefix: str = 'win',
                default_days: int = 252) -> tuple[Optional[str], Optional[str], int]:
    """返回 (start, end, days)。
    start/end 可为 None;UI 上 toggle 「按窗口天数」/「按日期区间」。
    """
    mode = st.radio('时间窗口', ['按天数', '按日期'], horizontal=True,
                    key=f'{key_prefix}_mode')
    if mode == '按天数':
        days = int(st.number_input('天数', min_value=20, max_value=2000,
                                   value=default_days, step=10,
                                   key=f'{key_prefix}_days'))
        return None, None, days
    col1, col2 = st.columns(2)
    start = col1.date_input('起始', key=f'{key_prefix}_start')
    end = col2.date_input('结束', key=f'{key_prefix}_end')
    return (str(start) if start else None,
            str(end) if end else None,
            default_days)


def market_toggle(*, key: str = 'market') -> str:
    """返回 'a' / 'hk'。"""
    return st.radio('市场', options=['a', 'hk'],
                    format_func=lambda x: 'A 股' if x == 'a' else '港股',
                    horizontal=True, key=key)

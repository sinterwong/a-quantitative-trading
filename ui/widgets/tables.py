"""ui/widgets/tables.py — 表格渲染 helper(positions / signals / trades)。

接到 backend 返回的 list[dict],转 DataFrame 后让 st.dataframe 自管渲染。
为了不耦合 schema 漂移,只挑常见字段,缺失的安静跳过。
"""
from __future__ import annotations

from typing import Iterable, Sequence

import pandas as pd
import streamlit as st


def _to_df(rows: Iterable[dict], cols: Sequence[str]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows) or [])
    if df.empty:
        return df
    present = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in present]
    return df[present + extra]


def positions_table(positions: list) -> None:
    """常见字段:symbol/name/shares/entry_price/last_price/unrealized_pnl/weight。"""
    if not positions:
        st.caption('当前无持仓')
        return
    df = _to_df(positions, [
        'symbol', 'name', 'shares', 'entry_price', 'latest_price',
        'market_value', 'unrealized_pnl', 'unrealized_pnl_pct', 'weight',
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def signals_table(signals: list) -> None:
    if not signals:
        st.caption('暂无信号记录')
        return
    df = _to_df(signals, [
        'ts', 'timestamp', 'symbol', 'signal_type', 'strength', 'price', 'reason',
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def trades_table(trades: list) -> None:
    if not trades:
        st.caption('暂无成交记录')
        return
    df = _to_df(trades, [
        'ts', 'timestamp', 'symbol', 'direction', 'side', 'shares', 'price',
        'commission', 'pnl', 'note',
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def orders_table(orders: list) -> None:
    if not orders:
        st.caption('无订单')
        return
    df = _to_df(orders, [
        'order_id', 'id', 'symbol', 'side', 'qty', 'shares', 'price',
        'order_type', 'status', 'submitted_at', 'filled_qty', 'avg_fill_price',
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


def generic_table(rows: list, *, hide_index: bool = True) -> None:
    if not rows:
        st.caption('暂无数据')
        return
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=hide_index)

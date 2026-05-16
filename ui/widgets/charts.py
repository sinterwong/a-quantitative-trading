"""ui/widgets/charts.py — plotly 图表 helper。

最小封装,后端 schema 不稳时尽量宽容:多字段名候选 (equity / nav / total /
equity_value)。所有函数自包含,接 list[dict] / dict 输入,输出 Figure。
"""
from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


_EQUITY_KEYS = ('equity', 'nav', 'total_equity', 'total', 'value')
_DATE_KEYS = ('date', 'ts', 'timestamp', 'day')


def _first_present(d: dict, keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        if k in d:
            return k
    return None


def equity_curve(daily: list) -> go.Figure:
    """daily: [{'date': '2025-..', 'equity': 12345.0, ...}, ...]"""
    if not daily:
        return go.Figure().update_layout(
            title='暂无每日记录', height=260,
            margin=dict(l=10, r=10, t=40, b=10),
        )
    sample = daily[0]
    date_k = _first_present(sample, _DATE_KEYS) or 'date'
    eq_k = _first_present(sample, _EQUITY_KEYS) or 'equity'
    df = pd.DataFrame(daily)
    if date_k in df.columns:
        df[date_k] = pd.to_datetime(df[date_k], errors='coerce')
        df = df.sort_values(date_k)
    if eq_k not in df.columns:
        # 兜底:数字列里挑最大量级的那一列当 equity
        num_cols = df.select_dtypes(include='number').columns.tolist()
        if not num_cols:
            return go.Figure().update_layout(title='daily 无可绘制字段')
        eq_k = num_cols[0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df[date_k] if date_k in df.columns else df.index,
        y=df[eq_k], mode='lines', name='权益',
        line=dict(color='#0366d6', width=2),
        fill='tozeroy', fillcolor='rgba(3,102,214,0.08)',
    ))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title=None, yaxis_title='权益 (¥)',
        hovermode='x unified',
    )
    return fig


def drawdown_curve(daily: list) -> go.Figure:
    if not daily:
        return go.Figure().update_layout(title='暂无每日记录', height=200)
    sample = daily[0]
    date_k = _first_present(sample, _DATE_KEYS) or 'date'
    eq_k = _first_present(sample, _EQUITY_KEYS) or 'equity'
    df = pd.DataFrame(daily)
    if date_k in df.columns:
        df[date_k] = pd.to_datetime(df[date_k], errors='coerce')
        df = df.sort_values(date_k)
    if eq_k not in df.columns:
        return go.Figure().update_layout(title='无 equity 字段')
    eq = df[eq_k].astype(float)
    dd = eq / eq.cummax() - 1.0
    fig = go.Figure(go.Scatter(
        x=df[date_k] if date_k in df.columns else df.index,
        y=dd * 100, mode='lines', name='回撤',
        line=dict(color='#d9534f', width=1.5),
        fill='tozeroy', fillcolor='rgba(217,83,79,0.15)',
    ))
    fig.update_layout(
        height=200, margin=dict(l=10, r=10, t=20, b=10),
        yaxis_title='回撤 (%)', xaxis_title=None,
    )
    return fig


def weights_pie(weights: dict, *, title: str = '权重') -> go.Figure:
    if not weights:
        return go.Figure().update_layout(title='无权重数据', height=320)
    labels, values = zip(*[(k, max(float(v), 0.0)) for k, v in weights.items()])
    fig = go.Figure(go.Pie(
        labels=list(labels), values=list(values), hole=0.45,
        textinfo='label+percent', textposition='outside',
    ))
    fig.update_layout(title=title, height=360, margin=dict(l=10, r=10, t=40, b=10))
    return fig


def weights_bar(weights: dict, *, title: str = '权重明细') -> go.Figure:
    if not weights:
        return go.Figure().update_layout(title='无权重数据', height=260)
    items = sorted(weights.items(), key=lambda kv: -float(kv[1]))
    labels = [k for k, _ in items]
    values = [float(v) * 100 for _, v in items]
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color='#0366d6'))
    fig.update_layout(
        title=title, height=300, yaxis_title='权重 (%)',
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


def kline(bars: list, *, title: str = 'K线') -> go.Figure:
    """bars: [{'date': '2025-..', 'open':, 'high':, 'low':, 'close':, 'volume':}, ...]"""
    if not bars:
        return go.Figure().update_layout(title='无 K 线数据', height=320)
    df = pd.DataFrame(bars)
    date_k = _first_present(df.iloc[0].to_dict(), _DATE_KEYS) or 'date'
    if date_k in df.columns:
        df[date_k] = pd.to_datetime(df[date_k], errors='coerce')
        df = df.sort_values(date_k)
    required = {'open', 'high', 'low', 'close'}
    if not required.issubset(df.columns):
        # fallback 折线
        close_col = 'close' if 'close' in df.columns else (df.select_dtypes('number').columns[:1].tolist() or [None])[0]
        if not close_col:
            return go.Figure().update_layout(title=f'无 OHLC 字段({list(df.columns)})')
        fig = go.Figure(go.Scatter(x=df[date_k] if date_k in df.columns else df.index,
                                   y=df[close_col], mode='lines', name=close_col))
        fig.update_layout(title=title, height=380, margin=dict(l=10, r=10, t=40, b=10))
        return fig
    fig = go.Figure(go.Candlestick(
        x=df[date_k] if date_k in df.columns else df.index,
        open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        increasing_line_color='#d9534f', decreasing_line_color='#5cb85c',
    ))
    fig.update_layout(
        title=title, height=420, margin=dict(l=10, r=10, t=40, b=10),
        xaxis_rangeslider_visible=False,
    )
    return fig


def line_series(rows: list, *, x_key: str, y_key: str,
                title: str = '', y_title: str = '') -> go.Figure:
    if not rows:
        return go.Figure().update_layout(title=title or '无数据', height=260)
    df = pd.DataFrame(rows)
    if x_key in df.columns:
        df[x_key] = pd.to_datetime(df[x_key], errors='ignore')
    fig = px.line(df, x=x_key, y=y_key)
    fig.update_layout(title=title, height=300, yaxis_title=y_title,
                      margin=dict(l=10, r=10, t=40, b=10))
    return fig

"""ui/pages/market.py — 宏观 / 资金流 / 北向 / 数据源健康。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, get_macro, get_fund_flow, get_northbound, get_data_status,
)
from ui.widgets.layout import section_header, error_banner, refresh_button
from ui.widgets.status import header_status_bar
from ui.widgets.charts import line_series
from ui.widgets.tables import generic_table


header_status_bar()
section_header('市场数据', '宏观 / 资金流 / 北向 / 数据源状态')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

tab_macro, tab_flow, tab_north, tab_status = st.tabs(
    ['宏观指标', '行业资金流', '北向资金', '数据源健康']
)

with tab_macro:
    indicator = st.selectbox('指标', ['PMI', 'M2_growth', 'CPI', 'PPI', 'GDP'])
    try:
        data = get_macro(indicator)
    except BackendError as exc:
        error_banner(exc)
        data = {}
    rows = data.get('data') or data.get('series') or data.get('values') or []
    if isinstance(rows, list) and rows:
        # 兜底字段名
        sample = rows[0] if isinstance(rows[0], dict) else {}
        x_key = next((k for k in ('date', 'period', 'ts', 'month') if k in sample), 'date')
        y_key = next((k for k in ('value', 'val', indicator.lower()) if k in sample), 'value')
        st.plotly_chart(
            line_series(rows, x_key=x_key, y_key=y_key,
                        title=indicator, y_title=indicator),
            use_container_width=True,
        )
        with st.expander('原始数据'):
            generic_table(rows)
    elif isinstance(rows, dict):
        st.json(rows)
    else:
        st.caption(f'{indicator} 无数据(后端返回:{data}')

with tab_flow:
    try:
        ff = get_fund_flow()
    except BackendError as exc:
        error_banner(exc)
        ff = {}
    sectors = ff.get('sectors') or ff.get('data') or ff.get('flows') or []
    if isinstance(sectors, list) and sectors:
        st.markdown('##### 行业资金净流入')
        generic_table(sectors)
    else:
        st.json(ff)

with tab_north:
    try:
        nb = get_northbound()
    except BackendError as exc:
        error_banner(exc)
        nb = {}
    series = nb.get('data') or nb.get('flow') or nb.get('series') or []
    if isinstance(series, list) and series:
        sample = series[0] if isinstance(series[0], dict) else {}
        x_key = next((k for k in ('date', 'ts') if k in sample), 'date')
        y_key = next((k for k in ('net', 'net_buy', 'amount', 'value')
                      if k in sample), 'net')
        st.plotly_chart(
            line_series(series, x_key=x_key, y_key=y_key,
                        title='北向资金净流入', y_title='净流入'),
            use_container_width=True,
        )
        with st.expander('原始数据'):
            generic_table(series)
    else:
        st.json(nb)

with tab_status:
    try:
        ds = get_data_status()
    except BackendError as exc:
        error_banner(exc)
        ds = {}
    st.markdown('##### Provider 健康')
    st.json(ds, expanded=True)

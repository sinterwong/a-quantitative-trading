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
    # 后端 /data/macro/<indicator> 只返单点 {indicator, value, date, unit}
    indicator = st.selectbox('指标', ['PMI', 'M2_growth', 'CPI', 'PPI', 'GDP'])
    try:
        data = get_macro(indicator)
    except BackendError as exc:
        error_banner(exc)
        data = {}
    value = data.get('value')
    date_str = data.get('date')
    unit = data.get('unit') or ''
    if value is not None:
        c1, c2 = st.columns(2)
        c1.metric(label=f'{indicator}', value=f'{value:g} {unit}'.strip())
        c2.metric(label='截至日期', value=str(date_str or '—'))
        st.caption('后端仅暴露单点最新值;若需历史曲线需扩 /data/macro/<indicator>/history。')
    else:
        st.caption(f'{indicator} 无数据')
        with st.expander('原始响应'):
            st.json(data)

with tab_flow:
    # 后端默认 source=market 返大盘汇总 {type, sh_close, sh_change, sz_close,
    # sz_change, main_net, main_pct, ...},不含 sectors 列表
    try:
        ff = get_fund_flow()
    except BackendError as exc:
        error_banner(exc)
        ff = {}
    if ff.get('type') == 'market':
        st.markdown('##### 大盘资金流(主力)')
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('上证收盘', ff.get('sh_close', '—'),
                  delta=f"{ff.get('sh_change', 0):+.2f}%"
                  if ff.get('sh_change') is not None else None)
        c2.metric('深证收盘', ff.get('sz_close', '—'),
                  delta=f"{ff.get('sz_change', 0):+.2f}%"
                  if ff.get('sz_change') is not None else None)
        c3.metric('主力净流入(亿)', ff.get('main_net', '—'))
        c4.metric('主力净流入占比', f"{ff.get('main_pct', 0):.2f}%"
                  if ff.get('main_pct') is not None else '—')
    else:
        sectors = (ff.get('stocks') or ff.get('sectors')
                   or ff.get('data') or ff.get('flows') or [])
        if isinstance(sectors, list) and sectors:
            st.markdown('##### 资金流明细')
            generic_table(sectors)
        else:
            with st.expander('原始响应'):
                st.json(ff)

with tab_north:
    # 后端返 {summary, net_north_yi, direction, history(dict), ...}
    try:
        nb = get_northbound()
    except BackendError as exc:
        error_banner(exc)
        nb = {}
    if nb:
        c1, c2, c3 = st.columns(3)
        c1.metric('今日净流入(亿)', nb.get('net_north_yi', '—'))
        c2.metric('方向', nb.get('direction', '—'))
        c3.metric('强度', nb.get('strength', '—'))
        if nb.get('summary'):
            st.caption(nb['summary'])

        history = nb.get('history') or {}
        if isinstance(history, dict) and history:
            series = [{'date': d, 'net': v} for d, v in sorted(history.items())]
            st.plotly_chart(
                line_series(series, x_key='date', y_key='net',
                            title='北向资金净流入(近10日, 亿元)',
                            y_title='净流入(亿)'),
                use_container_width=True,
            )
            with st.expander('原始数据'):
                generic_table(series)
        elif isinstance(history, list) and history:
            st.plotly_chart(
                line_series(history, x_key='date', y_key='net',
                            title='北向资金净流入', y_title='净流入'),
                use_container_width=True,
            )
    else:
        st.caption('北向数据不可用')

with tab_status:
    try:
        ds = get_data_status()
    except BackendError as exc:
        error_banner(exc)
        ds = {}
    st.markdown('##### Provider 健康')
    st.json(ds, expanded=True)

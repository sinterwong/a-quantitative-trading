"""ui/pages/portfolio.py — 持仓与现金。

- 持仓表 + 权重饼图
- 现金调整 form
- 手工录入/调整持仓 form
- 250 天每日权益时序
"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, clear_cache, get_positions, get_cash, get_daily,
    get_performance_summary, set_cash, upsert_position,
)
from ui.format import fmt_money, fmt_pct, fmt_num
from ui.widgets.layout import (
    section_header, kpi_row, error_banner, refresh_button, confirm_dialog,
)
from ui.widgets.status import header_status_bar
from ui.widgets.charts import equity_curve, weights_pie
from ui.widgets.tables import positions_table


header_status_bar()
section_header('持仓与现金', '双段确认 mutation form（现金 + 持仓录入）')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

# ── KPI（含现金，5 等宽卡片，鼠标悬停看完整值）──
try:
    cash = get_cash()
except BackendError as exc:
    error_banner(exc)
    cash = 0.0

try:
    perf = get_performance_summary()
except BackendError as exc:
    error_banner(exc)
    perf = {}

kpi_row([
    {'label': '可用现金', 'value': fmt_money(cash), 'raw': fmt_money(cash)},
    {'label': '累计收益', 'value': fmt_pct(perf.get('total_return_pct'), signed=True),
     'raw': f"{perf.get('total_return_pct') * 100:.4f}%" if perf.get('total_return_pct') is not None else '—'},
    {'label': '年化收益', 'value': fmt_pct(perf.get('annual_return'), signed=True),
     'raw': f"{perf.get('annual_return') * 100:.4f}%" if perf.get('annual_return') is not None else '—'},
    {'label': '夏普', 'value': fmt_num(perf.get('sharpe'), decimals=2),
     'raw': f"{perf.get('sharpe'):.4f}" if perf.get('sharpe') is not None else '—'},
    {'label': '最大回撤',
     'value': fmt_pct(perf.get('max_drawdown_pct')),
     'raw': f"{perf.get('max_drawdown_pct') * 100:.4f}%" if perf.get('max_drawdown_pct') is not None else '—'},
])

st.markdown('')

# ── 持仓表 + 饼图 ───────────────────────────────────────
col_table, col_pie = st.columns([3, 2])
with col_table:
    st.markdown('#### 当前持仓')
    try:
        positions = get_positions(refresh=False)
    except BackendError as exc:
        error_banner(exc)
        positions = []
    positions_table(positions)

with col_pie:
    st.markdown('#### 权重')
    weights = {}
    for p in positions or []:
        sym = p.get('symbol') or p.get('code') or ''
        w = p.get('weight') or p.get('market_value') or p.get('value') or 0
        if sym:
            weights[sym] = float(w or 0)
    if weights:
        st.plotly_chart(weights_pie(weights, title=''), use_container_width=True)
    else:
        st.caption('暂无持仓')

st.markdown('---')

# ── 现金调整 form ───────────────────────────────────────
st.markdown('#### 调整现金')
with st.form('cash_form', clear_on_submit=False):
    new_cash = st.number_input('新现金值 (¥)', min_value=0.0, step=100.0,
                               value=float(cash), format='%.2f')
    submitted = st.form_submit_button('提交')
if submitted:
    st.session_state['_pending_cash'] = new_cash

if '_pending_cash' in st.session_state:
    target = st.session_state['_pending_cash']
    st.warning(f'即将把现金从 {fmt_money(cash)} 改为 {fmt_money(target)}')
    if confirm_dialog('apply_cash', '不可撤销,确认提交。', confirm_label='⚠️ 确认修改'):
        try:
            set_cash(float(target))
            st.success('已更新')
            del st.session_state['_pending_cash']
            clear_cache()
            st.rerun()
        except BackendError as exc:
            error_banner(exc)

st.markdown('---')

# ── 持仓录入 form ───────────────────────────────────────
st.markdown('#### 手工录入 / 调整持仓')
with st.form('pos_form', clear_on_submit=True):
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    sym = c1.text_input('标的', placeholder='600519.SH')
    shares = c2.number_input('股数', min_value=0, step=100, value=0)
    entry = c3.number_input('成本价', min_value=0.0, step=0.01, value=0.0, format='%.4f')
    name = c4.text_input('名称(可空)')
    pos_submit = st.form_submit_button('提交')
if pos_submit:
    if not sym or shares <= 0 or entry <= 0:
        st.warning('symbol / shares / entry_price 都必填且 > 0')
    else:
        payload = {'symbol': sym.strip(), 'shares': int(shares), 'entry_price': float(entry)}
        if name.strip():
            payload['name'] = name.strip()
        try:
            upsert_position(payload)
            st.success(f'已写入 {sym}')
            clear_cache()
            st.rerun()
        except BackendError as exc:
            error_banner(exc)

st.markdown('---')

# ── 250 天权益 ──────────────────────────────────────────
st.markdown('#### 每日权益(近 250 天)')
try:
    daily = get_daily(limit=250)
    if daily:
        st.plotly_chart(equity_curve(daily), use_container_width=True)
    else:
        st.caption('暂无每日记录')
except BackendError as exc:
    error_banner(exc)

"""ui/pages/dashboard.py — 总览(每天第一眼)。

只读页:
- KPI strip:总权益 / 持仓市值 / 可用现金 / 当日收益 %
- 90 天权益曲线 + 回撤副图
- 风控快照 + 市场状态
- 未处理告警条
- 最近 8 条信号
"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, get_portfolio_summary, get_daily, get_risk_status,
    get_market_status, get_alerts, get_signals,
)
from ui.format import fmt_money, fmt_pct, fmt_num
from ui.widgets.layout import (
    section_header, kpi_row, error_banner, refresh_button, empty_state,
)
from ui.widgets.status import header_status_bar
from ui.widgets.charts import equity_curve, drawdown_curve
from ui.widgets.tables import signals_table


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


header_status_bar()
section_header('总览', '组合健康一眼看完')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

# ── 顶部 KPI ─────────────────────────────────────────────
try:
    summary = get_portfolio_summary()
except BackendError as exc:
    error_banner(exc)
    st.stop()

equity = _pick(summary, 'total_equity', 'equity', 'nav', 'value', default=0)
market_value = _pick(summary, 'market_value', 'positions_value', 'holding_value', default=0)
cash = _pick(summary, 'cash', 'available_cash', default=0)
daily_return = _pick(summary, 'daily_return', 'daily_return_pct',
                     'today_return', 'pnl_pct', default=None)
daily_pnl = _pick(summary, 'daily_pnl', 'today_pnl', default=None)

# 防御：后端有时返回字符串 'None' 而不是 Python None
if isinstance(daily_return, str) and daily_return.lower() in ('none', 'null', ''):
    daily_return = None
if isinstance(daily_pnl, str) and daily_pnl.lower() in ('none', 'null', ''):
    daily_pnl = None

kpi_row([
    {'label': '总权益', 'value': fmt_money(equity)},
    {'label': '持仓市值', 'value': fmt_money(market_value),
     'help': f'占比 {fmt_pct(market_value / equity) if equity else "—"}'},
    {'label': '可用现金', 'value': fmt_money(cash)},
    {'label': '当日收益', 'value': fmt_pct(daily_return, signed=True),
     'delta': fmt_money(daily_pnl) if daily_pnl is not None else None,
     'delta_color': 'normal'},
])

st.markdown('')

# ── 权益曲线 ────────────────────────────────────────────
col_curve, col_side = st.columns([3, 1])

with col_curve:
    st.markdown('#### 权益曲线 · 近 90 天')
    try:
        daily = get_daily(limit=90)
    except BackendError as exc:
        error_banner(exc)
        daily = []
    if daily:
        st.plotly_chart(equity_curve(daily), use_container_width=True)
        with st.expander('回撤副图'):
            st.plotly_chart(drawdown_curve(daily), use_container_width=True)
    else:
        empty_state('暂无每日权益记录',
                    'scheduler 跑过一次 daily_ops_report 之后就会有数据。')

with col_side:
    st.markdown('#### 市场')
    try:
        m = get_market_status()
        is_open = bool(m.get('is_open') or m.get('market_open'))
        session = m.get('session') or '—'
        st.write(f"**当前**: {'🟢 开盘中' if is_open else '⚪ 休市'}  ·  时段 `{session}`")
        nxt = _pick(m, 'next_change', 'next_open', 'next_open_time', default=None)
        if nxt:
            st.caption(f'下一次切换: {nxt}')
        srv = m.get('server_time')
        if srv:
            st.caption(f'服务器时间: {srv}')
    except BackendError as exc:
        error_banner(exc)

    st.markdown('#### 风控')
    try:
        risk = get_risk_status()
        for k, label in [
            ('cvar_95', 'CVaR(95%)'),
            ('cvar', 'CVaR'),
            ('max_drawdown_pct', '最大回撤'),
            ('exposure', '净敞口'),
            ('sector_concentration', '行业集中度'),
        ]:
            v = risk.get(k)
            if v is not None:
                st.write(f'{label}: **{fmt_num(v, decimals=4)}**')
    except BackendError as exc:
        error_banner(exc)

st.markdown('---')

# ── 告警条 ──────────────────────────────────────────────
st.markdown('#### 未处理告警')
try:
    alerts = get_alerts(limit=10)
    unack = [a for a in alerts if not (a.get('acknowledged') or a.get('ack'))]
    if not unack:
        st.success('🟢 当前没有未处理告警')
    else:
        for a in unack[:5]:
            level = (a.get('level') or a.get('severity') or 'info').lower()
            msg = a.get('message') or a.get('text') or str(a)
            ts = a.get('ts') or a.get('timestamp') or ''
            if level in ('error', 'critical'):
                st.error(f'[{ts}] {msg}')
            elif level in ('warn', 'warning'):
                st.warning(f'[{ts}] {msg}')
            else:
                st.info(f'[{ts}] {msg}')
        if len(unack) > 5:
            st.caption(f'另有 {len(unack) - 5} 条 — 见「系统与风控」页。')
except BackendError as exc:
    error_banner(exc)

# ── 最近信号 ────────────────────────────────────────────
st.markdown('#### 最近信号(Top 8)')
try:
    signals_table(get_signals(limit=8))
except BackendError as exc:
    error_banner(exc)

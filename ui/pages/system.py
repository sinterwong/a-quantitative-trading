"""ui/pages/system.py — 系统健康 / 风控 / 交易模式 / 告警。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, clear_cache, get_health, get_monitor_status, get_risk_status,
    get_trading_mode, set_trading_mode, get_alerts, clear_alerts,
)
from ui.config import BACKEND_URL
from ui.format import fmt_num
from ui.widgets.layout import (
    section_header, error_banner, refresh_button, confirm_dialog, kpi_row,
)
from ui.widgets.status import header_status_bar


header_status_bar()
section_header('系统与风控', '运维状态 + 告警管理 + 交易模式切换')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

# ── 健康 KPI ───────────────────────────────────────────
try:
    h = get_health()
except BackendError as exc:
    error_banner(exc)
    h = {}
try:
    mon = get_monitor_status()
except BackendError as exc:
    mon = {}
try:
    mode = get_trading_mode()
except BackendError:
    mode = 'unknown'

kpi_row([
    {'label': '后端', 'value': '🟢 OK' if (h.get('status') == 'ok' or h.get('ok')) else '🔴 异常'},
    {'label': '交易模式', 'value': mode},
    {'label': 'IntradayMonitor',
     'value': '✅ 运行' if (mon.get('data', {}).get('running') or mon.get('is_running')) else '⏸ 停'},
    {'label': '最近 tick',
     'value': str(mon.get('data', {}).get('last_scan_time') or mon.get('last_tick_ts') or mon.get('last_tick') or '—')},
])

st.markdown('')

col_risk, col_mode = st.columns([2, 1])

# ── 风控 ────────────────────────────────────────────────
with col_risk:
    st.markdown('#### 风控快照')
    try:
        risk = get_risk_status()
        st.json(risk, expanded=True)
    except BackendError as exc:
        error_banner(exc)

# ── 交易模式切换 ────────────────────────────────────────
with col_mode:
    st.markdown('#### 交易模式')
    st.caption('simulation = 虚拟券商;live = 实盘(注意)')
    target = st.selectbox('目标模式', ['simulation', 'paper', 'live'],
                          index=0 if mode != 'live' else 2, key='mode_target')
    if st.button('准备切换'):
        st.session_state['_pending_mode'] = target
    if '_pending_mode' in st.session_state:
        t = st.session_state['_pending_mode']
        if t == 'live':
            st.error('⚠️ 即将切换到 LIVE,会接真实 broker!')
        else:
            st.warning(f'即将切换到 {t}')
        if confirm_dialog('apply_mode', '不可撤销 — 立即生效',
                          confirm_label=f'⚠️ 确认切到 {t}'):
            try:
                set_trading_mode(t)
                st.success(f'已切到 {t}')
                del st.session_state['_pending_mode']
                clear_cache()
                st.rerun()
            except BackendError as exc:
                error_banner(exc)

st.markdown('---')

# ── 告警 ────────────────────────────────────────────────
st.markdown('#### 告警记录')
try:
    alerts = get_alerts(limit=50)
except BackendError as exc:
    error_banner(exc)
    alerts = []

if not alerts:
    st.success('🟢 无告警')
else:
    unack = [a for a in alerts if not (a.get('acknowledged') or a.get('ack'))]
    st.caption(f'共 {len(alerts)} 条,未处理 {len(unack)} 条')
    for a in alerts[:30]:
        level = (a.get('level') or a.get('severity') or 'info').lower()
        msg = a.get('message') or a.get('text') or str(a)
        ts = a.get('ts') or a.get('timestamp') or ''
        prefix = f'[{ts}] '
        if level in ('error', 'critical'):
            st.error(prefix + msg)
        elif level in ('warn', 'warning'):
            st.warning(prefix + msg)
        else:
            st.info(prefix + msg)

    st.markdown('')
    if st.button('准备清空告警'):
        st.session_state['_pending_clear_alerts'] = True
    if st.session_state.get('_pending_clear_alerts'):
        st.warning('清空后无法恢复(后端可能仍有日志)')
        if confirm_dialog('clear_alerts', '不可撤销', confirm_label='⚠️ 清空'):
            try:
                clear_alerts()
                st.success('已清空')
                del st.session_state['_pending_clear_alerts']
                clear_cache()
                st.rerun()
            except BackendError as exc:
                error_banner(exc)

st.markdown('---')
st.markdown('#### Prometheus metrics')
st.code(f'curl {BACKEND_URL}/metrics', language='bash')
st.caption('UI 不内嵌 metrics 渲染,Grafana / Prom 自己抓。')

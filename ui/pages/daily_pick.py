"""ui/pages/daily_pick.py — 触发每日选股 + 看上次结果。"""
from __future__ import annotations

import time

import streamlit as st

from ui.api_client import (
    BackendError, clear_cache, trigger_daily_analysis, get_analysis_status,
    get_analysis_health,
)
from ui.widgets.layout import section_header, error_banner, refresh_button
from ui.widgets.status import header_status_bar
from ui.widgets.tables import generic_table


header_status_bar()
section_header('每日选股', '触发 /analysis/run → DynamicStockSelector 流水线')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

# ── 健康 ────────────────────────────────────────────────
try:
    health = get_analysis_health()
    level = (health.get('level') or '').upper()
    badge = {'OK': '🟢', 'WARN': '🟡', 'CRITICAL': '🔴'}.get(level, 'ℹ️')
    st.markdown(f'**分析子系统**: {badge} {level or "未知"}')
    if health.get('notes'):
        st.caption(health['notes'])
except BackendError as exc:
    error_banner(exc)

st.markdown('---')

# ── 触发 ────────────────────────────────────────────────
col_run, col_info = st.columns([1, 2])
with col_run:
    if st.button('🚀 立即运行', type='primary'):
        with st.spinner('运行中(最多 120 秒)...'):
            try:
                trigger_daily_analysis()
                st.success('触发成功,刷新状态...')
                clear_cache()
                time.sleep(0.5)
                st.rerun()
            except BackendError as exc:
                error_banner(exc)

with col_info:
    st.caption(
        '每日 15:10 调度器会自动触发;手动触发用于补跑或盘前演练。'
    )

# ── 上次结果 ────────────────────────────────────────────
st.markdown('#### 上次运行结果')
try:
    status = get_analysis_status()
except BackendError as exc:
    error_banner(exc)
    status = {}

if not status:
    st.info('尚无运行记录')
else:
    last_ts = status.get('last_run') or status.get('timestamp') or status.get('ts')
    if last_ts:
        st.caption(f'上次运行时间: **{last_ts}**')

    # 候选 / 选中
    candidates = status.get('candidates') or status.get('selected') or status.get('picks') or []
    if candidates:
        st.markdown(f'**候选数: {len(candidates)}**')
        generic_table(candidates)
    else:
        # 没有 candidates 字段 → 直接 dump
        with st.expander('原始响应'):
            st.json(status)

"""ui/pages/wfa.py — Walk-Forward Analysis 历史 + 汇总(只读)。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import BackendError, get_wfa_history, get_wfa_summary
from ui.widgets.layout import section_header, error_banner, refresh_button
from ui.widgets.status import header_status_bar
from ui.widgets.tables import generic_table


header_status_bar()
section_header('WFA 研究', 'Walk-Forward Analysis 历史 / 汇总(只读)')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

st.info(
    '🛠 跑新 WFA 任务用 `python scripts/walkforward_job.py --start 20200101 --end 20251231`,'
    'UI 不在线触发以免阻塞 streamlit。'
)

col_sym, col_limit = st.columns([2, 1])
sym_filter = col_sym.text_input('symbol(可空,过滤历史)', value='').strip() or None
limit = int(col_limit.number_input('返回条数', min_value=10, max_value=500, value=50, step=10))

st.markdown('#### 历史')
try:
    history = get_wfa_history(symbol=sym_filter, limit=limit)
    if not history:
        st.caption('暂无 WFA 历史记录')
    else:
        generic_table(history)
except BackendError as exc:
    error_banner(exc)

st.markdown('---')

st.markdown('#### 汇总')
sum_sym = st.text_input('指定 symbol 看汇总', value=sym_filter or '',
                        placeholder='600519.SH').strip()
if sum_sym:
    try:
        s = get_wfa_summary(sum_sym)
        st.json(s, expanded=True)
    except BackendError as exc:
        error_banner(exc)
else:
    st.caption('输入 symbol 才能看汇总(后端要求 symbol 参数)')

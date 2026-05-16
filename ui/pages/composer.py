"""ui/pages/composer.py — 组合优化(走新 POST /portfolio/compose)。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import BackendError, compose_portfolio, get_watchlist
from ui.format import fmt_pct, fmt_num
from ui.widgets.layout import section_header, error_banner, kpi_row
from ui.widgets.status import header_status_bar
from ui.widgets.forms import universe_input
from ui.widgets.charts import weights_pie, weights_bar


header_status_bar()
section_header('组合优化', '基于历史日 K 收益 → 建议权重(不下单)')

# ── 从自选池一键填入 ───────────────────────────────────
col_fill, col_method = st.columns([1, 2])
with col_fill:
    if st.button('📥 用自选池填充', use_container_width=True):
        try:
            items = get_watchlist()
            syms = [it.get('symbol') for it in items if it.get('symbol')]
            st.session_state['composer_universe'] = '\n'.join(syms)
        except BackendError as exc:
            error_banner(exc)
with col_method:
    method = st.selectbox(
        '优化方法',
        ['min_variance', 'max_sharpe', 'risk_parity',
         'max_diversification', 'equal_weight'],
    )

# ── universe ───────────────────────────────────────────
universe = universe_input(
    key='composer_universe',
    default=st.session_state.get('composer_universe', '').splitlines(),
)

# ── 参数 ───────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
hist_days = c1.number_input('history_days', min_value=30, max_value=2000,
                            value=252, step=10)
max_w = c2.number_input('max_weight', min_value=0.0, max_value=1.0, value=0.25, step=0.05)
min_w = c3.number_input('min_weight', min_value=0.0, max_value=1.0, value=0.0, step=0.01)
rf = c4.number_input('rf_annual', min_value=0.0, max_value=0.2, value=0.02, step=0.005,
                     format='%.3f')
cov_method = st.selectbox('cov_method', ['ledoit_wolf', 'sample', 'oas'], index=0)

run_btn = st.button('🚀 计算建议权重', type='primary', disabled=len(universe) < 2)
if len(universe) < 2:
    st.caption('至少 2 个资产')

st.markdown('---')

if run_btn:
    with st.spinner('计算中...'):
        try:
            st.session_state['_compose_res'] = compose_portfolio({
                'universe': universe,
                'method': method,
                'history_days': int(hist_days),
                'max_weight': float(max_w),
                'min_weight': float(min_w),
                'cov_method': cov_method,
                'rf_annual': float(rf),
            })
        except BackendError as exc:
            error_banner(exc)

res = st.session_state.get('_compose_res')
if res:
    st.markdown('#### 结果')
    kpi_row([
        {'label': '方法', 'value': str(res.get('method', '—'))},
        {'label': '资产数', 'value': str(res.get('n_assets', '—'))},
        {'label': '预期年化收益',
         'value': fmt_pct(res.get('expected_return'), signed=True)},
        {'label': '预期年化波动', 'value': fmt_pct(res.get('expected_vol'))},
        {'label': '夏普', 'value': fmt_num(res.get('sharpe'), decimals=2)},
    ])

    weights = res.get('weights') or {}
    if weights:
        c_pie, c_bar = st.columns(2)
        with c_pie:
            st.plotly_chart(weights_pie(weights, title='权重分布'),
                            use_container_width=True)
        with c_bar:
            st.plotly_chart(weights_bar(weights, title='权重明细'),
                            use_container_width=True)

    diag = res.get('diagnostics') or {}
    with st.expander('诊断 / 排除'):
        st.json(diag)
        if diag.get('excluded_symbols'):
            st.warning(f'被排除: {diag["excluded_symbols"]}')

    with st.expander('原始响应'):
        st.json(res)

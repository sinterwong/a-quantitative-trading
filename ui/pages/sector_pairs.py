"""ui/pages/sector_pairs.py — 板块轮动 / 配对交易 / 板块对比。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, sector_rotation, pairs_trading, sector_compare,
)
from ui.widgets.layout import section_header, error_banner
from ui.widgets.status import header_status_bar
from ui.widgets.tables import generic_table


header_status_bar()
section_header('板块与配对', '板块轮动 · 配对交易 · 板块对比')

tab_rot, tab_pair, tab_cmp = st.tabs(['板块轮动', '配对交易', '板块对比'])

# ── 板块轮动 ───────────────────────────────────────────
with tab_rot:
    with st.form('rot_form'):
        c1, c2, c3, c4 = st.columns(4)
        top_n = c1.number_input('top_n', min_value=1, max_value=20, value=3)
        lb = c2.number_input('lookback_days', min_value=10, max_value=252, value=60)
        rb = c3.number_input('rebalance_days', min_value=1, max_value=60, value=21)
        method = c4.selectbox('momentum_method', ['return', 'sharpe'])
        cur_raw = st.text_area('当前持仓(每行一个,可空)', height=80)
        rot_submit = st.form_submit_button('计算', type='primary')

    if rot_submit:
        cur_holdings = [s.strip() for s in cur_raw.splitlines() if s.strip()]
        with st.spinner('计算中...'):
            try:
                res = sector_rotation({
                    'top_n': int(top_n),
                    'lookback_days': int(lb),
                    'rebalance_days': int(rb),
                    'momentum_method': method,
                    'current_holdings': cur_holdings,
                })
            except BackendError as exc:
                error_banner(exc)
                res = None
        if res:
            c1, c2, c3 = st.columns(3)
            c1.markdown('**🟢 BUY**')
            for s in res.get('buy', []):
                c1.markdown(f'- `{s}`')
            c2.markdown('**🔴 SELL**')
            for s in res.get('sell', []):
                c2.markdown(f'- `{s}`')
            c3.markdown('**🟡 HOLD**')
            for s in res.get('hold', []):
                c3.markdown(f'- `{s}`')

            st.markdown('---')
            st.markdown(
                f'**再平衡日**: {res.get("rebalance_date", "—")}'
                f'   ·   **样本数**: {res.get("universe_size", "—")}'
                f'   ·   **top_n**: {res.get("top_n", "—")}'
            )

            scores = res.get('scores') or {}
            if scores:
                st.markdown('##### 分数')
                generic_table([{'symbol': k, 'score': v} for k, v in scores.items()])

# ── 配对交易 ───────────────────────────────────────────
with tab_pair:
    with st.form('pair_form'):
        syms_raw = st.text_area(
            '候选标的池(每行一个,至少 2 个)', height=120,
            placeholder='600519.SH\n000858.SZ\n000568.SZ',
        )
        c1, c2, c3 = st.columns(3)
        entry_z = c1.number_input('entry_z', value=2.0, step=0.1)
        exit_z = c2.number_input('exit_z', value=0.5, step=0.1)
        stop_z = c3.number_input('stop_z', value=4.0, step=0.1)
        c4, c5 = st.columns(2)
        lb_days = c4.number_input('lookback_days', min_value=20, max_value=500, value=60)
        sc_days = c5.number_input('screen_days', min_value=60, max_value=1000, value=252)
        pair_submit = st.form_submit_button('查找配对', type='primary')

    if pair_submit:
        symbols = [s.strip() for s in syms_raw.splitlines() if s.strip()]
        if len(symbols) < 2:
            st.warning('至少 2 个标的')
        else:
            with st.spinner('扫描中...'):
                try:
                    res = pairs_trading({
                        'symbols': symbols,
                        'entry_z': float(entry_z), 'exit_z': float(exit_z),
                        'stop_z': float(stop_z),
                        'lookback_days': int(lb_days),
                        'screen_days': int(sc_days),
                    })
                except BackendError as exc:
                    error_banner(exc)
                    res = None
            if res:
                st.markdown(f'**找到 {res.get("n_pairs_found", 0)} 对**')
                pairs = res.get('pairs') or []
                if pairs:
                    rows = []
                    for p in pairs:
                        sig = p.get('signal') or {}
                        rows.append({
                            'A': p.get('symbol_a'),
                            'B': p.get('symbol_b'),
                            'spread_z': sig.get('spread_zscore'),
                            'action_a': sig.get('action_a'),
                            'action_b': sig.get('action_b'),
                            'corr': sig.get('correlation'),
                        })
                    generic_table(rows)
                else:
                    st.caption('当前阈值下无信号')

# ── 板块对比 ───────────────────────────────────────────
with tab_cmp:
    with st.form('cmp_form'):
        c1, c2 = st.columns([1, 2])
        sector = c1.text_input('板块代码(可空)', placeholder='801080.SI')
        syms_raw = c2.text_area('或标的列表(每行一个)', height=100)
        cmp_submit = st.form_submit_button('对比', type='primary')
    if cmp_submit:
        syms = [s.strip() for s in syms_raw.splitlines() if s.strip()]
        payload: dict = {}
        if sector.strip():
            payload['sector'] = sector.strip()
        if syms:
            payload['symbols'] = syms
        if not payload:
            st.warning('需要板块代码或标的列表')
        else:
            with st.spinner('对比中...'):
                try:
                    res = sector_compare(payload)
                    st.json(res, expanded=True)
                except BackendError as exc:
                    error_banner(exc)

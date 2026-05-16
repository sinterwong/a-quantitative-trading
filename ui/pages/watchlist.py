"""ui/pages/watchlist.py — 自选池 + 实时报价 + 每标的参数。"""
from __future__ import annotations

import streamlit as st
import pandas as pd

from ui.api_client import (
    BackendError, clear_cache, get_watchlist, watchlist_add, watchlist_remove,
    watchlist_patch, get_realtime, get_params, patch_params,
)
from ui.format import fmt_money, fmt_pct, fmt_num
from ui.widgets.layout import section_header, error_banner, refresh_button
from ui.widgets.status import header_status_bar


header_status_bar()
section_header('盯盘自选池', '管理自选 + 实时行情 + 每标的风控参数')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

# ── 自选清单 ───────────────────────────────────────────
try:
    items = get_watchlist()
except BackendError as exc:
    error_banner(exc)
    st.stop()

if not items:
    st.info('当前自选池为空,先在下方加几只。')
else:
    st.markdown('#### 实时报价')
    rows = []
    for it in items:
        sym = it.get('symbol') or it.get('code') or ''
        if not sym:
            continue
        row = {
            'symbol': sym, 'name': it.get('name', ''),
            'reason': it.get('reason', ''),
            'alert_pct': it.get('alert_pct'),
            'enabled': it.get('enabled', True),
        }
        try:
            rt = get_realtime(sym)
            row.update({
                'last': rt.get('last') or rt.get('price') or rt.get('current'),
                'chg_pct': rt.get('chg_pct') or rt.get('change_pct')
                          or rt.get('pct_change'),
                'volume': rt.get('volume') or rt.get('vol'),
                'turnover': rt.get('turnover'),
            })
        except BackendError:
            row.update({'last': None, 'chg_pct': None})
        rows.append(row)
    df = pd.DataFrame(rows)
    if 'last' in df.columns:
        df['last_disp'] = df['last'].apply(fmt_money)
    if 'chg_pct' in df.columns:
        df['chg_disp'] = df['chg_pct'].apply(lambda v: fmt_pct(v, signed=True))
    if 'volume' in df.columns:
        df['vol_disp'] = df['volume'].apply(fmt_num)
    display_cols = [c for c in [
        'symbol', 'name', 'last_disp', 'chg_disp', 'vol_disp',
        'alert_pct', 'reason', 'enabled',
    ] if c in df.columns]
    st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

st.markdown('---')

# ── 添加 ───────────────────────────────────────────────
col_add, col_edit = st.columns(2)
with col_add:
    st.markdown('#### 添加')
    with st.form('add_wl', clear_on_submit=True):
        a_sym = st.text_input('标的代码', placeholder='600900.SH')
        a_name = st.text_input('名称(可空)')
        a_reason = st.text_input('理由(可空)')
        a_alert = st.number_input('告警阈值 %', min_value=0.0, max_value=50.0,
                                  value=5.0, step=0.5)
        add_ok = st.form_submit_button('添加')
    if add_ok:
        if not a_sym.strip():
            st.warning('symbol 必填')
        else:
            try:
                watchlist_add({
                    'symbol': a_sym.strip(),
                    'name': a_name.strip(),
                    'reason': a_reason.strip(),
                    'alert_pct': float(a_alert),
                })
                st.success('已添加')
                clear_cache()
                st.rerun()
            except BackendError as exc:
                error_banner(exc)

with col_edit:
    st.markdown('#### 编辑 / 移除')
    if items:
        sym_options = [it.get('symbol') for it in items if it.get('symbol')]
        sel = st.selectbox('选择标的', sym_options)
        if sel:
            current = next(it for it in items if it.get('symbol') == sel)
            with st.form('edit_wl'):
                e_alert = st.number_input('告警阈值 %', min_value=0.0, max_value=50.0,
                                          value=float(current.get('alert_pct') or 5.0),
                                          step=0.5)
                e_reason = st.text_input('理由', value=current.get('reason', ''))
                e_enabled = st.checkbox('启用', value=bool(current.get('enabled', True)))
                colp, cold = st.columns(2)
                save_ok = colp.form_submit_button('保存')
                del_ok = cold.form_submit_button('🗑 移除', type='secondary')
            if save_ok:
                try:
                    watchlist_patch(sel, {
                        'alert_pct': float(e_alert),
                        'reason': e_reason,
                        'enabled': bool(e_enabled),
                    })
                    st.success('已更新')
                    clear_cache()
                    st.rerun()
                except BackendError as exc:
                    error_banner(exc)
            if del_ok:
                try:
                    watchlist_remove(sel)
                    st.success(f'{sel} 已移除')
                    clear_cache()
                    st.rerun()
                except BackendError as exc:
                    error_banner(exc)
    else:
        st.caption('自选池为空')

st.markdown('---')

# ── 每标的参数 ─────────────────────────────────────────
st.markdown('#### 每标的风控参数')
st.caption('字段白名单见 backend services.signals.PARAM_FIELDS_ALLOWED;'
           '止损/止盈是小数(0.05 = 5%)。')
if items:
    sym_options = [it.get('symbol') for it in items if it.get('symbol')]
    p_sel = st.selectbox('查看参数', sym_options, key='param_sel')
    if p_sel:
        # backend 返 {symbol, params: {...}};只读 params 子字典
        try:
            resp = get_params(p_sel)
        except BackendError as exc:
            error_banner(exc)
            resp = {}
        params = resp.get('params') if isinstance(resp.get('params'), dict) else {}
        with st.form('param_form'):
            c1, c2 = st.columns(2)
            stop_loss = c1.number_input(
                'stop_loss (止损,小数)', min_value=0.0, max_value=0.5,
                value=float(params.get('stop_loss') or 0.05), step=0.005, format='%.3f')
            take_profit = c2.number_input(
                'take_profit (止盈,小数)', min_value=0.0, max_value=2.0,
                value=float(params.get('take_profit') or 0.2), step=0.01, format='%.3f')
            c3, c4 = st.columns(2)
            rsi_buy = c3.number_input(
                'rsi_buy', min_value=0, max_value=100,
                value=int(params.get('rsi_buy') or 30), step=1)
            rsi_sell = c4.number_input(
                'rsi_sell', min_value=0, max_value=100,
                value=int(params.get('rsi_sell') or 70), step=1)
            min_hold = st.number_input(
                'min_hold_days', min_value=0, max_value=60,
                value=int(params.get('min_hold_days') or 1), step=1)
            p_save = st.form_submit_button('保存参数')
        if p_save:
            try:
                patch_params(p_sel, {
                    'stop_loss': float(stop_loss),
                    'take_profit': float(take_profit),
                    'rsi_buy': int(rsi_buy),
                    'rsi_sell': int(rsi_sell),
                    'min_hold_days': int(min_hold),
                })
                st.success(f'{p_sel} 参数已更新')
                clear_cache()
            except BackendError as exc:
                error_banner(exc)
else:
    st.caption('自选池为空,先添加标的')

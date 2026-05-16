"""ui/pages/signals.py — 信号 / 成交 / 订单 / 下单。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, clear_cache, get_signals, get_trades, get_orders_recent,
    get_orders_pending, submit_order, cancel_order, record_signal, record_trade,
)
from ui.widgets.layout import (
    section_header, error_banner, refresh_button, confirm_dialog,
)
from ui.widgets.status import header_status_bar
from ui.widgets.tables import signals_table, trades_table, orders_table


header_status_bar()
section_header('信号与交易', '查信号 / 成交 / 挂单,人工下单口')

cols = st.columns([8, 1])
with cols[1]:
    refresh_button()

filter_symbol = st.text_input('按标的过滤(空 = 全部)', value='').strip() or None

tab_sig, tab_trd, tab_pend, tab_recent, tab_new = st.tabs(
    ['信号', '成交', '挂单', '近期订单', '下单 / 录入']
)

with tab_sig:
    try:
        signals_table(get_signals(limit=100, symbol=filter_symbol))
    except BackendError as exc:
        error_banner(exc)

with tab_trd:
    try:
        trades_table(get_trades(limit=100, symbol=filter_symbol))
    except BackendError as exc:
        error_banner(exc)

with tab_pend:
    try:
        pending = get_orders_pending()
    except BackendError as exc:
        error_banner(exc)
        pending = []
    orders_table(pending)
    if pending:
        st.markdown('##### 取消挂单')
        for od in pending[:20]:
            oid = od.get('order_id') or od.get('id')
            sym = od.get('symbol', '-')
            side = od.get('side', '-')
            qty = od.get('qty') or od.get('shares', '-')
            if not oid:
                continue
            if st.button(f'取消 {sym} {side} {qty}  (#{oid})',
                         key=f'cancel_{oid}'):
                try:
                    cancel_order(str(oid))
                    st.success(f'已取消 {oid}')
                    clear_cache()
                    st.rerun()
                except BackendError as exc:
                    error_banner(exc)

with tab_recent:
    try:
        orders_table(get_orders_recent())
    except BackendError as exc:
        error_banner(exc)

with tab_new:
    sub_order, sub_signal, sub_trade = st.tabs(['下单', '录入信号', '录入成交'])

    # ── 下单 ────────────────────────────────────────
    with sub_order:
        with st.form('order_form'):
            c1, c2, c3 = st.columns(3)
            o_sym = c1.text_input('标的', placeholder='600519.SH')
            o_side = c2.selectbox('方向', ['BUY', 'SELL'])
            o_type = c3.selectbox('订单类型', ['LIMIT', 'MARKET', 'VWAP', 'TWAP'])
            c4, c5 = st.columns(2)
            o_qty = c4.number_input('股数', min_value=0, step=100, value=0)
            o_price = c5.number_input('价格(MARKET 单填 0)', min_value=0.0, step=0.01,
                                      value=0.0, format='%.4f')
            o_note = st.text_input('备注(可空)')
            order_submit = st.form_submit_button('准备提交')
        if order_submit:
            if not o_sym or o_qty <= 0:
                st.warning('symbol 必填,股数 > 0')
            else:
                st.session_state['_pending_order'] = {
                    'symbol': o_sym.strip(),
                    'side': o_side,
                    'order_type': o_type,
                    'qty': int(o_qty),
                    'price': float(o_price),
                    'note': o_note,
                }

        if '_pending_order' in st.session_state:
            p = st.session_state['_pending_order']
            st.warning(f'**待提交订单**: {p["side"]} {p["qty"]} 股 {p["symbol"]} '
                       f'@ {p["price"]:.4f} ({p["order_type"]})')
            if confirm_dialog('submit_order', '不可撤销 — 走 broker',
                              confirm_label='⚠️ 提交订单'):
                try:
                    res = submit_order({k: v for k, v in p.items() if v != ''})
                    st.success(f'订单已提交: {res}')
                    del st.session_state['_pending_order']
                    clear_cache()
                except BackendError as exc:
                    error_banner(exc)

    # ── 录入信号 ─────────────────────────────────
    with sub_signal:
        with st.form('signal_form', clear_on_submit=True):
            s_sym = st.text_input('标的', key='sig_sym')
            c1, c2 = st.columns(2)
            s_type = c1.selectbox('信号类型', ['BUY', 'SELL', 'HOLD', 'WATCH'])
            s_strength = c2.slider('强度', 0.0, 1.0, 0.5, 0.05)
            s_reason = st.text_area('原因 / 备注', max_chars=400)
            s_submit = st.form_submit_button('提交')
        if s_submit:
            if not s_sym.strip():
                st.warning('symbol 必填')
            else:
                try:
                    record_signal({
                        'symbol': s_sym.strip(),
                        'signal_type': s_type,
                        'strength': s_strength,
                        'reason': s_reason,
                    })
                    st.success('已写入')
                    clear_cache()
                except BackendError as exc:
                    error_banner(exc)

    # ── 录入成交 ─────────────────────────────────
    with sub_trade:
        with st.form('trade_form', clear_on_submit=True):
            t_sym = st.text_input('标的', key='trd_sym')
            c1, c2 = st.columns(2)
            t_dir = c1.selectbox('方向', ['BUY', 'SELL'])
            t_shares = c2.number_input('股数', min_value=0, step=100)
            c3, c4 = st.columns(2)
            t_price = c3.number_input('成交价', min_value=0.0, step=0.01,
                                      value=0.0, format='%.4f')
            t_pnl = c4.number_input('盈亏(可空,默认 0)', value=0.0,
                                    step=0.01, format='%.2f')
            t_note = st.text_input('备注(可空)')
            t_submit = st.form_submit_button('提交')
        if t_submit:
            if not t_sym.strip() or t_shares <= 0 or t_price <= 0:
                st.warning('symbol / shares / price 必填')
            else:
                try:
                    record_trade({
                        'symbol': t_sym.strip(),
                        'direction': t_dir,
                        'shares': int(t_shares),
                        'price': float(t_price),
                        'pnl': float(t_pnl),
                        'note': t_note,
                    })
                    st.success('已写入')
                    clear_cache()
                except BackendError as exc:
                    error_banner(exc)

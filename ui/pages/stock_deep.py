"""ui/pages/stock_deep.py — 个股深度(A 股 / 港股 / 基本面 / 新闻 / LLM / K 线)。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, analyze_a_stock, analyze_hk_stock, get_fundamentals,
    get_news, llm_analyze, get_daily_kline,
)
from ui.widgets.layout import section_header, error_banner
from ui.widgets.status import header_status_bar
from ui.widgets.forms import symbol_input, market_toggle
from ui.widgets.charts import kline
from ui.widgets.tables import generic_table


header_status_bar()
section_header('个股深度', 'A 股 / 港股综合分析 · 基本面 · 新闻 · LLM 解读 · K 线')

cols = st.columns([2, 1, 1])
with cols[0]:
    sym = symbol_input(key='stock_deep_sym',
                       placeholder='A: 600519.SH / sh600519  |  HK: 00700.HK')
with cols[1]:
    market = market_toggle(key='stock_deep_mkt')
with cols[2]:
    run = st.button('🔎 运行分析', type='primary', use_container_width=True)

if not sym:
    st.info('输入标的代码后点「运行分析」。')
    st.stop()

tab_an, tab_fun, tab_news, tab_llm, tab_kl = st.tabs(
    ['综合分析', '基本面', '新闻', 'LLM 解读', 'K 线']
)

# ── 综合分析 ───────────────────────────────────────────
with tab_an:
    if run:
        with st.spinner('分析中...'):
            try:
                payload = {'symbol': sym}
                if market == 'a':
                    res = analyze_a_stock(payload)
                else:
                    res = analyze_hk_stock(payload)
                st.session_state['_stock_deep_res'] = res
            except BackendError as exc:
                error_banner(exc)
    res = st.session_state.get('_stock_deep_res')
    if res:
        st.json(res, expanded=False)
    else:
        st.caption('点上方按钮运行。')

# ── 基本面 ────────────────────────────────────────────
with tab_fun:
    try:
        f = get_fundamentals(sym)
        st.json(f, expanded=True)
    except BackendError as exc:
        error_banner(exc)

# ── 新闻 ──────────────────────────────────────────────
with tab_news:
    n = st.slider('条数', 3, 30, 8)
    try:
        items = get_news(sym, n=n)
        if not items:
            st.caption('暂无新闻')
        else:
            for i, it in enumerate(items, 1):
                title = it.get('title') or it.get('headline') or str(it)
                url = it.get('url') or it.get('link')
                ts = it.get('ts') or it.get('time') or it.get('publish_time') or ''
                if url:
                    st.markdown(f'{i}. [{title}]({url})  <span class="small-muted">{ts}</span>',
                                unsafe_allow_html=True)
                else:
                    st.markdown(f'{i}. {title}  *{ts}*')
    except BackendError as exc:
        error_banner(exc)

# ── LLM ───────────────────────────────────────────────
with tab_llm:
    st.warning('调用 LLM 会产生费用,确认后点按钮。')
    if st.button('🤖 调用 LLM 分析', key='llm_call_btn'):
        with st.spinner('LLM 思考中(最多 120 秒)...'):
            try:
                out = llm_analyze({'symbol': sym, 'market': market})
                st.session_state['_llm_out'] = out
            except BackendError as exc:
                error_banner(exc)
    out = st.session_state.get('_llm_out')
    if out:
        if isinstance(out, dict):
            text = out.get('text') or out.get('summary') or out.get('answer')
            if text:
                st.markdown(text)
            else:
                st.json(out)
        else:
            st.write(out)

# ── K 线 ──────────────────────────────────────────────
with tab_kl:
    days = st.slider('天数', 30, 500, 120, step=10)
    try:
        bars = get_daily_kline(sym, days=days)
        st.plotly_chart(kline(bars, title=f'{sym} 日 K · 近 {days} 天'),
                        use_container_width=True)
    except BackendError as exc:
        error_banner(exc)

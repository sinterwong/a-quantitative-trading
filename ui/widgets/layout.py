"""ui/widgets/layout.py — 全局 CSS / 标题 / KPI / 错误条。"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import streamlit as st

from ui.api_client import BackendError
from ui.format import fmt_money


def global_css() -> None:
    # 卡片 + 文字色显式钉死(白底 → 深字),不再让 Streamlit 主题反差掉
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
        section[data-testid="stSidebar"] .stRadio > label {font-weight: 600;}
        [data-testid="stMetric"] {
            background: #ffffff; padding: 12px 16px;
            border: 1px solid #e1e4e8; border-radius: 8px;
            color: #24292e;
        }
        [data-testid="stMetric"] * {color: #24292e !important;}
        [data-testid="stMetricLabel"] * {color: #57606a !important; font-weight: 500;}
        [data-testid="stMetricValue"] * {color: #1f2328 !important; font-weight: 600;}
        [data-testid="stMetricDelta"] svg {fill: currentColor;}
        .small-muted {color: #6a737d; font-size: 0.85rem;}
        .badge {display: inline-block; padding: 2px 8px; border-radius: 10px;
            font-size: 0.78rem; font-weight: 600;}
        .badge.ok   {background: #e6ffed; color: #22863a;}
        .badge.warn {background: #fff5b1; color: #735c0f;}
        .badge.err  {background: #ffeef0; color: #b31d28;}
        .badge.info {background: #e1f0ff; color: #0366d6;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_header(title: str, subtitle: Optional[str] = None) -> None:
    st.markdown(f'## {title}')
    if subtitle:
        st.markdown(f'<div class="small-muted">{subtitle}</div>', unsafe_allow_html=True)
    st.divider()


def kpi_row(items: Sequence[dict]) -> None:
    """5 等宽 KPI 卡片行，HTML 渲染以支持鼠标悬停 tooltip。

    items: [{'label': '可用现金', 'value': '¥12,345', 'delta': '+1.2%',
             'raw': '12345.67'}]。raw 用于 tooltip，若不提供则用 value。
    """
    if not items:
        return

    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        label = it.get('label', '')
        value = it.get('value', '—')
        raw = it.get('raw')
        tooltip = str(raw) if raw is not None else str(value)
        delta = it.get('delta', '')
        # 涨红跌绿（A 股惯例）；'—' 不是有效 delta，等同于无 delta
        if delta and delta not in ('—',):
            dc = it.get('delta_color', 'normal')
            if dc == 'normal':
                if delta.startswith('+'):
                    dc = '#d9534f'
                elif delta.startswith('-'):
                    dc = '#5cb85c'
                else:
                    dc = '#6c757d'
            delta = f'<span style="color:{dc};font-size:0.8rem;margin-left:6px;">{delta}</span>'
        else:
            delta = ''

        col.markdown(
            f'''<div style="
                background:#ffffff;
                border:1px solid #e1e4e8;
                border-radius:8px;
                padding:12px 16px;
                display:flex;
                flex-direction:column;
                gap:4px;
                min-width:0;
            " title="{tooltip}">
                <span style="color:#57606a;font-size:0.8rem;font-weight:500;">{label}</span>
                <span style="color:#1f2328;font-size:1.15rem;font-weight:600;
                            letter-spacing:-0.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                    {value}{delta}
                </span>
            </div>''',
            unsafe_allow_html=True,
        )


def cash_display(cash: float) -> None:
    """单独渲染现金，用 HTML 避免 st.metric 截断长数字。

    用法:
        cash_display(get_cash())   # 独占一行，字体够大看不截断
    """
    cash_str = fmt_money(cash)
    st.markdown(
        f"""
        <div style="
            background: #ffffff;
            border: 1px solid #e1e4e8;
            border-radius: 8px;
            padding: 14px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        " title="{cash_str}">
            <span style="color: #57606a; font-size: 0.85rem; font-weight: 500;">可用现金</span>
            <span style="color: #1f2328; font-size: 1.1rem; font-weight: 600; letter-spacing: -0.02em;">
                {cash_str}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def error_banner(exc: BaseException) -> None:
    """page 入口统一 try/except BackendError 后调这个渲染。"""
    if isinstance(exc, BackendError):
        if exc.status in (401, 403):
            st.error(f'后端拒绝请求({exc.status}): {exc.message}\n\n'
                     f'检查 TRADING_API_KEY env 是否与后端一致。')
        elif exc.status >= 500:
            st.error(f'后端 {exc.status} 错误:{exc.message}\n\n'
                     f'看 `tail -F backend/backend.log`。')
        else:
            st.warning(f'请求被拒({exc.status}):{exc.message}')
    else:
        st.error(f'未预期错误:{exc!r}')


def empty_state(message: str, hint: Optional[str] = None) -> None:
    st.info(message)
    if hint:
        st.caption(hint)


def refresh_button(label: str = '刷新') -> bool:
    """点击后清缓存并 rerun。返回是否点击。"""
    clicked = st.button(label, icon=':material/refresh:', type='secondary')
    if clicked:
        st.cache_data.clear()
        st.rerun()
    return clicked


def confirm_dialog(key: str, prompt: str, confirm_label: str = '确认') -> bool:
    """两段式确认: 第一次点击 -> session_state 标位 -> 第二次点击 -> 返回 True。

    用法:
        if confirm_dialog('do_x', '确定要 X 吗?'):
            do_x()
    """
    flag = f'_confirm_{key}'
    if st.session_state.get(flag):
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button(f'⚠️ {confirm_label}', key=f'{flag}_yes', type='primary'):
                st.session_state[flag] = False
                return True
        with col2:
            if st.button('取消', key=f'{flag}_no'):
                st.session_state[flag] = False
                st.rerun()
        st.caption(prompt)
    else:
        if st.button(confirm_label, key=f'{flag}_init'):
            st.session_state[flag] = True
            st.rerun()
    return False

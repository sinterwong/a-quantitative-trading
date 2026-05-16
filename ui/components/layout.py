"""
ui/components/layout.py — 全局布局组件 (P4-1 阶段二)

不持有业务状态,仅做样式 / 文案映射 / Streamlit 元素渲染。
"""

from __future__ import annotations

import streamlit as st


_REGIME_CLASS = {
    'BULL': 'regime-bull',
    'BEAR': 'regime-bear',
    'VOLATILE': 'regime-vol',
    'CALM': 'regime-calm',
}

_REGIME_ZH = {
    'BULL': '牛市', 'BEAR': '熊市',
    'VOLATILE': '震荡', 'CALM': '平静',
    'UNKNOWN': '未知',
}


def regime_zh(regime: str) -> str:
    """Regime 英文 → 中文显示文案。"""
    return _REGIME_ZH.get(regime, regime)


def regime_badge(regime: str) -> None:
    """渲染 regime 徽章(配色 + 中文)到当前 streamlit 上下文。"""
    cls = _REGIME_CLASS.get(regime, 'regime-calm')
    st.markdown(
        f'<span class="{cls}">{regime_zh(regime)}</span>',
        unsafe_allow_html=True,
    )


def broker_badge() -> None:
    """SimulatedBroker 徽章(锁产品定位:虚拟模拟盘)。"""
    st.sidebar.markdown(
        '<span class="broker-badge">SimulatedBroker</span>',
        unsafe_allow_html=True,
    )
    st.sidebar.caption('A 股规则 · 整手 · 印花税 0.1% · 涨跌停保护')


def global_css() -> None:
    """注入全局 CSS(broker 徽章 + regime 徽章 + 因子方向色)。"""
    st.markdown("""
<style>
.broker-badge {
    background: #0e4429; color: #3fb950;
    padding: 4px 12px; border-radius: 20px;
    font-size: 0.85rem; font-weight: 600;
}
.regime-bull   { background:#0e4429; color:#3fb950; padding:4px 10px; border-radius:16px; font-weight:700; }
.regime-bear   { background:#3d0f0f; color:#f85149; padding:4px 10px; border-radius:16px; font-weight:700; }
.regime-vol    { background:#2d1f00; color:#e3b341; padding:4px 10px; border-radius:16px; font-weight:700; }
.regime-calm   { background:#161b22; color:#8b949e; padding:4px 10px; border-radius:16px; font-weight:700; }
.factor-bar-pos { color:#3fb950; }
.factor-bar-neg { color:#f85149; }
</style>
""", unsafe_allow_html=True)

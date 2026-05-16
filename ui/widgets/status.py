"""ui/widgets/status.py — 顶部状态条(健康 / broker / 告警计数)。"""
from __future__ import annotations

import streamlit as st

from ui.api_client import (
    BackendError, get_health, get_trading_mode, get_alerts, get_market_status,
)


def _badge(text: str, kind: str = 'info') -> str:
    return f'<span class="badge {kind}">{text}</span>'


def header_status_bar() -> None:
    """顶部一行: 后端健康 · 交易模式 · 市场状态 · 未读告警计数。"""
    parts = []

    # 后端健康
    try:
        h = get_health()
        ok = h.get('status') == 'ok' or h.get('ok') is True or h.get('healthy') is True
        parts.append(_badge('后端 OK' if ok else '后端异常', 'ok' if ok else 'err'))
    except BackendError as exc:
        parts.append(_badge(f'后端不可达 ({exc.status})', 'err'))
    except Exception:
        parts.append(_badge('后端不可达', 'err'))

    # 交易模式
    try:
        mode = (get_trading_mode() or '').lower()
        kind = 'warn' if mode in ('live', 'real', 'production') else 'info'
        parts.append(_badge(f'模式 · {mode or "unknown"}', kind))
    except Exception:
        parts.append(_badge('模式未知', 'warn'))

    # 市场状态
    try:
        m = get_market_status()
        is_open = m.get('is_open') or m.get('open') or m.get('market_open')
        parts.append(_badge('市场开盘中' if is_open else '市场休市', 'ok' if is_open else 'info'))
    except Exception:
        pass

    # 告警计数
    try:
        alerts = get_alerts(limit=50)
        unack = [a for a in alerts if not (a.get('acknowledged') or a.get('ack'))]
        if unack:
            parts.append(_badge(f'未处理告警 · {len(unack)}', 'warn'))
        else:
            parts.append(_badge('无未处理告警', 'ok'))
    except Exception:
        pass

    st.markdown(' &nbsp; '.join(parts), unsafe_allow_html=True)

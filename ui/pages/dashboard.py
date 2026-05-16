"""
ui/pages/dashboard.py — 📊 仪表盘 (P4-1 阶段二)

operator 主视图:账户摘要 + 净值曲线 + Regime + Top 信号 + 系统告警。

完全契合产品定位"准生产实盘 + 监控":
  - 数据全部来自 backend(load_portfolio_summary / load_daily_equity / load_signals)
  - core.regime / core.alerting 直连仅用于读运行时状态(下一周期可走 backend)
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import (
    load_portfolio_summary, load_positions, load_signals,
    load_daily_equity, load_daily_stats,
)
from ui.components import regime_badge, regime_zh


def render_page() -> None:
    st.title('📊 仪表盘')

    # ── 账户摘要 ──
    portfolio = load_portfolio_summary()
    cash    = float(portfolio.get('cash', 0) or 0)
    equity  = float(portfolio.get('total_equity', cash) or cash)
    pos_val = float(portfolio.get('position_value', 0) or 0)
    unreal  = float(portfolio.get('unrealized_pnl', 0) or 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('总权益', f'¥{equity:,.0f}', delta=f'{unreal:+,.0f}' if unreal else None)
    c2.metric('持仓市值', f'¥{pos_val:,.0f}')
    c3.metric('可用现金', f'¥{cash:,.0f}')
    c4.metric('持仓比例', f'{pos_val/equity*100:.1f}%' if equity > 0 else '—')

    st.markdown('---')
    col_left, col_right = st.columns([2, 1])

    # ── 净值曲线 ──
    with col_left:
        st.subheader('净值曲线')
        daily = load_daily_equity(90)
        if daily:
            df_eq = pd.DataFrame(daily)
            date_col = next((c for c in df_eq.columns if 'date' in c.lower()), None)
            eq_col   = next((c for c in df_eq.columns if 'equity' in c.lower()), None)
            if date_col and eq_col:
                df_eq[date_col] = pd.to_datetime(df_eq[date_col])
                fig = px.area(
                    df_eq, x=date_col, y=eq_col,
                    labels={date_col: '', eq_col: '净值 (¥)'},
                    color_discrete_sequence=['#4c78a8'],
                )
                fig.update_layout(margin=dict(t=10, b=20), height=260)
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info('暂无净值记录')

    # ── 市场 Regime + 快速指标 ──
    with col_right:
        st.subheader('市场状态')
        try:
            from core.regime import get_regime
            regime_info = get_regime()
            regime = regime_info.regime if hasattr(regime_info, 'regime') else str(regime_info)
        except Exception:
            regime = 'UNKNOWN'
        regime_badge(regime)
        st.caption('基于 MA20/MA60 + ATR ratio 识别')

        st.markdown('---')
        positions = load_positions()
        active = [p for p in positions if p.get('shares', 0) > 0]
        signals = load_signals(20)
        buy_sigs  = [s for s in signals if s.get('direction') == 'BUY']
        sell_sigs = [s for s in signals if s.get('direction') == 'SELL']

        st.metric('持仓标的数', len(active))
        st.metric('今日 BUY 信号', len(buy_sigs))
        st.metric('今日 SELL 信号', len(sell_sigs))

    st.markdown('---')

    # ── Top 信号 + 近期告警 ──
    col_sig, col_alert = st.columns(2)

    with col_sig:
        st.subheader('最新交易信号(近 10 条)')
        if signals:
            rows = []
            for s in signals[:10]:
                d = s.get('direction', '')
                rows.append({
                    '时间': str(s.get('timestamp', s.get('created_at', '')))[:16],
                    '标的': s.get('symbol', ''),
                    '方向': f"🟢 {d}" if d == 'BUY' else f"🔴 {d}" if d == 'SELL' else d,
                    '强度': f"{float(s.get('strength', 0)):.2f}",
                    '因子': s.get('factor', s.get('signal_type', '')),
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info('暂无信号记录')

    with col_alert:
        st.subheader('近期系统告警')
        try:
            from core.alerting import get_alert_manager
            am = get_alert_manager()
            history = am.get_history(last_n=8)
            if history:
                for rec in reversed(history):
                    icon = {'CRITICAL': '🔴', 'WARNING': '🟡', 'INFO': '🔵'}.get(rec.level, '⚪')
                    st.markdown(f"{icon} **[{rec.level}]** {rec.message[:80]}")
                    st.caption(f"  {rec.timestamp[:16]} · {rec.channel}")
            else:
                st.success('无未处理告警')
        except Exception:
            load_daily_stats(1)
            st.info('AlertManager 未初始化(将在策略运行后生效)')

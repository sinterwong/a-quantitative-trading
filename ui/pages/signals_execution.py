"""
ui/pages/signals_execution.py — 📈 信号 & 执行 (P4-1 阶段二)

operator/trader 视图:实时信号 + 自选股行情 + 算法订单(VWAP/TWAP) + 成交记录 TCA。

⚠ 架构债(下个周期 use case 化):
  - 直接 `from core.execution.* import ...` + `from core.brokers.simulated import ...`
    应该改为 backend 端点(如 POST /orders/algo)走 use case
  - core.tca 直接调用同上
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.data import (
    load_signals, load_positions, load_watchlist, load_trades, load_realtime,
    limit_up_pct,
)


def render_page() -> None:
    st.title('📈 信号 & 执行')
    st.caption('实时信号 · VWAP/TWAP 算法下单 · 市场冲击估算 · 成交记录')

    tab_sig, tab_algo, tab_trades = st.tabs(
        ['📡 实时信号', '⚡ 算法下单', '📋 成交记录']
    )

    with tab_sig:
        _render_signals_tab()
    with tab_algo:
        _render_algo_tab()
    with tab_trades:
        _render_trades_tab()


def _render_signals_tab() -> None:
    signals = load_signals(50)
    positions = load_positions()
    watchlist = load_watchlist()

    active = [p for p in positions if p.get('shares', 0) > 0]
    if active:
        st.subheader(f'当前持仓({len(active)} 只)')
        pos_rows = []
        for p in active:
            sym = p['symbol']
            snap = load_realtime(sym)
            cur = snap.get('price', 0) if snap else 0
            entry = float(p.get('entry_price', 0) or 0)
            pnl_pct = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0
            lu = limit_up_pct(sym) * 100
            pos_rows.append({
                '标的': sym,
                '持仓': p.get('shares', 0),
                '成本': f'{entry:.2f}',
                '现价': f'{cur:.2f}' if cur else '—',
                '涨跌%': f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                '盈亏%': f'{pnl_pct:+.2f}%',
                '距涨停': f'{lu - snap.get("pct", 0):.1f}%' if snap else '—',
                '量比': f'{snap.get("vol_ratio", 0):.1f}' if snap and snap.get("vol_ratio") else '—',
            })
        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
        st.markdown('---')

    st.subheader('交易信号')
    if signals:
        sig_rows = []
        for s in signals[:30]:
            d = s.get('direction', '')
            sig_rows.append({
                '时间': str(s.get('timestamp', s.get('created_at', '')))[:16],
                '标的': s.get('symbol', ''),
                '方向': "🟢 BUY" if d == 'BUY' else "🔴 SELL" if d == 'SELL' else d,
                '价格': s.get('price', '—'),
                '强度': f"{float(s.get('strength', 0)):.3f}",
                '因子': s.get('factor', s.get('signal_type', '')),
            })
        st.dataframe(pd.DataFrame(sig_rows), hide_index=True, use_container_width=True)
    else:
        st.info('暂无信号记录')

    if watchlist:
        st.markdown('---')
        st.subheader('自选股行情')
        wl_rows = []
        for item in watchlist[:10]:
            sym = item if isinstance(item, str) else item.get('symbol', '')
            if not sym:
                continue
            snap = load_realtime(sym)
            wl_rows.append({
                '标的': sym,
                '现价': f'{snap.get("price", 0):.2f}' if snap else '—',
                '涨跌%': f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                '最高': f'{snap.get("high", 0):.2f}' if snap else '—',
                '最低': f'{snap.get("low", 0):.2f}' if snap else '—',
            })
        if wl_rows:
            st.dataframe(pd.DataFrame(wl_rows), hide_index=True, use_container_width=True)


def _render_algo_tab() -> None:
    st.subheader('算法订单(VWAP / TWAP)')
    st.caption('SimulatedBroker 模拟撮合 · A 股整手 · Almgren-Chriss 市场冲击预估')

    try:
        from core.execution.impact_estimator import ImpactEstimator
        from core.brokers.simulated import SimulatedBroker, SimConfig
        from core.oms import OMS
        algo_ok = True
    except ImportError as e:
        st.error(f'执行模块加载失败: {e}')
        algo_ok = False
        return

    col_form, col_impact = st.columns([1, 1])
    with col_form:
        algo_sym = st.text_input('标的', '000001.SZ', key='algo_sym')
        algo_dir = st.radio('方向', ['BUY', 'SELL'], horizontal=True)
        algo_shares = st.number_input('数量(股)', 100, 100000, 1000, step=100)
        algo_type = st.selectbox('算法类型', ['VWAP', 'TWAP'])
        algo_dur = st.slider('执行时长(分钟)', 5, 120, 30, step=5)
        algo_slices = st.slider('切片数量', 3, 20, 10)

    with col_impact:
        st.subheader('市场冲击预估')
        snap_a = load_realtime(algo_sym)
        ref_price = snap_a.get('price', 0) if snap_a else 0

        if ref_price > 0:
            st.metric('参考价格', f'¥{ref_price:.2f}')
            st.metric('订单金额', f'¥{algo_shares * ref_price:,.0f}')

        market_vol = st.number_input(
            '市场日均成交量(股,估算)', 100_000, 100_000_000, 1_000_000, step=100_000,
        )

        try:
            estimator = ImpactEstimator()
            impact_bps = estimator.estimate(algo_shares, market_vol)
            perm_bps, temp_bps = estimator.decompose(algo_shares, market_vol)
            participation_rate = algo_shares / market_vol

            st.metric('估算总冲击', f'{impact_bps:.2f} bps',
                      delta=f'参与率 {participation_rate:.2%}')
            i1, i2 = st.columns(2)
            i1.metric('永久冲击', f'{perm_bps:.2f} bps')
            i2.metric('临时冲击', f'{temp_bps:.2f} bps')

            if ref_price > 0:
                cost_rmb = estimator.estimate_cost(algo_shares, market_vol, ref_price)
                st.metric('估算冲击成本', f'¥{cost_rmb:.2f}')

            max_qty = estimator.max_order_size(market_vol, max_impact_bps=20.0)
            st.caption(f'20 bps 冲击限制下最大可下量:{max_qty:,} 股')
        except Exception as e:
            st.warning(f'冲击估算失败: {e}')

    st.markdown('---')
    if st.button('模拟执行算法订单', type='primary'):
        with st.spinner(f'模拟 {algo_type} 执行...'):
            try:
                broker = SimulatedBroker(SimConfig(
                    initial_cash=10_000_000, price_source='manual',
                    slippage_bps=5.0, commission_rate=0.0003,
                    stamp_tax_rate=0.001, enforce_lot=True,
                ))
                broker.connect()
                oms = OMS(broker=broker)
                result = oms.submit_algo_order(
                    algo=algo_type, symbol=algo_sym, direction=algo_dir,
                    total_shares=int(algo_shares), duration_minutes=algo_dur,
                    reference_price=ref_price if ref_price > 0 else 10.0,
                    slice_interval=max(1, algo_dur // algo_slices),
                )
                st.success('算法订单执行完成!')
                r1, r2, r3, r4 = st.columns(4)
                r1.metric('成交率', f'{result.fill_rate:.1%}')
                r2.metric('成交股数', f'{result.filled_shares:,}')
                r3.metric('均价', f'¥{result.avg_fill_price:.3f}')
                r4.metric('实际滑点', f'{result.slippage_bps:.2f} bps')

                if result.slices:
                    slice_df = pd.DataFrame([{
                        '切片': s.slice_id, '目标股数': s.target_shares,
                        '成交股数': s.filled_shares,
                        '成交价': f'{s.fill_price:.3f}' if s.fill_price else '—',
                        '状态': s.status,
                    } for s in result.slices])
                    with st.expander('切片明细'):
                        st.dataframe(slice_df, hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(f'订单执行失败: {e}')


def _render_trades_tab() -> None:
    st.subheader('成交记录(含 TCA)')
    trades = load_trades(200)
    if not trades:
        st.info('暂无成交记录')
        return

    try:
        from core.tca import TCAAnalyzer
        tca = TCAAnalyzer.from_trade_dicts(trades)
        rpt = tca.analyze()
        t1, t2, t3, t4 = st.columns(4)
        t1.metric('样本笔数', rpt.n_trades)
        t2.metric('平均 IS', f'{rpt.avg_is_bps:.2f} bps')
        t3.metric('平均总成本', f'{rpt.avg_total_cost_bps:.2f} bps')
        t4.metric('建议滑点参数', f'{rpt.recommended_slippage_bps:.0f} bps')

        if rpt.monthly and len(rpt.monthly) > 1:
            monthly_df = pd.DataFrame([
                {'月份': k, 'avg IS (bps)': v['avg_is_bps']}
                for k, v in sorted(rpt.monthly.items())
            ]).set_index('月份')
            st.line_chart(monthly_df)
        st.markdown('---')
    except Exception:
        pass

    rows_t = []
    for t in trades[:100]:
        rows_t.append({
            '时间': str(t.get('timestamp', t.get('created_at', '')))[:16],
            '标的': t.get('symbol', ''),
            '方向': t.get('direction', ''),
            '股数': t.get('shares', 0),
            '成交价': f'{float(t.get("price", 0)):.3f}',
            '佣金': f'{float(t.get("commission", 0)):.2f}',
            '印花税': f'{float(t.get("stamp_tax", 0)):.2f}',
            '滑点bps': t.get('slippage_bps', '—'),
        })
    st.dataframe(pd.DataFrame(rows_t), hide_index=True, use_container_width=True)

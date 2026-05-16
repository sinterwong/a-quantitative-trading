"""
ui/pages/portfolio_optimization.py — ⚖️ 组合优化 (P4-1 阶段二)

researcher 视图:MVO + Black-Litterman + 多策略资金分配。

⚠ 重大架构债(下个周期):
  - core.portfolio_optimizer.PortfolioOptimizer 直连 → 走 use case + backend 端点
    P2-6 已有 compose_portfolio use case,本页面应改成调用它的 backend 端点
  - core.portfolio_allocator.PortfolioAllocator 同上
  - 拉历史价格数据 K 线 → DataLayer 直连 OK,但批量循环应在 backend 完成
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import (
    load_trading_config, load_positions, load_realtime, make_price_df,
)


def render_page() -> None:
    st.title('⚖️ 组合优化')
    st.caption('MVO · Black-Litterman · 风险平价 · 最大分散化 · 多策略资金分配')

    tab_opt, tab_bl, tab_alloc = st.tabs(
        ['📐 均值方差优化', '🔭 Black-Litterman', '🎯 策略资金分配']
    )
    with tab_opt:
        _render_mvo_tab()
    with tab_bl:
        _render_bl_tab()
    with tab_alloc:
        _render_alloc_tab()


def _render_mvo_tab() -> None:
    st.subheader('均值方差优化(PortfolioOptimizer)')

    symbols_input = st.text_area(
        '标的列表(每行一个)',
        '000001.SZ\n600519.SH\n300750.SZ\n600036.SH',
        height=120,
    )
    symbols_list = [s.strip() for s in symbols_input.strip().split('\n') if s.strip()]

    c1, c2, c3 = st.columns(3)
    with c1:
        opt_method = st.selectbox('优化方法', [
            'min_variance', 'max_sharpe', 'risk_parity',
            'max_diversification', 'equal_weight',
        ])
    with c2:
        cov_method = st.selectbox('协方差估计', ['ledoit_wolf', 'sample'])
        max_weight = st.slider('单标的上限', 0.10, 0.50, 0.25, step=0.05)
    with c3:
        data_days_o = st.slider('历史数据(天)', 120, 500, 252, step=20, key='opt_days')
        max_to = st.slider('换手率约束', 0.1, 1.0, 0.3, step=0.05)

    if not st.button('运行优化', type='primary'):
        return

    if len(symbols_list) < 2:
        st.error('至少输入 2 个标的。')
        return

    returns_dict = {}
    with st.spinner('拉取历史数据...'):
        for sym in symbols_list:
            df_s = make_price_df(sym, data_days_o)
            if df_s is not None and len(df_s) > 30:
                returns_dict[sym] = df_s['close'].pct_change().dropna()

    if len(returns_dict) < 2:
        st.error('数据获取失败,请检查网络或标的。')
        return

    try:
        from core.portfolio_optimizer import PortfolioOptimizer
        returns_df = pd.DataFrame(returns_dict).dropna()
        optimizer = PortfolioOptimizer(
            returns=returns_df, cov_method=cov_method,
            max_weight=max_weight, min_weight=0.0,
        )
        method_fn = getattr(optimizer, opt_method)
        weights = method_fn()

        st.success('优化完成!')

        w_df = pd.DataFrame({'标的': list(returns_dict.keys()), '权重': weights})
        fig = px.pie(w_df, values='权重', names='标的',
                     title=f'{opt_method} 优化权重',
                     color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

        sym_keys = list(returns_dict.keys())
        current_weights = pd.Series(np.ones(len(sym_keys)) / len(sym_keys), index=sym_keys)
        w_adj = optimizer.apply_turnover_constraint(
            weights, current_weights, max_turnover=max_to)

        result_rows = [
            {'标的': sym, '优化权重': f'{w:.1%}', '换手调整后': f'{wa:.1%}',
             '预期年化收益': f'{float(returns_df[sym].mean() * 252):.1%}',
             '年化波动率': f'{float(returns_df[sym].std() * np.sqrt(252)):.1%}'}
            for sym, w, wa in zip(returns_dict.keys(), weights, w_adj)
        ]
        st.dataframe(pd.DataFrame(result_rows), hide_index=True, use_container_width=True)

        if opt_method == 'max_sharpe':
            port_ret = float(np.dot(weights, returns_df.mean() * 252))
            port_vol = float(np.sqrt(np.dot(weights, np.dot(optimizer._cov * 252, weights))))
            port_sharpe = (port_ret - 0.02) / port_vol if port_vol > 0 else 0
            m1, m2, m3 = st.columns(3)
            m1.metric('组合年化收益', f'{port_ret:.2%}')
            m2.metric('组合年化波动率', f'{port_vol:.2%}')
            m3.metric('组合 Sharpe', f'{port_sharpe:.3f}')
    except Exception as e:
        st.error(f'优化失败: {e}')


def _render_bl_tab() -> None:
    st.subheader('Black-Litterman 观点融合')
    st.caption('将策略因子观点融入均衡收益,生成后验权重')

    bl_syms_input = st.text_area(
        '标的(每行一个)',
        '000001.SZ\n600519.SH\n300750.SZ\n600036.SH',
        height=100, key='bl_syms',
    )
    bl_symbols = [s.strip() for s in bl_syms_input.strip().split('\n') if s.strip()]

    st.markdown('**输入观点(年化预期收益)**')
    views = {}
    confidences = {}
    if bl_symbols:
        cols = st.columns(min(len(bl_symbols), 4))
        for i, sym in enumerate(bl_symbols):
            with cols[i % 4]:
                v = st.number_input(f'{sym} 预期收益', -0.30, 0.50, 0.08, step=0.01,
                                    format='%.2f', key=f'bl_v_{sym}')
                c = st.slider(f'{sym} 置信度', 0.1, 1.0, 0.6, step=0.05, key=f'bl_c_{sym}')
                views[sym] = v
                confidences[sym] = c

    bl_days = st.slider('历史数据(天)', 120, 500, 252, key='bl_days')

    if not st.button('计算 BL 权重', type='primary'):
        return

    returns_dict_bl = {}
    with st.spinner('拉取数据...'):
        for sym in bl_symbols:
            df_s = make_price_df(sym, bl_days)
            if df_s is not None and len(df_s) > 30:
                returns_dict_bl[sym] = df_s['close'].pct_change().dropna()

    if len(returns_dict_bl) < 2:
        st.error('数据获取失败。')
        return

    try:
        from core.portfolio_optimizer import PortfolioOptimizer
        returns_df_bl = pd.DataFrame(returns_dict_bl).dropna()
        opt_bl = PortfolioOptimizer(returns=returns_df_bl, max_weight=0.40)
        w_bl = opt_bl.black_litterman(views, confidences)
        w_eq = opt_bl.equal_weight()
        w_gmv = opt_bl.min_variance()

        st.success('BL 权重计算完成!')

        comp_df = pd.DataFrame({
            '标的': list(returns_dict_bl.keys()),
            'Black-Litterman': w_bl,
            '等权基准': w_eq,
            '全局最小方差': w_gmv,
        })
        fig = px.bar(
            comp_df.melt(id_vars='标的', var_name='方法', value_name='权重'),
            x='标的', y='权重', color='方法', barmode='group',
            title='BL 权重 vs 基准方法',
            color_discrete_sequence=px.colors.qualitative.Set1,
        )
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(comp_df, hide_index=True, use_container_width=True)
    except Exception as e:
        st.error(f'BL 计算失败: {e}')


def _render_alloc_tab() -> None:
    st.subheader('多策略资金分配(PortfolioAllocator)')
    try:
        from core.portfolio_allocator import AllocConfig, PortfolioAllocator, WeightMode
    except ImportError as e:
        st.error(f'PortfolioAllocator 导入失败: {e}')
        return

    cfg_a = load_trading_config()
    strategies_cfg = cfg_a.get('strategies', {})
    portfolio_cfg = cfg_a.get('portfolio', {})
    total_capital = float(portfolio_cfg.get('capital', 100_000))

    col_cfg, col_res = st.columns([1, 2])

    with col_cfg:
        total_capital = st.number_input(
            '总资金(元)', value=total_capital,
            min_value=10_000.0, step=10_000.0, format='%.0f',
        )
        mode_label = st.selectbox(
            '权重模式',
            ['等权 (EQUAL)', '固定权重 (FIXED)', '风险平价 (RISK_PARITY)'],
        )
        mode_map = {
            '等权 (EQUAL)': WeightMode.EQUAL,
            '固定权重 (FIXED)': WeightMode.FIXED,
            '风险平价 (RISK_PARITY)': WeightMode.RISK_PARITY,
        }
        weight_mode = mode_map[mode_label]
        reserve_pct = st.slider('保留现金 (%)', 0, 20, 5, step=1)
        reserve = reserve_pct / 100

        st.markdown('**策略权重(固定权重模式)**')
        custom_weights = {}
        default_names = list(strategies_cfg.keys()) or ['RSI', 'MACD', 'Bollinger']
        for name in default_names:
            w = st.number_input(
                f'{name}', value=1.0 / len(default_names),
                min_value=0.0, max_value=1.0, step=0.05,
                key=f'w_alloc_{name}', format='%.2f',
            )
            custom_weights[name] = w

    with col_res:
        try:
            config = AllocConfig(
                mode=weight_mode, reserve_ratio=reserve,
                min_strategy_weight=0.05, max_strategy_weight=0.60,
            )
            allocator = PortfolioAllocator(total_capital=total_capital, config=config)
            for name in default_names:
                w = custom_weights[name] if weight_mode == WeightMode.FIXED else None
                allocator.add_strategy(name, weight=w)

            positions = load_positions()
            pos_by_sym = {p['symbol']: p for p in positions if p.get('shares', 0) > 0}
            for name in default_names:
                strat_cfg = strategies_cfg.get(name, {})
                sym = strat_cfg.get('symbol', '')
                if sym and sym in pos_by_sym:
                    p = pos_by_sym[sym]
                    snap = load_realtime(sym)
                    cur = snap.get('price', p.get('entry_price', 0))
                    allocator.update_usage(name, cur * p['shares'])

            summary = allocator.summary()
            strat_info = summary['strategies']

            rows_a = []
            for sname, info in strat_info.items():
                rows_a.append({
                    '策略': sname,
                    '权重': f'{info["weight"]:.1%}',
                    '额度(¥)': f'{info["budget"]:,.0f}',
                    '已用(¥)': f'{info["used"]:,.0f}',
                    '可用(¥)': f'{info["available"]:,.0f}',
                    '利用率': f'{info["utilization"]:.1%}',
                })
            st.dataframe(pd.DataFrame(rows_a), hide_index=True, use_container_width=True)

            s1, s2, s3, s4 = st.columns(4)
            s1.metric('总资金', f'¥{summary["total_capital"]:,.0f}')
            s2.metric('已分配', f'¥{summary["total_budget"]:,.0f}')
            s3.metric('已使用', f'¥{summary["total_used"]:,.0f}')
            s4.metric('保留现金', f'¥{summary["reserve"]:,.0f}')

            if rows_a:
                budgets = [float(r['额度(¥)'].replace(',', '')) for r in rows_a]
                fig_a = px.pie(
                    names=[r['策略'] for r in rows_a],
                    values=budgets, title='策略资金分配',
                    color_discrete_sequence=px.colors.qualitative.Set2,
                )
                fig_a.update_layout(height=300)
                st.plotly_chart(fig_a, use_container_width=True)

            st.markdown('---')
            current_mv = {name: strat_info[name]['used'] for name in strat_info}
            if allocator.needs_rebalance(current_mv):
                st.warning('持仓偏离超阈值,建议再平衡。')
                if st.button('执行再平衡'):
                    new_budgets = allocator.rebalance(trigger='manual')
                    st.success(f'再平衡完成:{new_budgets}')
            else:
                st.success('持仓权重正常,无需再平衡。')
        except Exception as e:
            st.error(f'分配计算失败: {e}')

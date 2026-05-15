"""
ui/pages/backtest.py — 📉 回测验证 (P4-1 阶段二)

researcher 视图:Walk-Forward 历史 + 敏感性热力图 + 模拟一致性验证。

⚠ 重大架构债(下个周期):
  - 当前用 subprocess 调 scripts/walkforward_job.py / sensitivity_job.py
    本质上是 fire-and-forget,无进度回传、无错误结构化
  - 应改为 backend 任务 API:
      POST /tasks/wfa   → {task_id}
      GET  /tasks/<id>  → {status, progress, result}
  - core.walkforward.SensitivityAnalyzer / core.paper_trade_validator 直连
    应走 use case + backend 端点

文件管理:
  - WFA 结果读 state.db (load_wf_results 已封装)
  - 敏感性 PNG/CSV 落 outputs/
  - 一致性报告落 outputs/
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import (
    BASE_DIR, OUTPUTS_DIR,
    load_wf_results, load_trading_config, load_signals, load_positions, load_realtime,
)


def render_page() -> None:
    st.title('📉 回测验证')
    st.caption('Walk-Forward 分析 · 参数敏感性热力图 · 模拟实盘一致性验证')

    tab_wfa, tab_sens, tab_val = st.tabs(
        ['📈 Walk-Forward', '🌡️ 敏感性分析', '✅ 一致性验证']
    )
    with tab_wfa:
        _render_wfa_tab()
    with tab_sens:
        _render_sensitivity_tab()
    with tab_val:
        _render_validation_tab()


def _render_wfa_tab() -> None:
    wf = load_wf_results(30)
    if wf:
        st.subheader(f'历史 WFA 结果({len(wf)} 条)')
        rows_w = []
        for r in wf:
            try:
                params = json.loads(r.get('best_params', '{}'))  # noqa: F841
            except Exception:
                pass
            rows_w.append({
                '窗口': r.get('window', '?'),
                '标的': r.get('symbol', ''),
                '策略': r.get('strategy', ''),
                '训练Sharpe': f"{r.get('train_sharpe', 0):.2f}",
                '测试Sharpe': f"{r.get('test_sharpe', 0):.2f}",
                '测试收益%': f"{r.get('test_return_pct', 0):+.1f}%",
                '胜率%': f"{r.get('test_winrate_pct', 0):.0f}%",
                '最大回撤%': f"{r.get('test_maxdd_pct', 0):.1f}%",
            })
        df_wf = pd.DataFrame(rows_w)
        st.dataframe(df_wf, hide_index=True, use_container_width=True)

        sharpes = [float(r.get('test_sharpe', 0)) for r in wf]
        fig_wf = px.bar(
            x=[r.get('window', i) for i, r in enumerate(wf)],
            y=sharpes, color=sharpes,
            color_continuous_scale='RdYlGn',
            title='Walk-Forward OOS Sharpe 分布',
            labels={'x': '窗口', 'y': 'OOS Sharpe'},
        )
        fig_wf.add_hline(y=0, line_dash='dash', line_color='gray')
        fig_wf.update_layout(margin=dict(t=40, b=20))
        st.plotly_chart(fig_wf, use_container_width=True)

        pos_ratio = sum(1 for s in sharpes if s > 0) / len(sharpes) if sharpes else 0
        st.metric('正 Sharpe 窗口比例', f'{pos_ratio:.1%}',
                  delta='合格 ≥ 60%' if pos_ratio >= 0.6 else '不足 60%')
    else:
        st.info('暂无 WFA 结果。运行脚本:`python scripts/walkforward_job.py --symbol 510310.SH`')

    st.markdown('---')
    st.subheader('运行新 WFA')
    cfg_w = load_trading_config()
    live_syms_w = cfg_w.get('live_symbols', [])
    sym_opts_w = [s['symbol'] for s in live_syms_w] if live_syms_w else ['510310.SH']

    cw1, cw2, cw3 = st.columns(3)
    with cw1:
        wfa_sym_sel = st.selectbox('标的', sym_opts_w + ['自定义'], key='wfa_sym_sel')
        if wfa_sym_sel == '自定义':
            wfa_sym = st.text_input('输入标的代码', '000001.SZ', key='wfa_sym_custom')
        else:
            wfa_sym = wfa_sym_sel
    with cw2:
        wfa_strat = st.selectbox('策略', ['RSI', 'MACD', 'Bollinger'])
    with cw3:
        wfa_yrs = st.number_input('训练年数', 1, 5, 2)
    wfa_test_yrs = st.number_input('验证年数', 1, 3, 1)

    if st.button('开始 WFA'):
        cmd = [
            sys.executable,
            os.path.join(BASE_DIR, 'scripts', 'walkforward_job.py'),
            '--symbol', wfa_sym, '--strategy', wfa_strat,
            '--train-years', str(int(wfa_yrs)), '--test-years', str(int(wfa_test_yrs)),
        ]
        with st.spinner(f'训练 {wfa_sym} ({wfa_strat}) ...'):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, encoding='utf-8',
                    errors='replace', timeout=600,
                )
                if result.returncode == 0:
                    st.success('WFA 完成')
                    st.cache_data.clear()
                else:
                    st.warning(f'退出码 {result.returncode}')
                st.code(result.stdout[-3000:] or '(无输出)', language='text')
            except subprocess.TimeoutExpired:
                st.error('训练超时(> 600s)')
            except Exception as e:
                st.error(f'运行失败: {e}')


_FACTOR_PARAMS = {
    'RSI':           ('period', '7,10,14,21,28',  'buy_threshold', '20,25,30,35,40'),
    'ATR':           ('period', '7,10,14,21,28',  'lookback',      '10,15,20,30,40'),
    'MACD':          ('fast',   '5,8,12,16,20',   'slow',          '20,26,34,40,50'),
    'BollingerBands':('period', '10,15,20,30,40', 'nb_std',        '1.5,2.0,2.5,3.0,3.5'),
}


def _render_sensitivity_tab() -> None:
    st.subheader('参数敏感性热力图')
    st.caption('双参数网格扫描 → Sharpe 热力图,peak_sensitivity_ratio 量化稳健度')

    try:
        from core.walkforward import SensitivityAnalyzer  # noqa: F401
    except ImportError as e:
        st.error(f'SensitivityAnalyzer 加载失败: {e}')
        return

    cfg_s = load_trading_config()
    live_syms_s = cfg_s.get('live_symbols', [])
    sym_opts_s = [s['symbol'] for s in live_syms_s] if live_syms_s else ['510310.SH']
    sens_sym_sel = st.selectbox('标的', sym_opts_s + ['自定义'], key='sens_sym')
    if sens_sym_sel == '自定义':
        sens_sym = st.text_input('输入标的代码', '000001.SZ', key='sens_sym_custom')
    else:
        sens_sym = sens_sym_sel

    sens_factor = st.selectbox('因子', list(_FACTOR_PARAMS.keys()), key='sens_factor')
    _dp1n, _dp1v, _dp2n, _dp2v = _FACTOR_PARAMS[sens_factor]

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        p1_name = st.text_input('参数 1 名称', _dp1n, key='p1_name')
        p1_vals = st.text_input('参数 1 取值(逗号分隔)', _dp1v, key='p1_vals')
    with col_s2:
        p2_name = st.text_input('参数 2 名称', _dp2n, key='p2_name')
        p2_vals = st.text_input('参数 2 取值(逗号分隔)', _dp2v, key='p2_vals')

    if st.button('运行敏感性分析'):
        try:
            p1 = [float(v.strip()) for v in p1_vals.split(',') if v.strip()]
            p2 = [float(v.strip()) for v in p2_vals.split(',') if v.strip()]
            if not p1 or not p2:
                st.error('参数取值列表不能为空')
            else:
                cmd_s = [
                    sys.executable,
                    os.path.join(BASE_DIR, 'scripts', 'sensitivity_job.py'),
                    '--symbol', sens_sym, '--factor', sens_factor,
                    '--param1', p1_name, '--p1-values', ','.join(str(v) for v in p1),
                    '--param2', p2_name, '--p2-values', ','.join(str(v) for v in p2),
                ]
                with st.spinner('敏感性扫描中(可能需要 1-3 分钟)...'):
                    res_s = subprocess.run(
                        cmd_s, capture_output=True, encoding='utf-8',
                        errors='replace', timeout=300,
                    )
                if res_s.returncode == 0:
                    st.success('完成')
                    _show_sensitivity_artifact(sens_sym, sens_factor, p1_name, p2_name)
                    st.code(res_s.stdout[-2000:] or '(无输出)', language='text')
                else:
                    st.error(f'脚本返回错误 (exit {res_s.returncode})')
                    st.code(res_s.stderr[-2000:] or res_s.stdout[-1000:], language='text')
        except subprocess.TimeoutExpired:
            st.error('扫描超时(> 300s),请缩减参数网格')
        except Exception as e:
            st.error(f'运行失败: {e}')

    _show_existing_heatmaps()


def _show_sensitivity_artifact(sym: str, factor: str, p1_name: str, p2_name: str) -> None:
    png_path = os.path.join(OUTPUTS_DIR, f'sensitivity_{sym}.png')
    csv_path = os.path.join(OUTPUTS_DIR, f'sensitivity_{sym}.csv')
    if os.path.exists(png_path):
        st.image(png_path, caption='Sharpe 热力图')
    elif os.path.exists(csv_path):
        heat_df = pd.read_csv(csv_path, index_col=0)
        fig_heat = px.imshow(
            heat_df.astype(float),
            color_continuous_scale='RdYlGn', color_continuous_midpoint=0,
            text_auto='.2f',
            labels={'x': p2_name, 'y': p1_name, 'color': 'Sharpe'},
            title=f'{factor} Sensitivity — {sym}',
        )
        fig_heat.update_layout(height=400)
        st.plotly_chart(fig_heat, use_container_width=True)


def _show_existing_heatmaps() -> None:
    if not os.path.exists(OUTPUTS_DIR):
        return
    heatmaps = [f for f in os.listdir(OUTPUTS_DIR)
                if f.startswith('sensitivity_') and f.endswith(('.png', '.csv'))]
    if not heatmaps:
        return
    st.markdown('---')
    selected_hm = st.selectbox('已有热力图', heatmaps, key='sel_hm')
    hm_full = os.path.join(OUTPUTS_DIR, selected_hm)
    if selected_hm.endswith('.png'):
        st.image(hm_full)
    else:
        try:
            hm_df = pd.read_csv(hm_full, index_col=0)
            fig_hm = px.imshow(
                hm_df.astype(float),
                color_continuous_scale='RdYlGn', color_continuous_midpoint=0,
                text_auto='.2f', title=selected_hm.replace('.csv', ''),
            )
            fig_hm.update_layout(height=400)
            st.plotly_chart(fig_hm, use_container_width=True)
        except Exception as e:
            st.error(f'读取热力图失败: {e}')


def _render_validation_tab() -> None:
    st.subheader('模拟实盘一致性验证')
    st.caption('对比回测成交价 vs 模拟撮合价,目标:|偏差| ≤ 20 bps,通过率 ≥ 90%')

    try:
        from core.paper_trade_validator import PaperTradeValidator
        from core.brokers.simulated import SimConfig, SimulatedBroker
    except ImportError as e:
        st.error(f'PaperTradeValidator 加载失败: {e}')
        return

    reports = sorted(
        [f for f in os.listdir(OUTPUTS_DIR) if f.startswith('paper_trade')],
        reverse=True,
    ) if os.path.exists(OUTPUTS_DIR) else []

    if reports:
        st.success(f'找到 {len(reports)} 份历史报告')
        selected_r = st.selectbox('查看报告', reports)
        try:
            with open(os.path.join(OUTPUTS_DIR, selected_r), encoding='utf-8') as f:
                rpt_data = json.load(f)
            summary_v = rpt_data.get('summary', {})
            passed = summary_v.get('passed', False)
            vc1, vc2, vc3, vc4 = st.columns(4)
            vc1.metric('结论', 'PASS' if passed else 'FAIL')
            vc2.metric('交易总数', summary_v.get('n_trades', 0))
            vc3.metric('通过率', f"{summary_v.get('pass_rate', 0):.1%}")
            vc4.metric('平均偏差', f"{summary_v.get('avg_deviation_bps', 0):.2f} bps")
        except Exception as e:
            st.error(f'读取报告失败: {e}')
    else:
        st.info('暂无历史报告')

    st.markdown('---')
    st.subheader('快速验证')
    slippage_v = st.slider('模拟滑点 (bps)', 0.0, 50.0, 5.0, step=1.0, key='val_slip')
    threshold_v = st.slider('偏差阈值 (bps)', 5.0, 50.0, 20.0, step=5.0, key='val_th')

    if st.button('运行一致性验证'):
        sigs_v = load_signals(30)
        valid_sigs = [
            {'symbol': s['symbol'], 'direction': s.get('direction', 'BUY'),
             'price': float(s.get('price', 0) or 0), 'shares': 100}
            for s in sigs_v if s.get('price') and float(s.get('price', 0)) > 0
        ]
        if not valid_sigs:
            pos_v = load_positions()
            for p in pos_v[:5]:
                snap_v = load_realtime(p['symbol'])
                if snap_v and snap_v.get('price'):
                    valid_sigs.append({
                        'symbol': p['symbol'], 'direction': 'BUY',
                        'price': snap_v['price'], 'shares': 100,
                    })
        if not valid_sigs:
            st.warning('无可用信号数据')
            return

        try:
            broker_v = SimulatedBroker(SimConfig(
                initial_cash=10_000_000, price_source='manual',
                slippage_bps=slippage_v, commission_rate=0.0003,
                stamp_tax_rate=0.001, enforce_lot=True,
            ))
            broker_v.connect()
            validator = PaperTradeValidator(
                threshold_bps=threshold_v, large_dev_bps=50.0
            )
            report_v = validator.validate_from_signals(valid_sigs, broker_v)

            r1, r2, r3, r4 = st.columns(4)
            r1.metric('结论', 'PASS' if report_v.passed else 'FAIL')
            r2.metric('验证笔数', report_v.n_trades)
            r3.metric('通过率', f'{report_v.pass_rate:.1%}')
            r4.metric('平均偏差', f'{report_v.avg_deviation_bps:.2f} bps')

            if report_v.comparisons:
                comp_data = [{
                    '标的': c.symbol, '方向': c.direction,
                    '回测价': c.bt_price, '实盘价': c.live_price,
                    '偏差bps': c.deviation_bps, '合格': c.within_threshold,
                } for c in report_v.comparisons]
                st.dataframe(pd.DataFrame(comp_data), hide_index=True,
                             use_container_width=True)
            if st.button('保存报告'):
                path_v = report_v.save()
                st.success(f'已保存至: {path_v}')
        except Exception as e:
            st.error(f'验证失败: {e}')

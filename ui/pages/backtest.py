"""ui/pages/backtest.py — 单标的回测(走新 POST /backtest/run)。

注:v1 只返 KPI(BacktestResponse 字段),不含 equity curve 序列。
扩字段是另一坨工作,见 plan §H。
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from ui.api_client import BackendError, run_backtest, get_daily_kline
from ui.format import fmt_pct, fmt_num
from ui.widgets.layout import section_header, error_banner, kpi_row
from ui.widgets.status import header_status_bar
from ui.widgets.forms import symbol_input, date_window
from ui.widgets.charts import kline


header_status_bar()
section_header('回测', '单标的回测 → 走 POST /backtest/run')

st.markdown('#### 标的与窗口')
c1, c2 = st.columns([2, 3])
with c1:
    sym = symbol_input(key='bt_sym', placeholder='sh600519 或 600519.SH')
with c2:
    start, end, days = date_window(key_prefix='bt', default_days=252)

st.markdown('#### 资金 / 成本')
c1, c2, c3 = st.columns(3)
init_eq = c1.number_input('初始权益', min_value=1000, value=100_000, step=1000)
comm = c2.number_input('佣金 (万一 = 0.0001)', min_value=0.0, max_value=0.01,
                       value=0.0003, step=0.0001, format='%.4f')
slip = c3.number_input('滑点 bps', min_value=0.0, max_value=100.0,
                       value=5.0, step=0.5)

st.markdown('#### 策略列表(`st.data_editor` 内联编辑)')
st.caption('factor_name 可填 RSI / MACDTrend / Bollinger / ATR 等(由 backend FactorRegistry 决定);'
           ' params_json 是合法 JSON,如 `{"period": 14}`。')

default_strategies = pd.DataFrame([
    {'factor_name': 'RSI', 'threshold': 1.0, 'params_json': '{"period": 14}'},
])
edited = st.data_editor(
    default_strategies, num_rows='dynamic', use_container_width=True,
    key='bt_strategies',
    column_config={
        'factor_name': st.column_config.TextColumn('因子', required=True),
        'threshold': st.column_config.NumberColumn('阈值', step=0.1, format='%.2f'),
        'params_json': st.column_config.TextColumn('params (JSON)'),
    },
)

run_btn = st.button('🚀 运行回测', type='primary', disabled=not sym or len(edited) == 0)
st.markdown('---')

if run_btn:
    strategies = []
    err = None
    for _, row in edited.iterrows():
        fn = str(row.get('factor_name') or '').strip()
        if not fn:
            continue
        try:
            params = json.loads(row.get('params_json') or '{}')
        except json.JSONDecodeError as exc:
            err = f'{fn} 的 params_json 不是合法 JSON: {exc}'
            break
        strategies.append({
            'factor_name': fn,
            'threshold': float(row.get('threshold') or 1.0),
            'params': params if isinstance(params, dict) else {},
        })
    if err:
        st.warning(err)
    elif not strategies:
        st.warning('至少一个策略')
    else:
        payload = {
            'symbol': sym,
            'days': int(days),
            'initial_equity': float(init_eq),
            'commission_rate': float(comm),
            'slippage_bps': float(slip),
            'strategies': strategies,
        }
        if start:
            payload['start'] = start
        if end:
            payload['end'] = end
        with st.spinner('回测中(最多 120 秒)...'):
            try:
                st.session_state['_bt_result'] = run_backtest(payload)
            except BackendError as exc:
                error_banner(exc)

res = st.session_state.get('_bt_result')
if res:
    st.markdown('#### 结果')
    _tr = res.get('total_return')
    _ar = res.get('annual_return')
    _dd = res.get('max_drawdown_pct')
    kpi_row([
        {'label': '累计收益',
         'value': fmt_pct(_tr, signed=True) if _tr is not None else '—',
         'raw': f'{_tr*100:.2f}%' if isinstance(_tr, (int, float)) else '—'},
        {'label': '年化收益',
         'value': fmt_pct(_ar, signed=True) if _ar is not None else '—',
         'raw': f'{_ar*100:.2f}%' if isinstance(_ar, (int, float)) else '—'},
        {'label': '夏普', 'value': fmt_num(res.get('sharpe'), decimals=2)},
        {'label': '最大回撤',
         'value': fmt_pct(_dd) if _dd is not None else '—',
         'raw': f'{_dd*100:.2f}%' if isinstance(_dd, (int, float)) else '—'},
        {'label': '胜率', 'value': fmt_pct(res.get('win_rate'))},
    ])
    kpi_row([
        {'label': '盈亏比', 'value': fmt_num(res.get('profit_factor'), decimals=2)},
        {'label': '因子 IC', 'value': fmt_num(res.get('factor_ic'), decimals=4)},
        {'label': '因子 IR', 'value': fmt_num(res.get('factor_ir'), decimals=4)},
        {'label': 'K 线数', 'value': str(res.get('n_bars'))},
        {'label': '交易数', 'value': str(res.get('n_trades'))},
    ])

    st.markdown('##### 摘要')
    st.code(str(res.get('summary') or ''), language='text')

    with st.expander('原始响应'):
        st.json(res)

    st.markdown('---')
    st.markdown('#### 标的 K 线(上下文参考)')
    try:
        bars = get_daily_kline(sym, days=int(days))
        st.plotly_chart(kline(bars, title=f'{sym} 日 K'),
                        use_container_width=True)
    except BackendError as exc:
        error_banner(exc)

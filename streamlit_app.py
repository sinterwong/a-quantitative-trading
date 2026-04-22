#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamlit_app.py — 量化系统 Web UI
===================================
启动方式：
  pip install streamlit pandas plotly
  streamlit run streamlit_app.py --server.port 8501

页面结构：
  1. 组合概览     — 持仓、现金、总资产、盈亏
  2. 实时信号     — 持仓股涨跌停/RSI预警
  3. 动态选股     — 五维评分结果
  4. 回测分析     — Walk-Forward + Monte Carlo
  5. 持仓详情     — 个股 RSI/量比/距涨跌停
  6. 历史交易     — 成交记录
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import ssl
from datetime import datetime, date

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# ─── 禁用代理 ────────────────────────────────────────────────
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

# ─── 路径设置 ────────────────────────────────────────────────
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, 'backend')
sys.path.insert(0, BACKEND_DIR)

BACKEND_URL = os.environ.get('BACKEND_URL', 'http://127.0.0.1:5555')

# ─── Backend API 调用 ────────────────────────────────────────

def api_get(endpoint: str, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return {}

def api_post(endpoint: str, data: dict, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    payload = json.dumps(data).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return {}

def api_put(endpoint: str, data: dict, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    payload = json.dumps(data).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
            method='PUT'
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return {}

# ─── 数据获取 ────────────────────────────────────────────────

@st.cache_data(ttl=60)
def get_portfolio_summary():
    return api_get('/portfolio/summary')

@st.cache_data(ttl=60)
def get_positions():
    return api_get('/positions')

@st.cache_data(ttl=60)
def get_trades(limit=50):
    return api_get(f'/trades?limit={limit}')

@st.cache_data(ttl=120)
def get_signals(limit=20):
    return api_get(f'/signals?limit={limit}')

@st.cache_data(ttl=300)
def get_wf_results():
    """读取 walkforward 持久化结果"""
    wf_db = os.path.join(BACKEND_DIR, 'services', 'wf_results.db')
    if not os.path.exists(wf_db):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(wf_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM wf_results ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

@st.cache_data(ttl=60)
def get_live_params():
    """读取最新训练参数"""
    pfile = os.path.join(BACKEND_DIR, 'services', 'live_params.json')
    if os.path.exists(pfile):
        with open(pfile, encoding='utf-8') as f:
            return json.loads(f.read())
    return {}

# ─── 腾讯实时行情 ────────────────────────────────────────────

def _to_tencent_sym(symbol: str) -> str:
    u = symbol.upper()
    if u.endswith('.SH'): return 'sh' + u[:-3]
    if u.endswith('.SZ'): return 'sz' + u[:-3]
    return symbol.lower()

@st.cache_data(ttl=30)
def get_realtime(symbol: str) -> dict:
    sym = _to_tencent_sym(symbol)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f'https://qt.gtimg.cn/q={sym}'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com',
        })
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            eq = raw.find('="')
            if eq >= 0: raw = raw[eq+2:]
            f = raw.split('~')
            if len(f) < 40: return {}
            price    = float(f[3])  if f[3]  not in ('', '-') else 0.0
            prev_cls = float(f[4])  if f[4]  not in ('', '-') else 0.0
            pct      = float(f[32]) if f[32] not in ('', '-') else 0.0
            vol_ratio= float(f[38]) if len(f) > 38 and f[38] not in ('', '-', '0') else None
            high     = float(f[33]) if len(f) > 33 and f[33] not in ('', '-') else price
            low      = float(f[34]) if len(f) > 34 and f[34] not in ('', '-') else price
            return {
                'price': price, 'prev_close': prev_cls,
                'pct': pct, 'vol_ratio': vol_ratio,
                'high': high, 'low': low,
            }
    except Exception:
        return {}

def get_limit_pct(symbol: str) -> float:
    s = symbol.lower().replace('.sz','').replace('.sh','').replace('sz','').replace('sh','')
    if any(s.startswith(p) for p in ('st','*st','st*')): return 0.05
    if s.startswith('300') or s.startswith('688'): return 0.20
    return 0.10

# ─── 页面配置 ────────────────────────────────────────────────

st.set_page_config(
    page_title='小黑量化系统',
    page_icon='📊',
    layout='wide',
    menu_items={
        'About': '## 小黑量化系统 v1.0\n基于 A 股的量化交易研究平台',
        'Report a Bug': None,
    }
)

# 自定义 CSS
st.markdown("""
<style>
.stMetric-label { font-size: 0.85rem !important; }
.stMetricValue   { font-weight: 700 !important; }
[data-testid="stHorizontalBlock"] > div { gap: 1rem; }
</style>
""", unsafe_allow_html=True)

# ─── 侧边栏导航 ──────────────────────────────────────────────

st.sidebar.title('📊 小黑量化系统')
st.sidebar.markdown(f"**当前时间：** `{datetime.now().strftime('%Y-%m-%d %H:%M')}`")

backend_ok = bool(api_get('/health', timeout=3).get('status') == 'ok')
status_label = '🟢 Backend 运行中' if backend_ok else '🔴 Backend 未连接'
st.sidebar.markdown(f"**Backend：** {status_label}")

if not backend_ok:
    st.sidebar.warning('Backend 未连接。部分数据无法加载。')

page = st.sidebar.radio(
    '页面',
    [
        '📊 组合概览',
        '📈 实时信号',
        '🔍 动态选股',
        '📉 回测分析',
        '💼 持仓详情',
        '📋 历史交易',
        '📋 实盘记录',
    ],
    index=0,
)

st.sidebar.markdown('---')

# ─── 交易模式切换 ──────────────────────────────────────────────
mode_response = api_get('/trading/mode')
current_mode = mode_response.get('mode', 'simulation')

if current_mode == 'live':
    st.sidebar.success('🚀 实盘模式')
else:
    st.sidebar.info('🔒 模拟模式')

mode_changed = st.sidebar.toggle('🎮 开启实盘交易', value=(current_mode == 'live'), help='开启后盘中监控将自动执行真实下单指令')

if mode_changed != (current_mode == 'live'):
    new_mode = 'live' if mode_changed else 'simulation'
    put_resp = api_put('/trading/mode', {'mode': new_mode})
    st.rerun()

st.sidebar.markdown('---')
st.sidebar.caption('Powered by 小黑 · Streamlit')

# ─── 共用数据加载 ────────────────────────────────────────────

portfolio = get_portfolio_summary()
positions = get_positions().get('positions', [])
trades    = get_trades(50).get('trades', [])

# ─── 页面 1：组合概览 ───────────────────────────────────────

if page == '📊 组合概览':
    st.title('📊 组合概览')

    # 基本指标
    cash    = portfolio.get('cash', 0)
    total   = portfolio.get('total_equity', cash)
    pos_val = portfolio.get('position_value', 0)
    today_pnl = portfolio.get('unrealized_pnl', 0)  # 累计浮动盈亏
    total_pnl = portfolio.get('total_pnl', 0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric('💰 可用现金',     f'¥{cash:,.0f}')
    col2.metric('📦 持仓市值',     f'¥{pos_val:,.0f}')
    col3.metric('💹 总资产',        f'¥{total:,.0f}', delta=f'{today_pnl:+,.0f}' if today_pnl else None)
    col4.metric('📈 累计盈亏',     f'{total_pnl:+,.0f}' if total_pnl else '—', delta=f'{total_pnl/total*100:+.1f}%' if total and total_pnl else None)

    st.markdown('---')

    # 持仓饼图
    if positions:
        labels = []
        values = []
        for p in positions:
            if p.get('shares', 0) <= 0:
                continue
            snap = get_realtime(p['symbol'])
            current_price = snap.get('price', p.get('entry_price', 0)) if snap else p.get('entry_price', 0)
            labels.append(p['symbol'])
            values.append(current_price * p['shares'])
        if labels:
            fig = px.pie(
                names=labels, values=values,
                title='持仓分布',
                hole=0.4,
                color_discrete_sequence=px.colors.qualitative.Set3,
            )
            fig.update_traces(textposition='inside', textinfo='percent+label')
            fig.update_layout(margin=dict(t=30, b=30))
            st.plotly_chart(fig, width="stretch")

    # 持仓表格
    st.subheader('持仓明细')
    if positions:
        rows = []
        for p in positions:
            if p.get('shares', 0) <= 0:
                continue
            snap = get_realtime(p['symbol'])
            entry_price = p.get('entry_price', 0)
            current_price = snap.get('price', entry_price) if snap else entry_price
            shares = p['shares']
            market_value = current_price * shares
            unrealized_pnl = (current_price - entry_price) * shares
            unrealized_pnl_pct = unrealized_pnl / (entry_price * shares) if entry_price * shares else 0
            row = {
                '代码':     p['symbol'],
                '股数':     shares,
                '成本价':   f"{entry_price:.3f}",
                '当前价':   f"{current_price:.3f}" if current_price else '—',
                '市值':     f"¥{market_value:,.0f}",
                '盈亏额':   f"{unrealized_pnl:+,.0f}",
                '盈亏%':    f"{unrealized_pnl_pct:+.1%}",
                '今日涨跌': f"{snap.get('pct', 0):+.2f}%" if snap else '—',
            }
            rows.append(row)
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info('暂无持仓')

    # 刷新
    st.button('🔄 刷新数据', on_click=st.cache_data.clear)

# ─── 页面 2：实时信号 ───────────────────────────────────────

elif page == '📈 实时信号':
    st.title('📈 实时信号')
    st.caption('持仓股票涨跌停/RSI预警 · 每分钟自动刷新')

    signals = get_signals(30).get('signals', [])

    # 信号统计
    if signals:
        sig_types = {}
        for s in signals:
            t = s.get('signal', 'UNKNOWN')
            sig_types[t] = sig_types.get(t, 0) + 1
        cols = st.columns(min(len(sig_types), 4))
        for i, (sig, cnt) in enumerate(sorted(sig_types.items())):
            cols[i % 4].metric(f'信号: {sig}', f'{cnt}次')

    # 实时信号表格（从 signals API 获取）
    if signals:
        rows = []
        for s in signals:
            rows.append({
                '时间':    s.get('emitted_at', '')[:16],
                '代码':    s.get('symbol', ''),
                '信号':    s.get('signal', ''),
                '方向':    s.get('direction', ''),
                'RSI':     f"{s.get('prev_rsi', 0):.0f}" if s.get('prev_rsi') else '—',
                '涨跌幅%': f"{s.get('pct', 0):+.2f}%" if s.get('pct') else '—',
                '原因':    s.get('reason', '')[:40],
            })
        df = pd.DataFrame(rows)
        st.dataframe(df.sort_values('时间', ascending=False), width="stretch", hide_index=True)
    else:
        st.info('暂无信号记录')

    st.markdown('---')
    st.subheader('持仓实时行情')
    if positions:
        live_rows = []
        for p in positions:
            if p.get('shares', 0) <= 0:
                continue
            sym = p['symbol']
            snap = get_realtime(sym)
            limit_pct = get_limit_pct(sym)
            prev = snap.get('prev_close', 0)
            upper = prev * (1 + limit_pct) if prev else 0
            lower = prev * (1 - limit_pct) if prev else 0
            dist_up   = (upper - snap['price']) / snap['price'] if snap.get('price') else None
            dist_down = (snap['price'] - lower) / snap['price'] if snap.get('price') else None
            live_rows.append({
                '代码':        sym,
                '现价':        f"{snap.get('price', 0):.2f}",
                '涨跌幅':      f"{snap.get('pct', 0):+.2f}%",
                '量比':        f"{snap.get('vol_ratio', '—'):.2f}x" if snap.get('vol_ratio') else '—',
                '涨停价':      f"{upper:.2f}" if upper else '—',
                '距涨停':      f"{dist_up:.1%}" if dist_up is not None else '—',
                '跌停价':      f"{lower:.2f}" if lower else '—',
                '距跌停':      f"{dist_down:.1%}" if dist_down is not None else '—',
                '持仓成本':    f"{p.get('entry_price', 0):.3f}",
            })
        if live_rows:
            df = pd.DataFrame(live_rows)
            st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info('暂无持仓')

    st.button('🔄 刷新', on_click=st.cache_data.clear)

# ─── 页面 3：动态选股 ────────────────────────────────────────

elif page == '🔍 动态选股':
    st.title('🔍 动态选股')
    st.caption('五维评分 · 新闻+行情+资金+技术+一致性')

    live_params = get_live_params()

    # 最新参数展示
    if live_params:
        st.subheader('📌 最新训练参数')
        for key, val in live_params.items():
            with st.expander(f'{key}'):
                st.json(val)

    st.markdown('---')

    # 运行选股（点击时触发）
    st.subheader('执行选股')
    run = st.button('🚀 运行五维选股', disabled=not backend_ok)

    if run:
        # 五维选股耗时说明：需依次拉取新闻、板块行情、Top30 板块成分股，约 30-60s
        st.info('⏳ 五维选股通常需要 30-60 秒，请耐心等待……')
        with st.spinner('正在获取市场数据（新闻 → 板块行情 → 成分股分析）...'):
            try:
                import subprocess
                env = {**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'scripts')}
                if sys.platform == 'win32':
                    env['PYTHONIOENCODING'] = 'utf-8'
                    env['PYTHONUTF8'] = '1'
                result = subprocess.run(
                    [sys.executable, os.path.join(BASE_DIR, 'scripts', 'dynamic_selector.py')],
                    capture_output=True, encoding='utf-8', errors='replace',
                    timeout=120,
                    env=env
                )
                if result.returncode == 0:
                    st.success('✅ 选股完成')
                else:
                    st.warning(f'⚠️ 脚本退出码 {result.returncode}，结果可能不完整')
                st.code(result.stdout[-3000:] if result.stdout else '无输出', language='text')
                if result.stderr:
                    with st.expander('错误详情'):
                        st.code(result.stderr[-1000:], language='text')
            except subprocess.TimeoutExpired:
                st.error('❌ 选股超时（>120s）。可能原因：网络慢或东方财富 API 限流。请稍后重试，或使用已缓存结果。')
            except Exception as e:
                st.error(f'选股失败: {e}')

    # 读取最近选股结果（从文件缓存）
    cache_file = os.path.join(BASE_DIR, 'scripts', 'sector_scores.json')
    if os.path.exists(cache_file):
        with st.spinner('读取缓存结果...'):
            try:
                with open(cache_file, encoding='utf-8') as f:
                    cached = json.loads(f.read())
                st.success(f'缓存时间: {cached.get("updated", "未知")}')
                scores = cached.get('scores', {})
                if scores:
                    rows = [(k, v.get('total', 0), v.get('news', 0), v.get('sector', 0),
                             v.get('flow', 0), v.get('tech', 0))
                            for k, v in scores.items()]
                    rows.sort(key=lambda x: -x[1])
                    df = pd.DataFrame(rows, columns=['板块', '综合分', '新闻', '行情', '资金', '技术'])
                    fig = px.bar(
                        df.head(15), x='板块', y='综合分',
                        color='综合分',
                        title='热门板块TOP15',
                        color_continuous_scale='RdYlGn',
                    )
                    st.plotly_chart(fig, width="stretch")
                    st.dataframe(df.head(20), width="stretch", hide_index=True)
            except Exception as e:
                st.error(f'读取缓存失败: {e}')
    else:
        st.info('暂无选股结果缓存。点击"运行五维选股"生成。')

    st.button('🔄 刷新', on_click=st.cache_data.clear)

# ─── 页面 4：回测分析 ────────────────────────────────────────

elif page == '📉 回测分析':
    st.title('📉 回测分析')
    st.caption('Walk-Forward + Monte Carlo + 沪深300 Benchmark')

    wf = get_wf_results()

    if wf:
        st.subheader(f'历史 WFA 结果（{len(wf)} 条）')
        rows = []
        for r in wf:
            params = json.loads(r.get('best_params', '{}'))
            rows.append({
                '窗口':     r.get('window', '?'),
                '标的':     r.get('symbol', ''),
                '策略':     r.get('strategy', ''),
                '测试Sharpe': f"{r.get('test_sharpe', 0):.2f}",
                '测试收益%': f"{r.get('test_return_pct', 0):+.1f}%",
                '胜率%':    f"{r.get('test_winrate_pct', 0):.0f}%",
                '最大回撤%': f"{r.get('test_maxdd_pct', 0):.1f}%",
                '年化收益%': f"{r.get('annualized_return_pct', 0):+.1f}%",
                '最优参数': str(params)[:40],
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", hide_index=True)

        # Sharpe 柱状图
        fig = px.bar(
            df, x='窗口', y='测试Sharpe',
            color='测试Sharpe', color_continuous_scale='RdYlGn',
            title='各窗口测试集 Sharpe',
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info('暂无 WFA 结果。运行 walkforward_job.py 生成。')

    st.markdown('---')
    st.subheader('运行 Walk-Forward 训练')

    col_sym, col_strat = st.columns([1, 1])
    with col_sym:
        symbol = st.text_input('标的代码', value='510310.SH')
    with col_strat:
        strategy = st.selectbox('策略', ['RSI', 'MACD'], index=0)

    # ── 数据可用性检测 ──────────────────────────────
    st.markdown('**① 检测数据可用性**（改变标的后请重新检测）')

    if 'wfa_detected' not in st.session_state:
        st.session_state['wfa_detected'] = False
        st.session_state['wfa_days'] = 0
        st.session_state['wfa_first'] = ''
        st.session_state['wfa_last'] = ''

    btn_col1, btn_col2 = st.columns([1, 3])
    with btn_col1:
        clicked = st.button('🔍 检测数据可用性')
    with btn_col2:
        if st.session_state['wfa_detected']:
            days = st.session_state['wfa_days']
            first = st.session_state['wfa_first']
            last = st.session_state['wfa_last']
            max_train_test = days // 252
            st.success(f'📈 {symbol} 可用数据: **{days} 天**（{first} ~ {last}）— 最多支持 {max_train_test}y 训练+验证')
        else:
            st.info('点击"检测数据可用性"获取标的实际数据量')

    if clicked:
        with st.spinner('正在检测数据...'):
            try:
                import sys as _sys
                _sys.path.insert(0, os.path.join(BASE_DIR, 'scripts', 'quant'))
                from data_loader import DataLoader
                from datetime import datetime, timedelta
                loader = DataLoader()
                end = datetime.now().strftime('%Y%m%d')
                start = (datetime.now() - timedelta(days=365 * 6)).strftime('%Y%m%d')
                kline = loader.get_kline(symbol, start, end)
                if kline and len(kline) > 100:
                    st.session_state['wfa_detected'] = True
                    st.session_state['wfa_days'] = len(kline)
                    st.session_state['wfa_first'] = kline[0]['date'][:10]
                    st.session_state['wfa_last'] = kline[-1]['date'][:10]
                    st.rerun()
                else:
                    st.error(f'数据不足: 仅获取 {len(kline) if kline else 0} 条')
                    st.session_state['wfa_detected'] = False
            except Exception as e:
                st.error(f'检测失败: {e}')
                st.session_state['wfa_detected'] = False

    # ── 参数配置（仅在检测后才显示） ────────────────────
    BUFFER_DAYS = 90   # 预热缓冲天数

    if st.session_state['wfa_detected']:
        st.markdown('**② 配置训练参数**')
        days = st.session_state['wfa_days']

        # 根据数据量计算可用范围
        min_train_days = 252          # 至少 1 交易年用于训练
        max_train_days = days - 252 - BUFFER_DAYS  # 留下 1y 测试 + 缓冲

        if max_train_days < min_train_days:
            st.error(f'数据不足以进行 WFA：仅有 {days} 天，需至少 {min_train_days + 252 + BUFFER_DAYS} 天（1y 训练 + 1y 测试 + {BUFFER_DAYS}d 缓冲）')
            st.button('🔄 刷新', on_click=st.cache_data.clear)
            st.stop()

        # 初始化默认值（训练占 2/3，测试占 1/3）
        default_train_days = min(504, (max_train_days + min_train_days) // 2)
        if 'wfa_train_days' not in st.session_state:
            st.session_state['wfa_train_days'] = default_train_days

        # ── 单一滑条：训练天数 ──────────────────────────
        new_train_days = st.slider(
            '📐 训练天数',
            min_value=int(min_train_days),
            max_value=int(max_train_days),
            value=int(st.session_state['wfa_train_days']),
            step=21,
            key='wfa_train_days_slider',
        )
        st.session_state['wfa_train_days'] = new_train_days
        test_days = days - new_train_days - BUFFER_DAYS

        # ── 状态栏 ────────────────────────────────────
        total_used = new_train_days + test_days + BUFFER_DAYS
        col_stat = st.columns([1, 1, 1, 2])
        with col_stat[0]: st.metric('训练天数', f'{new_train_days}d')
        with col_stat[1]: st.metric('验证天数', f'{test_days}d')
        with col_stat[2]: st.metric('缓冲天数', f'{BUFFER_DAYS}d')
        with col_stat[3]:
            used_pct = total_used / days * 100
            st.caption(f'📊 共使用 {days} 天中的 {total_used} 天 ({used_pct:.0f}%)')

        # ── 警告判断 ──────────────────────────────────
        if test_days < 252:
            st.warning(f'⚠️ 验证天数仅 {test_days}d（< 252d），测试窗口太少，WFA 结果可能不稳定')
        elif new_train_days > days * 0.75:
            st.warning(f'⚡ 训练天数过长，测试窗口偏少（{test_days}d），建议适当增加验证比例')

        # ── 换算为年数（传给 job 脚本） ─────────────────
        train_yrs = max(1, new_train_days // 252)   # 向下取整，避免需求放大
        test_yrs  = max(1, test_days // 252)
        st.caption(f'→ 实际传参：--train-years={train_yrs} --test-years={test_yrs}（{train_yrs*252}d + {test_yrs*252}d）')

        if st.button('🚀 开始 Walk-Forward 训练'):
            st.info(f'📊 {symbol} | 训练 {new_train_days}d({train_yrs}y) + 验证 {test_days}d({test_yrs}y) | 缓冲 {BUFFER_DAYS}d | 共 {total_used}d')
            with st.spinner('训练中（可能需要几分钟）...'):
                try:
                    import subprocess
                    env = {**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'scripts')}
                    if sys.platform == 'win32':
                        env['PYTHONIOENCODING'] = 'utf-8'
                        env['PYTHONUTF8'] = '1'
                    cmd = [
                        sys.executable,
                        os.path.join(BASE_DIR, 'scripts', 'walkforward_job.py'),
                        '--symbol', symbol,
                        '--strategy', strategy,
                        '--train-years', str(train_yrs),
                        '--test-years', str(test_yrs),
                    ]
                    result = subprocess.run(
                        cmd,
                        capture_output=True, encoding='utf-8', errors='replace', timeout=600,
                        env=env
                    )
                    st.code(result.stdout[-5000:] if result.stdout else '无输出', language='text')
                    if result.stderr and 'warning' not in result.stderr.lower():
                        st.warning(result.stderr[-500:])
                except Exception as e:
                    st.error(f'训练失败: {e}')
    else:
        st.markdown('*请先点击上方「🔍 检测数据可用性」后再配置训练参数*')
        train_yrs, test_yrs = 2, 1  # placeholder

    st.button('🔄 刷新', on_click=st.cache_data.clear)

# ─── 页面 5：持仓详情 ────────────────────────────────────────

elif page == '💼 持仓详情':
    st.title('💼 持仓详情')

    if not positions:
        st.info('暂无持仓')
    else:
        for p in positions:
            if p.get('shares', 0) <= 0:
                continue
            sym = p['symbol']
            snap = get_realtime(sym)
            limit_pct = get_limit_pct(sym)
            prev = snap.get('prev_close', 0) if snap else 0
            upper = prev * (1 + limit_pct) if prev else 0
            lower = prev * (1 - limit_pct) if prev else 0

            entry_price = p.get('entry_price', 0)
            shares = p['shares']
            current_price = snap.get('price', entry_price) if snap else entry_price
            market_value = current_price * shares
            unrealized_pnl = (current_price - entry_price) * shares

            with st.container():
                col1, col2, col3, col4 = st.columns(4)
                col1.metric('代码', sym)
                col2.metric('持仓', f"{shares} 股")
                col3.metric('成本价', f"¥{entry_price:.3f}")
                col4.metric('当前价', f"¥{current_price:.2f}" if current_price else '—')

                col5, col6, col7, col8 = st.columns(4)
                col5.metric('持仓市值', f"¥{market_value:,.0f}")
                col6.metric('浮动盈亏', f"{unrealized_pnl:+,.0f}")
                col7.metric('今日涨跌', f"{snap.get('pct', 0):+.2f}%" if snap else '—')
                col8.metric('量比', f"{snap.get('vol_ratio', 0):.2f}x" if snap and snap.get('vol_ratio') else '—')

                # 涨跌停状态
                if snap and prev:
                    dist_up   = (upper - snap['price']) / snap['price']
                    dist_down = (snap['price'] - lower) / snap['price']

                    urgent = dist_down < 0.03 or dist_up < 0.01
                    if urgent:
                        st.error(f'⚠️ {sym} 接近涨跌停！' +
                                 (f'距涨停 {dist_up:.1%}' if dist_up < dist_down else f'距跌停 {dist_down:.1%}'))
                    else:
                        st.success(f'✅ {sym} 运行正常 | 涨停价 ¥{upper:.2f} | 跌停价 ¥{lower:.2f}')

                st.markdown('---')

    st.button('🔄 刷新', on_click=st.cache_data.clear)

# ─── 页面 6：历史交易 ────────────────────────────────────────

elif page == '📋 历史交易':
    st.title('📋 历史交易')

    if trades:
        rows = []
        for t in trades:
            rows.append({
                '时间':      t.get('date', '')[:16],
                '代码':      t.get('symbol', ''),
                '方向':      '🔴 卖出' if t.get('direction') == 'sell' else '🟢 买入',
                '价格':      f"¥{t.get('price', 0):.3f}",
                '股数':      t.get('shares', 0),
                '总额':      f"¥{t.get('shares', 0) * t.get('price', 0):,.0f}",
                '原因/备注': t.get('reason', '')[:30] if t.get('reason') else '',
            })
        df = pd.DataFrame(rows)
        st.dataframe(df.sort_values('时间', ascending=False), width="stretch", hide_index=True)

        # 买卖统计
        buys  = [r for r in rows if '买入' in r['方向']]
        sells = [r for r in rows if '卖出' in r['方向']]
        c1, c2 = st.columns(2)
        c1.metric('买入次数', len(buys))
        c2.metric('卖出次数', len(sells))
    else:
        st.info('暂无交易记录')

    st.button('🔄 刷新', on_click=st.cache_data.clear)


# ─── 页面 7：实盘记录 ────────────────────────────────────────
elif page == '📋 实盘记录':
    st.title('📋 实盘记录')
    st.caption('模拟交易全流程追踪 · 明日 09:30 自动开始扫描信号')

    # ── 账户概览 ──────────────────────────────────────
    summary = api_get('/portfolio/summary', timeout=5)
    cash     = summary.get('cash', 0)
    equity   = summary.get('total_equity', cash)
    pos_val  = summary.get('position_value', 0)
    real_pnl = summary.get('realized_pnl', 0)
    total_pnl  = summary.get('total_pnl', 0)

    m_col = st.columns([1, 1, 1, 1, 1])
    m_col[0].metric('总权益', f'¥{equity:,.0f}')
    m_col[1].metric('可用现金', f'¥{cash:,.0f}')
    m_col[2].metric('持仓市值', f'¥{pos_val:,.0f}')
    m_col[3].metric('已实现盈亏', f'{real_pnl:+.0f}',
                     delta=f'{real_pnl/equity*100:+.1f}%' if equity else None)
    m_col[4].metric('总盈亏', f'{total_pnl:+.0f}',
                     delta=f'{total_pnl/equity*100:+.1f}%' if equity else None)

    # ── 持仓明细 ──────────────────────────────────────
    positions_data = summary.get('positions', [])
    st.subheader('📦 当前持仓')
    if positions_data:
        rows = []
        for p in positions_data:
            sym = p.get('symbol', '')
            snap = get_realtime(sym)
            entry = p.get('entry_price', 0)
            cur = snap.get('price', entry) if snap else entry
            shares = p.get('shares', 0)
            mkt_val = cur * shares
            pnl = (cur - entry) * shares
            pnl_pct = pnl / (entry * shares) if entry * shares else 0
            day_pct = snap.get('pct', 0) if snap else 0
            rows.append({
                '代码': sym,
                '股数': shares,
                '成本': f'¥{entry:.3f}',
                '现价': f'¥{cur:.3f}',
                '市值': f'¥{mkt_val:,.0f}',
                '浮动盈亏': f'{pnl:+,.0f}',
                '盈亏%': f'{pnl_pct:+.1%}',
                '今日': f'{day_pct:+.2f}%',
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info('当前无持仓（市场收盘或已清仓）')

    # ── 权益曲线 ──────────────────────────────────────
    st.subheader('📈 权益曲线')
    daily_data = api_get('/portfolio/daily', timeout=5).get('daily', [])
    if daily_data:
        df_daily = pd.DataFrame(daily_data)
        df_daily = df_daily.sort_values('trade_date')
        fig = px.line(
            df_daily, x='trade_date', y='equity',
            title='每日权益（Paper Trade）',
            labels={'trade_date': '日期', 'equity': '总权益（¥）'},
            markers=True,
        )
        fig.update_layout(hovermode='x unified')
        st.plotly_chart(fig, width="stretch")
        start_eq = df_daily.iloc[0]['equity'] if not df_daily.empty else equity
        st.caption(f'起始: ¥{start_eq:,.0f} → 当前: ¥{equity:,.0f} | 收益率: {(equity-start_eq)/start_eq*100:+.1f}%')
    else:
        st.info('暂无日线权益数据')

    # ── 最近成交 ──────────────────────────────────────
    st.subheader('📋 最近成交记录')
    recent_trades = summary.get('recent_trades', [])
    if recent_trades:
        rows = []
        for t in recent_trades:
            rows.append({
                '时间':    t.get('executed_at', '')[:16],
                '代码':    t.get('symbol', ''),
                '方向':    '🔴 卖出' if t.get('direction') == 'SELL' else '🟢 买入',
                '价格':    f'¥{t.get("price", 0):.3f}',
                '股数':    t.get('shares', 0),
                '总额':    f'¥{t.get("shares", 0) * t.get("price", 0):,.0f}',
                '滑点':    f'{t.get("slippage_bps") or 0:+.1f}bps',
                '单笔盈亏': f'{t.get("pnl", 0):+.0f}' if t.get('pnl') is not None else '—',
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info('暂无成交记录')

    # ── 盘中预警 ──────────────────────────────────────
    st.subheader('🔔 盘中预警记录')
    alerts_data = api_get('/alerts/history?limit=10', timeout=5).get('alerts', [])
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_alerts = [a for a in alerts_data
                    if str(a.get('triggered_at', '')).startswith(today_str)]
    show_alerts = today_alerts if today_alerts else alerts_data[:5]
    if show_alerts:
        for a in show_alerts[:8]:
            col1, col2 = st.columns([1, 4])
            with col1:
                pct = a.get('pct_change', 0)
                emoji = '🔴' if pct < 0 else '🟢'
                st.markdown(f'{emoji} `{a.get("symbol","")}`')
            with col2:
                msg = a.get('message', '(无内容)')
                st.markdown(f"{msg.replace(chr(10), ' | ')} **{pct:+.2f}%** · {a.get('triggered_at','')[:16]}")
    else:
        st.info('今日暂无预警')

    # ── 候选标的 ──────────────────────────────────────
    st.subheader('👀 候选标的（明日待买）')
    watchlist = api_get('/watchlist', timeout=5).get('watchlist', [])
    enabled = [w for w in watchlist if w.get('enabled', 0) == 1]
    if enabled:
        rows = []
        for w in enabled[:10]:
            sym = w.get('symbol', '')
            snap = get_realtime(sym)
            cur = snap.get('price', 0) if snap else 0
            day_pct = snap.get('pct', 0) if snap else 0
            rows.append({
                '代码':     sym,
                '名称':     w.get('name', '—'),
                '关注理由': w.get('reason', ''),
                '现价':     f'¥{cur:.2f}' if cur else '—',
                '今日涨跌': f'{day_pct:+.2f}%',
                '预警阈值': f"±{w.get('alert_pct', 5):.1f}%",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info('候选标的为空（今日五维选股尚未运行）')

    st.button('🔄 刷新', on_click=st.cache_data.clear)

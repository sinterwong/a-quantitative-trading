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
        with open(pfile) as f:
            return json.load(f)
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
    ],
    index=0,
)

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
    total   = portfolio.get('total_value', cash)
    pos_val = total - cash
    today_pnl = portfolio.get('today_pnl', 0)
    total_pnl = portfolio.get('total_pnl', 0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric('💰 可用现金',     f'¥{cash:,.0f}')
    col2.metric('📦 持仓市值',     f'¥{pos_val:,.0f}')
    col3.metric('💹 总资产',        f'¥{total:,.0f}', delta=f'{today_pnl:+,.0f}' if today_pnl else None)
    col4.metric('📈 累计盈亏',     f'{total_pnl:+,.0f}' if total_pnl else '—', delta=f'{total_pnl/total*100:+.1f}%' if total and total_pnl else None)

    st.markdown('---')

    # 持仓饼图
    if positions:
        labels = [p['symbol'] for p in positions if p.get('shares', 0) > 0]
        values = [p.get('market_value', 0) for p in positions if p.get('shares', 0) > 0]
        if labels:
            fig = px.pie(
                names=labels, values=values,
                title='持仓分布',
                hole=0.4,
                color_discrete_sequence=px.colors.qualitative.Set3,
            )
            fig.update_traces(textposition='inside', textinfo='percent+label')
            fig.update_layout(margin=dict(t=30, b=30))
            st.plotly_chart(fig, use_container_width=True)

    # 持仓表格
    st.subheader('持仓明细')
    if positions:
        rows = []
        for p in positions:
            if p.get('shares', 0) <= 0:
                continue
            snap = get_realtime(p['symbol'])
            row = {
                '代码':     p['symbol'],
                '股数':     p['shares'],
                '成本价':   f"{p.get('avg_cost', 0):.3f}",
                '当前价':   f"{snap.get('price', '—'):.3f}" if snap else '—',
                '市值':     f"¥{p.get('market_value', 0):,.0f}",
                '盈亏额':   f"{p.get('unrealized_pnl', 0):+,.0f}",
                '盈亏%':    f"{p.get('unrealized_pnl_pct', 0):+.1%}" if p.get('unrealized_pnl_pct') else '—',
                '今日涨跌': f"{snap.get('pct', 0):+.2f}%" if snap else '—',
            }
            rows.append(row)
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
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
        st.dataframe(df.sort_values('时间', ascending=False), use_container_width=True, hide_index=True)
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
                '持仓成本':    f"{p.get('avg_cost', 0):.3f}",
            })
        if live_rows:
            df = pd.DataFrame(live_rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
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
        with st.spinner('正在获取市场数据...'):
            try:
                sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
                # 延迟导入避免启动慢
                import subprocess
                result = subprocess.run(
                    [sys.executable, os.path.join(BASE_DIR, 'scripts', 'dynamic_selector.py')],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'scripts')}
                )
                st.code(result.stdout[-2000:] if result.stdout else '无输出', language='text')
                if result.stderr:
                    st.warning(result.stderr[-500:])
            except Exception as e:
                st.error(f'选股失败: {e}')

    # 读取最近选股结果（从文件缓存）
    cache_file = os.path.join(BASE_DIR, 'scripts', 'sector_scores.json')
    if os.path.exists(cache_file):
        with st.spinner('读取缓存结果...'):
            try:
                with open(cache_file) as f:
                    cached = json.load(f)
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
                    st.plotly_chart(fig, use_container_width=True)
                    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
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
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Sharpe 柱状图
        fig = px.bar(
            df, x='窗口', y='测试Sharpe',
            color='测试Sharpe', color_continuous_scale='RdYlGn',
            title='各窗口测试集 Sharpe',
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info('暂无 WFA 结果。运行 walkforward_job.py 生成。')

    st.markdown('---')
    st.subheader('运行 Walk-Forward 训练')

    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input('标的代码', value='510310.SH')
    with col2:
        strategy = st.selectbox('策略', ['RSI', 'MACD'], index=0)

    if st.button('🚀 开始 Walk-Forward 训练'):
        with st.spinner('训练中（可能需要几分钟）...'):
            try:
                import subprocess
                result = subprocess.run(
                    [
                        sys.executable,
                        os.path.join(BASE_DIR, 'scripts', 'walkforward_job.py'),
                        '--symbol', symbol,
                        '--strategy', strategy,
                    ],
                    capture_output=True, text=True, timeout=300,
                    env={**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'scripts')}
                )
                st.code(result.stdout[-3000:] if result.stdout else '无输出', language='text')
                if result.stderr:
                    st.warning(result.stderr[-500:])
                st.success('训练完成！')
                st.cache_data.clear()
            except Exception as e:
                st.error(f'训练失败: {e}')

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
            prev = snap.get('prev_close', 0)
            upper = prev * (1 + limit_pct) if prev else 0
            lower = prev * (1 - limit_pct) if prev else 0

            with st.container():
                col1, col2, col3, col4 = st.columns(4)
                col1.metric('代码', sym)
                col2.metric('持仓', f"{p['shares']} 股")
                col3.metric('成本价', f"¥{p.get('avg_cost', 0):.3f}")
                col4.metric('当前价', f"¥{snap.get('price', 0):.2f}" if snap else '—')

                col5, col6, col7, col8 = st.columns(4)
                col5.metric('持仓市值', f"¥{p.get('market_value', 0):,.0f}")
                col6.metric('浮动盈亏', f"{p.get('unrealized_pnl', 0):+,.0f}")
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
        st.dataframe(df.sort_values('时间', ascending=False), use_container_width=True, hide_index=True)

        # 买卖统计
        buys  = [r for r in rows if '买入' in r['方向']]
        sells = [r for r in rows if '卖出' in r['方向']]
        c1, c2 = st.columns(2)
        c1.metric('买入次数', len(buys))
        c2.metric('卖出次数', len(sells))
    else:
        st.info('暂无交易记录')

    st.button('🔄 刷新', on_click=st.cache_data.clear)

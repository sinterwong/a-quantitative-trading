#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamlit_app.py — 量化系统 Web UI（重构版）
=============================================
系统定位：稳定模拟实盘 · SimulatedBroker · A 股市场

启动方式：
  streamlit run streamlit_app.py --server.port 8501

页面结构：
  1. 📊 仪表盘     — 系统状态、账户摘要、今日关键指标
  2. 💼 组合管理   — 持仓明细、权益曲线、历史成交
  3. 🎯 策略分配   — 多策略资金分配（PortfolioAllocator）
  4. 📈 交易信号   — 实时信号、持仓行情、候选标的
  5. 📉 回测验证   — Walk-Forward 分析、模拟一致性验证
  6. 🔍 数据质量   — Level2 完整率、数据源健康状态
  7. 🏥 策略健康   — Rolling Sharpe、TCA、CVaR/Monte Carlo
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ─── 路径设置 ────────────────────────────────────────────────
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, 'backend')
CORE_DIR    = os.path.join(BASE_DIR, 'core')
DATA_DIR    = os.path.join(BASE_DIR, 'data')
OUTPUTS_DIR = os.path.join(BASE_DIR, 'outputs')

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, BACKEND_DIR)

BACKEND_URL = os.environ.get('BACKEND_URL', 'http://127.0.0.1:5555')

# ─── SSL context ────────────────────────────────────────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ============================================================
# Backend HTTP helpers
# ============================================================

def api_get(endpoint: str, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'QuantUI/2.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def api_post(endpoint: str, data: dict, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    payload = json.dumps(data).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantUI/2.0'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ============================================================
# Cached data loaders
# ============================================================

@st.cache_data(ttl=60)
def load_portfolio_summary() -> dict:
    return api_get('/portfolio/summary')


@st.cache_data(ttl=60)
def load_positions() -> list:
    return api_get('/positions').get('positions', [])


@st.cache_data(ttl=60)
def load_trades(limit: int = 50) -> list:
    return api_get(f'/trades?limit={limit}').get('trades', [])


@st.cache_data(ttl=60)
def load_signals(limit: int = 30) -> list:
    return api_get(f'/signals?limit={limit}').get('signals', [])


@st.cache_data(ttl=60)
def load_daily_equity(limit: int = 60) -> list:
    return api_get(f'/portfolio/daily?limit={limit}').get('daily', [])


@st.cache_data(ttl=120)
def load_daily_stats(limit: int = 250) -> list:
    raw = api_get(f'/portfolio/daily_stats?limit={limit}')
    if raw.get('daily_stats'):
        return raw['daily_stats']
    db = os.path.join(BACKEND_DIR, 'services', 'portfolio.db')
    if not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, daily_return, n_trades, equity FROM daily_stats "
            "ORDER BY date ASC LIMIT 500"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=300)
def load_wf_results(limit: int = 20) -> list:
    for db_path in [
        os.path.join(BASE_DIR, 'backend', 'wf_results.db'),
        os.path.join(BACKEND_DIR, 'services', 'wf_results.db'),
    ]:
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM wf_results ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception:
                pass
    return []


@st.cache_data(ttl=30)
def load_realtime(symbol: str) -> dict:
    """从腾讯财经拉取实时行情。"""
    u = symbol.upper()
    if u.endswith('.SH'):
        sym = 'sh' + u[:-3]
    elif u.endswith('.SZ'):
        sym = 'sz' + u[:-3]
    else:
        sym = symbol.lower()
    try:
        req = urllib.request.Request(
            f'https://qt.gtimg.cn/q={sym}',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com'},
        )
        with urllib.request.urlopen(req, timeout=6, context=_SSL_CTX) as r:
            raw = r.read().decode('gbk', errors='replace')
        eq = raw.find('="')
        if eq >= 0:
            raw = raw[eq + 2:]
        f = raw.split('~')
        if len(f) < 40:
            return {}
        return {
            'price':     float(f[3])  if f[3]  not in ('', '-') else 0.0,
            'prev_close': float(f[4]) if f[4]  not in ('', '-') else 0.0,
            'pct':       float(f[32]) if f[32] not in ('', '-') else 0.0,
            'vol_ratio': float(f[38]) if len(f) > 38 and f[38] not in ('', '-', '0') else None,
            'high':      float(f[33]) if len(f) > 33 and f[33] not in ('', '-') else 0.0,
            'low':       float(f[34]) if len(f) > 34 and f[34] not in ('', '-') else 0.0,
        }
    except Exception:
        return {}


@st.cache_data(ttl=60)
def load_watchlist() -> list:
    return api_get('/watchlist').get('watchlist', [])


@st.cache_data(ttl=60)
def load_alerts(limit: int = 20) -> list:
    return api_get(f'/alerts/history?limit={limit}').get('alerts', [])


@st.cache_data(ttl=300)
def load_trading_config() -> dict:
    """读取 config/trading.yaml。"""
    cfg_path = os.path.join(BASE_DIR, 'config', 'trading.yaml')
    if not os.path.exists(cfg_path):
        return {}
    try:
        import yaml  # type: ignore
        with open(cfg_path, encoding='utf-8') as f:
            docs = list(yaml.safe_load_all(f))
        return docs[0] if docs else {}
    except ImportError:
        # 如果没有 pyyaml，尝试简单解析
        return {}
    except Exception:
        return {}


def limit_up_pct(symbol: str) -> float:
    s = symbol.lower().replace('.sz', '').replace('.sh', '')
    if any(s.startswith(p) for p in ('st', '*st')):
        return 0.05
    if s.startswith('300') or s.startswith('688'):
        return 0.20
    return 0.10


# ============================================================
# Page config & global CSS
# ============================================================

st.set_page_config(
    page_title='量化系统',
    page_icon='📊',
    layout='wide',
    menu_items={'About': '## 量化系统 · 稳定模拟实盘\nSimulatedBroker · A 股市场'},
)

st.markdown("""
<style>
.metric-card {
    background: #1e2130;
    border-radius: 8px;
    padding: 16px;
    border-left: 4px solid #4c78a8;
}
.broker-badge {
    background: #0e4429;
    color: #3fb950;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 600;
}
.mode-badge {
    background: #161b22;
    color: #8b949e;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.82rem;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# Sidebar
# ============================================================

st.sidebar.title('量化系统')
st.sidebar.caption(datetime.now().strftime('%Y-%m-%d  %H:%M'))

# 系统状态
backend_ok = api_get('/health', timeout=3).get('status') == 'ok'
if backend_ok:
    st.sidebar.success('Backend 运行中')
else:
    st.sidebar.error('Backend 未连接')

# Broker 模式（固定，不可切换）
st.sidebar.markdown(
    '<span class="broker-badge">SimulatedBroker</span>'
    '&nbsp;<span class="mode-badge">模拟实盘</span>',
    unsafe_allow_html=True,
)
st.sidebar.caption('券商接口：A 股规则 · 整手 · 印花税 · 滑点')

st.sidebar.markdown('---')

page = st.sidebar.radio(
    '导航',
    [
        '📊 仪表盘',
        '💼 组合管理',
        '🎯 策略分配',
        '📈 交易信号',
        '📉 回测验证',
        '🔍 数据质量',
        '🏥 策略健康',
    ],
    index=0,
)

st.sidebar.markdown('---')
st.sidebar.button('全局刷新', on_click=st.cache_data.clear, use_container_width=True)


# ============================================================
# Page 1: 仪表盘
# ============================================================

if page == '📊 仪表盘':
    st.title('📊 仪表盘')

    portfolio = load_portfolio_summary()
    cash      = portfolio.get('cash', 0)
    equity    = portfolio.get('total_equity', cash)
    pos_val   = portfolio.get('position_value', pos_val := portfolio.get('position_value', 0))
    unreal    = portfolio.get('unrealized_pnl', 0)
    real_pnl  = portfolio.get('realized_pnl', 0)
    total_pnl = portfolio.get('total_pnl', 0)
    updated   = portfolio.get('updated_at', '—')[:16]

    # ── 账户指标 ─────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric('总权益', f'¥{equity:,.0f}',
              delta=f'{unreal:+,.0f}' if unreal else None)
    c2.metric('可用现金', f'¥{cash:,.0f}')
    c3.metric('持仓市值', f'¥{pos_val:,.0f}')
    c4.metric('浮动盈亏', f'{unreal:+,.0f}',
              delta=f'{unreal/equity*100:+.1f}%' if equity else None)
    c5.metric('累计已实现', f'{real_pnl:+,.0f}')

    st.markdown(f'*数据更新时间: {updated}*')
    st.markdown('---')

    # ── 系统状态卡片 ─────────────────────────────────────────
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader('系统状态')
        positions = load_positions()
        signals   = load_signals(10)
        trades    = load_trades(10)

        info = {
            'Backend API':      '运行中' if backend_ok else '未连接',
            'Broker 模式':      'SimulatedBroker (A 股模拟)',
            'A 股整手规则':     '启用 (100 股/手)',
            '印花税':           '0.1%（卖出）',
            '佣金率':           '0.03%',
            '默认滑点':         '5 bps',
            '当前持仓数':       f'{sum(1 for p in positions if p.get("shares", 0) > 0)} 只',
            '今日信号数':       f'{len(signals)} 条',
            '历史成交数':       f'{len(trades)} 笔',
        }
        for k, v in info.items():
            st.markdown(f'**{k}：** {v}')

    with col_b:
        st.subheader('最近信号')
        if signals:
            rows = []
            for s in signals[:8]:
                rows.append({
                    '时间':  str(s.get('emitted_at', ''))[:16],
                    '代码':  s.get('symbol', ''),
                    '信号':  s.get('signal', ''),
                    '方向':  s.get('direction', ''),
                    '原因':  str(s.get('reason', ''))[:30],
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info('暂无信号')

    st.markdown('---')

    # ── 近期预警 ─────────────────────────────────────────────
    st.subheader('近期预警（最近 5 条）')
    alerts = load_alerts(5)
    if alerts:
        for a in alerts:
            pct = a.get('pct_change', 0)
            icon = '🔴' if pct < 0 else '🟢'
            msg = a.get('message', '')
            ts  = str(a.get('triggered_at', ''))[:16]
            st.markdown(f"{icon} `{a.get('symbol','')}` · {msg[:60]} · **{pct:+.2f}%** · {ts}")
    else:
        st.info('暂无近期预警')


# ============================================================
# Page 2: 组合管理
# ============================================================

elif page == '💼 组合管理':
    st.title('💼 组合管理')

    portfolio = load_portfolio_summary()
    positions = load_positions()
    trades    = load_trades(100)

    # ── 持仓明细 ─────────────────────────────────────────────
    st.subheader('持仓明细')
    active = [p for p in positions if p.get('shares', 0) > 0]

    if active:
        col_chart, col_table = st.columns([1, 2])
        rows = []
        pie_labels, pie_values = [], []
        for p in active:
            sym   = p['symbol']
            snap  = load_realtime(sym)
            entry = p.get('entry_price', 0)
            cur   = snap.get('price', entry) if snap else entry
            sh    = p['shares']
            mv    = cur * sh
            pnl   = (cur - entry) * sh
            pnl_pct = pnl / (entry * sh) if entry * sh else 0
            pie_labels.append(sym)
            pie_values.append(mv)
            rows.append({
                '代码':   sym,
                '股数':   sh,
                '成本':   f'¥{entry:.3f}',
                '现价':   f'¥{cur:.3f}' if cur else '—',
                '市值':   f'¥{mv:,.0f}',
                '浮动盈亏': f'{pnl:+,.0f}',
                '盈亏%':  f'{pnl_pct:+.2%}',
                '今日':   f'{snap.get("pct", 0):+.2f}%' if snap else '—',
            })

        with col_chart:
            fig = px.pie(
                names=pie_labels, values=pie_values,
                title='持仓分布',
                hole=0.45,
                color_discrete_sequence=px.colors.qualitative.Pastel,
            )
            fig.update_traces(textposition='inside', textinfo='percent+label')
            fig.update_layout(margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

        with col_table:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.info('当前无持仓')

    st.markdown('---')

    # ── 权益曲线 ─────────────────────────────────────────────
    st.subheader('权益曲线')
    daily = load_daily_equity(60)
    if daily:
        df_eq = pd.DataFrame(daily).sort_values('trade_date')
        if 'equity' in df_eq.columns:
            fig = px.line(
                df_eq, x='trade_date', y='equity',
                labels={'trade_date': '日期', 'equity': '总权益 (¥)'},
                markers=True,
            )
            fig.update_layout(hovermode='x unified', margin=dict(t=20, b=30))
            st.plotly_chart(fig, use_container_width=True)
            eq0 = df_eq.iloc[0]['equity']
            eq1 = df_eq.iloc[-1]['equity']
            st.caption(f'起始: ¥{eq0:,.0f} → 当前: ¥{eq1:,.0f}  |  收益率: {(eq1-eq0)/eq0*100:+.2f}%')
    else:
        st.info('暂无权益曲线数据（需运行至少一个交易日）')

    st.markdown('---')

    # ── 历史成交 ─────────────────────────────────────────────
    st.subheader('历史成交记录')
    if trades:
        rows = []
        for t in trades:
            direction = str(t.get('direction', '')).upper()
            rows.append({
                '时间':      str(t.get('date', t.get('executed_at', '')))[:16],
                '代码':      t.get('symbol', ''),
                '方向':      '🔴 卖出' if direction in ('SELL', 'sell') else '🟢 买入',
                '价格':      f'¥{t.get("price", 0):.3f}',
                '股数':      t.get('shares', 0),
                '金额':      f'¥{t.get("shares", 0) * t.get("price", 0):,.0f}',
                '滑点':      f'{t.get("slippage_bps") or 0:+.1f}bps' if t.get('slippage_bps') is not None else '—',
                '单笔盈亏':  f'{t.get("pnl", 0):+.0f}' if t.get('pnl') is not None else '—',
            })
        df_tr = pd.DataFrame(rows).sort_values('时间', ascending=False)
        st.dataframe(df_tr, hide_index=True, use_container_width=True)

        buys  = sum(1 for r in rows if '买入' in r['方向'])
        sells = sum(1 for r in rows if '卖出' in r['方向'])
        c1, c2, c3 = st.columns(3)
        c1.metric('买入次数', buys)
        c2.metric('卖出次数', sells)
        c3.metric('共计成交', len(rows))
    else:
        st.info('暂无成交记录')


# ============================================================
# Page 3: 策略分配
# ============================================================

elif page == '🎯 策略分配':
    st.title('🎯 策略分配')
    st.caption('基于 PortfolioAllocator · 支持等权 / 固定权重 / 风险平价')

    try:
        from core.portfolio_allocator import (
            AllocConfig, PortfolioAllocator, WeightMode,
        )
        alloc_available = True
    except ImportError as e:
        st.error(f'PortfolioAllocator 导入失败: {e}')
        alloc_available = False

    if alloc_available:
        # ── 配置面板 ─────────────────────────────────────────
        cfg = load_trading_config()
        strategies_cfg = cfg.get('strategies', {})
        portfolio_cfg  = cfg.get('portfolio', {})
        total_capital  = float(portfolio_cfg.get('capital', 100_000))

        col_cfg, col_result = st.columns([1, 2])

        with col_cfg:
            st.subheader('分配配置')
            total_capital = st.number_input(
                '总资金（元）',
                value=total_capital, min_value=10_000.0, step=10_000.0,
                format='%.0f',
            )
            mode_label = st.selectbox(
                '权重模式',
                ['等权 (EQUAL)', '固定权重 (FIXED)', '风险平价 (RISK_PARITY)'],
                index=0,
            )
            mode_map = {
                '等权 (EQUAL)': WeightMode.EQUAL,
                '固定权重 (FIXED)': WeightMode.FIXED,
                '风险平价 (RISK_PARITY)': WeightMode.RISK_PARITY,
            }
            weight_mode = mode_map[mode_label]

            reserve_pct = st.slider('保留现金比例 (%)', 0, 20, 5, step=1, format='%d%%')
            reserve = reserve_pct / 100

            st.markdown('**策略权重（固定权重模式有效）**')
            custom_weights = {}
            default_names = list(strategies_cfg.keys()) or ['RSI', 'MACD', 'Bollinger']
            for name in default_names:
                w = st.number_input(f'{name}', value=1.0/len(default_names),
                                    min_value=0.0, max_value=1.0, step=0.05,
                                    key=f'w_{name}', format='%.2f')
                custom_weights[name] = w

        with col_result:
            st.subheader('分配结果')
            try:
                config = AllocConfig(
                    mode=weight_mode,
                    reserve_ratio=reserve,
                    min_strategy_weight=0.05,
                    max_strategy_weight=0.60,
                )
                allocator = PortfolioAllocator(total_capital=total_capital, config=config)

                for name in default_names:
                    w = custom_weights[name] if weight_mode == WeightMode.FIXED else None
                    allocator.add_strategy(name, weight=w)

                # 读取当前持仓市值并更新 usage
                positions = load_positions()
                pos_by_sym = {p['symbol']: p for p in positions if p.get('shares', 0) > 0}
                for name in default_names:
                    strat_cfg = strategies_cfg.get(name, {})
                    sym = strat_cfg.get('symbol', '')
                    if sym and sym in pos_by_sym:
                        p   = pos_by_sym[sym]
                        snap = load_realtime(sym)
                        cur  = snap.get('price', p.get('entry_price', 0)) if snap else p.get('entry_price', 0)
                        mv   = cur * p['shares']
                        allocator.update_usage(name, mv)

                summary = allocator.summary()
                strat_info = summary['strategies']

                # 分配表格
                rows = []
                for sname, info in strat_info.items():
                    strat_sym = strategies_cfg.get(sname, {}).get('symbol', '—')
                    rows.append({
                        '策略':    sname,
                        '标的':    strat_sym,
                        '权重':    f'{info["weight"]:.1%}',
                        '额度(¥)': f'{info["budget"]:,.0f}',
                        '已用(¥)': f'{info["used"]:,.0f}',
                        '可用(¥)': f'{info["available"]:,.0f}',
                        '利用率':  f'{info["utilization"]:.1%}',
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

                # 汇总
                s1, s2, s3, s4 = st.columns(4)
                s1.metric('总资金', f'¥{summary["total_capital"]:,.0f}')
                s2.metric('已分配', f'¥{summary["total_budget"]:,.0f}')
                s3.metric('已使用', f'¥{summary["total_used"]:,.0f}')
                s4.metric('保留现金', f'¥{summary["reserve"]:,.0f}')

                # 分配可视化
                if rows:
                    budgets = [float(r['额度(¥)'].replace(',', '')) for r in rows]
                    fig = px.bar(
                        x=[r['策略'] for r in rows],
                        y=budgets,
                        color=[r['策略'] for r in rows],
                        labels={'x': '策略', 'y': '分配额度 (¥)'},
                        title='各策略资金分配',
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    fig.update_layout(showlegend=False, margin=dict(t=40, b=20))
                    st.plotly_chart(fig, use_container_width=True)

                # 再平衡检测
                st.markdown('---')
                st.subheader('再平衡检测')
                current_mv = {
                    name: strat_info[name]['used']
                    for name in strat_info
                }
                if allocator.needs_rebalance(current_mv):
                    st.warning('持仓偏离超过阈值，建议执行再平衡。')
                    if st.button('执行再平衡'):
                        new_budgets = allocator.rebalance(trigger='manual')
                        st.success(f'再平衡完成，新额度：{new_budgets}')
                else:
                    st.success('持仓权重正常，无需再平衡。')

            except Exception as e:
                st.error(f'PortfolioAllocator 运行错误: {e}')

        st.markdown('---')
        st.subheader('模式说明')
        st.markdown("""
| 模式 | 说明 | 适用场景 |
|------|------|---------|
| **等权 (EQUAL)** | 所有策略平均分配资金 | 策略差异不明显时 |
| **固定权重 (FIXED)** | 按用户设定比例分配 | 手动控制各策略敞口 |
| **风险平价 (RISK_PARITY)** | 按滚动波动率倒数加权（低波动 → 高权重） | 追求风险贡献均衡 |
""")


# ============================================================
# Page 4: 交易信号
# ============================================================

elif page == '📈 交易信号':
    st.title('📈 交易信号')

    signals   = load_signals(50)
    positions = load_positions()
    watchlist = load_watchlist()

    # ── 信号统计 ──────────────────────────────────────────────
    if signals:
        sig_types: Dict[str, int] = {}
        for s in signals:
            t = s.get('signal', 'OTHER')
            sig_types[t] = sig_types.get(t, 0) + 1
        cols = st.columns(min(len(sig_types), 5))
        for i, (sig, cnt) in enumerate(sorted(sig_types.items())):
            cols[i % 5].metric(sig, f'{cnt} 次')

    # ── 信号详情表格 ──────────────────────────────────────────
    st.subheader('最近信号')
    if signals:
        rows = []
        for s in signals:
            rows.append({
                '时间':    str(s.get('emitted_at', ''))[:16],
                '代码':    s.get('symbol', ''),
                '信号':    s.get('signal', ''),
                '方向':    s.get('direction', ''),
                '强度':    f"{s.get('strength', 0):.2f}",
                'RSI':     f"{s.get('prev_rsi', 0):.0f}" if s.get('prev_rsi') else '—',
                '涨跌%':   f"{s.get('pct', 0):+.2f}%" if s.get('pct') is not None else '—',
                '原因':    str(s.get('reason', ''))[:40],
            })
        df_sig = pd.DataFrame(rows).sort_values('时间', ascending=False)
        st.dataframe(df_sig, hide_index=True, use_container_width=True)
    else:
        st.info('暂无信号记录')

    st.markdown('---')

    # ── 持仓实时行情 ──────────────────────────────────────────
    st.subheader('持仓实时行情')
    active = [p for p in positions if p.get('shares', 0) > 0]
    if active:
        live_rows = []
        for p in active:
            sym   = p['symbol']
            snap  = load_realtime(sym)
            lu    = limit_up_pct(sym)
            prev  = snap.get('prev_close', 0) if snap else 0
            upper = prev * (1 + lu) if prev else 0
            lower = prev * (1 - lu) if prev else 0
            price = snap.get('price', 0) if snap else 0
            dist_up   = (upper - price) / price if price and upper else None
            dist_down = (price - lower) / price if price and lower else None
            live_rows.append({
                '代码':    sym,
                '现价':    f'¥{price:.2f}' if price else '—',
                '涨跌%':   f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                '量比':    f'{snap.get("vol_ratio", 0):.2f}x' if snap and snap.get('vol_ratio') else '—',
                '涨停价':  f'¥{upper:.2f}' if upper else '—',
                '距涨停':  f'{dist_up:.1%}' if dist_up is not None else '—',
                '跌停价':  f'¥{lower:.2f}' if lower else '—',
                '距跌停':  f'{dist_down:.1%}' if dist_down is not None else '—',
                '成本价':  f'¥{p.get("entry_price", 0):.3f}',
            })
        st.dataframe(pd.DataFrame(live_rows), hide_index=True, use_container_width=True)
    else:
        st.info('当前无持仓')

    st.markdown('---')

    # ── 候选标的（自选股）──────────────────────────────────────
    st.subheader('候选标的')
    enabled = [w for w in watchlist if w.get('enabled', 0) == 1]
    if enabled:
        rows = []
        for w in enabled[:15]:
            sym  = w.get('symbol', '')
            snap = load_realtime(sym)
            rows.append({
                '代码':    sym,
                '名称':    w.get('name', '—'),
                '理由':    w.get('reason', '')[:30],
                '现价':    f'¥{snap.get("price", 0):.2f}' if snap else '—',
                '今日':    f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                '预警阈值': f'±{w.get("alert_pct", 5):.1f}%',
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.info('自选股为空')


# ============================================================
# Page 5: 回测验证
# ============================================================

elif page == '📉 回测验证':
    st.title('📉 回测验证')

    tab_wfa, tab_validator = st.tabs(['Walk-Forward 分析', '模拟实盘一致性验证'])

    # ── Tab 1: WFA ────────────────────────────────────────────
    with tab_wfa:
        wf = load_wf_results(20)
        if wf:
            st.subheader(f'历史 WFA 结果（{len(wf)} 条）')
            rows = []
            for r in wf:
                try:
                    params = json.loads(r.get('best_params', '{}'))
                except Exception:
                    params = {}
                rows.append({
                    '窗口':     r.get('window', '?'),
                    '标的':     r.get('symbol', ''),
                    '策略':     r.get('strategy', ''),
                    '训练Sharpe': f"{r.get('train_sharpe', 0):.2f}",
                    '测试Sharpe': f"{r.get('test_sharpe', 0):.2f}",
                    '测试收益%': f"{r.get('test_return_pct', 0):+.1f}%",
                    '胜率%':    f"{r.get('test_winrate_pct', 0):.0f}%",
                    '最大回撤%': f"{r.get('test_maxdd_pct', 0):.1f}%",
                    '年化收益%': f"{r.get('annualized_return_pct', 0):+.1f}%",
                    '最优参数': str(params)[:50],
                    '时间':     str(r.get('created_at', ''))[:10],
                })
            df_wf = pd.DataFrame(rows)
            st.dataframe(df_wf, hide_index=True, use_container_width=True)

            # 测试集 Sharpe 图
            fig = px.bar(
                df_wf,
                x='窗口', y='测试Sharpe',
                color='测试Sharpe',
                color_continuous_scale='RdYlGn',
                title='各窗口测试集 Sharpe',
                barmode='group',
                text='测试Sharpe',
            )
            fig.update_layout(margin=dict(t=40, b=20))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info('暂无 WFA 结果。请运行 `python scripts/walkforward_job.py --symbol 510310.SH`')

        # ── 运行 WFA ───────────────────────────────────────────
        st.markdown('---')
        st.subheader('运行 Walk-Forward 训练')

        cfg = load_trading_config()
        live_syms = cfg.get('live_symbols', [])
        sym_options = [s['symbol'] for s in live_syms] if live_syms else ['510310.SH']

        c1, c2, c3 = st.columns(3)
        with c1:
            symbol = st.selectbox('标的', sym_options)
        with c2:
            strategy = st.selectbox('策略', ['RSI', 'MACD', 'Bollinger'])
        with c3:
            train_yrs = st.number_input('训练年数', value=2, min_value=1, max_value=5)

        test_yrs = st.number_input('验证年数', value=1, min_value=1, max_value=3)

        if st.button('开始 Walk-Forward 训练', disabled=not backend_ok):
            import subprocess
            env = {**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'scripts')}
            if sys.platform == 'win32':
                env['PYTHONIOENCODING'] = 'utf-8'
                env['PYTHONUTF8'] = '1'
            cmd = [
                sys.executable,
                os.path.join(BASE_DIR, 'scripts', 'walkforward_job.py'),
                '--symbol', symbol, '--strategy', strategy,
                '--train-years', str(int(train_yrs)),
                '--test-years', str(int(test_yrs)),
            ]
            with st.spinner(f'训练 {symbol} ({strategy}) ...'):
                try:
                    result = subprocess.run(
                        cmd, capture_output=True,
                        encoding='utf-8', errors='replace', timeout=600, env=env,
                    )
                    if result.returncode == 0:
                        st.success('训练完成')
                        st.cache_data.clear()
                    else:
                        st.warning(f'退出码 {result.returncode}')
                    st.code(result.stdout[-4000:] or '(无输出)', language='text')
                    if result.stderr and 'warning' not in result.stderr.lower():
                        with st.expander('错误详情'):
                            st.code(result.stderr[-1000:], language='text')
                except subprocess.TimeoutExpired:
                    st.error('训练超时 (>600s)')
                except Exception as e:
                    st.error(f'运行失败: {e}')

    # ── Tab 2: PaperTradeValidator ────────────────────────────
    with tab_validator:
        st.subheader('模拟实盘一致性验证')
        st.caption('对比回测成交价与模拟撮合价偏差，目标：|偏差| ≤ 20 bps，通过率 ≥ 90%')

        try:
            from core.paper_trade_validator import PaperTradeValidator
            from core.brokers.simulated import SimConfig, SimulatedBroker
            validator_available = True
        except ImportError as e:
            st.error(f'PaperTradeValidator 导入失败: {e}')
            validator_available = False

        if validator_available:
            # 查找已有报告
            reports = sorted(
                [f for f in os.listdir(OUTPUTS_DIR) if f.startswith('paper_trade_validation')],
                reverse=True,
            ) if os.path.exists(OUTPUTS_DIR) else []

            if reports:
                st.success(f'找到 {len(reports)} 份历史验证报告')
                selected = st.selectbox('查看报告', reports)
                try:
                    with open(os.path.join(OUTPUTS_DIR, selected), encoding='utf-8') as f:
                        rpt_data = json.load(f)
                    summary = rpt_data.get('summary', {})
                    c1, c2, c3, c4 = st.columns(4)
                    passed = summary.get('passed', False)
                    c1.metric('验证结论', 'PASS' if passed else 'FAIL',
                              delta='合格' if passed else '不合格')
                    c2.metric('交易总数', summary.get('n_trades', 0))
                    c3.metric('通过率', f"{summary.get('pass_rate', 0):.1%}")
                    c4.metric('平均偏差', f"{summary.get('avg_deviation_bps', 0):.2f} bps")

                    if rpt_data.get('large_deviations'):
                        with st.expander(f'大偏差明细（> 50 bps）— {len(rpt_data["large_deviations"])} 条'):
                            st.dataframe(pd.DataFrame(rpt_data['large_deviations']),
                                         hide_index=True, use_container_width=True)
                    if rpt_data.get('notes'):
                        for note in rpt_data['notes']:
                            st.warning(note)
                except Exception as e:
                    st.error(f'读取报告失败: {e}')
            else:
                st.info('暂无验证报告。可在下方运行快速验证。')

            st.markdown('---')
            st.subheader('快速验证（基于信号数据）')
            st.caption('从最近信号中提取参考价，通过 SimulatedBroker 撮合，计算偏差')

            slippage = st.slider('模拟滑点 (bps)', 0.0, 50.0, 5.0, step=1.0)
            threshold = st.slider('偏差阈值 (bps)', 5.0, 50.0, 20.0, step=5.0)

            if st.button('运行一致性验证'):
                signals = load_signals(30)
                # 从信号中构造验证信号列表
                valid_sigs = [
                    {'symbol': s['symbol'], 'direction': s.get('direction', 'BUY'),
                     'price': float(s.get('price', 0) or 0), 'shares': 100}
                    for s in signals
                    if s.get('price') and float(s.get('price', 0)) > 0
                ]
                if not valid_sigs:
                    # 从 watchlist 和 positions 创建测试信号
                    positions = load_positions()
                    for p in positions[:5]:
                        snap = load_realtime(p['symbol'])
                        if snap and snap.get('price'):
                            valid_sigs.append({
                                'symbol': p['symbol'], 'direction': 'BUY',
                                'price': snap['price'], 'shares': 100,
                            })

                if valid_sigs:
                    try:
                        broker = SimulatedBroker(SimConfig(
                            initial_cash=10_000_000,
                            price_source='manual',
                            slippage_bps=slippage,
                            commission_rate=0.0003,
                            stamp_tax_rate=0.001,
                            enforce_lot=True,
                        ))
                        broker.connect()
                        validator = PaperTradeValidator(
                            threshold_bps=threshold, large_dev_bps=50.0,
                        )
                        report = validator.validate_from_signals(valid_sigs, broker)

                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric('结论', 'PASS' if report.passed else 'FAIL')
                        c2.metric('验证笔数', report.n_trades)
                        c3.metric('通过率', f'{report.pass_rate:.1%}')
                        c4.metric('平均偏差', f'{report.avg_deviation_bps:.2f} bps')

                        if report.comparisons:
                            comp_data = [
                                {'标的': c.symbol, '方向': c.direction,
                                 '回测价': c.bt_price, '实盘价': c.live_price,
                                 '偏差bps': c.deviation_bps, '合格': c.within_threshold,
                                 '分类': c.cause}
                                for c in report.comparisons
                            ]
                            st.dataframe(pd.DataFrame(comp_data), hide_index=True,
                                         use_container_width=True)

                        for note in report.notes:
                            st.warning(note)

                        if st.button('保存验证报告'):
                            path = report.save()
                            st.success(f'已保存至: {path}')
                    except Exception as e:
                        st.error(f'验证运行失败: {e}')
                else:
                    st.warning('无可用信号数据，无法运行验证')


# ============================================================
# Page 6: 数据质量
# ============================================================

elif page == '🔍 数据质量':
    st.title('🔍 数据质量')

    tab_l2, tab_fetcher = st.tabs(['Level2 盘口完整率', '数据源健康状态'])

    # ── Tab 1: Level2 quality ─────────────────────────────────
    with tab_l2:
        st.subheader('Level2 盘口数据完整率')
        st.caption('目标：连续采集 5 个交易日，23 个字段完整率 > 95%')

        try:
            from core.level2_quality import Level2QualityCollector, Level2QualityReporter
            l2_available = True
        except ImportError as e:
            st.error(f'level2_quality 导入失败: {e}')
            l2_available = False

        if l2_available:
            default_db = os.path.join(DATA_DIR, 'level2_snapshots.db')
            db_path = st.text_input('数据库路径', value=default_db)
            days = st.slider('分析天数', 1, 30, 5)
            threshold_pct = st.slider('完整率阈值 (%)', 50, 100, 95, step=1, format='%d%%')
            threshold = threshold_pct / 100

            col_btn, col_status = st.columns([1, 3])
            with col_btn:
                run_report = st.button('生成质量报告')
            with col_status:
                if os.path.exists(db_path):
                    try:
                        collector = Level2QualityCollector([], db_path=db_path)
                        counts    = collector.n_snapshots
                        total_snaps = sum(counts.values())
                        st.info(f'数据库存在 · {len(counts)} 个标的 · 共 {total_snaps} 条快照')
                    except Exception as e:
                        st.warning(f'数据库状态读取失败: {e}')
                else:
                    st.warning('数据库不存在，请先运行采集器收集数据')

            if run_report:
                try:
                    reporter = Level2QualityReporter(db_path)
                    report   = reporter.generate(days=days, threshold=threshold)

                    # 总体指标
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric('总体结论', 'PASS' if report.passed else 'FAIL')
                    c2.metric('总体完整率', f'{report.overall_completeness:.1%}')
                    c3.metric('分析天数', report.days_analyzed)
                    c4.metric('覆盖标的', len(report.symbols))

                    if report.notes:
                        for note in report.notes:
                            st.warning(note)

                    # 各标的明细
                    if report.symbols:
                        rows = []
                        for sq in report.symbols:
                            rows.append({
                                '标的':    sq.symbol,
                                '快照数':  sq.n_snapshots,
                                '交易日数': sq.trading_days,
                                '平均间隔': f'{sq.avg_interval_sec:.0f}s',
                                '完整率':  f'{sq.overall_completeness:.1%}',
                                '状态':    '✓ 合格' if sq.passed else '✗ 不合格',
                            })
                        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

                        # 字段明细（第一个标的）
                        if report.symbols[0].field_stats:
                            with st.expander(f'{report.symbols[0].symbol} 字段明细'):
                                frows = [
                                    {'字段': fq.field_name, '总数': fq.total,
                                     '有效': fq.valid, '完整率': f'{fq.completeness:.1%}',
                                     '合格': '✓' if fq.passed else '✗'}
                                    for fq in report.symbols[0].field_stats
                                ]
                                st.dataframe(pd.DataFrame(frows), hide_index=True,
                                             use_container_width=True)

                    if st.button('保存报告到 outputs/'):
                        path = report.save()
                        st.success(f'已保存: {path}')

                except Exception as e:
                    st.error(f'生成报告失败: {e}')

            st.markdown('---')
            st.subheader('采集器使用说明')
            st.code("""\
from core.level2_quality import Level2QualityCollector

# 启动后台采集（每 30 秒一次）
collector = Level2QualityCollector(
    symbols=['600519.SH', '000858.SZ', '510310.SH'],
    db_path='data/level2_snapshots.db',
)
collector.start(interval=30)

# 手动触发一次
# n = collector.collect_once()

# 停止
# collector.stop()
""", language='python')

    # ── Tab 2: 数据源健康 ─────────────────────────────────────
    with tab_fetcher:
        st.subheader('数据源健康状态')
        status_data = api_get('/data/status', timeout=5)
        if status_data.get('status') == 'ok':
            fetcher_status = status_data.get('status', {})
            fetchers = status_data.get('fetchers', [])
            if fetchers:
                rows = []
                for fname in fetchers:
                    fs = fetcher_status.get(fname, {}) if isinstance(fetcher_status, dict) else {}
                    rows.append({
                        '数据源':  fname,
                        '状态':    fs.get('state', '—'),
                        '失败次数': fs.get('failures', 0),
                        '可用':    '✓' if fs.get('available', True) else '✗',
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info('暂无数据源状态信息')
        else:
            st.warning('Backend 未连接，无法获取数据源状态')

        st.markdown('---')
        st.subheader('实时数据测试')
        test_sym = st.text_input('测试标的', value='510310.SH')
        if st.button('获取实时行情'):
            snap = load_realtime(test_sym)
            if snap:
                c1, c2, c3 = st.columns(3)
                c1.metric('现价', f'¥{snap.get("price", 0):.3f}')
                c2.metric('涨跌幅', f'{snap.get("pct", 0):+.2f}%')
                c3.metric('量比', f'{snap.get("vol_ratio", 0):.2f}x' if snap.get('vol_ratio') else '—')
                st.json(snap)
            else:
                st.error(f'获取 {test_sym} 行情失败（网络或格式问题）')

        st.markdown('---')
        st.subheader('历史数据测试')
        hist_sym = st.text_input('历史数据标的', value='510310.SH', key='hist_sym')
        hist_days = st.number_input('天数', value=30, min_value=5, max_value=500)
        if st.button('获取历史数据'):
            data = api_get(f'/data/daily/{hist_sym}?days={hist_days}', timeout=15)
            if data.get('data'):
                df_hist = pd.DataFrame(data['data'])
                st.success(f'获取 {len(df_hist)} 条数据')
                if 'date' in df_hist.columns and 'close' in df_hist.columns:
                    fig = px.line(df_hist, x='date', y='close',
                                  title=f'{hist_sym} 收盘价')
                    st.plotly_chart(fig, use_container_width=True)
                st.dataframe(df_hist.tail(10), hide_index=True, use_container_width=True)
            else:
                st.error(f'获取历史数据失败: {data.get("error", "未知错误")}')


# ============================================================
# Page 7: 策略健康
# ============================================================

elif page == '🏥 策略健康':
    st.title('🏥 策略健康')
    st.caption('Rolling Sharpe · 连续亏损 · TCA · CVaR / Monte Carlo')

    daily_stats = load_daily_stats(250)

    if not daily_stats:
        st.info('暂无日度统计数据（backend 未连接或 portfolio.db 无记录）')
        st.stop()

    # ── StrategyHealthMonitor ─────────────────────────────────
    try:
        from core.strategy_health import StrategyHealthMonitor
        monitor = StrategyHealthMonitor()
        health_report = monitor.check(daily_stats)
        health_series = monitor.check_series(daily_stats)
    except Exception as e:
        st.warning(f'StrategyHealthMonitor 加载失败: {e}')
        health_report = None
        health_series = pd.DataFrame()

    if health_report:
        level = health_report.worst_level()
        level_icon = {'OK': '🟢', 'WARN': '🟡', 'CRITICAL': '🔴'}.get(level, '⚪')

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric('系统状态', f'{level_icon} {level}')
        c2.metric('Sharpe(20d)', f'{health_report.rolling_sharpe_20d:.3f}',
                  delta=f'{health_report.sharpe_change_pct:+.1f}%')
        c3.metric('Sharpe(60d)', f'{health_report.rolling_sharpe_60d:.3f}')
        c4.metric('今日收益', f'{health_report.latest_daily_return*100:+.2f}%')
        c5.metric('连续亏损', f'{health_report.consecutive_loss_days} 天')

        if health_report.alerts:
            st.markdown('---')
            for alert in health_report.alerts:
                fn_map = {'CRITICAL': st.error, 'WARN': st.warning, 'OK': st.success}
                fn = fn_map.get(alert.level, st.info)
                pause = ' **【建议暂停自动交易】**' if alert.should_pause else ''
                fn(f'**[{alert.level}] {alert.check_name}**: {alert.message}{pause}')
        else:
            st.success('策略运行正常，无健康告警')

    st.markdown('---')

    # ── Rolling Sharpe 图 ─────────────────────────────────────
    st.subheader('Rolling Sharpe 时序')
    if not health_series.empty and 'sharpe_20d' in health_series.columns:
        chart_df = (
            health_series[['date', 'sharpe_20d', 'sharpe_60d']]
            .dropna(subset=['sharpe_20d'])
            .rename(columns={'sharpe_20d': 'Sharpe(20d)', 'sharpe_60d': 'Sharpe(60d)'})
            .set_index('date')
        )
        st.line_chart(chart_df, use_container_width=True)
    else:
        st.info('数据不足（需 ≥ 20 条日度记录）')

    if not health_series.empty and 'win_rate' in health_series.columns:
        wr_df = (
            health_series[['date', 'win_rate']].dropna()
            .assign(win_rate=lambda x: x['win_rate'] * 100)
            .set_index('date')
        )
        st.subheader('近 20 日胜率 (%)')
        st.line_chart(wr_df, use_container_width=True)

    st.markdown('---')

    # ── TCA ───────────────────────────────────────────────────
    st.subheader('交易成本分析（TCA）')
    try:
        from core.tca import TCAAnalyzer
        trades_raw = load_trades(500)
        if trades_raw:
            tca = TCAAnalyzer.from_trade_dicts(trades_raw)
            rpt = tca.analyze()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric('样本笔数',  rpt.n_trades)
            c2.metric('平均 IS',   f'{rpt.avg_is_bps:.2f} bps')
            c3.metric('平均总成本', f'{rpt.avg_total_cost_bps:.2f} bps')
            c4.metric('建议滑点参数', f'{rpt.recommended_slippage_bps:.0f} bps')

            if rpt.by_direction:
                dir_rows = [
                    {'方向': d, '笔数': s['n_trades'],
                     'avg IS (bps)': f"{s['avg_is_bps']:.2f}",
                     'P95 IS (bps)': f"{s['p95_is_bps']:.2f}",
                     '总佣金': f"¥{s['total_commission']:,.0f}",
                     '总印花税': f"¥{s['total_stamp_tax']:,.0f}"}
                    for d, s in rpt.by_direction.items()
                ]
                st.dataframe(pd.DataFrame(dir_rows), hide_index=True, use_container_width=True)

            if rpt.monthly and len(rpt.monthly) > 1:
                monthly_df = pd.DataFrame([
                    {'月份': k, 'avg IS (bps)': v['avg_is_bps']}
                    for k, v in sorted(rpt.monthly.items())
                ]).set_index('月份')
                st.line_chart(monthly_df, use_container_width=True)
        else:
            st.info('暂无成交数据，TCA 需要至少 1 笔成交记录')
    except Exception as e:
        st.warning(f'TCA 加载失败: {e}')

    st.markdown('---')

    # ── CVaR / Monte Carlo ────────────────────────────────────
    st.subheader('风险分析（CVaR · Monte Carlo）')
    try:
        from core.portfolio_risk import MonteCarloStressTest
        portfolio = load_portfolio_summary()
        equity_val = float(portfolio.get('total_equity', 100_000) or 100_000)

        ret_series = pd.Series([
            float(s.get('daily_return', 0) if isinstance(s, dict)
                  else getattr(s, 'daily_return', 0))
            for s in daily_stats
        ]).dropna()

        if len(ret_series) >= 30:
            n_sim = st.slider('Monte Carlo 模拟次数', 500, 5000, 2000, step=500)
            mc = MonteCarloStressTest(n_simulations=n_sim, horizon_days=63, seed=42)
            mc_result = mc.run(ret_series, initial_equity=equity_val)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric('P5 净值（63日）', f'¥{mc_result.p5_final:,.0f}',
                      delta=f'{(mc_result.p5_final/equity_val-1)*100:+.1f}%')
            c2.metric('P50 净值', f'¥{mc_result.p50_final:,.0f}',
                      delta=f'{(mc_result.p50_final/equity_val-1)*100:+.1f}%')
            c3.metric('亏损概率', f'{mc_result.prob_loss*100:.1f}%')
            c4.metric('ES(95%)', f'{mc_result.expected_shortfall*100:.2f}%')

            with st.expander('完整 Monte Carlo 报告'):
                st.text(mc_result.summary())
        else:
            st.info(f'日度数据不足 ({len(ret_series)} 条，需 ≥ 30 条)')
    except Exception as e:
        st.warning(f'风险分析加载失败: {e}')

    st.markdown('---')

    # ── 近期日度明细 ─────────────────────────────────────────
    st.subheader('近期日度绩效明细（最近 30 天）')
    tail = daily_stats[-30:][::-1]
    detail_rows = []
    for s in tail:
        d = s if isinstance(s, dict) else {
            'date': getattr(s, 'date', ''),
            'daily_return': getattr(s, 'daily_return', 0),
            'n_trades': getattr(s, 'n_trades', 0),
            'equity': getattr(s, 'equity', 0),
        }
        ret = float(d.get('daily_return', 0))
        detail_rows.append({
            '日期':    str(d.get('date', ''))[:10],
            '日收益':  f'{ret*100:+.2f}%',
            '交易次数': int(d.get('n_trades', 0)),
            '净值':    f'¥{float(d.get("equity", 0)):,.0f}',
            '状态':    '🔴 亏损' if ret < 0 else '🟢 盈利',
        })
    if detail_rows:
        st.dataframe(pd.DataFrame(detail_rows), hide_index=True, use_container_width=True)
    else:
        st.info('暂无日度数据')

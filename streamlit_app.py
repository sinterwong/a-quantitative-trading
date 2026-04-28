#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamlit_app.py — 量化系统 Web UI
====================================
系统定位：SimulatedBroker 模拟实盘 · A 股市场 · ~95 分专业化量化平台

启动方式：
  streamlit run streamlit_app.py --server.port 8501

页面结构：
  1. 📊 仪表盘      — 账户摘要、市场 Regime、近期告警、Top 信号
  2. 🎯 因子工作台  — 22 因子评分、动态权重、NLP 情感、IC 分析
  3. 🤖 ML 模型     — 模型注册表、Walk-Forward 训练、特征重要性
  4. ⚖️ 组合优化    — MVO/BL/风险平价 + PortfolioAllocator
  5. 📈 信号 & 执行 — 实时信号、VWAP/TWAP 算法下单、成交记录
  6. 📉 回测验证    — Walk-Forward、敏感性热力图、一致性验证
  7. 🏥 监控 & 告警 — 策略健康、数据质量、AlertManager
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
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
DATA_DIR    = os.path.join(BASE_DIR, 'data')
OUTPUTS_DIR = os.path.join(BASE_DIR, 'outputs')

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, BACKEND_DIR)

BACKEND_URL = os.environ.get('BACKEND_URL', 'http://127.0.0.1:5555')

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ============================================================
# Backend HTTP helpers
# ============================================================

def api_get(endpoint: str, timeout: float = 8.0) -> dict:
    url = f"{BACKEND_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'QuantUI/3.0'})
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
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantUI/3.0'},
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
def load_daily_equity(limit: int = 90) -> list:
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
def load_wf_results(limit: int = 30) -> list:
    for db_path in [
        os.path.join(BASE_DIR, 'backend', 'wf_results.db'),
        os.path.join(BACKEND_DIR, 'services', 'wf_results.db'),
    ]:
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM wf_results ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                conn.close()
                return [dict(r) for r in rows]
            except Exception:
                pass
    return []


@st.cache_data(ttl=30)
def load_realtime(symbol: str) -> dict:
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
            'price':      float(f[3])  if f[3]  not in ('', '-') else 0.0,
            'prev_close': float(f[4])  if f[4]  not in ('', '-') else 0.0,
            'pct':        float(f[32]) if f[32] not in ('', '-') else 0.0,
            'vol_ratio':  float(f[38]) if len(f) > 38 and f[38] not in ('', '-', '0') else None,
            'high':       float(f[33]) if len(f) > 33 and f[33] not in ('', '-') else 0.0,
            'low':        float(f[34]) if len(f) > 34 and f[34] not in ('', '-') else 0.0,
        }
    except Exception:
        return {}


@st.cache_data(ttl=60)
def load_watchlist() -> list:
    return api_get('/watchlist').get('watchlist', [])


@st.cache_data(ttl=300)
def load_trading_config() -> dict:
    cfg_path = os.path.join(BASE_DIR, 'config', 'trading.yaml')
    if not os.path.exists(cfg_path):
        return {}
    try:
        import yaml
        with open(cfg_path, encoding='utf-8') as f:
            docs = list(yaml.safe_load_all(f))
        return docs[0] if docs else {}
    except Exception:
        return {}


def limit_up_pct(symbol: str) -> float:
    s = symbol.lower().replace('.sz', '').replace('.sh', '')
    if any(s.startswith(p) for p in ('st', '*st')):
        return 0.05
    if s.startswith('300') or s.startswith('688'):
        return 0.20
    return 0.10


def _make_price_df_from_akshare(symbol: str, days: int = 300) -> Optional[pd.DataFrame]:
    """拉取日线数据用于因子计算。优先使用 DataLayer，AKShare 作为备选。"""
    # ── 主路径：DataLayer（本地缓存 + tushare/baostock） ──
    try:
        from core.data_layer import DataLayer
        dl = DataLayer()
        df = dl.get_bars(symbol, days=days)
        if df is not None and not df.empty:
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df = df.sort_index()
            cols = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
            return df[cols].tail(days)
    except Exception:
        pass

    # ── 备选路径：AKShare ────────────────────────────────
    try:
        import akshare as ak
        code = symbol.split('.')[0]
        df = ak.stock_zh_a_hist(
            symbol=code, period='daily',
            start_date=(datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d'),
            end_date=datetime.now().strftime('%Y%m%d'),
            adjust='qfq',
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            '日期': 'date', '开盘': 'open', '最高': 'high',
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
        })
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        return df[['open', 'high', 'low', 'close', 'volume']].tail(days)
    except Exception:
        return None


# ============================================================
# Page config & global CSS
# ============================================================

st.set_page_config(
    page_title='量化系统',
    page_icon='📊',
    layout='wide',
    menu_items={'About': '## 量化系统 v3 · SimulatedBroker · A 股 · ~95分'},
)

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


# ============================================================
# Sidebar
# ============================================================

st.sidebar.title('量化系统')
st.sidebar.caption(datetime.now().strftime('%Y-%m-%d  %H:%M'))

backend_ok = api_get('/health', timeout=3).get('status') == 'ok'
if backend_ok:
    st.sidebar.success('Backend 运行中')
else:
    st.sidebar.warning('Backend 未连接（部分功能受限）')

st.sidebar.markdown(
    '<span class="broker-badge">SimulatedBroker</span>',
    unsafe_allow_html=True,
)
st.sidebar.caption('A 股规则 · 整手 · 印花税 0.1% · 涨跌停保护')
st.sidebar.markdown('---')

page = st.sidebar.radio(
    '导航',
    [
        '📊 仪表盘',
        '🎯 因子工作台',
        '🤖 ML 模型',
        '⚖️ 组合优化',
        '📈 信号 & 执行',
        '📉 回测验证',
        '🏥 监控 & 告警',
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

    # ── 账户摘要 ──────────────────────────────────────────────
    portfolio = load_portfolio_summary()
    cash      = float(portfolio.get('cash', 0) or 0)
    equity    = float(portfolio.get('total_equity', cash) or cash)
    pos_val   = float(portfolio.get('position_value', 0) or 0)
    unreal    = float(portfolio.get('unrealized_pnl', 0) or 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('总权益', f'¥{equity:,.0f}', delta=f'{unreal:+,.0f}' if unreal else None)
    c2.metric('持仓市值', f'¥{pos_val:,.0f}')
    c3.metric('可用现金', f'¥{cash:,.0f}')
    c4.metric('持仓比例', f'{pos_val/equity*100:.1f}%' if equity > 0 else '—')

    st.markdown('---')

    col_left, col_right = st.columns([2, 1])

    # ── 净值曲线 ──────────────────────────────────────────────
    with col_left:
        st.subheader('净值曲线')
        daily = load_daily_equity(90)
        if daily:
            df_eq = pd.DataFrame(daily)
            date_col  = next((c for c in df_eq.columns if 'date' in c.lower()), None)
            eq_col    = next((c for c in df_eq.columns if 'equity' in c.lower()), None)
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

    # ── 市场 Regime + 快速指标 ────────────────────────────────
    with col_right:
        st.subheader('市场状态')
        try:
            from core.regime import get_regime
            regime_info = get_regime()
            regime = regime_info.regime if hasattr(regime_info, 'regime') else str(regime_info)
        except Exception:
            regime = 'UNKNOWN'

        regime_cls = {
            'BULL': 'regime-bull', 'BEAR': 'regime-bear',
            'VOLATILE': 'regime-vol', 'CALM': 'regime-calm',
        }.get(regime, 'regime-calm')
        regime_zh = {'BULL': '牛市', 'BEAR': '熊市', 'VOLATILE': '震荡', 'CALM': '平静', 'UNKNOWN': '未知'}
        st.markdown(
            f'<span class="{regime_cls}">{regime_zh.get(regime, regime)}</span>',
            unsafe_allow_html=True,
        )
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

    # ── Top 信号 + 近期告警 ────────────────────────────────────
    col_sig, col_alert = st.columns(2)

    with col_sig:
        st.subheader('最新交易信号（近 10 条）')
        if signals:
            rows = []
            for s in signals[:10]:
                d = s.get('direction', '')
                rows.append({
                    '时间':   str(s.get('timestamp', s.get('created_at', '')))[:16],
                    '标的':   s.get('symbol', ''),
                    '方向':   f"🟢 {d}" if d == 'BUY' else f"🔴 {d}" if d == 'SELL' else d,
                    '强度':   f"{float(s.get('strength', 0)):.2f}",
                    '因子':   s.get('factor', s.get('signal_type', '')),
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
            alerts = load_daily_stats(1)
            st.info('AlertManager 未初始化（将在策略运行后生效）')


# ============================================================
# Page 2: 因子工作台
# ============================================================

elif page == '🎯 因子工作台':
    st.title('🎯 因子工作台')
    st.caption('22 个因子实时评分 · 动态 IC 权重 · NLP 情感 · 因子相关性')

    cfg = load_trading_config()
    live_syms = cfg.get('live_symbols', [])
    default_syms = [s['symbol'] for s in live_syms] if live_syms else ['000001.SZ', '600519.SH']

    col_ctrl, col_main = st.columns([1, 3])

    with col_ctrl:
        symbol = st.selectbox('选择标的', default_syms + ['自定义'])
        if symbol == '自定义':
            symbol = st.text_input('输入代码（如 600519.SH）', '600519.SH')

        days = st.slider('历史数据天数', 60, 300, 120, step=20)
        run_btn = st.button('运行因子分析', type='primary', use_container_width=True)

        st.markdown('---')
        st.markdown('**注册因子列表（22个）**')
        try:
            from core.factor_registry import registry
            for name in registry.list_factors():
                st.caption(f'• {name}')
        except Exception as e:
            st.warning(f'注册表加载失败: {e}')

    with col_main:
        if run_btn:
            with st.spinner(f'拉取 {symbol} 数据并计算因子...'):
                df = _make_price_df_from_akshare(symbol, days)

            if df is None or df.empty:
                st.error('无法获取历史数据，请检查网络或标的代码。')
            else:
                # ── 单因子评分 ─────────────────────────────────
                st.subheader(f'{symbol} · 因子评分（最新一日）')
                try:
                    from core.factor_registry import registry
                    from core.factors.base import FactorCategory

                    snap = load_realtime(symbol)
                    price = snap.get('price', float(df['close'].iloc[-1]))

                    factor_rows = []
                    for fname in registry.list_factors():
                        try:
                            fobj = registry.create(fname)
                            vals = fobj.evaluate(df)
                            score = float(vals.iloc[-1]) if len(vals) > 0 else 0.0
                            if not np.isfinite(score):
                                score = 0.0
                            factor_rows.append({'因子': fname, '得分': score})
                        except Exception:
                            factor_rows.append({'因子': fname, '得分': 0.0})

                    df_scores = pd.DataFrame(factor_rows).sort_values('得分', ascending=False)

                    fig = px.bar(
                        df_scores, x='得分', y='因子', orientation='h',
                        color='得分', color_continuous_scale='RdYlGn',
                        color_continuous_midpoint=0,
                        title='因子 Z-Score（正=偏多，负=偏空）',
                    )
                    fig.update_layout(height=550, margin=dict(t=40, b=10),
                                      yaxis={'categoryorder': 'total ascending'})
                    st.plotly_chart(fig, use_container_width=True)

                except Exception as e:
                    st.error(f'因子计算失败: {e}')

                # ── 综合流水线评分 ─────────────────────────────
                st.markdown('---')
                st.subheader('多因子流水线综合评分')
                try:
                    from core.factor_pipeline import DynamicWeightPipeline
                    pipeline = DynamicWeightPipeline(update_freq_days=21)
                    pipeline.add('RSI',            weight=0.15)
                    pipeline.add('MACD',           weight=0.12)
                    pipeline.add('ATR',            weight=0.08)
                    pipeline.add('BollingerBands', weight=0.10)
                    pipeline.add('OrderImbalance', weight=0.08)
                    pipeline.add('SectorMomentum', weight=0.10)
                    pipeline.add('PEPercentile',   weight=0.08)
                    pipeline.add('ROEMomentum',    weight=0.08)
                    pipeline.add('MarginTrading',  weight=0.07)
                    pipeline.add('NorthboundFlow', weight=0.07)
                    pipeline.add('MLPrediction',   weight=0.07)

                    result = pipeline.run(symbol=symbol, data=df, price=price)

                    c1, c2, c3 = st.columns(3)
                    score_color = 'normal' if abs(result.combined_score) < 0.5 else (
                        'inverse' if result.combined_score < 0 else 'off'
                    )
                    c1.metric('综合评分', f'{result.combined_score:+.3f}')
                    c2.metric('主信号', result.dominant_signal or 'HOLD')
                    c3.metric('信号数量', len(result.signals))

                    if result.signals:
                        sig_rows = [{'因子': s.factor_name, '方向': s.direction,
                                     '强度': f'{s.strength:.3f}', '价格': f'{s.price:.2f}'}
                                    for s in result.signals]
                        st.dataframe(pd.DataFrame(sig_rows), hide_index=True,
                                     use_container_width=True)

                    # 当前动态权重
                    try:
                        w_dict = pipeline.current_weights()
                        if w_dict:
                            st.caption('当前动态权重（基于滚动 IC）')
                            w_df = pd.DataFrame(
                                list(w_dict.items()), columns=['因子', '权重']
                            )
                            st.dataframe(w_df, hide_index=True, use_container_width=True)
                    except Exception:
                        pass

                except Exception as e:
                    st.warning(f'流水线评分失败: {e}')

                # ── NLP 情感 ───────────────────────────────────
                st.markdown('---')
                st.subheader('新闻情感（NewsSentimentFactor）')
                try:
                    from core.factors.nlp import NewsSentimentFactor, _fetch_news_eastmoney
                    headlines = _fetch_news_eastmoney(symbol, n=5)
                    if headlines:
                        st.caption('最新新闻标题：')
                        for h in headlines:
                            st.markdown(f'- {h}')
                        f_nlp = NewsSentimentFactor(symbol=symbol, use_api=False)
                        nlp_vals = f_nlp.evaluate(df)
                        latest_nlp = float(nlp_vals.iloc[-1])
                        sentiment_label = (
                            '🟢 正面' if latest_nlp > 0.2 else
                            '🔴 负面' if latest_nlp < -0.2 else '⚪ 中性'
                        )
                        st.metric('情感得分（Z-score）', f'{latest_nlp:+.3f}', delta=sentiment_label)
                    else:
                        st.info('未获取到新闻数据（网络限制或标的不支持）')
                except Exception as e:
                    st.info(f'NLP 因子未激活（需配置 ANTHROPIC_API_KEY）: {e}')

        else:
            st.info('👈 选择标的后点击「运行因子分析」')
            st.markdown("""
**因子分类说明：**

| 类别 | 因子 | 数量 |
|------|------|------|
| 价格动量 | RSI / Bollinger / MACD / ATR / OrderImbalance | 5 |
| 技术微观 | IntraVWAP / OpenGap / VolAcceleration / BidAskSpread / BuyingPressure / SectorMomentum / IndexRelativeStrength | 7 |
| 基本面 | PEPercentile / ROEMomentum / EarningsSurprise / RevenueGrowth / CashFlowQuality | 5 |
| 情绪 | MarginTrading / NorthboundFlow / ShortInterest | 3 |
| ML 预测 | MLPrediction（XGBoost Walk-Forward） | 1 |
| NLP 情感 | NewsSentiment（东财新闻 + Claude API） | 1 |
""")


# ============================================================
# Page 3: ML 模型
# ============================================================

elif page == '🤖 ML 模型':
    st.title('🤖 ML 模型')
    st.caption('XGBoost Walk-Forward 训练 · 模型注册表 · 特征重要性')

    try:
        from core.ml.model_registry import ModelRegistry
        from core.ml.price_predictor import MLPredictionFactor, WalkForwardTrainer
        from core.ml.feature_store import FeatureStore
        ml_available = True
    except ImportError as e:
        st.error(f'ML 模块加载失败: {e}')
        ml_available = False

    if ml_available:
        reg = ModelRegistry()

        tab_registry, tab_train, tab_importance = st.tabs(
            ['📦 模型注册表', '🚀 训练新模型', '📊 特征重要性']
        )

        # ── Tab 1: 注册表 ─────────────────────────────────────
        with tab_registry:
            st.subheader('已训练模型')
            models_dir = os.path.join(DATA_DIR, 'ml_models')
            if os.path.exists(models_dir):
                model_rows = []
                for sym_dir in sorted(os.listdir(models_dir)):
                    sym_path = os.path.join(models_dir, sym_dir)
                    if not os.path.isdir(sym_path):
                        continue
                    for model_type in sorted(os.listdir(sym_path)):
                        type_path = os.path.join(sym_path, model_type)
                        if not os.path.isdir(type_path):
                            continue
                        meta_path = os.path.join(type_path, 'meta.json')
                        if os.path.exists(meta_path):
                            try:
                                with open(meta_path) as mf:
                                    meta = json.load(mf)
                                model_rows.append({
                                    '标的':        sym_dir,
                                    '模型类型':    model_type,
                                    '版本':        meta.get('version', '—'),
                                    '训练样本数':  meta.get('n_samples', '—'),
                                    '特征数':      meta.get('n_features', '—'),
                                    'OOS AUC':     f"{meta.get('oos_auc', 0):.3f}" if meta.get('oos_auc') else '—',
                                    '训练时间':    str(meta.get('trained_at', ''))[:16],
                                })
                            except Exception:
                                pass
                if model_rows:
                    st.dataframe(pd.DataFrame(model_rows), hide_index=True, use_container_width=True)
                else:
                    st.info('暂无已训练模型。请在「训练新模型」标签页训练。')
            else:
                st.info('模型存储目录不存在，训练后将自动创建。')

        # ── Tab 2: 训练 ───────────────────────────────────────
        with tab_train:
            st.subheader('Walk-Forward 训练配置')
            st.caption('训练窗口 252 天 / 验证窗口 63 天 / 步长 21 天（防止过拟合）')

            cfg = load_trading_config()
            live_syms = cfg.get('live_symbols', [])
            sym_options = [s['symbol'] for s in live_syms] if live_syms else ['000001.SZ', '600519.SH']

            c1, c2 = st.columns(2)
            with c1:
                train_symbol  = st.selectbox('训练标的', sym_options)
                forward_days  = st.selectbox('预测周期（天）', [1, 2, 5], index=1)
            with c2:
                data_days     = st.slider('历史数据长度（天）', 300, 800, 500, step=50)
                use_wf        = st.checkbox('使用 Walk-Forward 验证', value=True)

            if st.button('开始训练', type='primary'):
                with st.spinner(f'拉取 {train_symbol} 数据...'):
                    df_train = _make_price_df_from_akshare(train_symbol, data_days)

                if df_train is None or len(df_train) < 100:
                    st.error('历史数据不足（< 100 天），无法训练。')
                else:
                    with st.spinner('Walk-Forward 训练中（可能需要 1-3 分钟）...'):
                        try:
                            factor = MLPredictionFactor(
                                symbol=train_symbol, forward_days=forward_days
                            )
                            wf_result = factor.fit(df_train, use_walk_forward=use_wf)

                            st.success('训练完成！')
                            col_r1, col_r2, col_r3 = st.columns(3)
                            if hasattr(wf_result, 'oos_accuracy') and wf_result.oos_accuracy is not None:
                                col_r1.metric('OOS 准确率', f'{wf_result.oos_accuracy:.3f}')
                            if hasattr(wf_result, 'oos_auc') and wf_result.oos_auc is not None:
                                col_r2.metric('OOS AUC', f'{wf_result.oos_auc:.3f}')
                            if hasattr(wf_result, 'n_folds'):
                                col_r3.metric('验证折数', wf_result.n_folds)

                            # 各折 AUC 图
                            if hasattr(wf_result, 'fold_metrics') and wf_result.fold_metrics:
                                aucs = [w.get('auc', 0) for w in wf_result.fold_metrics]
                                fig = px.bar(
                                    x=list(range(1, len(aucs) + 1)),
                                    y=aucs,
                                    color=aucs,
                                    color_continuous_scale='RdYlGn',
                                    labels={'x': '验证折', 'y': 'OOS AUC'},
                                    title='各折 OOS AUC',
                                )
                                fig.add_hline(y=0.5, line_dash='dash', line_color='gray')
                                st.plotly_chart(fig, use_container_width=True)

                        except Exception as e:
                            st.error(f'训练失败: {e}')

        # ── Tab 3: 特征重要性 ─────────────────────────────────
        with tab_importance:
            st.subheader('特征重要性分析')

            cfg2 = load_trading_config()
            live_syms2 = cfg2.get('live_symbols', [])
            sym_opts2 = [s['symbol'] for s in live_syms2] if live_syms2 else ['000001.SZ']
            imp_symbol = st.selectbox('选择标的', sym_opts2, key='imp_sym')

            if st.button('加载特征重要性'):
                try:
                    factor_imp = MLPredictionFactor(symbol=imp_symbol)
                    if factor_imp.load():
                        # feature_importance() is on the underlying predictor
                        predictor = getattr(factor_imp, '_predictor', None)
                        importance = predictor.feature_importance() if predictor is not None else pd.Series(dtype=float)
                        if importance is not None and not importance.empty:
                            df_imp = pd.DataFrame({
                                '特征': importance.index,
                                '重要性': importance.values,
                            }).head(20)
                            fig = px.bar(
                                df_imp, x='重要性', y='特征', orientation='h',
                                title=f'{imp_symbol} Top-20 特征重要性',
                                color='重要性', color_continuous_scale='Blues',
                            )
                            fig.update_layout(height=500, yaxis={'categoryorder': 'total ascending'})
                            st.plotly_chart(fig, use_container_width=True)
                        else:
                            st.warning('模型未返回特征重要性（可能是非树模型）')
                    else:
                        st.warning(f'未找到 {imp_symbol} 的已训练模型，请先训练。')
                except Exception as e:
                    st.error(f'加载失败: {e}')


# ============================================================
# Page 4: 组合优化
# ============================================================

elif page == '⚖️ 组合优化':
    st.title('⚖️ 组合优化')
    st.caption('MVO · Black-Litterman · 风险平价 · 最大分散化 · 多策略资金分配')

    tab_opt, tab_bl, tab_alloc = st.tabs(
        ['📐 均值方差优化', '🔭 Black-Litterman', '🎯 策略资金分配']
    )

    # ── Tab 1: MVO ───────────────────────────────────────────
    with tab_opt:
        st.subheader('均值方差优化（PortfolioOptimizer）')

        symbols_input = st.text_area(
            '标的列表（每行一个）',
            '000001.SZ\n600519.SH\n300750.SZ\n600036.SH',
            height=120,
        )
        symbols_list = [s.strip() for s in symbols_input.strip().split('\n') if s.strip()]

        c1, c2, c3 = st.columns(3)
        with c1:
            opt_method  = st.selectbox('优化方法', [
                'min_variance', 'max_sharpe', 'risk_parity',
                'max_diversification', 'equal_weight',
            ])
        with c2:
            cov_method  = st.selectbox('协方差估计', ['ledoit_wolf', 'sample'])
            max_weight  = st.slider('单标的上限', 0.10, 0.50, 0.25, step=0.05)
        with c3:
            data_days_o = st.slider('历史数据（天）', 120, 500, 252, step=20, key='opt_days')
            max_to      = st.slider('换手率约束', 0.1, 1.0, 0.3, step=0.05)

        if st.button('运行优化', type='primary'):
            if len(symbols_list) < 2:
                st.error('至少输入 2 个标的。')
            else:
                returns_dict = {}
                with st.spinner('拉取历史数据...'):
                    for sym in symbols_list:
                        df_s = _make_price_df_from_akshare(sym, data_days_o)
                        if df_s is not None and len(df_s) > 30:
                            returns_dict[sym] = df_s['close'].pct_change().dropna()

                if len(returns_dict) < 2:
                    st.error('数据获取失败，请检查网络或标的。')
                else:
                    try:
                        from core.portfolio_optimizer import PortfolioOptimizer
                        returns_df = pd.DataFrame(returns_dict).dropna()
                        optimizer = PortfolioOptimizer(
                            returns=returns_df, cov_method=cov_method,
                            max_weight=max_weight, min_weight=0.0,
                        )

                        method_fn = getattr(optimizer, opt_method)
                        weights = method_fn()

                        st.success('优化完成！')

                        # 权重饼图
                        w_df = pd.DataFrame({
                            '标的': list(returns_dict.keys()),
                            '权重': weights,
                        })
                        fig = px.pie(w_df, values='权重', names='标的',
                                     title=f'{opt_method} 优化权重',
                                     color_discrete_sequence=px.colors.qualitative.Set2)
                        fig.update_layout(height=350)
                        st.plotly_chart(fig, use_container_width=True)

                        # 权重表格 + 换手率约束
                        sym_keys = list(returns_dict.keys())
                        current_weights = pd.Series(
                            np.ones(len(sym_keys)) / len(sym_keys), index=sym_keys
                        )
                        w_adj = optimizer.apply_turnover_constraint(
                            weights, current_weights, max_turnover=max_to
                        )

                        result_rows = [
                            {'标的': sym, '优化权重': f'{w:.1%}', '换手调整后': f'{wa:.1%}',
                             '预期年化收益': f'{float(returns_df[sym].mean() * 252):.1%}',
                             '年化波动率': f'{float(returns_df[sym].std() * np.sqrt(252)):.1%}'}
                            for sym, w, wa in zip(returns_dict.keys(), weights, w_adj)
                        ]
                        st.dataframe(pd.DataFrame(result_rows), hide_index=True,
                                     use_container_width=True)

                        # 有效前沿（max_sharpe 模式下额外展示）
                        if opt_method == 'max_sharpe':
                            port_ret  = float(np.dot(weights, returns_df.mean() * 252))
                            port_vol  = float(np.sqrt(np.dot(weights, np.dot(
                                optimizer._cov * 252, weights
                            ))))
                            port_sharpe = (port_ret - 0.02) / port_vol if port_vol > 0 else 0
                            m1, m2, m3 = st.columns(3)
                            m1.metric('组合年化收益', f'{port_ret:.2%}')
                            m2.metric('组合年化波动率', f'{port_vol:.2%}')
                            m3.metric('组合 Sharpe', f'{port_sharpe:.3f}')

                    except Exception as e:
                        st.error(f'优化失败: {e}')

    # ── Tab 2: Black-Litterman ────────────────────────────────
    with tab_bl:
        st.subheader('Black-Litterman 观点融合')
        st.caption('将策略因子观点融入均衡收益，生成后验权重')

        bl_syms_input = st.text_area(
            '标的（每行一个）',
            '000001.SZ\n600519.SH\n300750.SZ\n600036.SH',
            height=100, key='bl_syms',
        )
        bl_symbols = [s.strip() for s in bl_syms_input.strip().split('\n') if s.strip()]

        st.markdown('**输入观点（年化预期收益）**')
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

        bl_days = st.slider('历史数据（天）', 120, 500, 252, key='bl_days')

        if st.button('计算 BL 权重', type='primary'):
            returns_dict_bl = {}
            with st.spinner('拉取数据...'):
                for sym in bl_symbols:
                    df_s = _make_price_df_from_akshare(sym, bl_days)
                    if df_s is not None and len(df_s) > 30:
                        returns_dict_bl[sym] = df_s['close'].pct_change().dropna()

            if len(returns_dict_bl) < 2:
                st.error('数据获取失败。')
            else:
                try:
                    from core.portfolio_optimizer import PortfolioOptimizer
                    returns_df_bl = pd.DataFrame(returns_dict_bl).dropna()
                    opt_bl = PortfolioOptimizer(returns=returns_df_bl, max_weight=0.40)
                    w_bl = opt_bl.black_litterman(views, confidences)
                    w_eq = opt_bl.equal_weight()
                    w_gmv = opt_bl.min_variance()

                    st.success('BL 权重计算完成！')

                    # 对比三种权重
                    comp_df = pd.DataFrame({
                        '标的': list(returns_dict_bl.keys()),
                        'Black-Litterman': w_bl,
                        '等权基准':  w_eq,
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

    # ── Tab 3: 策略资金分配 ───────────────────────────────────
    with tab_alloc:
        st.subheader('多策略资金分配（PortfolioAllocator）')

        try:
            from core.portfolio_allocator import AllocConfig, PortfolioAllocator, WeightMode
            alloc_ok = True
        except ImportError as e:
            st.error(f'PortfolioAllocator 导入失败: {e}')
            alloc_ok = False

        if alloc_ok:
            cfg_a = load_trading_config()
            strategies_cfg = cfg_a.get('strategies', {})
            portfolio_cfg  = cfg_a.get('portfolio', {})
            total_capital  = float(portfolio_cfg.get('capital', 100_000))

            col_cfg, col_res = st.columns([1, 2])

            with col_cfg:
                total_capital = st.number_input(
                    '总资金（元）', value=total_capital,
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

                st.markdown('**策略权重（固定权重模式）**')
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
                    allocator = PortfolioAllocator(
                        total_capital=total_capital, config=config
                    )
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
                            '策略':    sname,
                            '权重':    f'{info["weight"]:.1%}',
                            '额度(¥)': f'{info["budget"]:,.0f}',
                            '已用(¥)': f'{info["used"]:,.0f}',
                            '可用(¥)': f'{info["available"]:,.0f}',
                            '利用率':  f'{info["utilization"]:.1%}',
                        })
                    st.dataframe(pd.DataFrame(rows_a), hide_index=True, use_container_width=True)

                    s1, s2, s3, s4 = st.columns(4)
                    s1.metric('总资金', f'¥{summary["total_capital"]:,.0f}')
                    s2.metric('已分配', f'¥{summary["total_budget"]:,.0f}')
                    s3.metric('已使用', f'¥{summary["total_used"]:,.0f}')
                    s4.metric('保留现金', f'¥{summary["reserve"]:,.0f}')

                    # 饼图
                    if rows_a:
                        budgets = [float(r['额度(¥)'].replace(',', '')) for r in rows_a]
                        fig_a = px.pie(
                            names=[r['策略'] for r in rows_a],
                            values=budgets,
                            title='策略资金分配',
                            color_discrete_sequence=px.colors.qualitative.Set2,
                        )
                        fig_a.update_layout(height=300)
                        st.plotly_chart(fig_a, use_container_width=True)

                    # 再平衡检测
                    st.markdown('---')
                    current_mv = {name: strat_info[name]['used'] for name in strat_info}
                    if allocator.needs_rebalance(current_mv):
                        st.warning('持仓偏离超阈值，建议再平衡。')
                        if st.button('执行再平衡'):
                            new_budgets = allocator.rebalance(trigger='manual')
                            st.success(f'再平衡完成：{new_budgets}')
                    else:
                        st.success('持仓权重正常，无需再平衡。')

                except Exception as e:
                    st.error(f'分配计算失败: {e}')


# ============================================================
# Page 5: 信号 & 执行
# ============================================================

elif page == '📈 信号 & 执行':
    st.title('📈 信号 & 执行')
    st.caption('实时信号 · VWAP/TWAP 算法下单 · 市场冲击估算 · 成交记录')

    tab_sig, tab_algo, tab_trades = st.tabs(
        ['📡 实时信号', '⚡ 算法下单', '📋 成交记录']
    )

    # ── Tab 1: 信号 ───────────────────────────────────────────
    with tab_sig:
        signals   = load_signals(50)
        positions = load_positions()
        watchlist = load_watchlist()

        # 持仓行情
        active = [p for p in positions if p.get('shares', 0) > 0]
        if active:
            st.subheader(f'当前持仓（{len(active)} 只）')
            pos_rows = []
            for p in active:
                sym  = p['symbol']
                snap = load_realtime(sym)
                cur  = snap.get('price', 0) if snap else 0
                entry = float(p.get('entry_price', 0) or 0)
                pnl_pct = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0
                lu  = limit_up_pct(sym) * 100
                pos_rows.append({
                    '标的':   sym,
                    '持仓':   p.get('shares', 0),
                    '成本':   f'{entry:.2f}',
                    '现价':   f'{cur:.2f}' if cur else '—',
                    '涨跌%':  f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                    '盈亏%':  f'{pnl_pct:+.2f}%',
                    '距涨停': f'{lu - snap.get("pct", 0):.1f}%' if snap else '—',
                    '量比':   f'{snap.get("vol_ratio", 0):.1f}' if snap and snap.get("vol_ratio") else '—',
                })
            st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
            st.markdown('---')

        # 信号列表
        st.subheader('交易信号')
        if signals:
            sig_rows = []
            for s in signals[:30]:
                d = s.get('direction', '')
                sig_rows.append({
                    '时间':   str(s.get('timestamp', s.get('created_at', '')))[:16],
                    '标的':   s.get('symbol', ''),
                    '方向':   f"🟢 BUY" if d == 'BUY' else f"🔴 SELL" if d == 'SELL' else d,
                    '价格':   s.get('price', '—'),
                    '强度':   f"{float(s.get('strength', 0)):.3f}",
                    '因子':   s.get('factor', s.get('signal_type', '')),
                })
            st.dataframe(pd.DataFrame(sig_rows), hide_index=True, use_container_width=True)
        else:
            st.info('暂无信号记录')

        # 自选股
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
                    '标的':  sym,
                    '现价':  f'{snap.get("price", 0):.2f}' if snap else '—',
                    '涨跌%': f'{snap.get("pct", 0):+.2f}%' if snap else '—',
                    '最高':  f'{snap.get("high", 0):.2f}' if snap else '—',
                    '最低':  f'{snap.get("low", 0):.2f}' if snap else '—',
                })
            if wl_rows:
                st.dataframe(pd.DataFrame(wl_rows), hide_index=True, use_container_width=True)

    # ── Tab 2: 算法下单 ───────────────────────────────────────
    with tab_algo:
        st.subheader('算法订单（VWAP / TWAP）')
        st.caption('SimulatedBroker 模拟撮合 · A 股整手 · Almgren-Chriss 市场冲击预估')

        try:
            from core.execution.vwap_executor import VWAPExecutor
            from core.execution.twap_executor import TWAPExecutor
            from core.execution.impact_estimator import ImpactEstimator
            from core.brokers.simulated import SimulatedBroker, SimConfig
            from core.oms import OMS
            algo_ok = True
        except ImportError as e:
            st.error(f'执行模块加载失败: {e}')
            algo_ok = False

        if algo_ok:
            col_form, col_impact = st.columns([1, 1])

            with col_form:
                algo_sym  = st.text_input('标的', '000001.SZ', key='algo_sym')
                algo_dir  = st.radio('方向', ['BUY', 'SELL'], horizontal=True)
                algo_shares = st.number_input('数量（股）', 100, 100000, 1000, step=100)
                algo_type = st.selectbox('算法类型', ['VWAP', 'TWAP'])
                algo_dur  = st.slider('执行时长（分钟）', 5, 120, 30, step=5)
                algo_slices = st.slider('切片数量', 3, 20, 10)

            with col_impact:
                st.subheader('市场冲击预估')
                snap_a = load_realtime(algo_sym)
                ref_price = snap_a.get('price', 0) if snap_a else 0

                if ref_price > 0:
                    st.metric('参考价格', f'¥{ref_price:.2f}')
                    est_order_value = algo_shares * ref_price
                    st.metric('订单金额', f'¥{est_order_value:,.0f}')

                market_vol = st.number_input(
                    '市场日均成交量（股，估算）', 100_000, 100_000_000, 1_000_000, step=100_000,
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
                    st.caption(f'20 bps 冲击限制下最大可下量：{max_qty:,} 股')
                except Exception as e:
                    st.warning(f'冲击估算失败: {e}')

            st.markdown('---')
            if st.button('模拟执行算法订单', type='primary'):
                with st.spinner(f'模拟 {algo_type} 执行...'):
                    try:
                        broker = SimulatedBroker(SimConfig(
                            initial_cash=10_000_000,
                            price_source='manual',
                            slippage_bps=5.0,
                            commission_rate=0.0003,
                            stamp_tax_rate=0.001,
                            enforce_lot=True,
                        ))
                        broker.connect()
                        oms = OMS(broker=broker)

                        result = oms.submit_algo_order(
                            algo=algo_type,
                            symbol=algo_sym,
                            direction=algo_dir,
                            total_shares=int(algo_shares),
                            duration_minutes=algo_dur,
                            reference_price=ref_price if ref_price > 0 else 10.0,
                            slice_interval=max(1, algo_dur // algo_slices),
                        )

                        st.success('算法订单执行完成！')
                        r1, r2, r3, r4 = st.columns(4)
                        r1.metric('成交率', f'{result.fill_rate:.1%}')
                        r2.metric('成交股数', f'{result.filled_shares:,}')
                        r3.metric('均价', f'¥{result.avg_fill_price:.3f}')
                        r4.metric('实际滑点', f'{result.slippage_bps:.2f} bps')

                        # 切片明细
                        if result.slices:
                            slice_df = pd.DataFrame([{
                                '切片': s.slice_id,
                                '目标股数': s.target_shares,
                                '成交股数': s.filled_shares,
                                '成交价': f'{s.fill_price:.3f}' if s.fill_price else '—',
                                '状态': s.status,
                            } for s in result.slices])
                            with st.expander('切片明细'):
                                st.dataframe(slice_df, hide_index=True, use_container_width=True)

                    except Exception as e:
                        st.error(f'订单执行失败: {e}')

    # ── Tab 3: 成交记录 ───────────────────────────────────────
    with tab_trades:
        st.subheader('成交记录（含 TCA）')
        trades = load_trades(200)

        if trades:
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
                    '时间':   str(t.get('timestamp', t.get('created_at', '')))[:16],
                    '标的':   t.get('symbol', ''),
                    '方向':   t.get('direction', ''),
                    '股数':   t.get('shares', 0),
                    '成交价': f'{float(t.get("price", 0)):.3f}',
                    '佣金':   f'{float(t.get("commission", 0)):.2f}',
                    '印花税': f'{float(t.get("stamp_tax", 0)):.2f}',
                    '滑点bps': t.get('slippage_bps', '—'),
                })
            st.dataframe(pd.DataFrame(rows_t), hide_index=True, use_container_width=True)
        else:
            st.info('暂无成交记录')


# ============================================================
# Page 6: 回测验证
# ============================================================

elif page == '📉 回测验证':
    st.title('📉 回测验证')
    st.caption('Walk-Forward 分析 · 参数敏感性热力图 · 模拟实盘一致性验证')

    tab_wfa, tab_sens, tab_val = st.tabs(
        ['📈 Walk-Forward', '🌡️ 敏感性分析', '✅ 一致性验证']
    )

    # ── Tab 1: WFA ────────────────────────────────────────────
    with tab_wfa:
        wf = load_wf_results(30)
        if wf:
            st.subheader(f'历史 WFA 结果（{len(wf)} 条）')
            rows_w = []
            for r in wf:
                try:
                    params = json.loads(r.get('best_params', '{}'))
                except Exception:
                    params = {}
                rows_w.append({
                    '窗口':       r.get('window', '?'),
                    '标的':       r.get('symbol', ''),
                    '策略':       r.get('strategy', ''),
                    '训练Sharpe': f"{r.get('train_sharpe', 0):.2f}",
                    '测试Sharpe': f"{r.get('test_sharpe', 0):.2f}",
                    '测试收益%':  f"{r.get('test_return_pct', 0):+.1f}%",
                    '胜率%':      f"{r.get('test_winrate_pct', 0):.0f}%",
                    '最大回撤%':  f"{r.get('test_maxdd_pct', 0):.1f}%",
                })
            df_wf = pd.DataFrame(rows_w)
            st.dataframe(df_wf, hide_index=True, use_container_width=True)

            # OOS Sharpe 柱图
            sharpes = [float(r.get('test_sharpe', 0)) for r in wf]
            fig_wf = px.bar(
                x=[r.get('window', i) for i, r in enumerate(wf)],
                y=sharpes,
                color=sharpes,
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
            st.info('暂无 WFA 结果。运行脚本：`python scripts/walkforward_job.py --symbol 510310.SH`')

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
        with cw2: wfa_strat = st.selectbox('策略', ['RSI', 'MACD', 'Bollinger'])
        with cw3: wfa_yrs = st.number_input('训练年数', 1, 5, 2)
        wfa_test_yrs = st.number_input('验证年数', 1, 3, 1)

        if st.button('开始 WFA', disabled=not backend_ok):
            import subprocess
            cmd = [
                sys.executable,
                os.path.join(BASE_DIR, 'scripts', 'walkforward_job.py'),
                '--symbol', wfa_sym, '--strategy', wfa_strat,
                '--train-years', str(int(wfa_yrs)), '--test-years', str(int(wfa_test_yrs)),
            ]
            with st.spinner(f'训练 {wfa_sym} ({wfa_strat}) ...'):
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, encoding='utf-8', errors='replace', timeout=600,
                    )
                    if result.returncode == 0:
                        st.success('WFA 完成')
                        st.cache_data.clear()
                    else:
                        st.warning(f'退出码 {result.returncode}')
                    st.code(result.stdout[-3000:] or '（无输出）', language='text')
                except subprocess.TimeoutExpired:
                    st.error('训练超时（> 600s）')
                except Exception as e:
                    st.error(f'运行失败: {e}')

    # ── Tab 2: 敏感性分析 ─────────────────────────────────────
    with tab_sens:
        st.subheader('参数敏感性热力图')
        st.caption('双参数网格扫描 → Sharpe 热力图，peak_sensitivity_ratio 量化稳健度')

        try:
            from core.walkforward import SensitivityAnalyzer
            sens_available = True
        except ImportError as e:
            st.error(f'SensitivityAnalyzer 加载失败: {e}')
            sens_available = False

        if sens_available:
            cfg_s = load_trading_config()
            live_syms_s = cfg_s.get('live_symbols', [])
            sym_opts_s = [s['symbol'] for s in live_syms_s] if live_syms_s else ['510310.SH']
            sens_sym_sel = st.selectbox('标的', sym_opts_s + ['自定义'], key='sens_sym')
            if sens_sym_sel == '自定义':
                sens_sym = st.text_input('输入标的代码', '000001.SZ', key='sens_sym_custom')
            else:
                sens_sym = sens_sym_sel

            # 因子选择 + 参数提示
            _FACTOR_PARAMS = {
                'RSI':          ('period', '7,10,14,21,28',     'buy_threshold', '20,25,30,35,40'),
                'ATR':          ('period', '7,10,14,21,28',     'lookback',      '10,15,20,30,40'),
                'MACD':         ('fast',   '5,8,12,16,20',      'slow',          '20,26,34,40,50'),
                'BollingerBands':('period','10,15,20,30,40',    'nb_std',        '1.5,2.0,2.5,3.0,3.5'),
            }
            sens_factor = st.selectbox('因子', list(_FACTOR_PARAMS.keys()), key='sens_factor')
            _dp1n, _dp1v, _dp2n, _dp2v = _FACTOR_PARAMS[sens_factor]

            col_s1, col_s2 = st.columns(2)
            with col_s1:
                p1_name = st.text_input('参数 1 名称', _dp1n, key='p1_name')
                p1_vals = st.text_input('参数 1 取值（逗号分隔）', _dp1v, key='p1_vals')
            with col_s2:
                p2_name = st.text_input('参数 2 名称', _dp2n, key='p2_name')
                p2_vals = st.text_input('参数 2 取值（逗号分隔）', _dp2v, key='p2_vals')

            if st.button('运行敏感性分析'):
                try:
                    p1 = [float(v.strip()) for v in p1_vals.split(',') if v.strip()]
                    p2 = [float(v.strip()) for v in p2_vals.split(',') if v.strip()]
                    if not p1 or not p2:
                        st.error('参数取值列表不能为空')
                    else:
                        import subprocess
                        cmd_s = [
                            sys.executable,
                            os.path.join(BASE_DIR, 'scripts', 'sensitivity_job.py'),
                            '--symbol', sens_sym,
                            '--factor', sens_factor,
                            '--param1', p1_name, '--p1-values', ','.join(str(v) for v in p1),
                            '--param2', p2_name, '--p2-values', ','.join(str(v) for v in p2),
                        ]
                        with st.spinner('敏感性扫描中（可能需要 1-3 分钟）...'):
                            res_s = subprocess.run(
                                cmd_s, capture_output=True, encoding='utf-8',
                                errors='replace', timeout=300,
                            )
                        if res_s.returncode == 0:
                            st.success('完成')
                            # 优先展示 PNG，降级展示 CSV（matplotlib 未安装时）
                            png_path = os.path.join(OUTPUTS_DIR, f'sensitivity_{sens_sym}.png')
                            csv_path = os.path.join(OUTPUTS_DIR, f'sensitivity_{sens_sym}.csv')
                            if os.path.exists(png_path):
                                st.image(png_path, caption='Sharpe 热力图')
                            elif os.path.exists(csv_path):
                                heat_df = pd.read_csv(csv_path, index_col=0)
                                fig_heat = px.imshow(
                                    heat_df.astype(float),
                                    color_continuous_scale='RdYlGn',
                                    color_continuous_midpoint=0,
                                    text_auto='.2f',
                                    labels={'x': p2_name, 'y': p1_name, 'color': 'Sharpe'},
                                    title=f'{sens_factor} Sensitivity — {sens_sym}',
                                )
                                fig_heat.update_layout(height=400)
                                st.plotly_chart(fig_heat, use_container_width=True)
                            st.code(res_s.stdout[-2000:] or '（无输出）', language='text')
                        else:
                            st.error(f'脚本返回错误 (exit {res_s.returncode})')
                            st.code(res_s.stderr[-2000:] or res_s.stdout[-1000:], language='text')
                except subprocess.TimeoutExpired:
                    st.error('扫描超时（> 300s），请缩减参数网格')
                except Exception as e:
                    st.error(f'运行失败: {e}')

            # 显示已有热力图
            if os.path.exists(OUTPUTS_DIR):
                heatmaps = [f for f in os.listdir(OUTPUTS_DIR)
                            if f.startswith('sensitivity_') and f.endswith(('.png', '.csv'))]
                if heatmaps:
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
                                color_continuous_scale='RdYlGn',
                                color_continuous_midpoint=0,
                                text_auto='.2f',
                                title=selected_hm.replace('.csv', ''),
                            )
                            fig_hm.update_layout(height=400)
                            st.plotly_chart(fig_hm, use_container_width=True)
                        except Exception as e:
                            st.error(f'读取热力图失败: {e}')

    # ── Tab 3: 一致性验证 ─────────────────────────────────────
    with tab_val:
        st.subheader('模拟实盘一致性验证')
        st.caption('对比回测成交价 vs 模拟撮合价，目标：|偏差| ≤ 20 bps，通过率 ≥ 90%')

        try:
            from core.paper_trade_validator import PaperTradeValidator
            from core.brokers.simulated import SimConfig, SimulatedBroker
            val_ok = True
        except ImportError as e:
            st.error(f'PaperTradeValidator 加载失败: {e}')
            val_ok = False

        if val_ok:
            # 历史报告
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
            slippage_v  = st.slider('模拟滑点 (bps)', 0.0, 50.0, 5.0, step=1.0, key='val_slip')
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
                if valid_sigs:
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
                else:
                    st.warning('无可用信号数据')


# ============================================================
# Page 7: 监控 & 告警
# ============================================================

elif page == '🏥 监控 & 告警':
    st.title('🏥 监控 & 告警')
    st.caption('策略健康 · CVaR / Monte Carlo · 数据质量 · AlertManager')

    tab_health, tab_data, tab_alert = st.tabs(
        ['💓 策略健康', '🔍 数据质量', '🔔 告警中心']
    )

    # ── Tab 1: 策略健康 ───────────────────────────────────────
    with tab_health:
        daily_stats = load_daily_stats(250)

        if not daily_stats:
            st.info('暂无日度统计数据（backend 未连接或 portfolio.db 无记录）')
        else:
            # StrategyHealthMonitor
            try:
                from core.strategy_health import StrategyHealthMonitor
                monitor = StrategyHealthMonitor()
                health_report = monitor.check(daily_stats)
                health_series = monitor.check_series(daily_stats)
            except Exception as e:
                st.warning(f'健康监控加载失败: {e}')
                health_report = None
                health_series = pd.DataFrame()

            if health_report:
                level = health_report.worst_level()
                icon  = {'OK': '🟢', 'WARN': '🟡', 'CRITICAL': '🔴'}.get(level, '⚪')
                hc1, hc2, hc3, hc4, hc5 = st.columns(5)
                hc1.metric('系统状态', f'{icon} {level}')
                hc2.metric('Sharpe(20d)', f'{health_report.rolling_sharpe_20d:.3f}',
                           delta=f'{health_report.sharpe_change_pct:+.1f}%')
                hc3.metric('Sharpe(60d)', f'{health_report.rolling_sharpe_60d:.3f}')
                hc4.metric('今日收益', f'{health_report.latest_daily_return*100:+.2f}%')
                hc5.metric('连续亏损天', f'{health_report.consecutive_loss_days} 天')

                if health_report.alerts:
                    for alert in health_report.alerts:
                        fn = {'CRITICAL': st.error, 'WARN': st.warning, 'OK': st.success}.get(
                            alert.level, st.info
                        )
                        pause = ' **【建议暂停自动交易】**' if alert.should_pause else ''
                        fn(f'**[{alert.level}] {alert.check_name}**: {alert.message}{pause}')
                else:
                    st.success('策略运行正常，无健康告警')

            st.markdown('---')

            # Rolling Sharpe 图
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

            st.markdown('---')

            # CVaR / Monte Carlo
            st.subheader('风险分析（CVaR · Monte Carlo）')
            try:
                from core.portfolio_risk import MonteCarloStressTest
                portfolio_mc = load_portfolio_summary()
                equity_mc = float(portfolio_mc.get('total_equity', 100_000) or 100_000)

                ret_series = pd.Series([
                    float(s.get('daily_return', 0) if isinstance(s, dict)
                          else getattr(s, 'daily_return', 0))
                    for s in daily_stats
                ]).dropna()

                if len(ret_series) >= 30:
                    n_sim = st.slider('模拟次数', 500, 5000, 2000, step=500)
                    mc = MonteCarloStressTest(n_simulations=n_sim, horizon_days=63, seed=42)
                    mc_result = mc.run(ret_series, initial_equity=equity_mc)

                    mc1, mc2, mc3, mc4 = st.columns(4)
                    mc1.metric('P5 净值（63日）', f'¥{mc_result.p5_final:,.0f}',
                               delta=f'{(mc_result.p5_final/equity_mc-1)*100:+.1f}%')
                    mc2.metric('P50 净值', f'¥{mc_result.p50_final:,.0f}',
                               delta=f'{(mc_result.p50_final/equity_mc-1)*100:+.1f}%')
                    mc3.metric('亏损概率', f'{mc_result.prob_loss*100:.1f}%')
                    mc4.metric('ES (95%)', f'{mc_result.expected_shortfall*100:.2f}%')

                    with st.expander('完整 Monte Carlo 报告'):
                        st.text(mc_result.summary())
                else:
                    st.info(f'日度数据不足（{len(ret_series)} 条，需 ≥ 30 条）')
            except Exception as e:
                st.warning(f'风险分析失败: {e}')

    # ── Tab 2: 数据质量 ───────────────────────────────────────
    with tab_data:
        st.subheader('数据质量')

        # DataQualityChecker
        try:
            from core.data_quality import DataQualityChecker
            dq_sym = st.text_input('检查标的', '000001.SZ', key='dq_sym')
            dq_days = st.slider('检查天数', 30, 120, 60, key='dq_days')

            if st.button('运行数据质量检查'):
                with st.spinner('拉取数据...'):
                    df_dq = _make_price_df_from_akshare(dq_sym, dq_days)
                if df_dq is not None:
                    checker = DataQualityChecker(symbol=dq_sym)
                    checker.check_and_mark(df_dq)
                    report_dq = checker.report
                    dq1, dq2, dq3 = st.columns(3)
                    dq1.metric('数据质量评分', f'{report_dq.quality_score:.0f}/100')
                    n_anomalies = (report_dq.n_zero_volume
                                   + report_dq.n_abnormal_moves
                                   + report_dq.n_gaps)
                    dq2.metric('异常数据点', n_anomalies)
                    dq3.metric('总行数', report_dq.total_bars)

                    if report_dq.anomalies:
                        with st.expander('问题明细'):
                            for anm in report_dq.anomalies:
                                st.warning(f'[{anm.anomaly_type}] {anm.date} — {anm.detail}')
                else:
                    st.error('数据获取失败')
        except Exception as e:
            st.warning(f'DataQualityChecker 加载失败: {e}')

        st.markdown('---')

        # Level2 质量
        st.subheader('Level2 数据完整率')
        try:
            from core.level2_quality import Level2QualityReporter
            if st.button('生成 Level2 质量报告'):
                with st.spinner('分析 Level2 数据...'):
                    reporter = Level2QualityReporter()
                    report_l2 = reporter.generate(days=7, threshold=0.95)
                    l2c1, l2c2, l2c3 = st.columns(3)
                    l2c1.metric('完整率', f'{report_l2.overall_completeness:.1%}')
                    l2c2.metric('快照总数', report_l2.total_snapshots)
                    l2c3.metric('合格率', '✅ 合格' if report_l2.passed else '❌ 不合格')
                    if report_l2.field_completeness:
                        fc_df = pd.DataFrame(
                            list(report_l2.field_completeness.items()),
                            columns=['字段', '完整率']
                        ).sort_values('完整率')
                        st.dataframe(fc_df, hide_index=True, use_container_width=True)
        except Exception as e:
            st.info(f'Level2 质量模块: {e}')

        st.markdown('---')

        # 数据源健康
        st.subheader('实时行情连通性测试')
        test_sym = st.text_input('测试标的', '000001.SZ', key='ds_sym')
        if st.button('测试连通性'):
            snap_test = load_realtime(test_sym)
            if snap_test and snap_test.get('price', 0) > 0:
                st.success(f'行情获取成功：现价 ¥{snap_test["price"]:.2f}，'
                           f'涨跌 {snap_test.get("pct", 0):+.2f}%')
            else:
                st.error('行情获取失败（可能是交易日外或网络问题）')

    # ── Tab 3: 告警中心 ───────────────────────────────────────
    with tab_alert:
        st.subheader('AlertManager 告警中心')

        try:
            from core.alerting import AlertManager, get_alert_manager
            alert_ok = True
        except ImportError as e:
            st.error(f'AlertManager 加载失败: {e}')
            alert_ok = False

        if alert_ok:
            # Webhook 配置状态
            wechat_url  = os.environ.get('WECHAT_WEBHOOK', '')
            dingtalk_url = os.environ.get('DINGTALK_WEBHOOK', '')

            a1, a2, a3 = st.columns(3)
            a1.metric('企业微信', '✅ 已配置' if wechat_url else '❌ 未配置')
            a2.metric('钉钉', '✅ 已配置' if dingtalk_url else '❌ 未配置')
            a3.metric('SMTP 邮件', '✅ 已配置' if os.environ.get('SMTP_HOST') else '❌ 未配置')

            if not wechat_url and not dingtalk_url:
                st.info('当前为 log_only 模式。在 `.env` 中配置 WECHAT_WEBHOOK 或 '
                        'DINGTALK_WEBHOOK 后重启即可启用推送。')

            # 告警历史
            st.markdown('---')
            st.subheader('告警历史')
            am = get_alert_manager()
            history = am.get_history(last_n=50)

            if history:
                h_rows = [
                    {'时间': rec.timestamp[:16], '级别': rec.level,
                     '内容': rec.message[:80], '渠道': rec.channel, '成功': rec.sent}
                    for rec in reversed(history)
                ]
                h_df = pd.DataFrame(h_rows)

                # 级别分布
                level_counts = h_df['级别'].value_counts()
                fig_h = px.bar(
                    x=level_counts.index, y=level_counts.values,
                    color=level_counts.index,
                    color_discrete_map={'CRITICAL': '#f85149', 'WARNING': '#e3b341', 'INFO': '#4c78a8'},
                    labels={'x': '级别', 'y': '条数'},
                    title='告警级别分布',
                )
                fig_h.update_layout(showlegend=False, height=220, margin=dict(t=40, b=10))
                st.plotly_chart(fig_h, use_container_width=True)

                st.dataframe(h_df, hide_index=True, use_container_width=True)

                if st.button('清空告警历史'):
                    am.clear_history()
                    st.success('已清空')
                    st.rerun()
            else:
                st.info('暂无告警记录')

            # 测试推送
            st.markdown('---')
            st.subheader('测试推送')
            test_level = st.selectbox('告警级别', ['INFO', 'WARNING', 'CRITICAL'], key='test_lvl')
            test_msg   = st.text_input('测试消息', '这是一条测试告警', key='test_msg')

            if st.button('发送测试告警'):
                try:
                    am_test = AlertManager(
                        wechat_webhook=wechat_url,
                        dingtalk_webhook=dingtalk_url,
                        min_level='INFO',
                        rate_limit_sec=0,
                    )
                    fn_map = {'INFO': am_test.send_info, 'WARNING': am_test.send_warning,
                              'CRITICAL': am_test.send_critical}
                    result_t = fn_map[test_level](test_msg)
                    if result_t:
                        st.success('发送成功')
                    else:
                        st.warning('发送失败（检查 Webhook URL 是否正确）')
                except Exception as e:
                    st.error(f'发送失败: {e}')

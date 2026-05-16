"""
ui/data.py — UI 数据接入层 (P4-1 阶段二)

提供给 ui/pages/* 的所有数据获取统一入口:
  - BACKEND_URL / BASE_DIR / BACKEND_DIR 等路径常量
  - api_get / api_post:Backend HTTP 封装(失败返回空 dict,不抛异常)
  - load_*():@st.cache_data 的数据加载器(全部走 backend 端点)
  - limit_up_pct / make_price_df:轻量工具

设计原则:
  - 不直接访问 qt.gtimg.cn / 新闻 fetcher 等数据源(P4-2 完成)
  - DataLayer (make_price_df) 是项目内部数据抽象,UI 通过它取 K 线属于正常依赖
  - 业务逻辑(因子计算 / 优化 / 训练)严禁出现在本模块,只做数据获取

向后兼容:
  - 原 streamlit_helpers.py 保留为 thin shim,转发到本模块
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
from typing import Optional

import pandas as pd
import streamlit as st


# ─── 路径 & 配置 ─────────────────────────────────────────────

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


# ─── Backend HTTP 封装 ───────────────────────────────────────

def api_get(endpoint: str, timeout: float = 8.0) -> dict:
    """GET <BACKEND_URL><endpoint>;失败返回空 dict(不抛异常)。"""
    url = f"{BACKEND_URL}{endpoint}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'QuantUI/3.0'})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def api_post(endpoint: str, data: dict, timeout: float = 8.0) -> dict:
    """POST <BACKEND_URL><endpoint>;失败返回空 dict。"""
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


# ─── Cached data loaders ────────────────────────────────────

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
    """实时报价(P4-2: 走 backend /data/realtime/<symbol>,不直连 qt.gtimg.cn)。"""
    resp = api_get(f'/data/realtime/{symbol}', timeout=6)
    if resp.get('status') != 'ok':
        return {}
    return resp.get('data', resp) or {}


@st.cache_data(ttl=60)
def load_news_headlines(symbol: str, n: int = 5) -> list:
    """新闻标题(P4-2: 走 backend /data/news/<symbol>)。"""
    resp = api_get(f'/data/news/{symbol}?n={n}', timeout=8)
    if resp.get('status') != 'ok':
        return []
    return resp.get('headlines', []) or []


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


# ─── 工具函数 ────────────────────────────────────────────────

def limit_up_pct(symbol: str) -> float:
    """A 股涨跌停幅度(创业板/科创板 20%;ST 5%;其余 10%)。"""
    s = symbol.lower().replace('.sz', '').replace('.sh', '')
    if any(s.startswith(p) for p in ('st', '*st')):
        return 0.05
    if s.startswith('300') or s.startswith('688'):
        return 0.20
    return 0.10


def make_price_df(symbol: str, days: int = 300) -> Optional[pd.DataFrame]:
    """拉取日线 K 线;优先 DataLayer,AKShare 作为 fallback。"""
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


# Backward-compat alias(streamlit_app.py 原 fn 名)
_make_price_df_from_akshare = make_price_df

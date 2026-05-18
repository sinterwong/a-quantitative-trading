"""ui/api_client.py — backend HTTP 客户端。

设计原则:
- 只做 transport(URL 拼接、auth header、超时、状态码归一)
- 每个端点一个 typed wrapper;读端点贴 @st.cache_data,写端点不缓存
- 业务页面 try/except BackendError,统一在 widgets.layout.error_banner 渲染
- mutation 成功后页面自己调 st.cache_data.clear() 刷新

env:
- QUANT_UI_BACKEND_URL  默认 http://127.0.0.1:5555
- TRADING_API_KEY       后端 require 时必填,UI 走 X-API-Key header
- QUANT_UI_TIMEOUT      单请求超时,默认 8 秒
"""
from __future__ import annotations

from typing import Any, Optional

import requests
import streamlit as st

from ui.config import BACKEND_URL, API_KEY, REQUEST_TIMEOUT


class BackendError(RuntimeError):
    """非 2xx / 后端 status=error / transport 故障统一抛出。

    status=0 表示 transport 层(超时 / 连接拒绝 / DNS 等),非 HTTP 状态码。
    """

    def __init__(self, status: int, message: str):
        super().__init__(f'[{status}] {message}')
        self.status = status
        self.message = message


def _session() -> requests.Session:
    """每个 Streamlit session 共享一个 requests.Session。"""
    sess = st.session_state.get('_quant_http_session')
    if sess is None:
        sess = requests.Session()
        sess.headers.update({'User-Agent': 'QuantUI/4.0'})
        if API_KEY:
            sess.headers['X-API-Key'] = API_KEY
        st.session_state['_quant_http_session'] = sess
    return sess


def _unwrap(r: requests.Response) -> dict:
    try:
        body = r.json()
    except Exception:
        raise BackendError(r.status_code, (r.text or '')[:200] or 'non-json response')
    if r.status_code >= 400 or (isinstance(body, dict) and body.get('status') == 'error'):
        msg = (
            (body.get('error') if isinstance(body, dict) else None)
            or (body.get('message') if isinstance(body, dict) else None)
            or f'HTTP {r.status_code}'
        )
        raise BackendError(r.status_code, msg)
    return body if isinstance(body, dict) else {'data': body}


def _request(method: str, path: str, *, params: Optional[dict] = None,
             json: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    """统一 transport 入口:requests 异常 → BackendError(0, ...)。"""
    url = f'{BACKEND_URL}{path}'
    try:
        r = _session().request(method, url, params=params, json=json,
                               timeout=timeout or REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise BackendError(0, f'后端 {method} {path} 超时(>{timeout or REQUEST_TIMEOUT}s)')
    except requests.exceptions.ConnectionError as exc:
        raise BackendError(0, f'后端不可达({BACKEND_URL}): {exc.__class__.__name__}')
    except requests.exceptions.RequestException as exc:
        raise BackendError(0, f'请求失败: {exc!r}')
    return _unwrap(r)


def _get(path: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    return _request('GET', path, params=params, timeout=timeout)


def _post(path: str, json: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    return _request('POST', path, json=json, timeout=timeout)


def _patch(path: str, json: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    return _request('PATCH', path, json=json, timeout=timeout)


def _put(path: str, json: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    return _request('PUT', path, json=json, timeout=timeout)


def _delete(path: str, timeout: Optional[float] = None) -> dict:
    return _request('DELETE', path, timeout=timeout)


def clear_cache() -> None:
    """mutation 成功后调,让下一次读端点重新发请求。"""
    st.cache_data.clear()


# ── 健康 / 系统 ─────────────────────────────────────────────
@st.cache_data(ttl=5)
def get_health() -> dict:
    return _get('/health', timeout=3)


@st.cache_data(ttl=30)
def get_monitor_status() -> dict:
    return _get('/monitor/status')


@st.cache_data(ttl=30)
def get_risk_status() -> dict:
    return _get('/risk/status')


@st.cache_data(ttl=300)
def get_market_status() -> dict:
    return _get('/market/status')


@st.cache_data(ttl=60)
def get_trading_mode() -> str:
    return str(_get('/trading/mode').get('mode', 'unknown'))


def set_trading_mode(mode: str) -> dict:
    return _put('/trading/mode', {'mode': mode})


@st.cache_data(ttl=15)
def get_alerts(limit: int = 30) -> list:
    body = _get('/alerts/history', params={'limit': limit})
    return body.get('alerts') or body.get('history') or body.get('data') or []


def clear_alerts() -> dict:
    return _post('/alerts/clear')


# ── 组合 ───────────────────────────────────────────────────
@st.cache_data(ttl=60)
def get_portfolio_summary() -> dict:
    return _get('/portfolio/summary')


@st.cache_data(ttl=60)
def get_positions(refresh: bool = False) -> list:
    body = _get('/positions', params={'refresh': 1} if refresh else None)
    return body.get('positions') or []


@st.cache_data(ttl=60)
def get_cash() -> float:
    body = _get('/cash')
    return float(body.get('cash', 0.0))


def set_cash(amount: float) -> dict:
    return _post('/portfolio/cash', {'amount': amount})


def upsert_position(payload: dict) -> dict:
    return _post('/portfolio/positions', payload)


@st.cache_data(ttl=120)
def get_daily(limit: int = 90) -> list:
    body = _get('/portfolio/daily', params={'limit': limit})
    return body.get('daily') or body.get('data') or []


@st.cache_data(ttl=300)
def get_performance_summary() -> dict:
    return _get('/performance/summary')


# ── 信号 / 成交 / 订单 ──────────────────────────────────────
@st.cache_data(ttl=30)
def get_signals(limit: int = 30, symbol: Optional[str] = None) -> list:
    params: dict = {'limit': limit}
    if symbol:
        params['symbol'] = symbol
    body = _get('/signals', params=params)
    return body.get('signals') or []


def record_signal(payload: dict) -> dict:
    return _post('/signals', payload)


@st.cache_data(ttl=30)
def get_trades(limit: int = 30, symbol: Optional[str] = None) -> list:
    params: dict = {'limit': limit}
    if symbol:
        params['symbol'] = symbol
    body = _get('/trades', params=params)
    return body.get('trades') or []


def record_trade(payload: dict) -> dict:
    return _post('/trades', payload)


@st.cache_data(ttl=30)
def get_orders_recent() -> list:
    body = _get('/orders/recent')
    return body.get('orders') or body.get('data') or []


@st.cache_data(ttl=15)
def get_orders_pending() -> list:
    body = _get('/orders/pending')
    return body.get('orders') or body.get('data') or []


def submit_order(payload: dict) -> dict:
    return _post('/orders/submit', payload, timeout=30)


def cancel_order(order_id: str) -> dict:
    return _post(f'/orders/{order_id}/cancel')


# ── 自选股 / 参数 ──────────────────────────────────────────
@st.cache_data(ttl=60)
def get_watchlist() -> list:
    body = _get('/watchlist')
    return body.get('watchlist') or []


def watchlist_add(payload: dict) -> dict:
    return _post('/watchlist/add', payload)


def watchlist_remove(symbol: str) -> dict:
    return _delete(f'/watchlist/{symbol}')


def watchlist_patch(symbol: str, payload: dict) -> dict:
    return _patch(f'/watchlist/{symbol}', payload)


@st.cache_data(ttl=300)
def get_params_all() -> dict:
    return _get('/params')


@st.cache_data(ttl=300)
def get_params(symbol: str) -> dict:
    return _get(f'/params/{symbol}')


def patch_params(symbol: str, payload: dict) -> dict:
    return _patch(f'/params/{symbol}', payload)


# ── 数据 / 行情 ─────────────────────────────────────────────
@st.cache_data(ttl=15)
def get_realtime(symbol: str) -> dict:
    body = _get(f'/data/realtime/{symbol}')
    return body.get('quote') or body.get('data') or body


@st.cache_data(ttl=300)
def get_daily_kline(code: str, days: int = 120) -> list:
    body = _get(f'/data/daily/{code}', params={'days': days})
    return body.get('data') or body.get('kline') or body.get('bars') or []


@st.cache_data(ttl=60)
def get_data_status() -> dict:
    return _get('/data/status')


@st.cache_data(ttl=300)
def get_fund_flow() -> dict:
    return _get('/data/fund_flow')


_MACRO_TIMEOUT = 15.0   # akshare 宏观接口网络较慢，给予足够超时

@st.cache_data(ttl=86400)
def get_macro(indicator: str) -> dict:
    timeout = _MACRO_TIMEOUT if indicator == 'CREDIT' else None
    return _get(f'/data/macro/{indicator}', timeout=timeout)


@st.cache_data(ttl=300)
def get_news(symbol: str, n: int = 8) -> list:
    body = _get(f'/data/news/{symbol}', params={'n': n})
    return body.get('headlines') or body.get('news') or body.get('data') or []


@st.cache_data(ttl=600)
def get_fundamentals(symbol: str) -> dict:
    body = _get(f'/fundamentals/{symbol}')
    return body.get('data') or body


@st.cache_data(ttl=300)
def get_northbound() -> dict:
    return _get('/northbound/flow')


# ── 分析 use case ──────────────────────────────────────────
def trigger_daily_analysis() -> dict:
    return _post('/analysis/run', timeout=120)


@st.cache_data(ttl=30)
def get_analysis_status() -> dict:
    return _get('/analysis/status')


@st.cache_data(ttl=300)
def get_analysis_health() -> dict:
    return _get('/analysis/health')


def analyze_a_stock(payload: dict) -> dict:
    return _post('/analysis/stock/a', payload, timeout=60)


def analyze_hk_stock(payload: dict) -> dict:
    return _post('/analysis/stock/hk', payload, timeout=60)


def sector_rotation(payload: dict) -> dict:
    return _post('/analysis/sector_rotation', payload, timeout=30)


def pairs_trading(payload: dict) -> dict:
    return _post('/analysis/pairs_trading', payload, timeout=30)


def sector_compare(payload: dict) -> dict:
    return _post('/analysis/sector/compare', payload, timeout=30)


# ── 新加的研究端点 ──────────────────────────────────────────
def run_backtest(payload: dict) -> dict:
    return _post('/backtest/run', payload, timeout=120)


def compose_portfolio(payload: dict) -> dict:
    return _post('/portfolio/compose', payload, timeout=60)


# ── WFA ───────────────────────────────────────────────────
@st.cache_data(ttl=300)
def get_wfa_history(symbol: Optional[str] = None, limit: int = 50) -> list:
    params: dict = {'limit': limit}
    if symbol:
        params['symbol'] = symbol
    body = _get('/wfa/history', params=params)
    return body.get('records') or body.get('history') or body.get('data') or []


@st.cache_data(ttl=300)
def get_wfa_summary(symbol: str) -> dict:
    return _get('/wfa/summary', params={'symbol': symbol})

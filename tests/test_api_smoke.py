"""
tests/test_api_smoke.py — 36 个未覆盖端点的冒烟测试 (P1-1)

目标:每个端点至少跑一次,断言:
  - 返回是 200/4xx 中的某个状态码(不允许 500)
  - 响应是合法 JSON 或 Prometheus 文本(/metrics)
  - 必要时 mock 掉外部依赖(akshare / requests / LLM provider 等)

不验证业务正确性,只防止"加新代码改坏旧端点而 CI 不报"。
完整契约测试见 tests/test_api_contract.py(下一步 P2-2 会扩展 schema 校验)。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJ_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_DIR))
sys.path.insert(0, str(PROJ_DIR / 'backend'))


@pytest.fixture(scope='module')
def client():
    """加载 backend/api.py 的 Flask app 并返回 test_client。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'api', str(PROJ_DIR / 'backend' / 'api.py'),
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.app.test_client()


# 允许的状态码(任何端点都不能 5xx)
_OK = lambda r: r.status_code < 500


# ────────────────────────────────────────────────────────
# Analysis 系列
# ────────────────────────────────────────────────────────

def test_analysis_health_get(client):
    r = client.get('/analysis/health')
    assert _OK(r)
    if r.status_code == 200:
        data = r.get_json()
        assert 'level' in data and data['level'] in {'OK', 'WARN', 'CRITICAL'}


def test_analysis_run_post_uses_use_case(client):
    """端点会调 DynamicStockSelector,完整跑会触发网络。
    用 patch 替换 selector 内核,只验证 200 即可。"""
    fake_module = MagicMock()
    selector = MagicMock()
    selector.get_top_bk_sectors.return_value = []
    selector.get_news_summary.return_value = []
    selector.get_stock_with_context.return_value = []
    selector._last_news_source = 'test'
    selector._last_source = 'test'
    fake_module.DynamicStockSelector = MagicMock(return_value=selector)
    with patch.dict(sys.modules, {'dynamic_selector': fake_module}):
        r = client.post('/analysis/run')
    assert _OK(r), r.status_code


def test_analysis_sector_rotation_post_empty_body_handled(client):
    """缺数据时端点应返回结构化错误 (422/503),不应 500。"""
    import core.data_layer as _dl
    fake_dl = MagicMock()
    fake_dl.get_bars.return_value = None
    with patch.object(_dl, 'get_data_layer', return_value=fake_dl):
        r = client.post('/analysis/sector_rotation', json={})
    # 503 是 use case 报 DATA_UNAVAILABLE 的预期映射,允许
    assert r.status_code in (200, 422, 503), f'unexpected: {r.status_code}'


def test_analysis_pairs_trading_rejects_single_symbol(client):
    r = client.post('/analysis/pairs_trading', json={'symbols': ['A.SH']})
    assert r.status_code == 400


def test_analysis_pairs_trading_missing_body(client):
    r = client.post('/analysis/pairs_trading')
    assert r.status_code == 400


def test_analysis_sector_compare_requires_sector_or_symbols(client):
    r = client.post('/analysis/sector/compare', json={})
    assert r.status_code == 422


def test_analysis_monthly_get_default(client):
    """缺 services.performance 时可能 500,但用 patch 替换避免。"""
    with patch('services.performance.generate_monthly_report',
               return_value={'returns': {}, 'summary': {}, 'equity_series': [],
                             'benchmark_curve': [], 'chart_base64': None}):
        r = client.get('/analysis/monthly?year=2026&month=4&include_chart=0')
    assert _OK(r)


def test_analysis_monthly_snapshot_post(client):
    with patch('services.performance.record_monthly_snapshot',
               return_value={'year': 2026, 'month': 4}):
        r = client.post('/analysis/monthly/snapshot', json={'year': 2026, 'month': 4})
    assert _OK(r)


def test_analysis_monthly_history_get(client):
    """端点使用 get_monthly_snapshots(非 list_monthly_snapshots)。"""
    with patch('services.performance.get_monthly_snapshots', return_value=[]):
        r = client.get('/analysis/monthly/history')
    assert _OK(r)


# ────────────────────────────────────────────────────────
# Data 系列
# ────────────────────────────────────────────────────────

def test_data_daily_get_unknown_symbol(client):
    """未知 symbol 应返回 404,不应 500 + 不应泄漏 traceback。"""
    r = client.get('/data/daily/NONEXISTENT.SH')
    assert r.status_code == 404, f'expected 404, got {r.status_code}'
    body = r.get_json()
    assert body['status'] == 'error'
    # 错误消息不应含 traceback
    assert 'Traceback' not in body.get('error', '')


def test_data_fund_flow_get(client):
    r = client.get('/data/fund_flow')
    assert _OK(r)


def test_data_macro_get_valid_indicator(client):
    """已知指标。失败时端点应返回 404/500-side gracefully。"""
    r = client.get('/data/macro/PMI')
    assert _OK(r)


def test_data_macro_get_invalid_indicator(client):
    r = client.get('/data/macro/INVALID_INDICATOR_XYZ')
    assert r.status_code == 400


def test_data_news_get(client):
    """新增端点 (P4-2),应能调通,即使无网络数据返回。"""
    with patch('core.factors.nlp._fetch_news_eastmoney', return_value=['headline']):
        r = client.get('/data/news/600519.SH?n=3')
    assert r.status_code == 200
    data = r.get_json()
    assert data['headlines'] == ['headline']


def test_data_realtime_get(client):
    r = client.get('/data/realtime/600519.SH')
    assert _OK(r)


# ────────────────────────────────────────────────────────
# Fundamentals
# ────────────────────────────────────────────────────────

def test_fundamentals_get_unavailable(client):
    with patch('services.fundamentals.fetch_fundamentals', return_value=None):
        r = client.get('/fundamentals/UNKNOWN')
    assert r.status_code == 404


def test_fundamentals_get_with_data(client):
    fake = {'symbol': 'X', 'pe': 12.5, 'pb': 1.2}
    with patch('services.fundamentals.fetch_fundamentals', return_value=fake):
        r = client.get('/fundamentals/X')
    assert r.status_code == 200


# ────────────────────────────────────────────────────────
# LLM
# ────────────────────────────────────────────────────────

def test_llm_analyze_missing_field(client):
    r = client.post('/llm/analyze', json={'symbol': 'X'})
    assert r.status_code == 422


def test_llm_analyze_complete_body(client):
    """提供全部必填字段 + 关掉 provider 路径 → 走 fallback。"""
    with patch('services.llm.service.signal_review') as mock_review:
        result = MagicMock()
        result.approved = True
        result.decision = 'approve'
        result.reason = 'OK'
        result.confidence = 0.7
        result.size_rec = 'full'
        mock_review.return_value = result
        r = client.post('/llm/analyze', json={
            'symbol': 'X', 'direction': 'BUY', 'signal': 'RSI_BUY',
            'price': 10.0, 'alert_reason': 'test',
        })
    assert r.status_code == 200


# ────────────────────────────────────────────────────────
# Market / Metrics / Monitor / Risk
# ────────────────────────────────────────────────────────

def test_market_status_get(client):
    r = client.get('/market/status')
    assert r.status_code == 200
    data = r.get_json()
    assert 'is_open' in data and isinstance(data['is_open'], bool)


def test_metrics_get_returns_text(client):
    r = client.get('/metrics')
    assert _OK(r)
    # Prometheus 格式或错误注释
    assert b'#' in r.data or b'trading_' in r.data


def test_monitor_status_no_monitor_returns_503(client):
    r = client.get('/monitor/status')
    # 单元测试场景没启动 monitor
    assert r.status_code in (200, 503)


def test_northbound_flow_get(client):
    with patch('services.northbound.fetch_kamt', return_value={
        'net_north_cny': 1e9, 'timestamp': '2026-05-15 15:00',
    }), patch('services.northbound.get_north_flow_direction', return_value={
        'direction': '净流入', 'strength': '中等', 'trend_yi': 10, 'reason': '',
    }), patch('services.northbound.get_north_history', return_value={'D1': 1e9}), \
         patch('services.northbound.format_kamt_summary', return_value='summary'):
        r = client.get('/northbound/flow')
    assert r.status_code == 200


def test_performance_summary_get(client):
    with patch('services.performance.generate_monthly_report',
               return_value={'returns': {}, 'summary': {}, 'equity_series': [],
                             'benchmark_curve': [], 'chart_base64': None, 'generated_at': ''}), \
         patch('services.performance.compute_trade_stats', return_value={}), \
         patch('services.performance.compute_max_drawdown', return_value={}):
        r = client.get('/performance/summary?year=2026&month=4&include_chart=0')
    assert r.status_code == 200


def test_risk_status_get(client):
    r = client.get('/risk/status')
    assert r.status_code == 200
    data = r.get_json()
    assert 'total_equity' in data
    assert 'sector_exposure' in data


# ────────────────────────────────────────────────────────
# Orders
# ────────────────────────────────────────────────────────

def test_orders_pending_get(client):
    r = client.get('/orders/pending')
    assert r.status_code == 200


def test_orders_submit_missing_field(client):
    r = client.post('/orders/submit', json={'symbol': 'X'})
    assert r.status_code == 400


def test_orders_submit_invalid_direction(client):
    r = client.post('/orders/submit', json={
        'symbol': 'X', 'direction': 'HOLD', 'shares': 100,
    })
    assert r.status_code == 400


def test_orders_cancel_unknown_id(client):
    r = client.post('/orders/UNKNOWN_ID/cancel')
    assert r.status_code == 404


# ────────────────────────────────────────────────────────
# Params
# ────────────────────────────────────────────────────────

def test_params_get_all(client):
    r = client.get('/params')
    assert r.status_code == 200
    data = r.get_json()
    assert 'params' in data and 'count' in data


def test_params_get_symbol(client):
    r = client.get('/params/600519.SH')
    assert r.status_code == 200


def test_params_patch_no_valid_fields(client):
    r = client.patch('/params/600519.SH', json={'unknown_field': 1})
    assert r.status_code == 422


def test_params_patch_valid_field(client, tmp_path, monkeypatch):
    """提供合法字段 → 200。 ※ 会写入实际 params.json,因此包一层 monkeypatch。"""
    # 让 services.signals.update_symbol_params 走 mock,避免污染真实文件
    with patch('services.signals.update_symbol_params',
               return_value={'rsi_buy': 25}):
        r = client.patch('/params/600519.SH', json={'rsi_buy': 25})
    assert r.status_code == 200


# ────────────────────────────────────────────────────────
# Signals POST / Trades POST / Trading mode PUT / Watchlist DELETE+PATCH
# ────────────────────────────────────────────────────────

def test_signals_post_missing_field(client):
    r = client.post('/signals', json={'symbol': 'X'})
    assert r.status_code == 400


def test_signals_post_complete(client):
    r = client.post('/signals', json={
        'symbol': 'X', 'signal': 'BUY', 'strength': 0.8,
    })
    assert r.status_code == 200


def test_trades_post_missing_field(client):
    r = client.post('/trades', json={'symbol': 'X'})
    assert r.status_code == 400


def test_trading_mode_put_simulation(client):
    r = client.put('/trading/mode', json={'mode': 'simulation'})
    assert _OK(r)


def test_watchlist_patch(client):
    """先 add,再 patch — alert_pct + enabled 同时修改。"""
    client.post('/watchlist/add', json={'symbol': 'TEST_PATCH.SH', 'alert_pct': 5.0})
    r = client.patch('/watchlist/TEST_PATCH.SH',
                     json={'alert_pct': 7.0, 'enabled': 1})
    assert r.status_code == 200
    assert r.get_json().get('status') == 'ok'


def test_watchlist_delete(client):
    client.post('/watchlist/add', json={'symbol': 'TEST_DELETE.SH'})
    r = client.delete('/watchlist/TEST_DELETE.SH')
    assert _OK(r)


# ────────────────────────────────────────────────────────
# WFA
# ────────────────────────────────────────────────────────

def test_wfa_history_get(client):
    """端点在 view function 内做 lazy import,patch 需要先 import 一次模块。"""
    import importlib
    try:
        importlib.import_module('services.wfa_history')
    except Exception:
        pytest.skip('services.wfa_history 不可用')
    with patch('services.wfa_history.get_wfa_history', return_value=[]):
        r = client.get('/wfa/history')
    assert _OK(r)


def test_wfa_summary_get_missing_symbol(client):
    r = client.get('/wfa/summary')
    # 端点对 symbol 必填的处理:可能 400 或 404
    assert r.status_code in (400, 404, 422)

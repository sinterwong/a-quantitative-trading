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


# ────────────────────────────────────────────────────────
# Analysis 系列
# ────────────────────────────────────────────────────────

def test_analysis_health_get(client):
    r = client.get('/analysis/health')
    assert r.status_code == 200
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
    assert r.status_code == 200, r.status_code


def test_analysis_sector_rotation_post_empty_body_handled(client):
    """所有 ETF 行情为空时, use case 抛 DATA_UNAVAILABLE → 端点 503。"""
    import core.data_layer as _dl
    fake_dl = MagicMock()
    fake_dl.get_bars.return_value = None
    with patch.object(_dl, 'get_data_layer', return_value=fake_dl):
        r = client.post('/analysis/sector_rotation', json={})
    assert r.status_code == 503, f'expected 503 (DATA_UNAVAILABLE), got {r.status_code}'
    body = r.get_json()
    assert body['status'] == 'error'


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
    """services.performance 被 patch 后必须 200,不允许 4xx 退化。"""
    with patch('services.performance.generate_monthly_report',
               return_value={'returns': {}, 'summary': {}, 'equity_series': [],
                             'benchmark_curve': [], 'chart_base64': None}):
        r = client.get('/analysis/monthly?year=2026&month=4&include_chart=0')
    assert r.status_code == 200, r.status_code


def test_analysis_monthly_snapshot_post(client):
    with patch('services.performance.record_monthly_snapshot',
               return_value={'year': 2026, 'month': 4}):
        r = client.post('/analysis/monthly/snapshot', json={'year': 2026, 'month': 4})
    assert r.status_code == 200, r.status_code


def test_analysis_monthly_history_get(client):
    """端点使用 get_monthly_snapshots(非 list_monthly_snapshots)。"""
    with patch('services.performance.get_monthly_snapshots', return_value=[]):
        r = client.get('/analysis/monthly/history')
    assert r.status_code == 200, r.status_code


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
    """端点依赖 services.fund_flow + AkShare,CI 无网络/无 akshare 时全 mock。"""
    fake_svc = MagicMock()
    fake_svc.get_market_fund_flow.return_value = {
        'sh_close': 3000, 'sh_change': 0.5,
        'sz_close': 10000, 'sz_change': 0.7,
        'main_net': 1.0, 'main_pct': 0.5,
    }
    fake_module = MagicMock(FundFlowService=MagicMock(return_value=fake_svc))
    with patch.dict(sys.modules, {'services.fund_flow': fake_module}):
        r = client.get('/data/fund_flow')
    assert r.status_code == 200, r.status_code


def test_data_macro_get_valid_indicator(client):
    """已知 PMI 指标:本地有数据 → 200;CI 无网络 → gateway 返回空 → 404。"""
    r = client.get('/data/macro/PMI')
    # 200 = 命中本地缓存;404 = "indicator 存在但当前无数据"(api.py:1380);
    # 503 = gateway 临时不可用。绝不允许 5xx(=> 500/exception),
    # 也不允许除 404 外的 4xx(参数已被 endpoint 校验通过,不是客户端错)。
    assert r.status_code in (200, 404, 503), r.status_code


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
    # 实时行情依赖外网(腾讯/新浪),CI 无网络可能 503。不允许 4xx/500。
    assert r.status_code in (200, 503), r.status_code


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
    """提供全部必填字段 + mock signal_review 服务,避免触发真实 LLM provider。

    端点函数体内 `from services.llm.service import signal_review`,
    所以需要 sys.modules patch(避免 services.llm 子包未 import 时找不到)。
    """
    result = MagicMock()
    result.approved = True
    result.decision = 'approve'
    result.reason = 'OK'
    result.confidence = 0.7
    result.size_rec = 'full'

    fake_service = MagicMock(signal_review=MagicMock(return_value=result))
    with patch.dict(sys.modules, {'services.llm.service': fake_service}):
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
    assert r.status_code == 200
    # Prometheus 格式必须包含注释或 trading_ 前缀指标
    assert b'#' in r.data or b'trading_' in r.data


def test_monitor_status_no_monitor_returns_503(client):
    r = client.get('/monitor/status')
    # 单元测试场景 backend.api 模块导入时没启动 monitor → 必然 503。
    assert r.status_code == 503, r.status_code
    body = r.get_json()
    assert body['status'] == 'error'


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
    assert r.status_code == 200, r.status_code


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
    assert r.status_code == 200, r.status_code


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
    assert r.status_code == 200, r.status_code


def test_wfa_summary_get_missing_symbol(client):
    r = client.get('/wfa/summary')
    # symbol 缺失走输入校验分支 → 422
    assert r.status_code == 422, r.status_code


# ────────────────────────────────────────────────────────
# Backtest / Portfolio compose
# ────────────────────────────────────────────────────────

def test_backtest_run_missing_symbol(client):
    r = client.post('/backtest/run', json={})
    assert r.status_code == 422, r.status_code


def test_backtest_run_no_strategy_returns_422(client):
    r = client.post('/backtest/run', json={'symbol': 'sh600519'})
    assert r.status_code == 422, r.status_code


def test_backtest_run_happy_path(client):
    """patch use case 后必须 200,响应字段 = BacktestResponse.to_dict()。"""
    from core.use_cases.backtest import BacktestResponse
    fake = BacktestResponse(
        symbol='sh600519', n_bars=120, n_trades=5,
        total_return=0.1, annual_return=0.2, sharpe=1.2,
        max_drawdown_pct=0.05, win_rate=0.6, profit_factor=1.5,
        factor_ic=0.02, factor_ir=0.5, summary_text='ok',
    )
    with patch('core.use_cases.backtest.run_backtest', return_value=fake):
        r = client.post('/backtest/run', json={
            'symbol': 'sh600519',
            'strategies': [{'factor_name': 'RSI'}],
        })
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body['sharpe'] == 1.2
    assert body['symbol'] == 'sh600519'


def test_portfolio_compose_too_few_assets_returns_422(client):
    r = client.post('/portfolio/compose', json={'universe': ['600519.SH']})
    assert r.status_code == 422, r.status_code


def test_portfolio_compose_invalid_method_returns_422(client):
    r = client.post(
        '/portfolio/compose',
        json={'universe': ['A.SH', 'B.SZ'], 'method': 'bogus'},
    )
    assert r.status_code == 422, r.status_code


def test_portfolio_compose_happy_path(client):
    """patch use case 后必须 200,响应 = PortfolioAdvice.to_dict()。"""
    from core.use_cases.compose_portfolio import PortfolioAdvice
    fake = PortfolioAdvice(
        method='min_variance', weights={'A.SH': 0.5, 'B.SZ': 0.5},
        n_assets=2, expected_return=0.08, expected_vol=0.12, sharpe=0.5,
    )
    with patch(
        'core.use_cases.compose_portfolio.compose_portfolio',
        return_value=fake,
    ):
        r = client.post(
            '/portfolio/compose',
            json={'universe': ['A.SH', 'B.SZ'], 'method': 'min_variance'},
        )
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body['method'] == 'min_variance'
    assert body['weights'] == {'A.SH': 0.5, 'B.SZ': 0.5}


def test_backtest_run_data_unavailable_returns_503(client):
    """DATA_UNAVAILABLE 必须映射到 503(不是 422),否则 UI 会把降级误判为请求拒绝。"""
    from core.use_cases import UseCaseError
    with patch(
        'core.use_cases.backtest.run_backtest',
        side_effect=UseCaseError('no kline', code='DATA_UNAVAILABLE'),
    ):
        r = client.post('/backtest/run', json={
            'symbol': 'sh600519',
            'strategies': [{'factor_name': 'RSI'}],
        })
    assert r.status_code == 503, r.status_code


def test_backtest_run_unknown_factor_returns_422(client):
    """UNKNOWN_FACTOR(用户填错因子名)必须 422,不能 503。"""
    from core.use_cases import UseCaseError
    with patch(
        'core.use_cases.backtest.run_backtest',
        side_effect=UseCaseError('unknown factor', code='UNKNOWN_FACTOR'),
    ):
        r = client.post('/backtest/run', json={
            'symbol': 'sh600519',
            'strategies': [{'factor_name': 'NotARealFactor'}],
        })
    assert r.status_code == 422, r.status_code


def test_portfolio_compose_data_unavailable_returns_503(client):
    """compose 端点的 DATA_UNAVAILABLE 也要 503,保证 UI 一致语义。"""
    from core.use_cases import UseCaseError
    with patch(
        'core.use_cases.compose_portfolio.compose_portfolio',
        side_effect=UseCaseError('insufficient data', code='DATA_UNAVAILABLE'),
    ):
        r = client.post('/portfolio/compose', json={
            'universe': ['A.SH', 'B.SZ'], 'method': 'min_variance',
        })
    assert r.status_code == 503, r.status_code

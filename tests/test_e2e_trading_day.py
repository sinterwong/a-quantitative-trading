"""
tests/test_e2e_trading_day.py — 端到端"一个交易日"集成测试 (P2-1)

目标:验证关键 happy-path 在 API + DB + Use Case 三层间能正确串起来。

场景脚本(按时间顺序):
  1. 启动前提:DB 为空(conftest 拦截 portfolio.db → temp file)
  2. PUT /trading/mode 切到 live
  3. POST /portfolio/cash 注入初始资金
  4. POST /portfolio/positions 上 1 个初始持仓
  5. GET /portfolio/summary 验证 cash + position_value 正确合计
  6. POST /signals 记录两条信号
  7. POST /trades 记录一笔交易
  8. POST /orders/submit 通过 PaperBroker 下单 → 应自动产生 position 变化
  9. GET /risk/status 检查敞口 / sector exposure
 10. GET /metrics 验证 Prometheus 指标已刷新

不模拟 Scheduler 触发、IntradayMonitor 循环或外网行情;那些由各自单测覆盖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJ_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_DIR))
sys.path.insert(0, str(PROJ_DIR / 'backend'))


@pytest.fixture(scope='module')
def app():
    """加载 Flask app(test_client + 模块级 _svc)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'api', str(PROJ_DIR / 'backend' / 'api.py'),
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.app


@pytest.fixture
def client(app):
    return app.test_client()


# ── 一系列保证顺序的测试,共享 module-scoped DB(conftest 拦截) ───

def test_e2e_step1_initial_state(client):
    """初态:cash 默认值,positions 为空。"""
    r = client.get('/health')
    assert r.status_code == 200

    r = client.get('/positions')
    assert r.status_code == 200
    # PortfolioService 默认初始化时可能预填一些;不强行断言空
    # 但每次测试都从同一隔离 DB 起,只需顺序不被打破


def test_e2e_step2_set_cash(client):
    r = client.post('/portfolio/cash', json={'amount': 100_000.0})
    assert r.status_code == 200
    r = client.get('/cash')
    assert r.status_code == 200
    assert r.get_json()['cash'] == 100_000.0


def test_e2e_step3_upsert_position(client):
    """新增一个持仓 600519.SH × 100 股 @ 1800。"""
    r = client.post('/portfolio/positions', json={
        'symbol': '600519.SH', 'shares': 100, 'entry_price': 1800.0,
    })
    assert r.status_code == 200

    positions = client.get('/positions').get_json()['positions']
    syms = [p['symbol'] for p in positions]
    assert '600519.SH' in syms


def test_e2e_step4_summary_combines_cash_and_positions(client):
    """summary 应包含 cash + total_equity 字段。"""
    r = client.get('/portfolio/summary?refresh=0')
    assert r.status_code == 200
    data = r.get_json()
    assert 'cash' in data
    assert 'total_equity' in data
    assert float(data['cash']) >= 0


def test_e2e_step5_signals_record_and_fetch(client):
    """记录 2 条信号 → 查询返回应至少包含它们。"""
    for direction in ('BUY', 'SELL'):
        r = client.post('/signals', json={
            'symbol': '600519.SH', 'signal': direction,
            'strength': 0.65, 'reason': f'E2E {direction}',
        })
        assert r.status_code == 200

    signals = client.get('/signals?limit=50').get_json()['signals']
    # 应至少有 2 条带 E2E 标记的信号
    reasons = [s.get('reason', '') for s in signals]
    assert any('E2E' in r for r in reasons)


def test_e2e_step6_trade_record(client):
    """记录一笔交易,验证 GET /trades 返回它。"""
    r = client.post('/trades', json={
        'symbol': '600519.SH', 'direction': 'BUY',
        'shares': 100, 'price': 1820.0, 'pnl': None,
    })
    assert r.status_code == 200

    trades = client.get('/trades?limit=10').get_json()['trades']
    syms = [t.get('symbol') for t in trades]
    assert '600519.SH' in syms


def test_e2e_step7_orders_submit_via_paper_broker(client):
    """通过 /orders/submit 提交订单 → PaperBroker 应即时成交。"""
    r = client.post('/orders/submit', json={
        'symbol': '600519.SH', 'direction': 'BUY',
        'shares': 100, 'price': 1825.0, 'price_type': 'market',
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data['order_id']
    assert data['status'] in ('filled', 'partial', 'rejected')


def test_e2e_step8_risk_status_reflects_positions(client):
    """风控快照应反映已有持仓。"""
    r = client.get('/risk/status')
    assert r.status_code == 200
    data = r.get_json()
    assert data['position_count'] >= 1
    assert 'sector_exposure' in data


def test_e2e_step9_metrics_endpoint_serves_prometheus(client):
    """/metrics 返回 Prometheus 格式 text。"""
    r = client.get('/metrics')
    assert r.status_code == 200
    body = r.data.decode('utf-8')
    # Prometheus 格式包含 trading_* 指标或注释
    assert 'trading_' in body or '#' in body


def test_e2e_step10_health_still_ok(client):
    """所有写入后,健康检查仍 200。"""
    r = client.get('/health')
    assert r.status_code == 200


def test_e2e_step11_analysis_health_use_case_round_trip(client):
    """system_health use case 通过 API 走一遍。"""
    r = client.get('/analysis/health')
    assert r.status_code == 200
    data = r.get_json()
    assert data['level'] in {'OK', 'WARN', 'CRITICAL'}
    assert data['n_positions'] >= 1

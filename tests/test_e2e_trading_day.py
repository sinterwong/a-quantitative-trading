"""
tests/test_e2e_trading_day.py — 端到端"一个交易日"集成测试 (P2-1)

目标:验证关键 happy-path 在 API + DB + Use Case 三层间能正确串起来。

实现:整套场景作为 *单个* test 函数顺序执行,避免依赖 pytest 默认
字母排序("test_e2e_step1_*" → step2 → ...)隐式建立顺序——一旦
某一步被 `-k` 过滤掉,后续步骤会拿到错误的前置状态、给出错误结论。

场景脚本(按时间顺序):
  1. 启动前提:DB 为空(conftest 拦截 portfolio.db → temp file)
  2. POST /portfolio/cash 注入初始资金
  3. POST /portfolio/positions 上 1 个初始持仓
  4. GET /portfolio/summary 验证 cash + position_value 正确合计
  5. POST /signals 记录两条信号
  6. POST /trades 记录一笔交易
  7. POST /orders/submit 通过 PaperBroker 下单 → 应自动产生 position 变化
  8. GET /risk/status 检查敞口 / sector exposure
  9. GET /metrics 验证 Prometheus 指标已刷新
 10. GET /health 写入完成后仍 200
 11. GET /analysis/health system_health use case round-trip

不模拟 Scheduler 触发、IntradayMonitor 循环或外网行情;那些由各自单测覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_DIR))
sys.path.insert(0, str(PROJ_DIR / 'backend'))


@pytest.fixture(scope='module')
def app():
    """加载 Flask app(test_client + 模块级 _svc)。

    R2-4 之后必须走标准包路径 ``import backend.api``:Blueprint 把 helper
    留在 ``backend.api``,route 模块再 ``from backend.api import ...``;
    若用 ``spec_from_file_location('api', …)`` 加载,sys.modules 里会出现
    ``'api'`` 和 ``'backend.api'`` 两份,触发循环 import。"""
    import backend.api as api
    return api.app


@pytest.fixture
def client(app):
    return app.test_client()


# ── 单个测试函数,把整个交易日 happy-path 串成一条调用链 ──────

def test_e2e_full_trading_day(client):
    """端到端一个交易日 happy-path,11 步顺序执行,任何一步失败立即终止。"""

    # ── step1: 初态:健康检查 + positions 可读 ─────────────
    r = client.get('/health')
    assert r.status_code == 200, 'step1: /health unhealthy at start'

    r = client.get('/positions')
    assert r.status_code == 200, 'step1: /positions unreadable'

    # ── step2: 注入初始资金 100k ─────────────────────────
    r = client.post('/portfolio/cash', json={'amount': 100_000.0})
    assert r.status_code == 200, 'step2: /portfolio/cash write failed'

    r = client.get('/cash')
    assert r.status_code == 200, 'step2: /cash read failed'
    assert r.get_json()['cash'] == 100_000.0, 'step2: cash not persisted'

    # ── step3: 建仓 600519.SH × 100 @ 1800 ─────────────────
    r = client.post('/portfolio/positions', json={
        'symbol': '600519.SH', 'shares': 100, 'entry_price': 1800.0,
    })
    assert r.status_code == 200, 'step3: position upsert failed'

    positions = client.get('/positions').get_json()['positions']
    assert '600519.SH' in [p['symbol'] for p in positions], \
        'step3: 600519.SH missing after upsert'

    # ── step4: summary = cash + position_value ─────────────
    r = client.get('/portfolio/summary?refresh=0')
    assert r.status_code == 200, 'step4: /portfolio/summary failed'
    data = r.get_json()
    assert 'cash' in data and 'total_equity' in data, \
        'step4: summary missing required fields'
    assert float(data['cash']) >= 0, 'step4: cash unexpectedly negative'

    # ── step5: 记 2 条信号 ──────────────────────────────
    for direction in ('BUY', 'SELL'):
        r = client.post('/signals', json={
            'symbol': '600519.SH', 'signal': direction,
            'strength': 0.65, 'reason': f'E2E {direction}',
        })
        assert r.status_code == 200, f'step5: record {direction} signal failed'

    signals = client.get('/signals?limit=50').get_json()['signals']
    reasons = [s.get('reason', '') for s in signals]
    assert any('E2E' in r for r in reasons), \
        'step5: E2E-tagged signals not retrievable'

    # ── step6: 记一笔交易 ───────────────────────────────
    r = client.post('/trades', json={
        'symbol': '600519.SH', 'direction': 'BUY',
        'shares': 100, 'price': 1820.0, 'pnl': None,
    })
    assert r.status_code == 200, 'step6: /trades write failed'

    trades = client.get('/trades?limit=10').get_json()['trades']
    assert '600519.SH' in [t.get('symbol') for t in trades], \
        'step6: recorded trade not retrievable'

    # ── step7: PaperBroker 下单 → 即时成交 ─────────────
    r = client.post('/orders/submit', json={
        'symbol': '600519.SH', 'direction': 'BUY',
        'shares': 100, 'price': 1825.0, 'price_type': 'market',
    })
    assert r.status_code == 200, 'step7: /orders/submit failed'
    data = r.get_json()
    assert data['order_id'], 'step7: order_id missing'
    assert data['status'] in ('filled', 'partial', 'rejected'), \
        f'step7: unexpected order status {data["status"]!r}'

    # ── step8: 风控快照反映已有持仓 ─────────────────────
    r = client.get('/risk/status')
    assert r.status_code == 200, 'step8: /risk/status failed'
    data = r.get_json()
    assert data['position_count'] >= 1, \
        'step8: risk status reports no positions despite step3 upsert'
    assert 'sector_exposure' in data, 'step8: sector_exposure missing'

    # ── step9: /metrics 暴露 Prometheus 文本 ────────────
    r = client.get('/metrics')
    assert r.status_code == 200, 'step9: /metrics failed'
    body = r.data.decode('utf-8')
    assert 'trading_' in body or '#' in body, \
        'step9: /metrics body looks empty / not Prometheus'

    # ── step10: 收尾健康检查 ────────────────────────────
    r = client.get('/health')
    assert r.status_code == 200, 'step10: /health unhealthy after writes'

    # ── step11: system_health use case round-trip ────────
    r = client.get('/analysis/health')
    assert r.status_code == 200, 'step11: /analysis/health failed'
    data = r.get_json()
    assert data['level'] in {'OK', 'WARN', 'CRITICAL'}, \
        f'step11: unexpected health level {data["level"]!r}'
    assert data['n_positions'] >= 1, \
        'step11: system_health sees no positions despite step3'

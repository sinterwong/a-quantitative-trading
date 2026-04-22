"""
tests/test_api.py — Flask test-client tests for backend/api.py

Covers every endpoint the Streamlit UI calls, plus all other core endpoints.
Uses an in-memory SQLite DB via PortfolioService so no disk state leaks
between tests.

Run:  pytest tests/test_api.py -v
"""

import json
import os
import sys
import tempfile
import pytest

# ── path setup ──────────────────────────────────────────────────────────────
THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
BACKEND = os.path.join(ROOT, 'backend')
sys.path.insert(0, ROOT)
sys.path.insert(0, BACKEND)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def app(tmp_path):
    """Create Flask test app with isolated SQLite DB and temp mode file."""
    import importlib
    import backend.api as api_mod          # noqa: import for reload side-effect

    # Fresh PortfolioService backed by temp SQLite
    from services.portfolio import PortfolioService
    db_path = str(tmp_path / 'portfolio.db')
    svc = PortfolioService(db_path=db_path)
    svc.set_cash(500_000.0)               # seed cash

    # Patch singleton + mode file
    api_mod._svc = svc
    api_mod._MODE_FILE = str(tmp_path / 'trading_mode.json')

    api_mod.app.config['TESTING'] = True
    with api_mod.app.test_client() as client:
        yield client

    # cleanup
    api_mod._svc = None


# ── Helper ───────────────────────────────────────────────────────────────────

def jpost(client, url, body):
    return client.post(url, data=json.dumps(body),
                       content_type='application/json')


def jput(client, url, body):
    return client.put(url, data=json.dumps(body),
                      content_type='application/json')


# ============================================================
# /health
# ============================================================

class TestHealth:
    def test_health_ok(self, app):
        r = app.get('/health')
        assert r.status_code == 200
        d = r.get_json()
        assert d['status'] == 'ok'
        assert 'cash' in d

    def test_health_has_timestamp(self, app):
        d = app.get('/health').get_json()
        assert 'timestamp' in d


# ============================================================
# /trading/mode  (UI: 开启实盘交易 button)
# ============================================================

class TestTradingMode:
    """These two endpoints were the root cause of the live-trading button error."""

    def test_get_default_mode_is_simulation(self, app):
        r = app.get('/trading/mode')
        assert r.status_code == 200
        d = r.get_json()
        assert d['status'] == 'ok'
        assert d['mode'] == 'simulation'

    def test_set_mode_to_live(self, app):
        r = jput(app, '/trading/mode', {'mode': 'live'})
        assert r.status_code == 200
        d = r.get_json()
        assert d['mode'] == 'live'

    def test_mode_persists_across_requests(self, app):
        jput(app, '/trading/mode', {'mode': 'live'})
        r = app.get('/trading/mode')
        assert r.get_json()['mode'] == 'live'

    def test_revert_mode_to_simulation(self, app):
        jput(app, '/trading/mode', {'mode': 'live'})
        jput(app, '/trading/mode', {'mode': 'simulation'})
        assert app.get('/trading/mode').get_json()['mode'] == 'simulation'

    def test_invalid_mode_returns_422(self, app):
        r = jput(app, '/trading/mode', {'mode': 'turbo'})
        assert r.status_code == 422

    def test_missing_mode_field_returns_422(self, app):
        r = jput(app, '/trading/mode', {})
        assert r.status_code == 422

    def test_put_without_json_returns_415(self, app):
        r = app.put('/trading/mode', data='mode=live')
        assert r.status_code == 415

    def test_get_mode_response_shape(self, app):
        d = app.get('/trading/mode').get_json()
        assert set(d.keys()) >= {'status', 'mode', 'timestamp'}


# ============================================================
# /positions
# ============================================================

class TestPositions:
    def test_get_positions_empty(self, app):
        r = app.get('/positions')
        assert r.status_code == 200
        d = r.get_json()
        assert d['status'] == 'ok'
        assert isinstance(d['positions'], list)

    def test_upsert_position(self, app):
        r = jpost(app, '/portfolio/positions',
                  {'symbol': '600519', 'shares': 100, 'entry_price': 1800.0})
        assert r.status_code == 200
        assert r.get_json()['status'] == 'ok'

    def test_upsert_then_list(self, app):
        jpost(app, '/portfolio/positions',
              {'symbol': '000001', 'shares': 200, 'entry_price': 15.0})
        positions = app.get('/positions').get_json()['positions']
        symbols = [p['symbol'] for p in positions]
        assert '000001' in symbols

    def test_upsert_missing_symbol_returns_400(self, app):
        r = jpost(app, '/portfolio/positions',
                  {'shares': 100, 'entry_price': 10.0})
        assert r.status_code == 400

    def test_upsert_missing_shares_returns_400(self, app):
        r = jpost(app, '/portfolio/positions',
                  {'symbol': '000001', 'entry_price': 10.0})
        assert r.status_code == 400

    def test_upsert_without_json_returns_415(self, app):
        r = app.post('/portfolio/positions', data='symbol=600519')
        assert r.status_code == 415


# ============================================================
# /cash  &  /portfolio/cash
# ============================================================

class TestCash:
    def test_get_cash(self, app):
        r = app.get('/cash')
        assert r.status_code == 200
        d = r.get_json()
        assert d['status'] == 'ok'
        assert 'cash' in d
        assert isinstance(d['cash'], (int, float))

    def test_set_cash(self, app):
        r = jpost(app, '/portfolio/cash', {'amount': 999_999.0})
        assert r.status_code == 200
        assert app.get('/cash').get_json()['cash'] == pytest.approx(999_999.0)

    def test_set_cash_missing_amount_returns_400(self, app):
        r = jpost(app, '/portfolio/cash', {})
        assert r.status_code == 400

    def test_set_cash_without_json_returns_415(self, app):
        r = app.post('/portfolio/cash', data='amount=1000')
        assert r.status_code == 415


# ============================================================
# /trades
# ============================================================

class TestTrades:
    def test_get_trades_empty(self, app):
        r = app.get('/trades')
        assert r.status_code == 200
        d = r.get_json()
        assert isinstance(d['trades'], list)

    def test_record_trade(self, app):
        r = jpost(app, '/trades',
                  {'symbol': '600519', 'direction': 'BUY',
                   'shares': 100, 'price': 1800.0})
        assert r.status_code == 200
        assert 'trade_id' in r.get_json()

    def test_record_trade_with_pnl(self, app):
        r = jpost(app, '/trades',
                  {'symbol': '600519', 'direction': 'SELL',
                   'shares': 100, 'price': 1900.0, 'pnl': 10000.0})
        assert r.status_code == 200

    def test_record_trade_missing_symbol(self, app):
        r = jpost(app, '/trades',
                  {'direction': 'BUY', 'shares': 100, 'price': 10.0})
        assert r.status_code == 400

    def test_record_trade_missing_direction(self, app):
        r = jpost(app, '/trades',
                  {'symbol': '000001', 'shares': 100, 'price': 10.0})
        assert r.status_code == 400

    def test_trades_limit_param(self, app):
        for i in range(5):
            jpost(app, '/trades',
                  {'symbol': '000001', 'direction': 'BUY',
                   'shares': 100, 'price': float(10 + i)})
        r = app.get('/trades?limit=3')
        assert len(r.get_json()['trades']) <= 3

    def test_trades_symbol_filter(self, app):
        jpost(app, '/trades',
              {'symbol': 'AAAA', 'direction': 'BUY', 'shares': 100, 'price': 5.0})
        jpost(app, '/trades',
              {'symbol': 'BBBB', 'direction': 'BUY', 'shares': 100, 'price': 5.0})
        trades = app.get('/trades?symbol=AAAA').get_json()['trades']
        assert all(t['symbol'] == 'AAAA' for t in trades)

    def test_trades_without_json_returns_415(self, app):
        r = app.post('/trades', data='symbol=600519')
        assert r.status_code == 415


# ============================================================
# /signals
# ============================================================

class TestSignals:
    def test_get_signals_empty(self, app):
        r = app.get('/signals')
        assert r.status_code == 200
        assert isinstance(r.get_json()['signals'], list)

    def test_record_signal(self, app):
        r = jpost(app, '/signals',
                  {'symbol': '600519', 'signal': 'BUY',
                   'strength': 0.8, 'reason': 'momentum'})
        assert r.status_code == 200

    def test_record_signal_missing_symbol(self, app):
        r = jpost(app, '/signals', {'signal': 'BUY'})
        assert r.status_code == 400

    def test_record_signal_missing_signal(self, app):
        r = jpost(app, '/signals', {'symbol': '000001'})
        assert r.status_code == 400

    def test_signals_limit_param(self, app):
        for i in range(5):
            jpost(app, '/signals',
                  {'symbol': str(i), 'signal': 'BUY', 'strength': 0.5})
        r = app.get('/signals?limit=2')
        assert len(r.get_json()['signals']) <= 2


# ============================================================
# /portfolio/summary
# ============================================================

class TestPortfolioSummary:
    def test_summary_has_required_fields(self, app):
        r = app.get('/portfolio/summary')
        assert r.status_code == 200
        d = r.get_json()
        assert d['status'] == 'ok'
        # Fields the Streamlit UI reads via .get(...)
        for field in ('cash', 'total_equity'):
            assert field in d, f"missing field: {field}"

    def test_summary_cash_matches_set(self, app):
        jpost(app, '/portfolio/cash', {'amount': 123_456.0})
        d = app.get('/portfolio/summary').get_json()
        assert d['cash'] == pytest.approx(123_456.0)

    def test_summary_status_ok(self, app):
        assert app.get('/portfolio/summary').get_json()['status'] == 'ok'


# ============================================================
# /portfolio/daily
# ============================================================

class TestPortfolioDaily:
    def test_get_daily_empty(self, app):
        r = app.get('/portfolio/daily')
        assert r.status_code == 200
        assert isinstance(r.get_json()['daily'], list)

    def test_record_daily(self, app):
        r = jpost(app, '/portfolio/daily',
                  {'equity': 500_000, 'cash': 200_000,
                   'market_value': 300_000, 'nav': 1.0, 'notes': 'test'})
        assert r.status_code == 200

    def test_daily_limit_param(self, app):
        for _ in range(5):
            jpost(app, '/portfolio/daily',
                  {'equity': 500_000, 'cash': 200_000})
        r = app.get('/portfolio/daily?limit=2')
        assert len(r.get_json()['daily']) <= 2


# ============================================================
# /orders/recent
# ============================================================

class TestOrdersRecent:
    def test_recent_orders_shape(self, app):
        r = app.get('/orders/recent')
        assert r.status_code == 200
        d = r.get_json()
        assert 'orders' in d
        assert 'realized_pnl' in d


# ============================================================
# /analysis/status
# ============================================================

class TestAnalysisStatus:
    def test_status_no_run(self, app):
        r = app.get('/analysis/status')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'ok'


# ============================================================
# /watchlist
# ============================================================

class TestWatchlist:
    def test_get_watchlist_empty(self, app):
        r = app.get('/watchlist')
        assert r.status_code == 200
        d = r.get_json()
        assert isinstance(d.get('watchlist', []), list)

    def test_add_to_watchlist(self, app):
        r = jpost(app, '/watchlist/add', {'symbol': '600519', 'name': '茅台'})
        assert r.status_code == 200

    def test_add_then_get(self, app):
        jpost(app, '/watchlist/add', {'symbol': '600519', 'name': '茅台'})
        wl = app.get('/watchlist').get_json().get('watchlist', [])
        syms = [w['symbol'] for w in wl]
        assert '600519' in syms

    def test_add_missing_symbol(self, app):
        r = jpost(app, '/watchlist/add', {'name': '茅台'})
        # should return 400 or 422
        assert r.status_code in (400, 422)

    def test_delete_from_watchlist(self, app):
        jpost(app, '/watchlist/add', {'symbol': '600519', 'name': '茅台'})
        r = app.delete('/watchlist/600519')
        assert r.status_code == 200


# ============================================================
# /alerts/history
# ============================================================

class TestAlertsHistory:
    def test_alerts_history_empty(self, app):
        r = app.get('/alerts/history')
        assert r.status_code == 200
        d = r.get_json()
        assert 'alerts' in d
        assert isinstance(d['alerts'], list)

    def test_alerts_history_limit(self, app):
        r = app.get('/alerts/history?limit=5')
        assert r.status_code == 200

    def test_alerts_clear(self, app):
        r = jpost(app, '/alerts/clear', {})
        assert r.status_code == 200


# ============================================================
# /data/status
# ============================================================

class TestDataStatus:
    def test_data_status(self, app):
        r = app.get('/data/status')
        assert r.status_code == 200
        body = r.get_json()
        # /data/status returns {fetchers, status: [...circuit-breakers...], timestamp}
        assert 'fetchers' in body or 'status' in body


# ============================================================
# Error handling
# ============================================================

class TestErrorHandling:
    def test_404_returns_json(self, app):
        r = app.get('/nonexistent_endpoint_xyz')
        assert r.status_code == 404
        d = r.get_json()
        assert d['status'] == 'error'

    def test_response_always_has_timestamp(self, app):
        for url in ['/health', '/positions', '/cash', '/trades',
                    '/signals', '/portfolio/summary', '/portfolio/daily',
                    '/trading/mode']:
            d = app.get(url).get_json()
            assert 'timestamp' in d, f"{url} missing timestamp"

    def test_error_response_shape(self, app):
        r = jput(app, '/trading/mode', {'mode': 'invalid'})
        d = r.get_json()
        assert 'status' in d
        assert d['status'] == 'error'
        assert 'error' in d
        assert 'timestamp' in d

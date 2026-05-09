"""
tests/test_api_auth_ratelimit.py — P2-20 HTTP API 认证 + 限流测试

覆盖：
  - TRADING_API_KEY 未设置 → 所有请求放行（dev 默认）
  - 设置后 → 缺少 X-API-Key 头返回 401
  - 设置后 → 错误 X-API-Key 返回 401
  - 设置后 → 正确 X-API-Key 返回 200
  - 公共端点 /health /docs /metrics 始终免认证
  - 全局限流：超过 TRADING_RL_PER_MIN 返回 429
  - TRADING_RL_PER_MIN=0 关闭限流
"""

from __future__ import annotations

import json
import os
import sys
import pytest

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
BACKEND = os.path.join(ROOT, 'backend')
sys.path.insert(0, ROOT)
sys.path.insert(0, BACKEND)


REMOTE_NON_LOCAL = '203.0.113.42'   # TEST-NET-3，可放心当作"非本地"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """构造一个隔离的 Flask test client。

    注意：默认强制把请求来源伪装成非 loopback，这样 auth/限流逻辑会真实执行；
    需要 loopback 行为的测试可显式 GET(..., environ_base={'REMOTE_ADDR':'127.0.0.1'})。
    """
    monkeypatch.delenv('TRADING_API_KEY', raising=False)
    monkeypatch.delenv('TRADING_RL_PER_MIN', raising=False)
    monkeypatch.delenv('TRADING_API_REQUIRE_LOCALHOST', raising=False)

    import backend.api as api_mod
    from services.portfolio import PortfolioService
    db_path = str(tmp_path / 'portfolio.db')
    svc = PortfolioService(db_path=db_path)
    svc.set_cash(100_000.0)
    api_mod._svc = svc
    api_mod._GLOBAL_RATE_LIMIT.clear()
    api_mod.app.config['TESTING'] = True

    test_client = api_mod.app.test_client()
    # 默认所有请求伪装成外网来源，避免 loopback 自动豁免遮蔽测试
    test_client.environ_base['REMOTE_ADDR'] = REMOTE_NON_LOCAL

    yield test_client

    api_mod._svc = None
    api_mod._GLOBAL_RATE_LIMIT.clear()


class TestNoAuthByDefault:

    def test_health_passes_without_key(self, app):
        r = app.get('/health')
        assert r.status_code == 200

    def test_positions_passes_without_key_when_unset(self, app):
        r = app.get('/positions')
        assert r.status_code == 200


class TestApiKeyEnforced:

    def test_missing_key_returns_401(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'secret-abc')
        r = app.get('/positions')
        assert r.status_code == 401
        body = r.get_json()
        assert body['status'] == 'error'
        assert 'X-API-Key' in body['error']

    def test_wrong_key_returns_401(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'secret-abc')
        r = app.get('/positions', headers={'X-API-Key': 'wrong'})
        assert r.status_code == 401

    def test_correct_key_passes(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'secret-abc')
        r = app.get('/positions', headers={'X-API-Key': 'secret-abc'})
        assert r.status_code == 200

    def test_post_also_protected(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.post(
            '/portfolio/cash',
            data=json.dumps({'cash': 1.0}),
            content_type='application/json',
        )
        assert r.status_code == 401


class TestLoopbackBypass:
    """127.0.0.1 / ::1 / localhost 默认免认证，可被 env 覆盖。"""

    def test_loopback_skips_api_key(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.get('/positions', environ_base={'REMOTE_ADDR': '127.0.0.1'})
        assert r.status_code == 200

    def test_ipv6_loopback_skips_api_key(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.get('/positions', environ_base={'REMOTE_ADDR': '::1'})
        assert r.status_code == 200

    def test_external_ip_still_required(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.get('/positions',
                    environ_base={'REMOTE_ADDR': '198.51.100.7'})
        assert r.status_code == 401

    def test_require_localhost_env_disables_bypass(self, app, monkeypatch):
        """TRADING_API_REQUIRE_LOCALHOST=1 → loopback 也必须带 key。"""
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        monkeypatch.setenv('TRADING_API_REQUIRE_LOCALHOST', '1')
        r = app.get('/positions', environ_base={'REMOTE_ADDR': '127.0.0.1'})
        assert r.status_code == 401


class TestPublicPaths:
    """/health /docs /metrics 即使设了 API key 也免认证。"""

    def test_health_bypass_auth(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.get('/health')
        assert r.status_code == 200

    def test_docs_bypass_auth(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_API_KEY', 'sk')
        r = app.get('/docs')
        # /docs 200 或 404 都接受（视实现），重点是不被 401 拦截
        assert r.status_code != 401


class TestGlobalRateLimit:

    def test_exceeds_limit_returns_429(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_RL_PER_MIN', '5')

        # 关键：每次 before_request 重新读取 env，所以这里 5 次成功 + 第 6 次被拒
        for _ in range(5):
            r = app.get('/positions')
            assert r.status_code == 200

        r = app.get('/positions')
        assert r.status_code == 429
        body = r.get_json()
        assert body['code'] == 429

    def test_disabled_when_zero(self, app, monkeypatch):
        monkeypatch.setenv('TRADING_RL_PER_MIN', '0')
        # 即使 50 次也不应触发 429（无限流）
        for _ in range(20):
            r = app.get('/positions')
            assert r.status_code == 200

    def test_health_does_not_consume_quota(self, app, monkeypatch):
        """公共路径不应占用全局限流配额。"""
        monkeypatch.setenv('TRADING_RL_PER_MIN', '3')
        for _ in range(10):
            r = app.get('/health')
            assert r.status_code == 200
        # /positions 仍有完整 3 次配额
        for _ in range(3):
            r = app.get('/positions')
            assert r.status_code == 200
        r = app.get('/positions')
        assert r.status_code == 429


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))

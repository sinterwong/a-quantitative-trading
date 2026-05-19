"""R0-1: Order submission idempotency.

Verifies:
- core/idempotency.py: put/get/conflict/expiry semantics
- /orders/submit endpoint: same Idempotency-Key replays prior response
- /orders/submit endpoint: same key + different body → 422
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from freezegun import freeze_time

from core.idempotency import (
    IdempotencyKeyConflict, IdempotencyStore, compute_request_hash,
)


class TestIdempotencyStore(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix='idem_test_')
        self.db_path = os.path.join(self.tmpdir, 'idem.db')
        self.store = IdempotencyStore(db_path=self.db_path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_returns_none_for_unknown_key(self) -> None:
        self.assertIsNone(self.store.get('does-not-exist'))

    def test_put_then_get_round_trips_response(self) -> None:
        payload = {'symbol': '600000', 'shares': 100}
        request_hash = compute_request_hash(payload)
        self.store.put('key-1', request_hash, {'order_id': 'O1', 'status': 'ok'})

        result = self.store.get('key-1')
        self.assertIsNotNone(result)
        self.assertEqual(result.response, {'order_id': 'O1', 'status': 'ok'})
        self.assertEqual(result.request_hash, request_hash)

    def test_same_key_same_hash_is_noop(self) -> None:
        payload = {'a': 1}
        request_hash = compute_request_hash(payload)
        self.store.put('k', request_hash, {'order_id': 'O1'})
        # 第二次同 key + 同 hash 应当幂等不抛
        self.store.put('k', request_hash, {'order_id': 'WILL_BE_IGNORED'})
        # 取回的仍是第一次的响应
        result = self.store.get('k')
        self.assertEqual(result.response['order_id'], 'O1')

    def test_same_key_different_hash_raises_conflict(self) -> None:
        self.store.put('k', compute_request_hash({'a': 1}), {'order_id': 'O1'})
        with self.assertRaises(IdempotencyKeyConflict):
            self.store.put('k', compute_request_hash({'a': 2}), {'order_id': 'O2'})

    def test_expired_entry_is_not_returned(self) -> None:
        """R3-3: 用 freeze_time 替代 time.sleep+短 TTL hack。
        旧实现 sleep(0.1) 在 CI 慢机器上脆弱，且需要改 module 常量。"""
        with freeze_time('2026-05-19 10:00:00') as frozen:
            self.store.put('k', compute_request_hash({'a': 1}), {'order_id': 'O1'})
            # TTL 是 24h，向前跳 24h+1s 应该过期
            frozen.tick(delta=24 * 3600 + 1)
            self.assertIsNone(self.store.get('k'))

    def test_entry_still_valid_within_ttl(self) -> None:
        """对照：TTL 窗口内同 key 仍可回放。"""
        with freeze_time('2026-05-19 10:00:00') as frozen:
            self.store.put('k', compute_request_hash({'a': 1}), {'order_id': 'O1'})
            frozen.tick(delta=23 * 3600)  # 23h 后还在窗口内
            result = self.store.get('k')
            self.assertIsNotNone(result)
            self.assertEqual(result.response['order_id'], 'O1')

    def test_compute_request_hash_is_order_insensitive(self) -> None:
        a = compute_request_hash({'shares': 100, 'symbol': '600000'})
        b = compute_request_hash({'symbol': '600000', 'shares': 100})
        self.assertEqual(a, b)

    def test_compute_request_hash_distinguishes_different_payloads(self) -> None:
        a = compute_request_hash({'shares': 100})
        b = compute_request_hash({'shares': 200})
        self.assertNotEqual(a, b)

    def test_concurrent_put_resolves_to_single_winner(self) -> None:
        """两个线程同 key + 同 payload 并发 put，没人抛错。"""
        payload = {'a': 1}
        request_hash = compute_request_hash(payload)
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def _worker(i: int) -> None:
            try:
                barrier.wait()
                self.store.put('race', request_hash, {'order_id': f'O{i}'})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 同 key + 同 hash 必须全部成功，不抛 IntegrityError 也不抛 Conflict
        self.assertEqual(errors, [])
        # 存的是其中某一个写入的响应
        result = self.store.get('race')
        self.assertIsNotNone(result)
        self.assertTrue(result.response['order_id'].startswith('O'))


class TestOrderEndpointIdempotency(unittest.TestCase):
    """端到端：POST /orders/submit 在同 Idempotency-Key 重试时只成交一次。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix='idem_endpoint_')
        # 让 idempotency store 用一个干净的 tempfile
        os.environ['QUANT_STATE_DB'] = os.path.join(self.tmpdir, 'state.db')

        import backend.api as api_mod
        from services.portfolio import PortfolioService

        self.svc = PortfolioService(db_path=os.path.join(self.tmpdir, 'svc.db'))
        self.svc.set_cash(1_000_000.0)
        api_mod.reset_svc(self.svc)

        # 重置 idempotency store singleton 以拾起新的 QUANT_STATE_DB
        api_mod._idempotency_store_singleton.reset()
        # R2-4: 之前 test_api_smoke 用 importlib 隔离了 _GLOBAL_RATE_LIMIT，
        # 现在共享一个 backend.api 模块；前置测试可能填满了 ip 桶。
        api_mod._GLOBAL_RATE_LIMIT.clear()
        api_mod._RATE_LIMIT.clear()

        # 替换 broker 工厂为一个计数的 mock，便于断言 submit_order 调用次数。
        from backend.services.broker import OrderResult

        class _CountingBroker:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def submit_order(self, **kwargs) -> OrderResult:
                self.calls.append(kwargs)
                return OrderResult(
                    order_id=f'ORD-{len(self.calls)}',
                    status='filled',
                    symbol=kwargs['symbol'],
                    direction=kwargs['direction'],
                    submitted_shares=kwargs['shares'],
                    filled_shares=kwargs['shares'],
                    avg_price=kwargs.get('price', 0.0) or 10.0,
                )

            def _fetch_market_price(self, symbol: str) -> float:
                return 10.0

        self.broker = _CountingBroker()
        self._patch_broker = patch.object(
            api_mod, '_get_or_build_broker', return_value=self.broker,
        )
        self._patch_broker.start()
        # PreTrade 风控关掉（专注幂等性测试）
        self._patch_risk = patch.object(api_mod, '_get_risk_engine', return_value=None)
        self._patch_risk.start()

        api_mod.app.config['TESTING'] = True
        self.client = api_mod.app.test_client()

    def tearDown(self) -> None:
        self._patch_broker.stop()
        self._patch_risk.stop()
        import backend.api as api_mod
        api_mod.reset_svc(None)
        api_mod._idempotency_store_singleton.reset()
        os.environ.pop('QUANT_STATE_DB', None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, body: dict, idem_key: str | None = None):
        headers = {'Content-Type': 'application/json'}
        if idem_key is not None:
            headers['Idempotency-Key'] = idem_key
        return self.client.post('/orders/submit', json=body, headers=headers)

    def test_no_idempotency_key_executes_normally(self) -> None:
        body = {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 100, 'price': 10.0}
        r1 = self._post(body)
        r2 = self._post(body)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # 没幂等 key → 两次都成交
        self.assertEqual(len(self.broker.calls), 2)

    def test_same_idempotency_key_replays_response(self) -> None:
        body = {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 100, 'price': 10.0}
        r1 = self._post(body, idem_key='key-A')
        r2 = self._post(body, idem_key='key-A')
        r3 = self._post(body, idem_key='key-A')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 200)
        # 关键：broker.submit_order 只被调用一次
        self.assertEqual(len(self.broker.calls), 1)
        # 后两次响应里有 replayed 标记
        self.assertNotIn('replayed', r1.get_json())
        self.assertTrue(r2.get_json().get('replayed'))
        self.assertTrue(r3.get_json().get('replayed'))
        # order_id 一致
        self.assertEqual(r1.get_json()['order_id'], r2.get_json()['order_id'])

    def test_same_key_different_payload_returns_422(self) -> None:
        self._post(
            {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 100, 'price': 10.0},
            idem_key='key-B',
        )
        # 同 key 不同 payload
        r = self._post(
            {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 200, 'price': 10.0},
            idem_key='key-B',
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()['code'], 'IDEMPOTENCY_KEY_CONFLICT')
        # broker 仍然只成交了第一次
        self.assertEqual(len(self.broker.calls), 1)


if __name__ == '__main__':
    unittest.main()

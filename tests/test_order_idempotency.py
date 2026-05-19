"""R0-1: Order submission idempotency.

Verifies:
- core/idempotency.py: reserve/complete/release + put/get backward-compat
  + race semantics + stale-pending steal.
- /orders/submit endpoint:
    · same Idempotency-Key replays prior response
    · same key + different body → 422 (IDEMPOTENCY_KEY_CONFLICT)
    · concurrent same-key requests → exactly one broker submit, peers get
      409 IDEMPOTENCY_IN_FLIGHT (R0-1 review-fix)
    · transient submission failure releases the reservation so a retry
      with the same key succeeds (R0-1 review-fix)
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from freezegun import freeze_time

# 跟 test_api_smoke.py 一致：把项目根和 backend/ 都塞到 sys.path，
# 这样 `from services.portfolio import PortfolioService` 能解析。
_PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ_ROOT))
sys.path.insert(0, str(_PROJ_ROOT / 'backend'))

from core.idempotency import (
    IdempotencyKeyConflict, IdempotencyStore, PENDING_TIMEOUT_SECONDS,
    ReserveOutcome, compute_request_hash,
)


class TestIdempotencyStore(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix='idem_test_')
        self.db_path = os.path.join(self.tmpdir, 'idem.db')
        self.store = IdempotencyStore(db_path=self.db_path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── Backward-compat put / get ────────────────────────────────────────

    def test_get_returns_none_for_unknown_key(self) -> None:
        self.assertIsNone(self.store.get('does-not-exist'))

    def test_reserve_then_complete_replay_round_trips_response(self) -> None:
        payload = {'symbol': '600000', 'shares': 100}
        request_hash = compute_request_hash(payload)
        outcome, stored = self.store.reserve('key-1', request_hash)
        self.assertIs(outcome, ReserveOutcome.NEW)
        self.assertIsNone(stored)
        self.store.complete('key-1', request_hash, {'order_id': 'O1', 'status': 'ok'})

        result = self.store.get('key-1')
        self.assertIsNotNone(result)
        self.assertEqual(result.response, {'order_id': 'O1', 'status': 'ok'})
        self.assertEqual(result.request_hash, request_hash)

        # Re-reserve same key+hash → REPLAY.
        outcome2, stored2 = self.store.reserve('key-1', request_hash)
        self.assertIs(outcome2, ReserveOutcome.REPLAY)
        self.assertIsNotNone(stored2)
        self.assertEqual(stored2.response, {'order_id': 'O1', 'status': 'ok'})

    def test_reserve_same_key_different_hash_raises_conflict(self) -> None:
        h1 = compute_request_hash({'a': 1})
        h2 = compute_request_hash({'a': 2})
        self.store.reserve('k', h1)
        self.store.complete('k', h1, {'order_id': 'O1'})
        with self.assertRaises(IdempotencyKeyConflict):
            self.store.reserve('k', h2)

    def test_reserve_pending_same_hash_returns_in_flight(self) -> None:
        """The core R0-1 fix: a second caller arriving while the first is
        still mid-flight must NOT proceed."""
        h = compute_request_hash({'a': 1})
        outcome1, _ = self.store.reserve('k', h)
        self.assertIs(outcome1, ReserveOutcome.NEW)
        # Same key+hash, still pending (no complete yet).
        outcome2, stored2 = self.store.reserve('k', h)
        self.assertIs(outcome2, ReserveOutcome.IN_FLIGHT)
        self.assertIsNone(stored2)

    def test_release_lets_retry_succeed(self) -> None:
        h = compute_request_hash({'a': 1})
        self.store.reserve('k', h)
        # Simulated transient failure path.
        self.store.release('k', h)
        # Retry with same key should be NEW again, not IN_FLIGHT.
        outcome, _ = self.store.reserve('k', h)
        self.assertIs(outcome, ReserveOutcome.NEW)

    def test_release_is_noop_after_complete(self) -> None:
        h = compute_request_hash({'a': 1})
        self.store.reserve('k', h)
        self.store.complete('k', h, {'order_id': 'O1'})
        # Release after complete should NOT clear the stored response.
        self.store.release('k', h)
        outcome, stored = self.store.reserve('k', h)
        self.assertIs(outcome, ReserveOutcome.REPLAY)
        self.assertEqual(stored.response['order_id'], 'O1')

    def test_stale_pending_is_stolen_after_timeout(self) -> None:
        """If a worker crashes after reserve but before complete, the key
        must NOT be locked for 24h — after PENDING_TIMEOUT_SECONDS a fresh
        reserve should steal it."""
        h = compute_request_hash({'a': 1})
        with freeze_time('2026-05-19 10:00:00') as frozen:
            outcome, _ = self.store.reserve('k', h)
            self.assertIs(outcome, ReserveOutcome.NEW)
            # Within the window, peer sees IN_FLIGHT.
            self.assertIs(
                self.store.reserve('k', h)[0], ReserveOutcome.IN_FLIGHT,
            )
            # Skip past timeout.
            frozen.tick(delta=PENDING_TIMEOUT_SECONDS + 1.0)
            outcome2, _ = self.store.reserve('k', h)
            self.assertIs(outcome2, ReserveOutcome.NEW)

    def test_expired_completed_entry_is_not_returned(self) -> None:
        h = compute_request_hash({'a': 1})
        with freeze_time('2026-05-19 10:00:00') as frozen:
            self.store.reserve('k', h)
            self.store.complete('k', h, {'order_id': 'O1'})
            # TTL is 24h.
            frozen.tick(delta=24 * 3600 + 1)
            self.assertIsNone(self.store.get('k'))

    def test_entry_still_valid_within_ttl(self) -> None:
        h = compute_request_hash({'a': 1})
        with freeze_time('2026-05-19 10:00:00') as frozen:
            self.store.reserve('k', h)
            self.store.complete('k', h, {'order_id': 'O1'})
            frozen.tick(delta=23 * 3600)
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

    def test_concurrent_reserve_exactly_one_winner(self) -> None:
        """The headline R0-1 invariant at the store layer: 10 threads with
        the same key+hash → exactly one NEW, the rest IN_FLIGHT.

        The pre-fix store had get/put semantics where all 10 callers would
        get past the `get is None` check and submit before the PRIMARY KEY
        race resolved — that meant duplicate side effects in the endpoint.
        """
        h = compute_request_hash({'a': 1})
        outcomes: list[ReserveOutcome] = []
        errors: list[Exception] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def _worker() -> None:
            try:
                barrier.wait()
                outcome, _ = self.store.reserve('race', h)
                with lock:
                    outcomes.append(outcome)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        new_count = sum(1 for o in outcomes if o is ReserveOutcome.NEW)
        in_flight_count = sum(1 for o in outcomes if o is ReserveOutcome.IN_FLIGHT)
        self.assertEqual(new_count, 1,
                         f'expected exactly 1 NEW winner, got {new_count}; '
                         f'outcomes={outcomes}')
        self.assertEqual(in_flight_count, 9)

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
                self._lock = threading.Lock()
                # 当被设为非 None 时，撮合前 sleep 这么多秒，便于制造并发窗口。
                self.submit_delay: float = 0.0

            def submit_order(self, **kwargs) -> OrderResult:
                if self.submit_delay > 0:
                    time.sleep(self.submit_delay)
                with self._lock:
                    self.calls.append(kwargs)
                    n = len(self.calls)
                return OrderResult(
                    order_id=f'ORD-{n}',
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

    def test_concurrent_same_key_executes_broker_only_once(self) -> None:
        """The bug PR #27 review flagged: with the old get→submit→put flow,
        N concurrent requests with the same Idempotency-Key all submitted
        before any of them persisted. The reserve/complete protocol must
        give us exactly 1 NEW + (N-1) IN_FLIGHT (HTTP 409).
        """
        body = {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 100, 'price': 10.0}
        # Make broker slow so concurrent peers definitely overlap the
        # in-flight window of the winner.
        self.broker.submit_delay = 0.2

        n = 8
        results: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def _worker() -> None:
            barrier.wait()
            r = self._post(body, idem_key='race-key')
            with lock:
                results.append(r.status_code)

        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one 200 (the winner) + (n-1) 409s (IN_FLIGHT). Replays
        # only happen for callers that arrive AFTER complete() — those will
        # also get 200 but with replayed=True. The barrier here ensures
        # everyone reserves before the winner completes, so we expect 1+7.
        ok_count = sum(1 for s in results if s == 200)
        in_flight_count = sum(1 for s in results if s == 409)
        # broker must be hit exactly once regardless of timing classification.
        self.assertEqual(len(self.broker.calls), 1,
                         f'broker hit {len(self.broker.calls)} times; '
                         f'status codes={results}')
        self.assertEqual(ok_count + in_flight_count, n,
                         f'unexpected status codes: {results}')
        # At least one of each must have happened.
        self.assertGreaterEqual(ok_count, 1)
        self.assertGreaterEqual(in_flight_count, 1)

    def test_broker_failure_releases_reservation_so_retry_can_succeed(self) -> None:
        """If the first submission fails (broker throws), the same key
        with the same payload must be re-runnable — otherwise transient
        failures lock the key for 24h."""
        body = {'symbol': '600000.SH', 'direction': 'BUY', 'shares': 100, 'price': 10.0}

        # Force one transient broker failure, then a real success.
        call_count = {'n': 0}
        real_submit = self.broker.submit_order

        def _flaky_submit(**kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError('simulated broker outage')
            return real_submit(**kwargs)

        # Flask testing=True 默认 propagate_exceptions=True，会让 test_client
        # 直接抛而不是返回 500。这里临时关掉，模拟生产 WSGI 行为。
        import backend.api as api_mod
        api_mod.app.config['PROPAGATE_EXCEPTIONS'] = False
        try:
            with patch.object(self.broker, 'submit_order', side_effect=_flaky_submit):
                r1 = self._post(body, idem_key='retry-key')
                # First call fails — Flask returns 500 because we re-raise.
                self.assertGreaterEqual(r1.status_code, 500)
                # Second call with the SAME key should now succeed, not get
                # IN_FLIGHT (release should have removed the pending row) and
                # not get REPLAY (no completed response existed).
                r2 = self._post(body, idem_key='retry-key')
                self.assertEqual(r2.status_code, 200)
                self.assertNotIn('replayed', r2.get_json())
        finally:
            api_mod.app.config.pop('PROPAGATE_EXCEPTIONS', None)
        # Broker was called exactly twice: failing attempt + retry.
        self.assertEqual(call_count['n'], 2)


if __name__ == '__main__':
    unittest.main()

"""R0-6: Tests for core.lifecycle — process-wide shutdown coordinator."""
from __future__ import annotations

import threading
import time
import unittest

from core.lifecycle import Shutdown, ShuttingDown, get_shutdown


class TestShutdown(unittest.TestCase):

    def setUp(self) -> None:
        # Reset the global between tests (also done by conftest, but explicit
        # here so this file can run standalone).
        get_shutdown()._reset()

    def test_initial_state_is_not_shutting_down(self) -> None:
        s = get_shutdown()
        self.assertFalse(s.is_shutting_down)

    def test_request_sets_flag_and_invokes_handlers_in_order(self) -> None:
        s = get_shutdown()
        log: list[str] = []
        s.register_handler(lambda: log.append('a'))
        s.register_handler(lambda: log.append('b'))
        s.register_handler(lambda: log.append('c'))

        s.request()
        self.assertTrue(s.is_shutting_down)
        self.assertEqual(log, ['a', 'b', 'c'])

    def test_request_is_idempotent(self) -> None:
        s = get_shutdown()
        counter = [0]
        s.register_handler(lambda: counter.__setitem__(0, counter[0] + 1))

        s.request()
        s.request()
        s.request()
        self.assertEqual(counter[0], 1)  # handler invoked exactly once

    def test_handler_exception_does_not_block_subsequent_handlers(self) -> None:
        s = get_shutdown()
        log: list[str] = []
        s.register_handler(lambda: log.append('first'))
        s.register_handler(lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        s.register_handler(lambda: log.append('third'))

        s.request()
        self.assertEqual(log, ['first', 'third'])

    def test_check_or_raise_before_shutdown(self) -> None:
        s = get_shutdown()
        # Should NOT raise
        s.check_or_raise()

    def test_check_or_raise_during_shutdown(self) -> None:
        s = get_shutdown()
        s.request()
        with self.assertRaises(ShuttingDown):
            s.check_or_raise()

    def test_wait_unblocks_when_request_called(self) -> None:
        s = get_shutdown()
        events: list[str] = []

        def _waiter() -> None:
            s.wait()
            events.append('unblocked')

        t = threading.Thread(target=_waiter)
        t.start()
        time.sleep(0.05)  # let waiter park
        self.assertEqual(events, [])  # still parked
        s.request(reason='test')
        t.join(timeout=1.0)
        self.assertEqual(events, ['unblocked'])

    def test_wait_with_timeout_returns_false(self) -> None:
        s = get_shutdown()
        start = time.monotonic()
        result = s.wait(timeout=0.05)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.04)

    def test_handler_registered_after_request_does_not_execute(self) -> None:
        """register_handler after shutdown started is allowed (no crash) but
        the callback is not invoked retroactively — that's the test."""
        s = get_shutdown()
        s.request()
        called: list[str] = []
        s.register_handler(lambda: called.append('late'))
        self.assertEqual(called, [])

    def test_install_signal_handlers_in_worker_thread_is_safe(self) -> None:
        """signal.signal raises ValueError off the main thread; we swallow it."""
        s = Shutdown()
        result: list[Exception | None] = [None]

        def _worker() -> None:
            try:
                s.install_signal_handlers()
            except Exception as e:  # noqa: BLE001
                result[0] = e

        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        self.assertIsNone(result[0], f"install_signal_handlers raised: {result[0]}")


if __name__ == '__main__':
    unittest.main()

"""Tests for core.singleton — thread-safe lazy singleton container."""
from __future__ import annotations

import threading
import unittest

from core.singleton import LockedSingleton, SingletonRegistry


class _Counter:
    """Construction counter used to detect double-init under contention."""
    instances_created = 0

    def __init__(self) -> None:
        type(self).instances_created += 1


def _reset_counter() -> None:
    _Counter.instances_created = 0


class TestLockedSingleton(unittest.TestCase):

    def setUp(self) -> None:
        _reset_counter()

    def test_lazy_init_returns_same_instance(self) -> None:
        singleton: LockedSingleton[_Counter] = LockedSingleton(_Counter)
        a = singleton.get()
        b = singleton.get()
        self.assertIs(a, b)
        self.assertEqual(_Counter.instances_created, 1)

    def test_peek_does_not_trigger_init(self) -> None:
        singleton: LockedSingleton[_Counter] = LockedSingleton(_Counter)
        self.assertIsNone(singleton.peek())
        self.assertEqual(_Counter.instances_created, 0)
        singleton.get()
        self.assertIsNotNone(singleton.peek())

    def test_reset_clears_instance(self) -> None:
        singleton: LockedSingleton[_Counter] = LockedSingleton(_Counter)
        a = singleton.get()
        singleton.reset()
        b = singleton.get()
        self.assertIsNot(a, b)
        self.assertEqual(_Counter.instances_created, 2)

    def test_reset_with_instance_injects_fixture(self) -> None:
        singleton: LockedSingleton[_Counter] = LockedSingleton(_Counter)
        fake = _Counter()  # counter incremented to 1 from this construction
        singleton.reset(fake)
        self.assertIs(singleton.get(), fake)
        # factory must not have been called
        self.assertEqual(_Counter.instances_created, 1)

    def test_dispose_called_on_replace(self) -> None:
        disposed: list[_Counter] = []
        singleton: LockedSingleton[_Counter] = LockedSingleton(
            _Counter, dispose=lambda inst: disposed.append(inst)
        )
        first = singleton.get()
        singleton.reset()
        self.assertEqual(disposed, [first])

    def test_dispose_not_called_when_replacing_with_same_instance(self) -> None:
        disposed: list[_Counter] = []
        singleton: LockedSingleton[_Counter] = LockedSingleton(
            _Counter, dispose=lambda inst: disposed.append(inst)
        )
        first = singleton.get()
        singleton.reset(first)  # injecting same instance must not dispose it
        self.assertEqual(disposed, [])

    def test_dispose_exception_does_not_block_reset(self) -> None:
        def _bad_dispose(_inst: _Counter) -> None:
            raise RuntimeError("intentional")

        singleton: LockedSingleton[_Counter] = LockedSingleton(
            _Counter, dispose=_bad_dispose
        )
        singleton.get()
        singleton.reset()  # should not raise
        self.assertIsNone(singleton.peek())

    def test_concurrent_get_creates_only_one_instance(self) -> None:
        singleton: LockedSingleton[_Counter] = LockedSingleton(_Counter)
        results: list[_Counter] = []
        barrier = threading.Barrier(50)

        def _worker() -> None:
            barrier.wait()
            results.append(singleton.get())

        threads = [threading.Thread(target=_worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(_Counter.instances_created, 1)
        self.assertEqual(len({id(r) for r in results}), 1)


class TestSingletonRegistry(unittest.TestCase):

    def test_reset_all_clears_every_registered_singleton(self) -> None:
        before_count = SingletonRegistry.count()
        s1: LockedSingleton[_Counter] = LockedSingleton(_Counter, name="t1")
        s2: LockedSingleton[_Counter] = LockedSingleton(_Counter, name="t2")
        s1.get()
        s2.get()
        self.assertIsNotNone(s1.peek())
        self.assertIsNotNone(s2.peek())

        SingletonRegistry.reset_all()

        self.assertIsNone(s1.peek())
        self.assertIsNone(s2.peek())
        # other singletons created elsewhere should also be in the registry
        self.assertGreaterEqual(SingletonRegistry.count(), before_count + 2)

    def test_list_names_includes_registered_singletons(self) -> None:
        LockedSingleton(_Counter, name="registry_check_unique_name")
        self.assertIn("registry_check_unique_name", SingletonRegistry.list_names())


if __name__ == "__main__":
    unittest.main()

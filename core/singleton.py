"""Thread-safe lazy singleton container and registry.

Why this module exists
----------------------
~20 modules in the codebase had hand-rolled ``_global_*`` + ``global`` patterns
with inconsistent locking. ``core.alerting`` / ``backend.api`` had no lock at
all and would race under Flask's multi-threaded WSGI worker; ``core.data_layer``
/ ``core.metrics`` / ``core.data_gateway.http`` had ad-hoc double-checked locks
each implemented slightly differently.

This module centralizes the pattern. Each singleton is registered with a
process-wide :class:`SingletonRegistry` so test fixtures can wipe all global
state in one call instead of importing every module's hand-written ``reset_*``.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Generic, List, Optional, TypeVar

T = TypeVar("T")


class LockedSingleton(Generic[T]):
    """Thread-safe lazy singleton with explicit reset.

    Parameters
    ----------
    factory
        Zero-argument callable that produces the instance on first access.
    name
        Optional human-readable label used by :meth:`SingletonRegistry.list_names`
        and in repr. Defaults to ``factory.__name__``.
    dispose
        Optional callable invoked with the previous instance just before it is
        replaced or cleared. Use it to close connections / release resources
        (mirrors what ``http.py`` and ``fetcher_manager.py`` did by hand).
        Exceptions raised by ``dispose`` are swallowed; the singleton state is
        always advanced so a misbehaving cleanup cannot wedge the registry.

    Notes
    -----
    Uses double-checked locking: the fast path reads ``_instance`` without
    acquiring the lock (CPython attribute assignment is atomic), then re-checks
    under the lock before constructing. Safe for the multi-threaded Flask /
    worker setup the project runs on.
    """

    def __init__(
        self,
        factory: Callable[[], T],
        *,
        name: Optional[str] = None,
        dispose: Optional[Callable[[T], None]] = None,
    ) -> None:
        self._factory = factory
        self._dispose = dispose
        # getattr fallback guarantees a str; cast for mypy strict.
        self._name: str = str(name or getattr(factory, "__name__", "<anonymous>"))
        self._lock = threading.Lock()
        self._instance: Optional[T] = None
        SingletonRegistry._register(self)

    def get(self) -> T:
        """Return the instance, creating it on first access."""
        instance = self._instance
        if instance is not None:
            return instance
        with self._lock:
            if self._instance is None:
                self._instance = self._factory()
            return self._instance

    def reset(self, instance: Optional[T] = None) -> None:
        """Clear or replace the held instance.

        Pass ``instance`` to inject a fixture (test helpers do this). Otherwise
        the next :meth:`get` will rebuild via ``factory``.
        """
        with self._lock:
            previous = self._instance
            if previous is not None and previous is not instance and self._dispose is not None:
                try:
                    self._dispose(previous)
                except Exception:
                    # Cleanup failures must not block reset; the user already
                    # asked us to discard the instance.
                    pass
            self._instance = instance

    def peek(self) -> Optional[T]:
        """Return the current instance without triggering initialization."""
        return self._instance

    @property
    def name(self) -> str:
        return self._name

    def __repr__(self) -> str:
        state = "initialized" if self._instance is not None else "uninitialized"
        return f"<LockedSingleton {self._name} {state}>"


class SingletonRegistry:
    """Process-wide registry of every :class:`LockedSingleton`.

    Tests use :meth:`reset_all` in an autouse conftest fixture so module-level
    singletons cannot leak state between cases. Application code rarely calls
    this directly.
    """

    _instances: "List[LockedSingleton[Any]]" = []
    _lock = threading.Lock()

    @classmethod
    def _register(cls, singleton: "LockedSingleton[Any]") -> None:
        with cls._lock:
            cls._instances.append(singleton)

    @classmethod
    def reset_all(cls) -> None:
        """Reset every registered singleton. Intended for test isolation."""
        with cls._lock:
            instances = list(cls._instances)
        for s in instances:
            s.reset()

    @classmethod
    def list_names(cls) -> List[str]:
        with cls._lock:
            return [s.name for s in cls._instances]

    @classmethod
    def count(cls) -> int:
        with cls._lock:
            return len(cls._instances)


__all__ = ["LockedSingleton", "SingletonRegistry"]

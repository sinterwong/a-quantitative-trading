"""Process-wide shutdown coordinator.

Why this module exists
----------------------
Subsystems (IntradayMonitor / Scheduler / StrategyRunner / async_runner)
each have their own ``threading.Event`` stop flag. The signal handler in
``quant_app/main.py`` calls ``.stop()`` on each subsystem in sequence,
but there's no global "we are shutting down" state for new tasks to query.
Result: SIGTERM arrives, the scheduler stops, but a request currently
being processed by Flask still enqueues a fresh job into a worker that's
about to die. That job becomes a zombie (started but never finished,
no order trail).

:class:`Shutdown` is a process-wide flag + handler registry. Subsystems
register their stop callbacks; producers (enqueue points) check
``is_shutting_down`` and raise :class:`ShuttingDown` to refuse new work
during the drain window.

This module does *not* replace existing ``threading.Event`` flags inside
each subsystem — those are still useful as cooperative wakeup signals.
It coordinates *across* subsystems.
"""

from __future__ import annotations

import logging
import signal as _signal
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class ShuttingDown(RuntimeError):
    """Raised by producers when the process is in the middle of shutting
    down and cannot accept new work."""


class Shutdown:
    """Process-wide shutdown coordinator (lazy singleton).

    Use :func:`get_shutdown` to access it; avoid constructing directly.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._handlers: List[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._signals_installed = False

    # ------------------------------------------------------------------ state
    @property
    def is_shutting_down(self) -> bool:
        return self._event.is_set()

    def check_or_raise(self) -> None:
        """Raise :class:`ShuttingDown` if a shutdown is in progress.

        Call this at enqueue points / new-task entrypoints to refuse work
        during the drain window. Example::

            def submit_signal(...):
                get_shutdown().check_or_raise()
                queue.put(signal)
        """
        if self._event.is_set():
            raise ShuttingDown("process is shutting down; refusing new work")

    # ------------------------------------------------------------- handlers
    def register_handler(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked once when shutdown is requested.

        Callbacks are executed in registration order on the thread that
        called :meth:`request`. Exceptions in one handler do not stop
        subsequent handlers from running.
        """
        with self._lock:
            self._handlers.append(callback)

    # ---------------------------------------------------------------- request
    def request(self, *, reason: str = "explicit") -> None:
        """Mark the process as shutting down and invoke all registered
        handlers. Idempotent — calling twice is a no-op.
        """
        # Use the event's "set/test" as the gate; only the first caller
        # passes through the handler loop.
        already_set = self._event.is_set()
        self._event.set()
        if already_set:
            return
        logger.info('Shutdown requested (reason=%s); invoking %d handler(s)',
                    reason, len(self._handlers))
        # Snapshot handlers under the lock to avoid races with late
        # register_handler calls — but invoke outside the lock so a handler
        # that itself registers something doesn't deadlock.
        with self._lock:
            handlers = list(self._handlers)
        for cb in handlers:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001 — must not block siblings
                logger.warning('Shutdown handler %r raised: %s', cb, exc)

    # ---------------------------------------------------------- wait helpers
    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until :meth:`request` is called.

        Returns True if the shutdown was requested, False on timeout.
        """
        return self._event.wait(timeout=timeout)

    # ---------------------------------------------------------- signal setup
    def install_signal_handlers(self) -> None:
        """Wire SIGINT / SIGTERM to :meth:`request`.

        Safe to call multiple times — subsequent calls are no-ops. Must be
        called from the main thread (Python's ``signal.signal`` restriction).
        """
        if self._signals_installed:
            return

        def _handle(signum: int, _frame: Any) -> None:
            name = _signal.Signals(signum).name if hasattr(_signal, 'Signals') else str(signum)
            self.request(reason=f'signal:{name}')

        try:
            _signal.signal(_signal.SIGINT, _handle)
            _signal.signal(_signal.SIGTERM, _handle)
            self._signals_installed = True
        except (ValueError, OSError) as exc:
            # Worker thread or non-main thread — signal.signal raises ValueError.
            logger.debug('install_signal_handlers skipped: %s', exc)

    # ----------------------------------------------------------- test helpers
    def _reset(self) -> None:
        """Test-only: clear the event and handler list."""
        with self._lock:
            self._event.clear()
            self._handlers.clear()
            self._signals_installed = False


# ─── Module-level singleton ────────────────────────────────────────────────
from core.singleton import LockedSingleton

_shutdown_singleton: LockedSingleton[Shutdown] = LockedSingleton(
    Shutdown, name="lifecycle.shutdown", dispose=lambda s: s._reset()
)


def get_shutdown() -> Shutdown:
    """Return the process-wide shutdown coordinator."""
    return _shutdown_singleton.get()


__all__ = ["Shutdown", "ShuttingDown", "get_shutdown"]

"""Idempotency-Key based replay protection for order submission.

When clients retry an order POST (network blip, double-click, broker bridge
timeout), the same Idempotency-Key must return the *original* response —
not execute the order a second time.

Concurrency model — reserve / complete / release
------------------------------------------------
The original implementation only had ``get`` / ``put``: it checked for a
prior response BEFORE submitting and only persisted AFTER. Two concurrent
requests with the same key both saw "no prior" and both executed the order
end-to-end (the SQLite PRIMARY KEY then merely deduplicated the *record*,
not the side effect). PR #27 review flagged this as the R0-1 gap.

We now use a two-phase protocol modeled after Stripe-style idempotency:

1. ``reserve(key, request_hash)`` — atomically INSERT a row with
   ``status='pending'``. The DB PRIMARY KEY constraint serializes
   concurrent attempts: exactly one caller gets :data:`ReserveOutcome.NEW`
   and is authorized to submit the side-effecting work. Concurrent peers
   with the same hash get :data:`ReserveOutcome.IN_FLIGHT` (the endpoint
   returns 409 so the client retries after a backoff). Anyone with a
   *different* hash gets :class:`IdempotencyKeyConflict` (HTTP 422).
2. ``complete(key, request_hash, response)`` — UPDATE the row to
   ``status='completed'`` with the response payload. Subsequent
   ``reserve`` calls return :data:`ReserveOutcome.REPLAY` with the stored
   response.
3. ``release(key, request_hash)`` — DELETE the pending row on error so
   the client can retry with the same key. Without this, a transient
   broker failure would lock the key for the entire TTL.

Stale pending rows (e.g., from a crashed worker) are auto-stolen after
:data:`PENDING_TIMEOUT_SECONDS`; the new caller takes over the
reservation. Completed rows expire after :data:`TTL_SECONDS`.

Storage: SQLite (project state DB, resolved via
:func:`core.state_db.state_db_path`). Expired rows are reaped by a
periodic sweep (probability + min-interval gated; see ``_maybe_cleanup``)
so high-QPS writers do not pay a full-table DELETE on every call.

Threading: writes use ``BEGIN IMMEDIATE`` + SQLite busy_timeout for
DB-level serialization; an internal lock keeps the cleanup sweep from
racing with itself within a single process.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600
# 单笔下单全链路（风控 + 撮合 + 写持仓）通常 < 数百 ms。给 60s 兜底，
# 既能容忍极端慢的下游券商，又能让"进程崩在 pending"的 key 在用户重试
# 时被合理 steal，不至于卡到 24h TTL 才能复用。
PENDING_TIMEOUT_SECONDS = 60.0

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS order_idempotency (
    key             TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL,        -- 'pending' | 'completed'
    response_json   TEXT,                 -- NULL while pending
    created_at      REAL NOT NULL,        -- reservation time
    completed_at    REAL                  -- NULL while pending
)
"""

# 旧表（PR #27 第一版）没有 status / completed_at 字段。lazy migrate。
_LEGACY_MIGRATION_SQL = [
    "ALTER TABLE order_idempotency ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
    "ALTER TABLE order_idempotency ADD COLUMN completed_at REAL",
    "UPDATE order_idempotency SET completed_at = created_at "
    "WHERE completed_at IS NULL AND status = 'completed'",
]


class ReserveOutcome(enum.Enum):
    """Result of a :meth:`IdempotencyStore.reserve` call."""

    NEW = 'new'
    """Caller won the race; proceed with the side-effecting work, then call
    :meth:`IdempotencyStore.complete` (or :meth:`release` on error)."""

    REPLAY = 'replay'
    """Key already completed with this same hash. Caller must skip the
    side effect and return the stored response."""

    IN_FLIGHT = 'in_flight'
    """Another request with the same key+hash is currently being processed.
    Caller should return HTTP 409 so the client retries after a backoff."""


@dataclass
class StoredResponse:
    """Result of a previous successful POST with this idempotency key."""
    response: Dict[str, Any]
    request_hash: str
    created_at: float


class IdempotencyKeyConflict(ValueError):
    """The same key was used with a different request payload — the client
    is reusing keys incorrectly. The right response is HTTP 422."""


def compute_request_hash(payload: Any) -> str:
    """SHA-256 of the canonical JSON serialization of the payload.

    Used to detect "same key, different body" — a client bug that should
    return 422 rather than silently shadow the original."""
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# Cleanup gating — probability per write + min-interval per process.
# 旧实现每次 put 都跑全表 DELETE，高 QPS 下成开销热点。
_CLEANUP_PROBABILITY = 0.05
_CLEANUP_MIN_INTERVAL_SECONDS = 60.0


class IdempotencyStore:
    """SQLite-backed idempotency store with reserve / complete / release semantics.

    See module docstring for the concurrency protocol. Construct one per
    process and share across threads (operations are thread-safe).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            from core.state_db import state_db_path
            db_path = state_db_path()
        self._db_path = db_path
        self._lock = threading.Lock()
        self._last_cleanup_at: float = 0.0
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
        except sqlite3.DatabaseError:
            pass
        return conn

    def _init_table(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(_TABLE_DDL)
                # Lazy migration for rows written by the pre-reserve schema.
                for stmt in _LEGACY_MIGRATION_SQL:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        # Column already exists or DDL idempotent — fine.
                        pass
                conn.commit()

    # ---------------------------------------------------------------- reserve
    def reserve(
        self,
        key: str,
        request_hash: str,
        *,
        now: Optional[float] = None,
    ) -> Tuple[ReserveOutcome, Optional[StoredResponse]]:
        """Attempt to claim ``key`` for an in-flight order submission.

        Returns ``(outcome, stored)``:

        * ``(NEW, None)``      — caller is authorized to proceed. MUST call
          :meth:`complete` on success or :meth:`release` on failure.
        * ``(REPLAY, stored)`` — key already completed; return ``stored.response``.
        * ``(IN_FLIGHT, None)``— another caller is mid-flight. Endpoint
          should return 409 (or wait + poll, app's choice).

        Raises :class:`IdempotencyKeyConflict` if an entry exists with a
        different ``request_hash``.
        """
        now_ts = time.time() if now is None else now

        # Fast path: row already exists. Read first; the INSERT below
        # handles the race where two callers see "no row" simultaneously.
        existing = self._read_row(key)
        outcome = self._classify_existing(existing, request_hash, now_ts)
        if outcome is not None:
            return outcome

        # No row (or pending+stale eligible for steal). Try to insert /
        # overwrite atomically via INSERT OR REPLACE — but only when the
        # row is stale, to keep PRIMARY KEY serialization meaningful.
        if existing is not None and existing['status'] == 'pending':
            return self._steal_stale_pending(key, request_hash, existing, now_ts)
        return self._insert_pending(key, request_hash, now_ts)

    def _read_row(self, key: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                'SELECT key, request_hash, status, response_json, '
                'created_at, completed_at '
                'FROM order_idempotency WHERE key = ?',
                (key,),
            ).fetchone()

    def _classify_existing(
        self,
        row: Optional[sqlite3.Row],
        request_hash: str,
        now_ts: float,
    ) -> Optional[Tuple[ReserveOutcome, Optional[StoredResponse]]]:
        """Map a pre-read row to a reserve outcome, or None if caller
        still needs to attempt an INSERT (no row, or stale pending row)."""
        if row is None:
            return None

        if row['request_hash'] != request_hash:
            raise IdempotencyKeyConflict(
                f'idempotency key {row["key"]!r} already used with a '
                f'different payload'
            )

        status = row['status']
        if status == 'completed':
            cutoff = now_ts - TTL_SECONDS
            if row['created_at'] < cutoff:
                # Completed but TTL-expired — treat as gone; caller will
                # INSERT fresh below. Cleanup sweep will reap the row later.
                return None
            try:
                response = json.loads(row['response_json'])
            except (TypeError, json.JSONDecodeError) as exc:
                logger.warning(
                    'IdempotencyStore: completed row for key=%s has corrupt '
                    'response_json (%s); treating as missing',
                    row['key'], exc,
                )
                return None
            stored = StoredResponse(
                response=response,
                request_hash=row['request_hash'],
                created_at=row['created_at'],
            )
            return (ReserveOutcome.REPLAY, stored)

        # status == 'pending'
        age = now_ts - row['created_at']
        if age <= PENDING_TIMEOUT_SECONDS:
            return (ReserveOutcome.IN_FLIGHT, None)
        # Stale pending — caller will try to steal it (handled by caller).
        return None

    def _insert_pending(
        self,
        key: str,
        request_hash: str,
        now_ts: float,
    ) -> Tuple[ReserveOutcome, Optional[StoredResponse]]:
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    'INSERT INTO order_idempotency '
                    '(key, request_hash, status, response_json, created_at, completed_at) '
                    "VALUES (?, ?, 'pending', NULL, ?, NULL)",
                    (key, request_hash, now_ts),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Lost the race — another caller inserted between our
                # read and our INSERT. Re-classify their row.
                conn.rollback()
                return self._reclassify_after_race(key, request_hash, now_ts)
        self._maybe_cleanup()
        return (ReserveOutcome.NEW, None)

    def _steal_stale_pending(
        self,
        key: str,
        request_hash: str,
        stale_row: sqlite3.Row,
        now_ts: float,
    ) -> Tuple[ReserveOutcome, Optional[StoredResponse]]:
        """A previous reservation went stale (worker crashed mid-flight or
        held the key past PENDING_TIMEOUT_SECONDS). Atomically reset
        created_at so we own the reservation now."""
        old_created_at = stale_row['created_at']
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                # Conditional UPDATE: only succeeds if the row is still
                # the stale pending one we read. If another caller already
                # stole or completed it, rowcount will be 0 and we re-read.
                cur = conn.execute(
                    'UPDATE order_idempotency '
                    "SET created_at = ?, request_hash = ? "
                    "WHERE key = ? AND status = 'pending' AND created_at = ?",
                    (now_ts, request_hash, key, old_created_at),
                )
                conn.commit()
                if cur.rowcount == 1:
                    self._maybe_cleanup()
                    return (ReserveOutcome.NEW, None)
            except sqlite3.DatabaseError:
                conn.rollback()
                raise
        # Someone else moved/completed the row while we tried to steal.
        return self._reclassify_after_race(key, request_hash, now_ts)

    def _reclassify_after_race(
        self,
        key: str,
        request_hash: str,
        now_ts: float,
    ) -> Tuple[ReserveOutcome, Optional[StoredResponse]]:
        """After losing a race in INSERT or UPDATE, re-read the row and
        return the up-to-date outcome. The new row CANNOT be stale (it
        was just written), so this terminates."""
        existing = self._read_row(key)
        outcome = self._classify_existing(existing, request_hash, now_ts)
        if outcome is not None:
            return outcome
        # The row vanished between our INSERT failure and re-read (e.g.,
        # cleanup sweep reaped it). Pessimistic answer: tell the caller
        # someone else is in flight; client will retry.
        logger.debug(
            'IdempotencyStore: race resolution for key=%s found no row; '
            'returning IN_FLIGHT to be safe', key,
        )
        return (ReserveOutcome.IN_FLIGHT, None)

    # --------------------------------------------------------------- complete
    def complete(
        self,
        key: str,
        request_hash: str,
        response: Dict[str, Any],
        *,
        now: Optional[float] = None,
    ) -> None:
        """Mark a reserved key as completed with the given response.

        Caller MUST have received ``ReserveOutcome.NEW`` from a prior
        :meth:`reserve` call for the same ``(key, request_hash)``.

        Raises :class:`IdempotencyKeyConflict` if the stored hash drifts
        (defensive: caller has a coding bug if this fires).
        """
        now_ts = time.time() if now is None else now
        response_json = json.dumps(response, ensure_ascii=False, default=str)
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                cur = conn.execute(
                    'UPDATE order_idempotency '
                    "SET status = 'completed', response_json = ?, completed_at = ? "
                    'WHERE key = ? AND request_hash = ?',
                    (response_json, now_ts, key, request_hash),
                )
                if cur.rowcount == 0:
                    # Either the row was reaped, or someone changed the hash.
                    # Re-check to give the right error.
                    conn.rollback()
                    row = self._read_row(key)
                    if row is None:
                        logger.warning(
                            'IdempotencyStore.complete: key=%s no longer exists '
                            '(stolen / reaped). Response will not be replayable.',
                            key,
                        )
                        return
                    raise IdempotencyKeyConflict(
                        f'idempotency key {key!r} hash changed during in-flight '
                        f'(reserved={row["request_hash"]!r}, completing={request_hash!r})'
                    )
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()
                raise

    # ---------------------------------------------------------------- release
    def release(
        self,
        key: str,
        request_hash: str,
    ) -> None:
        """Drop a pending reservation so the same key can be retried.

        Call this from the error path of a side-effecting endpoint after
        :meth:`reserve` returned ``NEW``. Safe no-op if the row has already
        been completed (we leave completed rows alone) or stolen (the
        WHERE clause matches nothing).
        """
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    'DELETE FROM order_idempotency '
                    "WHERE key = ? AND request_hash = ? AND status = 'pending'",
                    (key, request_hash),
                )
                conn.commit()
            except sqlite3.DatabaseError:
                conn.rollback()
                raise

    # ------------------------------------------------------------- query helpers
    def get(self, key: str) -> Optional[StoredResponse]:
        """Return the stored response for ``key`` if it's completed and
        within the TTL window. Returns ``None`` for pending / expired /
        missing entries.

        Kept for backward compatibility with the pre-reserve API; new code
        should use :meth:`reserve` to get a definite outcome.
        """
        cutoff = time.time() - TTL_SECONDS
        with self._connect() as conn:
            row = conn.execute(
                'SELECT request_hash, response_json, created_at '
                "FROM order_idempotency WHERE key = ? "
                "AND status = 'completed' AND created_at >= ?",
                (key, cutoff),
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row['response_json'])
        except (TypeError, json.JSONDecodeError) as exc:
            logger.warning('IdempotencyStore: stored response for key=%s corrupt: %s',
                           key, exc)
            return None
        return StoredResponse(
            response=response,
            request_hash=row['request_hash'],
            created_at=row['created_at'],
        )

    # ------------------------------------------------------------------ cleanup
    def _maybe_cleanup(self) -> None:
        """Run :meth:`_cleanup_expired` opportunistically.

        Gated on (a) probability ``_CLEANUP_PROBABILITY`` and (b) at least
        ``_CLEANUP_MIN_INTERVAL_SECONDS`` since the last sweep in this
        process. High-QPS writers thus pay an amortized fraction of the
        full-table DELETE.
        """
        now_ts = time.time()
        with self._lock:
            if now_ts - self._last_cleanup_at < _CLEANUP_MIN_INTERVAL_SECONDS:
                return
            if random.random() >= _CLEANUP_PROBABILITY:
                return
            self._last_cleanup_at = now_ts
        with self._connect() as conn:
            self._cleanup_expired(conn)

    def _cleanup_expired(self, conn: sqlite3.Connection) -> None:
        """Reap rows older than TTL_SECONDS (completed) or older than
        PENDING_TIMEOUT_SECONDS while still pending (orphan reservations
        from crashed workers)."""
        now_ts = time.time()
        completed_cutoff = now_ts - TTL_SECONDS
        pending_cutoff = now_ts - PENDING_TIMEOUT_SECONDS
        try:
            conn.execute(
                'DELETE FROM order_idempotency '
                "WHERE (status = 'completed' AND created_at < ?) "
                "   OR (status = 'pending'   AND created_at < ?)",
                (completed_cutoff, pending_cutoff),
            )
            conn.commit()
        except sqlite3.DatabaseError as exc:
            logger.debug('IdempotencyStore cleanup skipped: %s', exc)


__all__ = [
    'IdempotencyStore', 'IdempotencyKeyConflict', 'StoredResponse',
    'ReserveOutcome', 'compute_request_hash',
    'TTL_SECONDS', 'PENDING_TIMEOUT_SECONDS',
]

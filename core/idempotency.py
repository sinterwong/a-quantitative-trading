"""Idempotency-Key based replay protection for order submission.

When clients retry an order POST (network blip, double-click, broker bridge
timeout), the same Idempotency-Key must return the *original* response —
not execute the order a second time.

Storage: SQLite (project state DB, resolved via :func:`core.state_db.state_db_path`).
Entries auto-expire after :data:`TTL_SECONDS`; expired rows are reaped lazily
on each :meth:`put`.

Threading: writes serialized by an internal lock + ``BEGIN IMMEDIATE``
transactions. The DB-level write lock used by ``backend.services.portfolio``
is *not* shared — different tables, no contention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS order_idempotency (
    key             TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      REAL NOT NULL
)
"""


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


class IdempotencyStore:

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            from core.state_db import state_db_path
            db_path = state_db_path()
        self._db_path = db_path
        self._lock = threading.Lock()
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
                conn.commit()

    def get(self, key: str) -> Optional[StoredResponse]:
        """Return the previously stored response, or None if not found / expired."""
        cutoff = time.time() - TTL_SECONDS
        with self._connect() as conn:
            row = conn.execute(
                'SELECT request_hash, response_json, created_at '
                'FROM order_idempotency WHERE key = ? AND created_at >= ?',
                (key, cutoff),
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row['response_json'])
        except json.JSONDecodeError as exc:
            logger.warning('IdempotencyStore: stored response for key=%s corrupt: %s',
                           key, exc)
            return None
        return StoredResponse(
            response=response,
            request_hash=row['request_hash'],
            created_at=row['created_at'],
        )

    def put(self, key: str, request_hash: str, response: Dict[str, Any]) -> None:
        """Store the response for this key+hash. If the key exists with a
        different hash, raise :class:`IdempotencyKeyConflict`.

        Idempotent: re-putting the same (key, hash) is a no-op."""
        existing = self.get(key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyKeyConflict(
                    f'idempotency key {key!r} already used with a different payload'
                )
            # Same key, same payload, just no-op.
            return

        response_json = json.dumps(response, ensure_ascii=False, default=str)
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                try:
                    conn.execute(
                        'INSERT INTO order_idempotency '
                        '(key, request_hash, response_json, created_at) '
                        'VALUES (?, ?, ?, ?)',
                        (key, request_hash, response_json, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    # Lost a race with another writer using the same key.
                    # Re-check whether it's the same payload — if not, raise.
                    existing = self.get(key)
                    if existing is not None and existing.request_hash != request_hash:
                        raise IdempotencyKeyConflict(
                            f'idempotency key {key!r} race lost to a different payload'
                        )
                self._cleanup_expired(conn)

    def _cleanup_expired(self, conn: sqlite3.Connection) -> None:
        """Lazy GC of rows older than TTL_SECONDS."""
        cutoff = time.time() - TTL_SECONDS
        try:
            conn.execute(
                'DELETE FROM order_idempotency WHERE created_at < ?',
                (cutoff,),
            )
            conn.commit()
        except sqlite3.DatabaseError as exc:
            logger.debug('IdempotencyStore cleanup skipped: %s', exc)


__all__ = [
    'IdempotencyStore', 'IdempotencyKeyConflict', 'StoredResponse',
    'compute_request_hash', 'TTL_SECONDS',
]

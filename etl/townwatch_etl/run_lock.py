"""
Per-jurisdiction run lock (Postgres advisory locks).

Only one pipeline run should work a jurisdiction at a time, so a fund deposit can
safely kick off a run without doubling up with the cron (or a second deposit).
We use session-level advisory locks rather than a lock table because they
AUTO-RELEASE when the holding connection closes — including on process crash —
so there is no stale-lock bookkeeping or TTL to get wrong.

    with jurisdiction_lock(jid) as got:
        if not got:
            return  # another run owns this jurisdiction right now — skip
        ...do the work...

    if not is_running(jid):
        spawn a run

The lock is keyed (namespace, jurisdiction_id) so it can't collide with any
other advisory-lock use in the app.
"""

from __future__ import annotations

import contextlib

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL
from .resilience import retry_transient

# Arbitrary constant namespace so our (namespace, jurisdiction_id) locks never
# collide with another feature's advisory-lock keys.
_LOCK_NAMESPACE = 30471


def _lock_conn() -> psycopg.Connection:
    # autocommit: session-level advisory locks live for the connection, not a txn.
    conn = retry_transient(
        lambda: psycopg.connect(DATABASE_URL, row_factory=dict_row), label="run_lock")
    conn.autocommit = True
    return conn


@contextlib.contextmanager
def jurisdiction_lock(jurisdiction_id: int):
    """Hold an exclusive per-jurisdiction lock for the block. Yields True if
    acquired, False if another run already holds it (caller should skip). Held on
    a dedicated connection that is unlocked + closed on exit."""
    conn = _lock_conn()
    got = False
    try:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS ok",
            (_LOCK_NAMESPACE, jurisdiction_id),
        ).fetchone()["ok"]
        yield got
    finally:
        try:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NAMESPACE, jurisdiction_id))
        except Exception:
            pass  # connection close releases the lock regardless
        try:
            conn.close()
        except Exception:
            pass


def is_running(jurisdiction_id: int) -> bool:
    """Best-effort check: True if a run currently holds this jurisdiction's lock.
    (Acquires-and-releases to test; there's an inherent TOCTOU window, so the
    real guard is jurisdiction_lock() inside the run itself.)"""
    with jurisdiction_lock(jurisdiction_id) as got:
        return not got

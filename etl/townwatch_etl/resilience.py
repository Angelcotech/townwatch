"""
Shared transient-failure handling for the ETL pipeline.

At scale (re-extracting a whole jurisdiction, or many jurisdictions back to
back) the bottleneck is rarely the work itself — it's momentary infrastructure
hiccups: the macOS resolver (mDNSResponder) returning EAI_NONAME when the rate
of fresh DNS lookups overwhelms it, a Railway proxy dropping a connection, an
SSL session closing mid-query. Each is transient and self-heals in seconds, but
without a backoff the pipeline hammers straight through and a single blip
cascades into mass failure (observed: one DNS hiccup → 439 "failures", all of
which were retryable).

This module is the single source of truth for (a) what counts as a transient
infra error and (b) the retry/backoff policy. It is deliberately dependency-free
(only stdlib) so it can be imported by the DB layer, the HTTP layer, and the
job loops without import cycles.

Two layers use it:
  * db.connect() wraps connection ESTABLISHMENT — catches DNS / connect-refused
    failures for every one of the ~20 jobs that open a connection.
  * the extract job loops wrap each UNIT OF WORK — catches a connection dropped
    mid-session (which a connect-level retry can't see) by re-running the whole
    meeting. DB drops surface at connect() before any model spend, so retrying
    a unit is cheap.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Substrings (matched case-insensitively against str(exception)) that mark a
# transient infra failure worth retrying rather than recording as permanent.
# Covers DNS-resolver overload, socket/proxy blips, and psycopg mid-session
# connection drops. Keep this list as the ONE place these strings live.
TRANSIENT_MARKERS: tuple[str, ...] = (
    # DNS resolver overwhelmed (getaddrinfo EAI_NONAME / EAI_AGAIN)
    "failed to resolve host",
    "nodename nor servname",
    "temporary failure in name resolution",
    "name or service not known",
    # Socket / ephemeral-port / proxy blips
    "can't assign requested address",
    "could not receive data from server",
    "could not connect to server",
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "network is unreachable",
    # psycopg mid-session connection drops / bad-state (a fresh-connection retry
    # clears these — validated in production on a real "Can't assign requested
    # address" blip that recovered on attempt 2)
    "server closed the connection unexpectedly",
    "the connection is lost",
    "connection is bad",
    "consuming input failed",
    "sending query failed",
    "another command is already in progress",
    "ssl connection has been closed",
    "ssl syscall error",
)

# Default policy: 5 attempts, exponential backoff 4 → 8 → 16 → 32s (capped).
# The pauses are what let the resolver/proxy recover; they break the cascade.
DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_SECS = 4
DEFAULT_MAX_SECS = 60


def is_transient_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a momentary infra failure that should be
    retried (DNS-resolver overload, socket/proxy blip, mid-session DB drop)."""
    msg = str(exc).lower()
    return any(marker in msg for marker in TRANSIENT_MARKERS)


def backoff_secs(attempt: int, *, base: int = DEFAULT_BASE_SECS,
                 cap: int = DEFAULT_MAX_SECS) -> int:
    """Exponential backoff for a 1-indexed attempt, capped at ``cap``."""
    return min(cap, base * (2 ** (attempt - 1)))


def retry_transient(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_secs: int = DEFAULT_BASE_SECS,
    max_secs: int = DEFAULT_MAX_SECS,
    label: str = "",
    on_retry: Callable[[int, BaseException, int], None] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` and retry on transient infra errors with exponential backoff.

    Non-transient exceptions raise immediately. If all attempts are exhausted,
    the last exception is re-raised. ``on_retry(attempt, exc, delay)`` is invoked
    before each backoff sleep so callers can log; a default printer is used when
    none is given (so unattended runs leave a trail)."""
    if on_retry is None:
        def on_retry(attempt: int, exc: BaseException, delay: int) -> None:  # noqa: E306
            where = f" [{label}]" if label else ""
            print(f"   ⏳ transient error{where} (attempt {attempt}/{attempts}): "
                  f"{str(exc)[:90]} — retrying in {delay}s")

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise if not transient
            last_exc = exc
            if is_transient_error(exc) and attempt < attempts:
                delay = backoff_secs(attempt, base=base_secs, cap=max_secs)
                on_retry(attempt, exc, delay)
                _sleep(delay)
                continue
            raise
    # Unreachable (loop either returns or raises), but satisfies type checkers.
    assert last_exc is not None
    raise last_exc

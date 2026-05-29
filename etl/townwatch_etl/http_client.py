"""
Shared outbound HTTP for every ETL job — the single chokepoint all
civic-platform fetches go through, so throttle/backoff policy can't be
bypassed by a job that simply forgot to add it.

Why this exists
---------------
Civic platforms (CivicClerk especially) apply aggressive per-IP
throttling. Before this module each job built its own ``httpx.Client``
with an ad-hoc delay (or none at all), which produced two failure modes:

  * A bulk scan of one CivicClerk tenant 429'd ~75% of the way through
    and never recovered — there was no Retry-After honoring and no
    backoff, just a fixed 500ms cadence the platform kept rejecting. The
    scan completed with 1164/1500 URLs "inconclusive": safe (we never
    overwrite a known flag on an inconclusive verdict) but useless.
  * Two jobs hitting the same tenant had no shared notion of "slow down".

This module centralises:
  * one pooled ``httpx.Client`` (connection reuse across calls);
  * an *adaptive* per-host throttle that widens a host's interval when it
    pushes back (429) and decays back toward the floor on sustained
    success;
  * Retry-After honoring on 429, bounded exponential backoff on 5xx and
    network errors.

Callers still classify the final response themselves — this layer owns
*transport policy*, not *meaning*. A terminal 429 (every retry throttled)
is returned, not raised, so e.g. the availability scanner can record it
as "inconclusive" rather than as a citizen-facing finding.

Scope — what this is NOT (yet)
------------------------------
Throttle state lives in-process. That is sufficient for the pipeline as
it runs today: ``daily_refresh`` executes jobs as *sequential*
subprocesses and fans out across *jurisdictions*, and distinct
jurisdictions are distinct CivicClerk tenants = distinct hostnames, which
the per-host throttle already separates. The one shape this does NOT
cover is *concurrent processes hitting the same host* (e.g. splitting a
single tenant's work across workers).

When the architecture grows that shape, swap ``_AdaptiveHostThrottle``
for a Postgres-backed token bucket: a ``host_throttle(host, tokens,
next_allowed_at)`` row advanced with a single atomic
``UPDATE ... RETURNING``. Do NOT reach for ``pg_advisory_xact_lock`` —
holding an advisory lock across the fetch pins a transaction open across
network I/O and exhausts the pool. The ``civic_request()`` contract is
designed to stay identical across that swap, so no caller changes.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"

# Floor delay between requests to the same host under good conditions.
# CivicClerk throttled a sustained bulk scan at 0.5s; 1.0s is the floor.
BASE_INTERVAL_SECS = 1.0
# Ceiling the adaptive per-host interval can grow to after repeated 429s.
# Capped low (vs. an earlier 30s) so a single throttled URL can't stall the
# whole host for half a minute — a live bulk scan showed the interval
# pinning at the old 30s cap and tanking throughput to ~27s/URL.
MAX_INTERVAL_SECS = 12.0
# We adapt the per-host interval with MULTIPLICATIVE INCREASE / ADDITIVE
# DECREASE: a 429/5xx doubles the interval (back off fast), while each clean
# response shaves a fixed step off it (recover steadily). The old
# multiplicative ×0.9 recovery decayed too slowly near the cap, so under a
# sustained ~29% 429 rate the interval never climbed back down.
BACKOFF_FACTOR = 2.0
RECOVERY_STEP_SECS = 0.5
# Default per-request timeout; override per call (e.g. large PDF fetches).
DEFAULT_TIMEOUT = 30.0
# Total attempts (initial try + retries) for retryable failures.
MAX_ATTEMPTS = 5
# Never honor a Retry-After (or back off) longer than this in one wait —
# beyond it we'd rather give up the attempt than block a job for minutes.
MAX_WAIT_SECS = 120.0


class _AdaptiveHostThrottle:
    """Per-host request spacing that adapts to platform pushback.

    Each host carries a current interval (starts at ``BASE_INTERVAL_SECS``).
    A 429/5xx widens it (×``BACKOFF_FACTOR``, capped at
    ``MAX_INTERVAL_SECS``); a clean response narrows it back toward the
    floor (−``RECOVERY_STEP_SECS``). ``wait()`` blocks until the host's
    next-allowed time. Thread-safe so a threaded caller can share one
    instance without coordinating.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interval: dict[str, float] = {}
        self._next_allowed: dict[str, float] = {}

    def wait(self, host: str) -> None:
        """Block until this host's next slot, then reserve the following one."""
        while True:
            with self._lock:
                now = time.monotonic()
                nxt = self._next_allowed.get(host, 0.0)
                if now >= nxt:
                    interval = self._interval.get(host, BASE_INTERVAL_SECS)
                    self._next_allowed[host] = now + interval
                    return
                sleep_for = min(nxt - now, MAX_WAIT_SECS)
            time.sleep(sleep_for)

    def penalize(self, host: str, cooldown: float | None = None) -> None:
        """Widen this host's interval after pushback. If ``cooldown`` is
        given (e.g. a Retry-After), hold the host off for at least that long
        on top of the widened interval."""
        with self._lock:
            cur = self._interval.get(host, BASE_INTERVAL_SECS)
            self._interval[host] = min(cur * BACKOFF_FACTOR, MAX_INTERVAL_SECS)
            if cooldown is not None:
                self._next_allowed[host] = time.monotonic() + cooldown

    def reward(self, host: str) -> None:
        """Shave a fixed step off this host's interval after success
        (additive decrease toward the floor)."""
        with self._lock:
            cur = self._interval.get(host, BASE_INTERVAL_SECS)
            self._interval[host] = max(cur - RECOVERY_STEP_SECS, BASE_INTERVAL_SECS)


_throttle = _AdaptiveHostThrottle()
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """Lazily build the one shared, connection-pooled client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                    limits=httpx.Limits(
                        max_connections=20, max_keepalive_connections=10
                    ),
                )
    return _client


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds
    from now. Returns None if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff for a 0-indexed attempt, capped."""
    return min(BASE_INTERVAL_SECS * (2 ** attempt), MAX_INTERVAL_SECS)


def civic_request(
    method: str,
    url: str,
    *,
    timeout: float | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    **kwargs,
) -> httpx.Response:
    """Issue one request through the shared client with adaptive per-host
    throttling, Retry-After honoring, and bounded exponential backoff on
    429 / 5xx / network errors.

    Returns the final ``httpx.Response``. If every attempt was throttled
    (429) or 5xx'd, the LAST such response is returned — NOT raised — so
    callers can classify it (the availability scanner treats a terminal
    429 as 'inconclusive' rather than a finding). Network-layer errors
    that survive all attempts re-raise the last exception.

    ``**kwargs`` pass straight through to ``httpx.Client.request`` (e.g.
    ``headers=`` for a Range GET).
    """
    host = urlparse(url).netloc
    client = _get_client()
    req_timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    last_response: httpx.Response | None = None
    last_exc: Exception | None = None

    for attempt in range(max_attempts):
        _throttle.wait(host)
        try:
            response = client.request(method, url, timeout=req_timeout, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(_backoff_delay(attempt))
            continue

        if response.status_code == 429:
            last_response = response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            cooldown = retry_after if retry_after is not None else _backoff_delay(attempt)
            cooldown = min(cooldown, MAX_WAIT_SECS)
            _throttle.penalize(host, cooldown=cooldown)
            if attempt < max_attempts - 1:
                time.sleep(cooldown)
            continue

        if response.status_code >= 500:
            last_response = response
            _throttle.penalize(host)
            if attempt < max_attempts - 1:
                time.sleep(_backoff_delay(attempt))
            continue

        # 2xx / 3xx / non-429 4xx are all definitive answers about the URL,
        # not throttle signals. Reward the host and hand the response back.
        _throttle.reward(host)
        return response

    if last_response is not None:
        return last_response
    assert last_exc is not None
    raise last_exc


def civic_get(url: str, **kwargs) -> httpx.Response:
    """GET ``url`` through the shared throttled/retrying client."""
    return civic_request("GET", url, **kwargs)


class CivicClient:
    """Drop-in stand-in for ``httpx.Client`` that routes every request
    through the shared throttled ``civic_request``. Lets existing code that
    threads a ``client`` object through helper functions adopt the
    chokepoint without being restructured.

    Constructor args other than ``default_timeout`` are intentionally
    ignored — the shared client already sets the User-Agent and follows
    redirects. ``default_timeout`` mirrors ``httpx.Client(timeout=...)``: it
    applies to any call that doesn't pass its own ``timeout=``.
    """

    def __init__(self, default_timeout: float | None = None) -> None:
        self._default_timeout = default_timeout

    def _kw(self, kwargs: dict) -> dict:
        if self._default_timeout is not None and "timeout" not in kwargs:
            kwargs["timeout"] = self._default_timeout
        return kwargs

    def get(self, url: str, **kwargs) -> httpx.Response:
        return civic_request("GET", url, **self._kw(kwargs))

    def post(self, url: str, **kwargs) -> httpx.Response:
        return civic_request("POST", url, **self._kw(kwargs))

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return civic_request(method, url, **self._kw(kwargs))


@contextmanager
def civic_client(default_timeout: float | None = None):
    """Yield a :class:`CivicClient`. The shape mirrors
    ``with httpx.Client(...) as client:`` for drop-in adoption — there's
    nothing to close, since the proxy delegates to the process-wide shared
    client."""
    yield CivicClient(default_timeout)

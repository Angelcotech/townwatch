"""
Per-jurisdiction funds — reserve / settle / pause.

The domain layer over migration 028's three tables. Compartmentalized: this is
the ONLY module that reads/writes the fund_* tables; everything else calls these
functions. Money is Decimal end to end (never float arithmetic on balances).

Accounting model (see 028): reserve-then-settle.
    available(j) = ledger_balance(j) − reserved(j)
  * reserve(): under a per-jurisdiction row lock, check the floor + status and
    place a hold; auto-pause if the unit can't be afforded. The lock makes the
    check-and-hold atomic, so concurrent workers can't both slip past the floor.
  * settle(): append the real spend to the immutable ledger and drop the hold.
  * release(): drop a hold without charging (e.g. nothing was spent).

reserve/settle/release each run in their OWN short transaction (the caller opens
a `with connect()` around them), NOT inside the long extraction transaction — so
the row lock is held for milliseconds, never across a multi-minute extraction.
The reservation ROW carries the hold in the meantime; a crashed unit leaves an
orphan hold that release_stale() reaps.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

ZERO = Decimal("0")
DEFAULT_ESTIMATE = Decimal("0.10")  # conservative pre-data hold per unit of work


def _dec(x: Any) -> Decimal:
    """Coerce float/int/str to Decimal via str (avoids float binary noise)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


# ── fund row / policy ────────────────────────────────────────────────────────

def ensure_fund(conn, jurisdiction_id: int, *, floor: Any = ZERO) -> None:
    """Create the jurisdiction_fund row if missing (idempotent). Does not change
    an existing row's floor/status."""
    conn.execute(
        "INSERT INTO jurisdiction_fund (jurisdiction_id, min_balance_floor) "
        "VALUES (%s, %s) ON CONFLICT (jurisdiction_id) DO NOTHING",
        (jurisdiction_id, _dec(floor)),
    )


def get_fund(conn, jurisdiction_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM jurisdiction_fund WHERE jurisdiction_id = %s",
        (jurisdiction_id,),
    ).fetchone()


def set_floor(conn, jurisdiction_id: int, floor: Any) -> None:
    ensure_fund(conn, jurisdiction_id)
    conn.execute(
        "UPDATE jurisdiction_fund SET min_balance_floor = %s, updated_at = now() "
        "WHERE jurisdiction_id = %s",
        (_dec(floor), jurisdiction_id),
    )


def status(conn, jurisdiction_id: int) -> str:
    row = get_fund(conn, jurisdiction_id)
    return row["status"] if row else "active"


def pause(conn, jurisdiction_id: int, reason: str) -> None:
    ensure_fund(conn, jurisdiction_id)
    conn.execute(
        "UPDATE jurisdiction_fund SET status = 'paused', paused_reason = %s, "
        "paused_at = now(), updated_at = now() "
        "WHERE jurisdiction_id = %s AND status <> 'suspended'",
        (reason, jurisdiction_id),
    )


def resume(conn, jurisdiction_id: int) -> None:
    """Clear an auto-pause (does not override a manual 'suspended')."""
    conn.execute(
        "UPDATE jurisdiction_fund SET status = 'active', paused_reason = NULL, "
        "paused_at = NULL, updated_at = now() "
        "WHERE jurisdiction_id = %s AND status = 'paused'",
        (jurisdiction_id,),
    )


# ── balances ─────────────────────────────────────────────────────────────────

def ledger_balance(conn, jurisdiction_id: int) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) AS bal FROM fund_ledger WHERE jurisdiction_id = %s",
        (jurisdiction_id,),
    ).fetchone()
    return _dec(row["bal"])


def reserved_total(conn, jurisdiction_id: int) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) AS held FROM fund_reservation WHERE jurisdiction_id = %s",
        (jurisdiction_id,),
    ).fetchone()
    return _dec(row["held"])


def available(conn, jurisdiction_id: int) -> Decimal:
    """Spendable balance = settled ledger balance − open holds."""
    return ledger_balance(conn, jurisdiction_id) - reserved_total(conn, jurisdiction_id)


# ── deposits ─────────────────────────────────────────────────────────────────

def deposit(conn, jurisdiction_id: int, amount: Any, *, kind: str = "deposit",
            ref_kind: str | None = None, ref_id: str | None = None,
            description: str | None = None, meta: dict | None = None) -> int:
    """Append a positive ledger entry (deposit/refund/positive adjustment).
    Also clears an auto-pause if the top-up restores the balance."""
    ensure_fund(conn, jurisdiction_id)
    amt = _dec(amount)
    row = conn.execute(
        "INSERT INTO fund_ledger (jurisdiction_id, kind, amount_usd, ref_kind, ref_id, description, meta) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (jurisdiction_id, kind, amt, ref_kind, ref_id, description, Json(meta or {})),
    ).fetchone()
    # A top-up should lift an insufficient-funds auto-pause.
    if amt > 0:
        resume(conn, jurisdiction_id)
    return row["id"]


# ── reserve / settle / release ───────────────────────────────────────────────

def reserve(conn, jurisdiction_id: int, expected_cost: Any, *, run_id=None,
            meeting_id: int | None = None, job_name: str | None = None) -> int | None:
    """Atomically gate-and-hold for one unit of work.

    Locks the jurisdiction_fund row, then: if the fund is not 'active', or
    available − expected_cost would fall below the floor, returns None (and
    auto-pauses on the insufficient-funds case). Otherwise inserts a hold and
    returns its reservation id. Run inside a short `with connect()` so the lock
    releases on commit."""
    ensure_fund(conn, jurisdiction_id)
    fund = conn.execute(
        "SELECT min_balance_floor, status FROM jurisdiction_fund "
        "WHERE jurisdiction_id = %s FOR UPDATE",
        (jurisdiction_id,),
    ).fetchone()
    if fund["status"] != "active":
        return None

    floor = _dec(fund["min_balance_floor"])
    expected = _dec(expected_cost)
    avail = available(conn, jurisdiction_id)
    if avail - expected < floor:
        pause(
            conn, jurisdiction_id,
            f"insufficient funds: available ${avail:.4f} − expected ${expected:.4f} "
            f"< floor ${floor:.4f}",
        )
        return None

    row = conn.execute(
        "INSERT INTO fund_reservation (jurisdiction_id, run_id, meeting_id, job_name, amount_usd) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (jurisdiction_id, run_id, meeting_id, job_name, expected),
    ).fetchone()
    return row["id"]


def settle(conn, reservation_id: int, actual_cost: Any, *, ref_kind: str | None = None,
           ref_id: str | None = None, description: str | None = None,
           meta: dict | None = None) -> None:
    """Replace a hold with the real charge: append a 'spend' ledger row for the
    actual cost (always charged, success or failure — honest accounting), then
    delete the reservation. A zero actual_cost settles to no ledger row."""
    res = conn.execute(
        "DELETE FROM fund_reservation WHERE id = %s RETURNING jurisdiction_id",
        (reservation_id,),
    ).fetchone()
    if res is None:
        return  # already settled/released — nothing to do
    jid = res["jurisdiction_id"]
    amt = _dec(actual_cost)
    if amt > 0:
        conn.execute(
            "INSERT INTO fund_ledger (jurisdiction_id, kind, amount_usd, ref_kind, ref_id, description, meta) "
            "VALUES (%s, 'spend', %s, %s, %s, %s, %s)",
            (jid, -amt, ref_kind, ref_id, description, Json(meta or {})),
        )


def release(conn, reservation_id: int) -> None:
    """Drop a hold without charging."""
    conn.execute("DELETE FROM fund_reservation WHERE id = %s", (reservation_id,))


def release_stale(conn, older_than_minutes: int = 60) -> int:
    """Reap orphan holds from units that died without settling. Returns count."""
    row = conn.execute(
        "WITH d AS (DELETE FROM fund_reservation "
        "WHERE created_at < now() - make_interval(mins => %s) RETURNING 1) "
        "SELECT count(*) AS n FROM d",
        (older_than_minutes,),
    ).fetchone()
    return row["n"]


def estimate_cost(conn, jurisdiction_id: int) -> Decimal:
    """Expected cost of the next unit for this jurisdiction — rolling average of
    recent settled spend, or a conservative default before any data exists.
    Sizes the reservation hold."""
    row = conn.execute(
        "SELECT AVG(-amount_usd) AS a FROM ("
        "  SELECT amount_usd FROM fund_ledger WHERE jurisdiction_id = %s AND kind = 'spend' "
        "  ORDER BY created_at DESC LIMIT 20) t",
        (jurisdiction_id,),
    ).fetchone()
    return Decimal(str(row["a"])) if row and row["a"] is not None else DEFAULT_ESTIMATE


# ── the site-wide gate ───────────────────────────────────────────────────────

class Gate:
    """Yielded by gate(). `.paused` is True when the jurisdiction is funded but
    can't afford this unit (caller should skip). `.metered` is True when the unit
    is being charged against a fund. `.cost` is the settled USD (set on exit)."""
    __slots__ = ("paused", "metered", "cost")

    def __init__(self) -> None:
        self.paused = False
        self.metered = False
        self.cost = ZERO


@contextlib.contextmanager
def gate(jurisdiction_id: int, *, run_id=None, meeting_id: int | None = None,
         job_name: str | None = None, ref_kind: str = "meeting",
         ref_id: str | None = None, description: str | None = None):
    """The ONE per-jurisdiction spend gate, shared by every job that spends.

    On enter: if the jurisdiction has a fund, RESERVE the estimated cost (and set
    `.paused` if it can't afford the floor — caller skips). While the body runs,
    model/OCR spend is metered. On exit: SETTLE the real metered cost and drop
    the hold (success or failure). A jurisdiction with NO fund row is ungated and
    unmetered — runs exactly as before. Usage:

        with funds.gate(jid, run_id=run, meeting_id=mid, job_name='extract_x',
                        ref_id=str(mid), description='extract_x') as g:
            if g.paused:
                return 'paused'
            ...do the work (cache hits cost $0, so a re-run settles $0)...
    """
    # Lazy imports avoid import cycles (funds is imported widely / early).
    from .db import connect
    from .llm_client import meter
    from .pricing import cost_usd, cost_breakdown

    g = Gate()
    reservation_id = None
    with connect() as gc:
        if get_fund(gc, jurisdiction_id) is not None:
            est = estimate_cost(gc, jurisdiction_id)
            reservation_id = reserve(gc, jurisdiction_id, est, run_id=run_id,
                                     meeting_id=meeting_id, job_name=job_name)
            if reservation_id is None:
                g.paused = True
    if g.paused:
        yield g
        return

    g.metered = reservation_id is not None
    with meter() as usage:
        try:
            yield g
        finally:
            g.cost = Decimal(str(cost_usd(usage)))
            if reservation_id is not None:
                try:
                    with connect() as gc:
                        settle(gc, reservation_id, cost_usd(usage), ref_kind=ref_kind,
                               ref_id=ref_id, description=description,
                               meta=cost_breakdown(usage))
                    print(f"     spend: ${g.cost:.4f}")
                except Exception as se:  # settlement must never mask the unit's outcome
                    print(f"   ⚠ fund settle failed ({ref_kind} {ref_id}): {se}")

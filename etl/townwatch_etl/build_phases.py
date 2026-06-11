"""
Build-phase ladder — depth-gated extraction with funded unlocks.

Onboarding a jurisdiction is phased, and each phase's paid work only starts
when its funding covers its estimated cost:

  Phase 1 — initial window. Catalog EVERYTHING (inventory/scan are cheap and
      always-on), but extract only documents from meetings within the last
      INITIAL_WINDOW_YEARS. This is the "frame out the basics" build a new
      jurisdiction gets by default.
  Phase 2 — historical seeding. Documents older than the initial window.
      Locked until the jurisdiction's fund can cover the phase's estimated
      remaining cost (jurisdiction_onboarding_estimate.meta.phases.historical,
      recomputed daily by estimate_onboarding and self-calibrating to real
      spend). The estimate IS the price tag: when available funds reach it,
      the next daily run starts draining history — no operator action needed.

Properties:
  * The catalog is always complete ("map broadly"); only EXTRACTION depth is
    phased — citizens see the full meeting list, with old documents marked
    not-yet-extracted rather than invisible.
  * A jurisdiction whose history is already extracted (remaining ≈ $0) is
    trivially unlocked, so pre-ladder jurisdictions keep their behavior.
  * Re-locks if funding dips below the remaining-historical estimate mid-drain:
    the drain pauses where it stands and resumes when covered again.
  * Pure read-side helper — no schema, no state of its own. The fund ledger
    and the estimate row are the only inputs, so the unlock is auditable.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

INITIAL_WINDOW_YEARS = 2


def initial_cutoff(today: date | None = None) -> date:
    today = today or date.today()
    return today - timedelta(days=INITIAL_WINDOW_YEARS * 365)


def historical_state(conn, jurisdiction_id: int) -> dict:
    """The historical-seeding phase state for one jurisdiction:
    {unlocked: bool, reason: str, needed_usd: float|None, available_usd: float}.
    """
    from . import funds

    row = conn.execute(
        "SELECT meta FROM jurisdiction_onboarding_estimate WHERE jurisdiction_id = %s",
        (jurisdiction_id,),
    ).fetchone()
    phases = ((row["meta"] or {}).get("phases") or {}) if row else {}
    hist = phases.get("historical") or {}
    needed = hist.get("remaining_usd")

    if needed is None:
        # No phase-aware estimate yet (estimate_onboarding hasn't run since the
        # phase split landed). Fail SAFE for spend: keep history locked rather
        # than draining an unpriced backlog — the next daily run prices it.
        return {"unlocked": False, "reason": "historical phase not yet estimated",
                "needed_usd": None, "available_usd": 0.0}

    if Decimal(str(needed)) <= 0:
        return {"unlocked": True, "reason": "no historical documents remaining",
                "needed_usd": 0.0, "available_usd": 0.0}

    avail = funds.available(conn, jurisdiction_id)
    if avail >= Decimal(str(needed)):
        return {"unlocked": True,
                "reason": f"funded: ${avail:.2f} available ≥ ${needed:.2f} estimated",
                "needed_usd": float(needed), "available_usd": float(avail)}
    return {"unlocked": False,
            "reason": f"awaiting funding: ${avail:.2f} available < ${needed:.2f} estimated",
            "needed_usd": float(needed), "available_usd": float(avail)}


def filter_phase_locked(conn, rows, *, date_key: str = "meeting_date",
                        jid_key: str = "jurisdiction_id") -> tuple[list, dict]:
    """Drop rows older than the initial window for jurisdictions whose
    historical phase is locked. Returns (kept_rows, deferred) where deferred
    is {jurisdiction_id: {"count": n, "state": historical_state(...)}} for
    the caller to report — a silent cap would read as full coverage."""
    cutoff = initial_cutoff()
    states: dict[int, dict] = {}
    kept: list = []
    deferred: dict[int, dict] = {}
    for r in rows:
        jid = r[jid_key]
        if r[date_key] >= cutoff:
            kept.append(r)
            continue
        if jid not in states:
            states[jid] = historical_state(conn, jid)
        if states[jid]["unlocked"]:
            kept.append(r)
        else:
            d = deferred.setdefault(jid, {"count": 0, "state": states[jid]})
            d["count"] += 1
    return kept, deferred

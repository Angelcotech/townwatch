"""
Onboarding cost estimate — the forward-looking dollar figure to fully index a
jurisdiction's published agendas + minutes. This is the /adopt funding GOAL
("be the first to build this town for ~$X"), distinct from the fund_ledger
balance (what's actually been contributed/spent).

Cheap by construction: pure SQL over data we already have. It does NOT download
or extract anything — it reads the meeting inventory that meetings_inventory
built and scan_document_availability already classified, so it must run AFTER
those two steps (it relies on the *_is_placeholder flags to exclude stubs).

The estimate is reconciled to real money, not a guess:

    estimate = agenda_docs x agenda_rate + minutes_docs x minutes_rate

where each rate is the rolling average of what real extractions of that kind
have ACTUALLY cost (from fund_ledger spend rows, system-wide), falling back to
funds.DEFAULT_ESTIMATE only until enough real spend exists to calibrate. So as
the system extracts more documents the rates self-improve, and the goal shown on
/adopt tracks true cost. The per-kind split matters: a minutes PDF (long, often
vision/OCR) costs far more than an agenda, so blending them would mislead a town
that publishes lots of one and little of the other.

`documents_total` is the full indexing workload (every real doc); `remaining` is
the not-yet-extracted subset (agenda with no agenda_items / minutes with no
motions) — i.e. what a fresh contribution would actually buy.

Idempotent: one row per jurisdiction, upserted each run. Failures are recorded
to pipeline_failure (per-jurisdiction, so one bad town never sinks the sweep)
and the job still returns success for the rest.

Run:
    python -m townwatch_etl.jobs.estimate_onboarding
    python -m townwatch_etl.jobs.estimate_onboarding --jurisdiction columbia-county-ga
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from ..db import connect
from ..funds import DEFAULT_ESTIMATE
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


# How many recent spend rows of a kind to average for its rate. Matches the
# rolling window funds.estimate_cost uses for reservation sizing, so the estimate
# and the live hold are derived the same way.
RATE_WINDOW = 50

# Ledger description prefixes that the extract jobs write (see daily_refresh /
# the gate's `description=`). Used to attribute spend to a document kind.
_AGENDA_DESC = "extract_agenda%"
_MINUTES_DESC = "extract_minute%"


def _kind_rate(conn, like: str) -> tuple[Decimal | None, int]:
    """Empirical per-document rate for one extraction kind: the average real
    settled cost of the most recent RATE_WINDOW spend rows whose description
    matches `like`, system-wide. Returns (rate, sample_size); rate is None when
    there's no spend history yet for that kind."""
    row = conn.execute(
        """
        SELECT AVG(-amount_usd) AS rate, count(*) AS n
        FROM (
            SELECT amount_usd FROM fund_ledger
            WHERE kind = 'spend' AND description LIKE %s
            ORDER BY created_at DESC LIMIT %s
        ) t
        """,
        (like, RATE_WINDOW),
    ).fetchone()
    if row and row["rate"] is not None:
        return Decimal(str(row["rate"])), int(row["n"])
    return None, 0


def _counts(conn, jurisdiction_id: int) -> dict:
    """Real (non-placeholder) agenda/minutes document counts for a jurisdiction,
    split into total workload vs not-yet-extracted remainder. A document is
    'extracted' when its meeting has the corresponding output: agenda_items for
    an agenda, motions for minutes."""
    return conn.execute(
        """
        SELECT
          count(*) FILTER (
            WHERE m.agenda_url IS NOT NULL AND NOT COALESCE(m.agenda_is_placeholder, false)
          ) AS agenda_total,
          count(*) FILTER (
            WHERE m.minutes_url IS NOT NULL AND NOT COALESCE(m.minutes_is_placeholder, false)
          ) AS minutes_total,
          count(*) FILTER (
            WHERE m.agenda_url IS NOT NULL AND NOT COALESCE(m.agenda_is_placeholder, false)
              AND NOT EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id)
          ) AS agenda_remaining,
          count(*) FILTER (
            WHERE m.minutes_url IS NOT NULL AND NOT COALESCE(m.minutes_is_placeholder, false)
              AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
          ) AS minutes_remaining
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        WHERE gb.jurisdiction_id = %s
        """,
        (jurisdiction_id,),
    ).fetchone()


def estimate_for(conn, jurisdiction_id: int) -> dict:
    """Compute and upsert the onboarding estimate for one jurisdiction. Returns
    the computed row (for logging)."""
    c = _counts(conn, jurisdiction_id)
    agenda_total = int(c["agenda_total"])
    minutes_total = int(c["minutes_total"])
    agenda_remaining = int(c["agenda_remaining"])
    minutes_remaining = int(c["minutes_remaining"])

    agenda_rate, agenda_n = _kind_rate(conn, _AGENDA_DESC)
    minutes_rate, minutes_n = _kind_rate(conn, _MINUTES_DESC)
    agenda_basis = "empirical" if agenda_rate is not None else "default"
    minutes_basis = "empirical" if minutes_rate is not None else "default"
    a_rate = agenda_rate if agenda_rate is not None else DEFAULT_ESTIMATE
    m_rate = minutes_rate if minutes_rate is not None else DEFAULT_ESTIMATE

    estimate_usd = agenda_total * a_rate + minutes_total * m_rate
    remaining_usd = agenda_remaining * a_rate + minutes_remaining * m_rate
    documents_total = agenda_total + minutes_total
    documents_remaining = agenda_remaining + minutes_remaining

    # Overall basis: 'empirical' only if every kind PRESENT in the workload is
    # calibrated; 'default' if none are; 'mixed' otherwise. A kind with zero
    # documents doesn't drag the basis down (its rate doesn't affect the total).
    present = []
    if agenda_total:
        present.append(agenda_basis)
    if minutes_total:
        present.append(minutes_basis)
    if not present:
        basis = "default"
    elif all(b == "empirical" for b in present):
        basis = "empirical"
    elif all(b == "default" for b in present):
        basis = "default"
    else:
        basis = "mixed"

    meta = {
        "agenda": {"documents": agenda_total, "remaining": agenda_remaining,
                   "rate_usd": float(a_rate), "basis": agenda_basis, "sample": agenda_n},
        "minutes": {"documents": minutes_total, "remaining": minutes_remaining,
                    "rate_usd": float(m_rate), "basis": minutes_basis, "sample": minutes_n},
        "rate_window": RATE_WINDOW,
        "default_rate_usd": float(DEFAULT_ESTIMATE),
    }

    from psycopg.types.json import Json
    conn.execute(
        """
        INSERT INTO jurisdiction_onboarding_estimate
            (jurisdiction_id, agenda_documents, minutes_documents,
             documents_total, documents_remaining, estimate_usd, remaining_usd,
             basis, meta, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (jurisdiction_id) DO UPDATE SET
            agenda_documents    = EXCLUDED.agenda_documents,
            minutes_documents   = EXCLUDED.minutes_documents,
            documents_total     = EXCLUDED.documents_total,
            documents_remaining = EXCLUDED.documents_remaining,
            estimate_usd        = EXCLUDED.estimate_usd,
            remaining_usd       = EXCLUDED.remaining_usd,
            basis               = EXCLUDED.basis,
            meta                = EXCLUDED.meta,
            computed_at         = now()
        """,
        (jurisdiction_id, agenda_total, minutes_total, documents_total,
         documents_remaining, estimate_usd, remaining_usd, basis, Json(meta)),
    )
    return {
        "documents_total": documents_total, "documents_remaining": documents_remaining,
        "estimate_usd": estimate_usd, "remaining_usd": remaining_usd, "basis": basis,
    }


def _jid_for_slug(conn, slug: str) -> int | None:
    """Resolve a config slug to its jurisdiction id via canonical fips (same path
    daily_refresh uses), so school districts resolve to their own row, not the
    county's."""
    try:
        fips = jurisdiction_fips(load_config(slug))
    except Exception:
        return None
    row = conn.execute("SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
    return row["id"] if row else None


def _record_failure(conn, slug: str, exc: Exception) -> None:
    """One town's failure is recorded but never sinks the sweep."""
    try:
        conn.execute(
            "INSERT INTO pipeline_failure (job_name, step, exception_class, message) "
            "VALUES ('estimate_onboarding', %s, %s, %s)",
            (slug, type(exc).__name__, str(exc)[:2000]),
        )
    except Exception as e:  # pragma: no cover — bookkeeping must never raise
        print(f"   ⚠ could not record failure for {slug}: {e}")


def run(jurisdiction_slug: str | None) -> int:
    slugs = [jurisdiction_slug] if jurisdiction_slug else list_slugs()
    print(f"estimating onboarding cost for {len(slugs)} jurisdiction(s)...")
    failures = 0
    for slug in slugs:
        # Each jurisdiction in its own transaction so one failure can't roll back
        # another's estimate.
        try:
            with connect() as conn:
                jid = _jid_for_slug(conn, slug)
                if jid is None:
                    print(f"  {slug}: no jurisdiction row yet — skipping")
                    continue
                r = estimate_for(conn, jid)
            print(
                f"  {slug}: {r['documents_total']} docs "
                f"({r['documents_remaining']} remaining) → goal ${r['estimate_usd']:.2f}, "
                f"remaining ${r['remaining_usd']:.2f} [{r['basis']}]"
            )
        except Exception as exc:
            failures += 1
            print(f"  ✗ {slug}: {type(exc).__name__}: {exc}")
            with connect() as conn:
                _record_failure(conn, slug, exc)
    print(f"--- done ({len(slugs) - failures}/{len(slugs)} ok) ---")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'columbia-county-ga'; default all")
    args = parser.parse_args()
    return run(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())

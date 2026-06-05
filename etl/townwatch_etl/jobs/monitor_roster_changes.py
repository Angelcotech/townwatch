"""
Monitor roster / position changes — who holds elected and appointed seats, and
when that changes. Positions can turn over fast (resignations, special elections,
new terms), and a citizen cares when their representative changes. This surfaces
those transitions as activity milestones on the public feed.

Anchored on the immutable `term` record, so it's naturally idempotent and each
event is dated to when it actually happened (start_date / end_date), not "now":
  * official_joined_seat — a term began (someone took a seat)
  * official_left_seat   — a term ended (end_date is set and in the past)
  * seat_vacant          — a seat that PREVIOUSLY had a holder now has no current
                           term (a real vacancy, not merely an unmapped seat)

All once-only (dedupe on the term/seat), so a re-run never double-posts and the
first run backfills a jurisdiction's real roster history at correct dates.

Unlike clerk-contact health (operator-only), roster changes ARE citizen-facing —
they belong on the public Activity feed.

Run:
    python -m townwatch_etl.jobs.monitor_roster_changes
    python -m townwatch_etl.jobs.monitor_roster_changes --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import sys

from .. import activity
from ..db import connect


def _scope(jurisdiction_slug: str | None) -> tuple[str, list]:
    if jurisdiction_slug:
        return ("AND LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) = LOWER(%s)",
                [jurisdiction_slug])
    return ("", [])


def run(jurisdiction_slug: str | None) -> int:
    clause, params = _scope(jurisdiction_slug)
    joined = left = vacant = 0
    with connect() as conn:
        # ── joined: every term that has begun ──
        for t in conn.execute(
            f"""
            SELECT t.id, t.start_date, t.is_current, o.canonical_name AS name,
                   s.name AS seat_name, gb.name AS body_name, j.id AS jurisdiction_id
            FROM term t
            JOIN official o ON o.id = t.official_id
            JOIN seat s ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE t.start_date IS NOT NULL {clause}
            ORDER BY t.start_date
            """,
            params,
        ).fetchall():
            activity.record(
                conn, t["jurisdiction_id"], "official_joined_seat",
                title=f"{t['name']} joined the {t['body_name']}"
                      + (f" ({t['seat_name']})" if t["seat_name"] else ""),
                ref_kind="term", ref_id=str(t["id"]), once=True,
                occurred_at=t["start_date"],
                meta={"seat": t["seat_name"], "body": t["body_name"], "current": t["is_current"]},
            )
            joined += 1

        # ── left: every term that has ended ──
        for t in conn.execute(
            f"""
            SELECT t.id, t.end_date, o.canonical_name AS name,
                   s.name AS seat_name, gb.name AS body_name, j.id AS jurisdiction_id
            FROM term t
            JOIN official o ON o.id = t.official_id
            JOIN seat s ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE t.end_date IS NOT NULL AND t.end_date <= CURRENT_DATE {clause}
            ORDER BY t.end_date
            """,
            params,
        ).fetchall():
            activity.record(
                conn, t["jurisdiction_id"], "official_left_seat",
                title=f"{t['name']} left the {t['body_name']}"
                      + (f" ({t['seat_name']})" if t["seat_name"] else ""),
                ref_kind="term", ref_id=str(t["id"]), once=True,
                occurred_at=t["end_date"],
                meta={"seat": t["seat_name"], "body": t["body_name"]},
            )
            left += 1

        # ── vacant: a seat that HELD someone before but has no current term now.
        # Requiring a prior ended term avoids flagging merely-unmapped seats. ──
        for s in conn.execute(
            f"""
            SELECT s.id AS seat_id, s.name AS seat_name, gb.name AS body_name,
                   j.id AS jurisdiction_id,
                   (SELECT max(t2.end_date) FROM term t2
                     WHERE t2.seat_id = s.id AND t2.end_date IS NOT NULL) AS vacated_on
            FROM seat s
            JOIN governing_body gb ON gb.id = s.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE NOT EXISTS (SELECT 1 FROM term tc WHERE tc.seat_id = s.id AND tc.is_current)
              AND EXISTS (SELECT 1 FROM term tp WHERE tp.seat_id = s.id AND tp.end_date IS NOT NULL)
              {clause}
            """,
            params,
        ).fetchall():
            activity.record(
                conn, s["jurisdiction_id"], "seat_vacant",
                title=f"{s['seat_name'] or 'A seat'} on the {s['body_name']} is vacant",
                ref_kind="seat", ref_id=str(s["seat_id"]), once=True,
                occurred_at=s["vacated_on"],
                meta={"seat": s["seat_name"], "body": s["body_name"]},
            )
            vacant += 1

    print(f"--- roster changes: {joined} joined, {left} left, {vacant} vacant (once-only) ---")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; default all")
    args = parser.parse_args()
    return run(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())

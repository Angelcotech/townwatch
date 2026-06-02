"""
Public error-report intake + triage — the correction loop.

A faithful mirror of the public record needs a way for the public to flag a
wrong reflection, and a logged trail of how each report was resolved. This is
the ETL-side owner of those writes: the web layer forwards a report here (it
never writes Postgres itself), and triage tooling reads/resolves from here.

Design rule: intake records a CLAIM, it does not alter the record. A report
never flips a datum to 'disputed' on its own — that would let anonymous input
silently suppress an inconvenient vote. Only `accept()` (a human/triage action)
changes data, and even then it's the operator's call whether to re-extract.

Submit a report (used by the intake service):
    from townwatch_etl import corrections
    corrections.submit(entity_type="motion", entity_id=2092,
                       reported_issue="Vote tally says 4-1 but minutes say 5-0",
                       field="vote_tally", suggested_value="5-0")

Triage from the CLI:
    python -m townwatch_etl.corrections --list
    python -m townwatch_etl.corrections --accept 12 --note "fixed, re-extracted"
    python -m townwatch_etl.corrections --reject 13 --note "tally is correct per p.3"
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .db import connect

# Entity types we know how to flag for review when a correction is ACCEPTED.
# Maps entity_type -> table carrying the data_status quarantine columns.
_DISPUTABLE: dict[str, str] = {
    "motion": "motion",
    "agenda_item": "agenda_item",
}

# Guard rails on free-text input from the public endpoint.
_MAX_ISSUE = 2000
_MAX_FIELD = 120
_MAX_SUGGEST = 2000
_MAX_CONTACT = 320
_KNOWN_ENTITIES = {"motion", "vote", "finding", "meeting", "official", "agenda_item"}


def submit(
    *,
    entity_type: str,
    entity_id: int,
    reported_issue: str,
    field: str | None = None,
    suggested_value: str | None = None,
    source_note: str | None = None,
    reporter_contact: str | None = None,
    reporter_ip: str | None = None,
) -> dict[str, Any]:
    """
    Record one public error report. Returns {id, status, deduped}.

    Idempotent against an exact repeat (same entity + same issue text while still
    open) via the partial unique index, so a double-submit collapses instead of
    stacking. Does NOT mutate the referenced datum.
    """
    entity_type = (entity_type or "").strip().lower()
    reported_issue = (reported_issue or "").strip()
    if entity_type not in _KNOWN_ENTITIES:
        raise ValueError(f"unknown entity_type: {entity_type!r}")
    if not isinstance(entity_id, int) or entity_id <= 0:
        raise ValueError("entity_id must be a positive integer")
    if not reported_issue:
        raise ValueError("reported_issue is required")

    # Truncate rather than reject — a citizen shouldn't lose a report to a length
    # cap; we keep the signal and bound the storage.
    reported_issue = reported_issue[:_MAX_ISSUE]
    field = (field or None) and field.strip()[:_MAX_FIELD]
    suggested_value = (suggested_value or None) and suggested_value.strip()[:_MAX_SUGGEST]
    source_note = (source_note or None) and source_note.strip()[:_MAX_SUGGEST]
    reporter_contact = (reporter_contact or None) and reporter_contact.strip()[:_MAX_CONTACT]

    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO data_correction
                (entity_type, entity_id, field, reported_issue, suggested_value,
                 source_note, reporter_contact, reporter_ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id, md5(reported_issue))
                WHERE status = 'open'
                DO NOTHING
            RETURNING id
            """,
            (entity_type, entity_id, field, reported_issue, suggested_value,
             source_note, reporter_contact, reporter_ip),
        ).fetchone()

        if row is not None:
            return {"id": row["id"], "status": "open", "deduped": False}

        # Conflict: an identical open report already exists. Return it.
        existing = conn.execute(
            """
            SELECT id FROM data_correction
            WHERE entity_type = %s AND entity_id = %s
              AND md5(reported_issue) = md5(%s) AND status = 'open'
            LIMIT 1
            """,
            (entity_type, entity_id, reported_issue),
        ).fetchone()
        return {"id": existing["id"] if existing else None, "status": "open", "deduped": True}


def list_pending(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, entity_type, entity_id, field, reported_issue,
                   suggested_value, source_note, reporter_contact, created_at
            FROM data_correction
            WHERE status IN ('open', 'reviewing')
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _resolve(correction_id: int, status: str, note: str | None) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE data_correction
            SET status = %s, resolution_note = %s, resolved_at = now()
            WHERE id = %s
            RETURNING entity_type, entity_id
            """,
            (status, note, correction_id),
        ).fetchone()
        if row is None:
            return False
        # On acceptance, flag the datum for review IF it lives in a table with
        # the data_status quarantine columns. This is the one place a correction
        # touches the record — and only after a human accepted it.
        if status == "accepted":
            table = _DISPUTABLE.get(row["entity_type"])
            if table is not None:
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET data_status = 'disputed',
                        data_status_reason = %s,
                        data_status_at = now()
                    WHERE id = %s
                    """,
                    (f"citizen_correction:#{correction_id}", row["entity_id"]),
                )
        return True


def accept(correction_id: int, note: str | None = None) -> bool:
    """Accept a report: flag the datum disputed (pending re-extract/fix)."""
    return _resolve(correction_id, "accepted", note)


def reject(correction_id: int, note: str | None = None) -> bool:
    return _resolve(correction_id, "rejected", note)


def main() -> int:
    p = argparse.ArgumentParser(description="Triage public data corrections")
    p.add_argument("--list", action="store_true", help="list open/reviewing reports")
    p.add_argument("--accept", type=int, metavar="ID", help="accept a report (flags datum disputed)")
    p.add_argument("--reject", type=int, metavar="ID", help="reject a report")
    p.add_argument("--note", default=None, help="resolution note")
    args = p.parse_args()

    if args.list:
        for r in list_pending():
            print(f"#{r['id']:>4}  {r['entity_type']}#{r['entity_id']}"
                  f"{('.' + r['field']) if r['field'] else ''}  {r['created_at']:%Y-%m-%d}")
            print(f"        {r['reported_issue']}")
            if r["suggested_value"]:
                print(f"        → suggested: {r['suggested_value']}")
        return 0
    if args.accept is not None:
        ok = accept(args.accept, args.note)
        print("accepted" if ok else "not found")
        return 0 if ok else 1
    if args.reject is not None:
        ok = reject(args.reject, args.note)
        print("rejected" if ok else "not found")
        return 0 if ok else 1
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

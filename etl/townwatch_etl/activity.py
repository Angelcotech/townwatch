"""
Activity log — TownWatch's own action history.

The domain layer over migration 041's activity_log table. Compartmentalized:
this is the ONLY module that writes activity_log; every job that does something
worth remembering calls record() here. Mirrors funds.py's discipline (single
writer, append-only, ref_kind/ref_id pointers + JSONB meta).

Milestones only. Do NOT log routine per-document extraction here — it would bury
the signal in the timeline. Log the things a citizen would care to see: a town
added, a build phase finished, a records request generated/sent, a public-comment
digest submitted to the clerk, funding received.

Idempotency: pass once=True for milestones that must appear at most once (a phase
first indexed, a town first added). record() derives a dedupe_key and relies on
the partial UNIQUE index to make a re-emit a no-op. Ordinary events omit once and
can repeat freely.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json


def record(conn, jurisdiction_id: int | None, action_type: str, *, title: str,
           ref_kind: str | None = None, ref_id: str | None = None,
           meta: dict[str, Any] | None = None, actor_user_id: int | None = None,
           occurred_at: Any = None, once: bool = False,
           dedupe_key: str | None = None) -> None:
    """Append one milestone to the activity log.

    jurisdiction_id is None for org-level events. occurred_at defaults to now().
    once=True (or an explicit dedupe_key) makes the insert idempotent: a row with
    the same (jurisdiction_id, dedupe_key) is inserted at most once. When once is
    set without an explicit key, the key is derived from action_type/ref.
    """
    dk = dedupe_key
    if once and dk is None:
        dk = f"{action_type}:{ref_kind or ''}:{ref_id or ''}"

    sql = (
        "INSERT INTO activity_log "
        "(jurisdiction_id, action_type, title, ref_kind, ref_id, meta, "
        " actor_user_id, dedupe_key, occurred_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))"
    )
    if dk is not None:
        # Match the partial unique index (uq_activity_log_dedupe) as the arbiter.
        sql += " ON CONFLICT (jurisdiction_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"

    conn.execute(
        sql,
        (jurisdiction_id, action_type, title, ref_kind, ref_id, Json(meta or {}),
         actor_user_id, dk, occurred_at),
    )


def record_jurisdiction_added(conn, jurisdiction_id: int, display_name: str,
                              occurred_at: Any = None, *,
                              founder_name: str | None = None,
                              founder_user_id: str | None = None,
                              founder_number: int | None = None) -> None:
    """The genesis event: a town joined TownWatch. Idempotent (once-only); the
    first row in every jurisdiction's timeline. occurred_at should be the
    jurisdiction's created_at when backfilling so the timeline reads true.

    When a town is adopted by an operator, pass the founder so the genesis reads
    "Founded by <name>" and the actor is recorded — the founder becomes the
    permanent first line of the town's record. Backfilled/unfounded towns keep
    the plain "<name> added to TownWatch" genesis."""
    if founder_name:
        title = f"Founded by {founder_name}"
        meta = {"founder_user_id": founder_user_id, "founder_number": founder_number}
    else:
        title = f"{display_name} added to TownWatch"
        meta = None
    record(conn, jurisdiction_id, "jurisdiction_added",
           title=title, once=True, occurred_at=occurred_at,
           actor_user_id=None, meta=meta)


def backfill_genesis(conn) -> int:
    """Seed a jurisdiction_added milestone for every existing jurisdiction at its
    created_at. Idempotent — safe to run repeatedly (once-only dedupe). Returns
    the number of jurisdictions processed."""
    rows = conn.execute(
        "SELECT id, display_name, created_at FROM jurisdiction ORDER BY created_at"
    ).fetchall()
    for r in rows:
        record_jurisdiction_added(conn, r["id"], r["display_name"], occurred_at=r["created_at"])
    return len(rows)

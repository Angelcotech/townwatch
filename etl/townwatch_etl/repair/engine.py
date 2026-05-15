"""
Repair engine — dispatches quarantined motions to the right handler.

For every motion with data_status='disputed', the engine:
  1. Pulls the highest-severity QA finding pointing at it
  2. Asks each handler in priority order if it can_handle()
  3. Runs the first claiming handler's repair()
  4. Records the result in motion.meta.repair_log

The engine does NOT clear data_status itself. After all repairs are
attempted, run_patterns is re-executed; the quarantine bridge then
clears motions whose findings disappeared and keeps the rest disputed.

Attempt cap: a motion that has been attempted MAX_ATTEMPTS times without
clearing is marked permanently unrepairable in motion.meta and skipped
on future runs. This prevents loops on hopeless cases.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from .handlers.base import RepairHandler, RepairOutcome, RepairResult
from .handlers.bundled_tally import BundledTallyHandler
from .handlers.petitioner_is_staff import PetitionerIsStaffHandler
from .handlers.voice_vote import VoiceVoteHandler
from .handlers.vote_mismatch import VoteMismatchHandler


MAX_ATTEMPTS = 3


# Handler priority: deterministic / cheap handlers first, so vision calls
# only run when nothing simpler claims the motion.
HANDLER_REGISTRY: list[RepairHandler] = [
    VoiceVoteHandler(),
    BundledTallyHandler(),
    PetitionerIsStaffHandler(),
    VoteMismatchHandler(),
]


def run_repairs(
    conn: psycopg.Connection,
    *,
    data_source_id: int | None = None,
    limit: int | None = None,
    motion_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Iterate every disputed motion and apply the first matching handler.

    Each motion runs inside its own SAVEPOINT — if a handler crashes, that
    motion is rolled back but the rest of the run continues.

    motion_ids: optional explicit list to repair (operator-targeted runs);
                if omitted, every motion with data_status='disputed' is processed.

    Returns a summary dict: {repaired, unrepairable, skipped, errored, total}.
    """
    if motion_ids:
        motions = conn.execute("""
            SELECT m.*, mtg.meeting_date
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE m.id = ANY(%s) AND m.data_status = 'disputed'
            ORDER BY m.id
        """, (motion_ids,)).fetchall()
    else:
        motions = conn.execute("""
            SELECT m.*, mtg.meeting_date
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE m.data_status = 'disputed'
            ORDER BY m.data_status_at NULLS FIRST
        """).fetchall()
    if limit:
        motions = motions[:limit]

    summary = {"repaired": 0, "unrepairable": 0, "skipped": 0, "errored": 0, "no_handler": 0, "total": len(motions)}

    for motion in motions:
        motion = dict(motion)
        # Pipe the run's data_source_id through to handlers that insert rows
        motion["_repair_data_source_id"] = data_source_id

        meta = motion.get("meta") or {}
        repair_log = meta.get("repair_log") or []
        if len(repair_log) >= MAX_ATTEMPTS:
            print(f"  motion #{motion['id']}: max attempts ({MAX_ATTEMPTS}) reached; skipping")
            summary["skipped"] += 1
            continue

        finding = conn.execute("""
            SELECT pattern_id, severity, title, metrics
            FROM finding
            WHERE subject_motion_id = %s AND pattern_id LIKE %s
            ORDER BY severity DESC LIMIT 1
        """, (motion["id"], "qa\\_%")).fetchone()
        if not finding:
            summary["skipped"] += 1
            continue
        finding = dict(finding)

        handler = _select_handler(finding, motion)
        if handler is None:
            print(f"  motion #{motion['id']}: no handler claimed (pattern={finding['pattern_id']})")
            summary["no_handler"] += 1
            if not dry_run:
                _record_attempt(conn, motion["id"], meta, _no_handler_result(finding), dry_run)
            continue

        if dry_run:
            print(f"  motion #{motion['id']} WOULD dispatch to {handler.handler_id} (pattern={finding['pattern_id']})")
            summary["repaired"] += 1   # what would have been attempted
            continue

        print(f"  motion #{motion['id']} → {handler.handler_id} (pattern={finding['pattern_id']})")

        savepoint_name = f"motion_{motion['id']}"
        conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            result = handler.repair(conn, finding, motion)
            if result.outcome == RepairOutcome.ERROR:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception as e:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            result = RepairResult(
                outcome=RepairOutcome.ERROR,
                handler=handler.handler_id,
                notes=f"handler raised: {type(e).__name__}: {e}",
            )

        _record_attempt(conn, motion["id"], meta, result, dry_run)

        if result.outcome == RepairOutcome.REPAIRED:
            summary["repaired"] += 1
            print(f"     ✓ repaired: {result.notes}")
        elif result.outcome == RepairOutcome.UNREPAIRABLE:
            summary["unrepairable"] += 1
            print(f"     ⨯ unrepairable: {result.notes}")
        elif result.outcome == RepairOutcome.ERROR:
            summary["errored"] += 1
            print(f"     ! error: {result.notes}")
        else:
            summary["skipped"] += 1
            print(f"     – skipped: {result.notes}")

    return summary


def _select_handler(finding: dict, motion: dict) -> RepairHandler | None:
    for h in HANDLER_REGISTRY:
        if h.can_handle(finding, motion):
            return h
    return None


def _no_handler_result(finding: dict) -> RepairResult:
    return RepairResult(
        outcome=RepairOutcome.SKIPPED,
        handler="(none)",
        notes=f"No registered handler claimed pattern_id={finding.get('pattern_id')}",
    )


def _record_attempt(
    conn: psycopg.Connection,
    motion_id: int,
    existing_meta: dict,
    result: RepairResult,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    log = list(existing_meta.get("repair_log") or [])
    log.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "handler": result.handler,
        "outcome": result.outcome.value,
        "notes": result.notes,
        "mutations": result.mutations,
    })
    new_meta = {**existing_meta, "repair_log": log}
    conn.execute(
        "UPDATE motion SET meta = %s::jsonb WHERE id = %s",
        (json.dumps(new_meta), motion_id),
    )

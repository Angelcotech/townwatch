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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from ..db import connect
from .handlers.base import RepairHandler, RepairOutcome, RepairResult
from .handlers.bundled_tally import BundledTallyHandler
from .handlers.official_as_petitioner import OfficialAsPetitionerHandler
from .handlers.orphan_official import OrphanOfficialHandler
from .handlers.petitioner_is_staff import PetitionerIsStaffHandler
from .handlers.voice_vote import VoiceVoteHandler
from .handlers.vote_mismatch import VoteMismatchHandler


MAX_ATTEMPTS = 3
DEFAULT_WORKERS = 4


# Motion-subject handlers: deterministic / cheap handlers first so vision
# calls only run when nothing simpler claims the motion.
MOTION_HANDLERS: list[RepairHandler] = [
    VoiceVoteHandler(),
    BundledTallyHandler(),
    OfficialAsPetitionerHandler(),
    PetitionerIsStaffHandler(),
    VoteMismatchHandler(),
]

# Official-subject handlers: findings that point at an official rather
# than a motion (e.g., qa_orphan_official).
OFFICIAL_HANDLERS: list[RepairHandler] = [
    OrphanOfficialHandler(),
]


def run_repairs(
    conn: psycopg.Connection,
    *,
    data_source_id: int | None = None,
    limit: int | None = None,
    motion_ids: list[int] | None = None,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
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

    # Pre-filter: motions over max attempts, dry-run dispatches, and no-handler cases
    # are handled on the main connection (no API calls). Only real handler runs
    # go to the thread pool.
    tasks: list[int] = []   # motion ids that need a worker
    for motion in motions:
        motion = dict(motion)
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
            summary["repaired"] += 1
            continue

        tasks.append(motion["id"])

    if not tasks:
        return summary

    # Parallel dispatch — each worker opens its own connection so transactions
    # don't interfere. workers=1 falls back to sequential for debugging.
    print(f"\n  → dispatching {len(tasks)} motion repair(s) across {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_repair_one_motion, mid, data_source_id): mid for mid in tasks}
        for future in as_completed(futures):
            mid = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = RepairResult(
                    outcome=RepairOutcome.ERROR,
                    handler="(worker)",
                    notes=f"worker raised: {type(e).__name__}: {e}",
                )

            if result.outcome == RepairOutcome.REPAIRED:
                summary["repaired"] += 1
                print(f"  motion #{mid} ✓ repaired: {result.notes}")
            elif result.outcome == RepairOutcome.UNREPAIRABLE:
                summary["unrepairable"] += 1
                print(f"  motion #{mid} ⨯ unrepairable: {result.notes}")
            elif result.outcome == RepairOutcome.ERROR:
                summary["errored"] += 1
                print(f"  motion #{mid} ! error: {result.notes}")
            else:
                summary["skipped"] += 1
                print(f"  motion #{mid} – skipped: {result.notes}")

    return summary


def _repair_one_motion(motion_id: int, data_source_id: int | None) -> RepairResult:
    """
    Worker: open a dedicated connection, run one motion's repair, commit/rollback,
    return RepairResult. Safe to call concurrently from a thread pool because
    each invocation owns its own psycopg connection.
    """
    with connect() as conn:
        motion_row = conn.execute("""
            SELECT m.*, mtg.meeting_date
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE m.id = %s AND m.data_status = 'disputed'
        """, (motion_id,)).fetchone()
        if not motion_row:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler="(worker)",
                notes=f"motion {motion_id} no longer disputed; skipped",
            )
        motion = dict(motion_row)
        motion["_repair_data_source_id"] = data_source_id

        finding = conn.execute("""
            SELECT pattern_id, severity, title, metrics
            FROM finding
            WHERE subject_motion_id = %s AND pattern_id LIKE %s
            ORDER BY severity DESC LIMIT 1
        """, (motion_id, "qa\\_%")).fetchone()
        if not finding:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler="(worker)",
                notes=f"finding for motion {motion_id} disappeared; skipped",
            )
        finding = dict(finding)

        handler = _select_handler(finding, motion)
        if handler is None:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler="(worker)",
                notes=f"no handler for pattern {finding['pattern_id']}",
            )

        # Savepoint-wrap the handler so a crash rolls back its mutations
        # without losing our ability to record the attempt afterward.
        savepoint = f"motion_{motion_id}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = handler.repair(conn, finding, motion)
            if result.outcome == RepairOutcome.ERROR:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as e:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            result = RepairResult(
                outcome=RepairOutcome.ERROR,
                handler=handler.handler_id,
                notes=f"handler raised: {type(e).__name__}: {e}",
            )

        _record_attempt(conn, motion_id, motion.get("meta") or {}, result, dry_run=False)
        # commit is handled by db.connect()'s context manager on success
        return result


def _select_handler(finding: dict, motion: dict) -> RepairHandler | None:
    for h in MOTION_HANDLERS:
        if h.can_handle(finding, motion):
            return h
    return None


def _select_official_handler(finding: dict, official: dict) -> RepairHandler | None:
    for h in OFFICIAL_HANDLERS:
        if h.can_handle(finding, official):
            return h
    return None


def run_official_repairs(conn: psycopg.Connection, *, dry_run: bool = False) -> dict:
    """
    Repair findings whose subject is an official (not a motion).

    Officials don't have a data_status column — once deleted, the finding
    that flagged them is deleted alongside, so they self-resolve. No
    separate quarantine bridge is needed.
    """
    findings = conn.execute("""
        SELECT f.id AS finding_id, f.pattern_id, f.severity, f.title, f.metrics,
               f.subject_official_id
        FROM finding f
        WHERE f.pattern_id LIKE %s
          AND f.subject_official_id IS NOT NULL
        ORDER BY f.severity DESC
    """, ("qa\\_%",)).fetchall()

    summary = {"repaired": 0, "unrepairable": 0, "skipped": 0, "errored": 0, "no_handler": 0, "total": len(findings)}

    for finding in findings:
        finding = dict(finding)
        official = conn.execute(
            "SELECT * FROM official WHERE id = %s", (finding["subject_official_id"],)
        ).fetchone()
        if not official:
            summary["skipped"] += 1
            continue
        official = dict(official)

        handler = _select_official_handler(finding, official)
        if handler is None:
            summary["no_handler"] += 1
            continue

        if dry_run:
            print(f"  official #{official['id']} '{official['canonical_name']}' "
                  f"WOULD dispatch to {handler.handler_id} (pattern={finding['pattern_id']})")
            summary["repaired"] += 1
            continue

        print(f"  official #{official['id']} '{official['canonical_name']}' → "
              f"{handler.handler_id} (pattern={finding['pattern_id']})")

        savepoint_name = f"official_{official['id']}"
        conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            result = handler.repair(conn, finding, official)
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

        if result.outcome == RepairOutcome.REPAIRED:
            summary["repaired"] += 1
            print(f"     ✓ {result.notes}")
        elif result.outcome == RepairOutcome.UNREPAIRABLE:
            summary["unrepairable"] += 1
            print(f"     ⨯ {result.notes}")
        elif result.outcome == RepairOutcome.ERROR:
            summary["errored"] += 1
            print(f"     ! {result.notes}")
        else:
            summary["skipped"] += 1
            print(f"     – {result.notes}")

    return summary


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

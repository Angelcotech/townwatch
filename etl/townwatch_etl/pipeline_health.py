"""
Pipeline health — the automation's own operational status.

The domain layer over migration 050's pipeline_run + pipeline_issue. Compartmentalized:
this is the ONLY module that writes those two tables (mirrors activity.py / funds.py —
single writer, append/upsert discipline).

Two concerns, kept separate from compliance_finding (which audits the published RECORDS):

  * pipeline_run   — a heartbeat. record_run() writes one row per per-jurisdiction
    daily_refresh run (outcome + steps + what surfaced). Reading the latest row per
    jurisdiction answers "is the pipeline still running for this town?"

  * pipeline_issue — a deduplicated, resolvable problem. observe_issue() upserts one
    row per (jurisdiction, dedupe_key): a recurrence refreshes it, a recurrence after
    resolution reopens it, and close_issue() auto-resolves it when the condition clears
    — the same observe/upsert/close discipline as refresh_findings.upsert_finding.
    resolve_issue() is the human/agent "mark fixed". A 'wont_fix' issue is never
    auto-reopened (an explicit suppression).
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json


# ---------------------------------------------------------------- heartbeat

def record_run(conn, jurisdiction_id: int, *, outcome: str,
               started_at: Any, finished_at: Any = None, trigger: str = "cron",
               steps: list[dict] | None = None, surfaced: dict | None = None,
               error_count: int = 0) -> int:
    """Append one run heartbeat. outcome ∈ ok|partial|failed|paused. Returns row id."""
    row = conn.execute(
        """
        INSERT INTO pipeline_run
            (jurisdiction_id, trigger, started_at, finished_at, outcome,
             steps, surfaced, error_count)
        VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s::jsonb, %s::jsonb, %s)
        RETURNING id
        """,
        (jurisdiction_id, trigger, started_at, finished_at, outcome,
         Json(steps or []), Json(surfaced or {}), error_count),
    ).fetchone()
    return row["id"]


def recent_runs(conn, jurisdiction_id: int, limit: int = 10) -> list[dict]:
    return conn.execute(
        "SELECT id, trigger, started_at, finished_at, outcome, steps, surfaced, error_count "
        "FROM pipeline_run WHERE jurisdiction_id = %s ORDER BY started_at DESC LIMIT %s",
        (jurisdiction_id, limit),
    ).fetchall()


# ---------------------------------------------------------------- issues

def observe_issue(conn, jurisdiction_id: int, *, issue_type: str, dedupe_key: str,
                  title: str, detail: str | None = None, severity: str = "medium",
                  context: dict | None = None) -> tuple[str, int]:
    """Upsert one issue for (jurisdiction_id, dedupe_key). Returns (action, id) where
    action ∈ inserted|reopened|updated. Mirrors refresh_findings.upsert_finding:
    SELECT-then-branch so the lifecycle is explicit.

    - none exists                  → INSERT (open)
    - exists & resolved            → REOPEN (status=open, reset first_observed_at, clear resolution)
    - exists & open                → UPDATE (refresh last_observed_at + fields)
    - exists & wont_fix            → UPDATE fields but stay suppressed (no reopen)
    """
    existing = conn.execute(
        "SELECT id, status FROM pipeline_issue WHERE jurisdiction_id = %s AND dedupe_key = %s",
        (jurisdiction_id, dedupe_key),
    ).fetchone()

    if existing is None:
        row = conn.execute(
            """
            INSERT INTO pipeline_issue
                (jurisdiction_id, issue_type, severity, title, detail, status,
                 context, dedupe_key, first_observed_at, last_observed_at)
            VALUES (%s, %s, %s, %s, %s, 'open', %s::jsonb, %s, now(), now())
            RETURNING id
            """,
            (jurisdiction_id, issue_type, severity, title, detail,
             Json(context or {}), dedupe_key),
        ).fetchone()
        return "inserted", row["id"]

    if existing["status"] == "wont_fix":
        conn.execute(
            "UPDATE pipeline_issue SET severity = %s, title = %s, detail = %s, "
            "context = %s::jsonb, last_observed_at = now() WHERE id = %s",
            (severity, title, detail, Json(context or {}), existing["id"]),
        )
        return "updated", existing["id"]

    if existing["status"] == "resolved":
        conn.execute(
            "UPDATE pipeline_issue SET severity = %s, title = %s, detail = %s, "
            "context = %s::jsonb, status = 'open', first_observed_at = now(), "
            "last_observed_at = now(), resolved_at = NULL, resolved_by = NULL "
            "WHERE id = %s",
            (severity, title, detail, Json(context or {}), existing["id"]),
        )
        return "reopened", existing["id"]

    # open → refresh
    conn.execute(
        "UPDATE pipeline_issue SET severity = %s, title = %s, detail = %s, "
        "context = %s::jsonb, last_observed_at = now() WHERE id = %s",
        (severity, title, detail, Json(context or {}), existing["id"]),
    )
    return "updated", existing["id"]


def close_issue(conn, jurisdiction_id: int, dedupe_key: str, *, reason: str | None = None) -> bool:
    """Auto-resolve an OPEN issue whose condition no longer holds (the observer found
    it healthy again). Leaves wont_fix untouched. Returns True if one was closed."""
    row = conn.execute(
        "UPDATE pipeline_issue SET status = 'resolved', resolved_at = now(), "
        "resolved_by = 'auto-cleared', "
        "fix_notes = COALESCE(%s, 'condition no longer observed at refresh'), "
        "last_observed_at = now() "
        "WHERE jurisdiction_id = %s AND dedupe_key = %s AND status = 'open' RETURNING id",
        (reason, jurisdiction_id, dedupe_key),
    ).fetchone()
    return row is not None


def resolve_issue(conn, issue_id: int, *, resolved_by: str, status: str = "resolved",
                  notes: str | None = None, diagnosis: str | None = None) -> bool:
    """Mark an open issue fixed (or wont_fix). The human/agent action — also appends
    a pipeline_fix row so the resolution accumulates in the knowledge base (the
    observer's auto-clear does NOT, since it isn't a troubleshooting fix). Returns
    True if the row was open and got updated."""
    row = conn.execute(
        "UPDATE pipeline_issue SET status = %s, resolved_at = now(), resolved_by = %s, "
        "fix_notes = COALESCE(%s, fix_notes), diagnosis = COALESCE(%s, diagnosis) "
        "WHERE id = %s AND status = 'open' RETURNING jurisdiction_id, dedupe_key",
        (status, resolved_by, notes, diagnosis, issue_id),
    ).fetchone()
    if row is None:
        return False
    record_fix(conn, issue_id=issue_id, jurisdiction_id=row["jurisdiction_id"],
               dedupe_key=row["dedupe_key"], resolution=status,
               diagnosis=diagnosis, fix_notes=notes, resolved_by=resolved_by)
    return True


# ---------------------------------------------------------------- fix log (KB)

def record_fix(conn, *, issue_id: int | None, jurisdiction_id: int | None,
               dedupe_key: str, resolution: str, diagnosis: str | None,
               fix_notes: str | None, resolved_by: str) -> None:
    """Append one resolution to the knowledge base (append-only; never overwritten)."""
    conn.execute(
        "INSERT INTO pipeline_fix "
        "(issue_id, jurisdiction_id, dedupe_key, resolution, diagnosis, fix_notes, resolved_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (issue_id, jurisdiction_id, dedupe_key, resolution, diagnosis, fix_notes, resolved_by),
    )


def fixes_for(conn, dedupe_key: str, limit: int = 10) -> list[dict]:
    """Prior resolutions for a problem CLASS (across jurisdictions) — the 'how did
    we fix this last time' panel shown at triage time."""
    return conn.execute(
        "SELECT f.created_at, f.resolution, f.diagnosis, f.fix_notes, f.resolved_by, "
        "       j.display_name AS jurisdiction "
        "FROM pipeline_fix f LEFT JOIN jurisdiction j ON j.id = f.jurisdiction_id "
        "WHERE f.dedupe_key = %s ORDER BY f.created_at DESC LIMIT %s",
        (dedupe_key, limit),
    ).fetchall()


def list_fixes(conn, *, grep: str | None = None, limit: int = 50) -> list[dict]:
    """The whole fix knowledge base, newest first. `grep` filters across the
    dedupe_key / diagnosis / fix_notes text."""
    sql = (
        "SELECT f.id, f.created_at, f.dedupe_key, f.resolution, f.diagnosis, f.fix_notes, "
        "       f.resolved_by, j.display_name AS jurisdiction "
        "FROM pipeline_fix f LEFT JOIN jurisdiction j ON j.id = f.jurisdiction_id WHERE TRUE"
    )
    params: list[Any] = []
    if grep:
        sql += (" AND (f.dedupe_key ILIKE %s OR f.diagnosis ILIKE %s OR f.fix_notes ILIKE %s)")
        like = f"%{grep}%"
        params += [like, like, like]
    sql += " ORDER BY f.created_at DESC LIMIT %s"
    params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def list_issues(conn, *, jurisdiction_id: int | None = None, status: str | None = "open",
                limit: int = 200) -> list[dict]:
    sql = (
        "SELECT i.id, i.jurisdiction_id, j.display_name AS jurisdiction, j.state_abbr, "
        "       i.issue_type, i.severity, i.title, i.detail, i.status, "
        "       i.first_observed_at, i.last_observed_at, i.resolved_at, i.resolved_by, i.dedupe_key "
        "FROM pipeline_issue i JOIN jurisdiction j ON j.id = i.jurisdiction_id WHERE TRUE"
    )
    params: list[Any] = []
    if status:
        sql += " AND i.status = %s"
        params.append(status)
    if jurisdiction_id is not None:
        sql += " AND i.jurisdiction_id = %s"
        params.append(jurisdiction_id)
    sql += (" ORDER BY CASE i.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "i.last_observed_at DESC LIMIT %s")
    params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def get_issue(conn, issue_id: int) -> dict | None:
    return conn.execute(
        "SELECT i.*, j.display_name AS jurisdiction, j.state_abbr "
        "FROM pipeline_issue i JOIN jurisdiction j ON j.id = i.jurisdiction_id WHERE i.id = %s",
        (issue_id,),
    ).fetchone()

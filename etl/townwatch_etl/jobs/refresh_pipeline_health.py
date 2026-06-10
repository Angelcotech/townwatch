"""
refresh_pipeline_health — derive operational ISSUES from pipeline state.

The interpreter half of the pipeline-health system (daily_refresh records the raw
facts: per-jurisdiction run heartbeats + step_failed issues). This observer, run as a
jurisdiction-agnostic step (like refresh_findings), turns the rest of the signal into
deduplicated, resolvable pipeline_issues:

  1. STALE — a jurisdiction whose most recent pipeline_run is older than the threshold
     (the cron stopped running for it). Closes when a fresh run appears.
  2. JOB FAILURES — rolls up unresolved pipeline_failure rows (the rich per-item errors
     jobs log, with tracebacks) into one issue per (jurisdiction, job, step), so a human
     or Claude Code sees an actionable problem instead of a wall of log rows. Closes when
     no unresolved failures of that kind remain.

Observe/close discipline mirrors refresh_findings.upsert_finding + close_resolved_finding.
All writes go through pipeline_health.py (the single writer). Read-mostly, no spend.

Run:
    python -m townwatch_etl.jobs.refresh_pipeline_health
    python -m townwatch_etl.jobs.refresh_pipeline_health --jurisdiction grovetown-ga --dry-run
"""

from __future__ import annotations

import argparse
import sys

from ..db import connect
from .. import pipeline_health


# A jurisdiction that hasn't run in this long has effectively fallen off the cron.
# Daily cron + buffer for one missed run. Paused-fund jurisdictions still record a
# heartbeat every run, so staleness means "not running at all", not "not funded".
STALE_AFTER = "36 hours"


def _resolve_fips(slug: str) -> str:
    from ..jurisdiction import load_config, jurisdiction_fips
    return jurisdiction_fips(load_config(slug))


def _check_stale(conn, fips: str | None, dry_run: bool) -> tuple[int, int]:
    """Open a pipeline_stale issue for each jurisdiction whose last run is too old;
    close it for those running again. Only jurisdictions with a prior run are judged
    (a never-run town has no baseline — it gets one on its next daily_refresh)."""
    sql = (
        "SELECT j.id, j.display_name, max(r.started_at) AS last_run, "
        "       now() - max(r.started_at) > %s::interval AS stale "
        "FROM pipeline_run r JOIN jurisdiction j ON j.id = r.jurisdiction_id "
    )
    params: list = [STALE_AFTER]
    if fips:
        sql += "WHERE j.fips_code = %s "
        params.append(fips)
    sql += "GROUP BY j.id, j.display_name"
    rows = conn.execute(sql, tuple(params)).fetchall()

    opened = closed = 0
    for r in rows:
        key = "pipeline_stale"
        if r["stale"]:
            opened += 1
            if not dry_run:
                pipeline_health.observe_issue(
                    conn, r["id"], issue_type="pipeline_stale", dedupe_key=key,
                    severity="high",
                    title=f"Pipeline stale — no run since {r['last_run']:%Y-%m-%d %H:%M}",
                    detail=(f"{r['display_name']} has had no daily_refresh run in over {STALE_AFTER}. "
                            f"The cron may have stopped, the run may be hanging, or the jurisdiction "
                            f"lock may be stuck. Check Railway cron logs and run_lock for this town."),
                    context={"last_run": str(r["last_run"])},
                )
            print(f"  ⚠ stale: {r['display_name']} (last run {r['last_run']})")
        else:
            if not dry_run and pipeline_health.close_issue(conn, r["id"], key,
                                                           reason="ran again"):
                closed += 1
    return opened, closed


def _rollup_failures(conn, fips: str | None, dry_run: bool) -> tuple[int, int]:
    """One issue per (jurisdiction, job_name, step) with unresolved pipeline_failure
    rows. Resolves each failure to a jurisdiction via governing_body / meeting / the
    context jurisdiction_id. Closes job_failure issues with no surviving failures."""
    sql = """
        SELECT f.jid, j.display_name, f.job_name, f.step,
               count(*) AS n, max(f.created_at) AS latest,
               (array_agg(f.message ORDER BY f.created_at DESC))[1] AS latest_message,
               (array_agg(f.id ORDER BY f.created_at DESC))[1:10] AS failure_ids
        FROM (
            SELECT pf.id, pf.job_name, pf.step, pf.message, pf.created_at,
                   COALESCE(gb1.jurisdiction_id, gb2.jurisdiction_id,
                            (pf.context->>'jurisdiction_id')::int) AS jid
            FROM pipeline_failure pf
            LEFT JOIN governing_body gb1 ON gb1.id = pf.governing_body_id
            LEFT JOIN meeting m ON m.id = pf.meeting_id
            LEFT JOIN governing_body gb2 ON gb2.id = m.governing_body_id
            WHERE pf.resolved_at IS NULL
        ) f
        JOIN jurisdiction j ON j.id = f.jid
        WHERE f.jid IS NOT NULL
    """
    params: list = []
    if fips:
        sql += " AND j.fips_code = %s"
        params.append(fips)
    sql += " GROUP BY f.jid, j.display_name, f.job_name, f.step"
    groups = conn.execute(sql, tuple(params)).fetchall()

    observed: set[tuple[int, str]] = set()
    opened = 0
    for g in groups:
        key = f"failure:{g['job_name']}:{g['step'] or ''}"
        observed.add((g["jid"], key))
        opened += 1
        if dry_run:
            print(f"  ✗ {g['display_name']}: {g['job_name']}"
                  f"{':' + g['step'] if g['step'] else ''} ({g['n']}×)")
            continue
        step_txt = f" step `{g['step']}`" if g["step"] else ""
        pipeline_health.observe_issue(
            conn, g["jid"], issue_type="job_failure", dedupe_key=key,
            severity="high" if g["n"] >= 5 else "medium",
            title=f"{g['job_name']}{step_txt} failing ({g['n']}×)",
            detail=(f"{g['n']} unresolved failure(s) in `{g['job_name']}`{step_txt}. "
                    f"Latest: {g['latest_message']}. Tracebacks are in pipeline_failure "
                    f"ids {list(g['failure_ids'])}. Fix the root cause, then resolve."),
            context={"job_name": g["job_name"], "step": g["step"],
                     "failure_ids": list(g["failure_ids"]), "latest": str(g["latest"])},
        )

    # Close job_failure issues whose underlying failures are all resolved/gone.
    closed = 0
    open_rows = conn.execute(
        "SELECT id, jurisdiction_id, dedupe_key FROM pipeline_issue "
        "WHERE status = 'open' AND issue_type = 'job_failure'"
        + (" AND jurisdiction_id IN (SELECT id FROM jurisdiction WHERE fips_code = %s)" if fips else ""),
        ((fips,) if fips else ()),
    ).fetchall()
    for row in open_rows:
        if (row["jurisdiction_id"], row["dedupe_key"]) not in observed:
            if not dry_run and pipeline_health.close_issue(
                conn, row["jurisdiction_id"], row["dedupe_key"], reason="failures resolved"):
                closed += 1
    return opened, closed


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive pipeline-health issues from run/failure state.")
    ap.add_argument("--jurisdiction", help="restrict to one slug (per-jurisdiction pipeline use)")
    ap.add_argument("--dry-run", action="store_true", help="report; no issue writes")
    args = ap.parse_args()

    fips = _resolve_fips(args.jurisdiction) if args.jurisdiction else None
    with connect() as conn:
        s_open, s_closed = _check_stale(conn, fips, args.dry_run)
        f_open, f_closed = _rollup_failures(conn, fips, args.dry_run)
    print(f"pipeline-health: stale(open={s_open} closed={s_closed}) "
          f"failures(observed={f_open} closed={f_closed})"
          + ("  [dry-run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

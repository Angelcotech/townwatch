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
  3. SOURCE DRIFT — absence signals that look like a site/service CHANGE rather than a
     publishing gap: a body whose inventory has gone cold relative to its own cadence,
     or whose recent documents are dying (placeholder spike). These open recon_needed
     issues that route to the recon-jurisdiction skill (web-search pass + structure
     sweep) — the GA audit showed sites migrate platforms, restructure URLs, and move
     content behind new portals while a naive scraper quietly reports "nothing new".

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


def _check_source_drift(conn, fips: str | None, dry_run: bool) -> tuple[int, int]:
    """Open a recon_needed issue when a body's source looks like it MOVED.

    Two signals, both judged against the body's own history (no per-platform
    config needed):

      cold_inventory — no meeting newer than max(3× the body's median
          inter-meeting gap, 90 days). A monthly board that suddenly has no
          meetings for a quarter hasn't gone quiet — its listing page most
          likely moved (new CMS, new portal, restructured URLs).
      dead_documents — ≥3 placeholder-flagged documents among the last 180
          days' meetings AND more than half of that window's documents are
          placeholders. Documents dying en masse means the file host or URL
          scheme changed, not that the clerk un-published everything.

    The issue is a RECON worklist item, not a code bug: resolution is running
    the recon-jurisdiction skill (independent web search + section-structure
    sweep) and updating config/registry with attestations."""
    rows = conn.execute(
        """
        WITH gaps AS (
          SELECT m.governing_body_id,
                 m.meeting_date - lag(m.meeting_date) OVER (
                     PARTITION BY m.governing_body_id ORDER BY m.meeting_date) AS gap
          FROM meeting m
        ),
        cadence AS (
          SELECT governing_body_id,
                 percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap
          FROM gaps WHERE gap IS NOT NULL AND gap > 0
          GROUP BY governing_body_id
          HAVING count(*) >= 5
        )
        SELECT gb.id AS body_id, gb.name AS body_name, j.id AS jid,
               j.display_name, j.fips_code,
               max(m.meeting_date) AS last_meeting,
               c.median_gap,
               (CURRENT_DATE - max(m.meeting_date)) AS days_silent,
               count(*) FILTER (
                 WHERE m.meeting_date >= CURRENT_DATE - 180
                   AND (COALESCE(m.agenda_is_placeholder, false)
                        OR COALESCE(m.minutes_is_placeholder, false))
               ) AS recent_placeholders,
               count(*) FILTER (WHERE m.meeting_date >= CURRENT_DATE - 180
                                  AND m.meeting_date <= CURRENT_DATE) AS recent_meetings
        FROM governing_body gb
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        JOIN meeting m ON m.governing_body_id = gb.id
        JOIN cadence c ON c.governing_body_id = gb.id
        """ + (" WHERE j.fips_code = %s" if fips else "") + """
        GROUP BY gb.id, gb.name, j.id, j.display_name, j.fips_code, c.median_gap
        """,
        ((fips,) if fips else ()),
    ).fetchall()

    observed: set[tuple[int, str]] = set()
    opened = 0
    for r in rows:
        median_days = float(r["median_gap"] or 30)
        threshold = max(3 * median_days, 90)
        drifts: list[tuple[str, str]] = []
        if float(r["days_silent"]) > threshold:
            drifts.append(("cold_inventory",
                f"no meeting inventoried in {r['days_silent']} days (median cadence "
                f"{median_days:.0f}d, threshold {threshold:.0f}d) — the listing page "
                f"has likely moved or changed platform"))
        if (r["recent_placeholders"] or 0) >= 3 and r["recent_meetings"] and \
                r["recent_placeholders"] > r["recent_meetings"] / 2:
            drifts.append(("dead_documents",
                f"{r['recent_placeholders']} of {r['recent_meetings']} recent meetings "
                f"have placeholder/dead documents — the file host or URL scheme has "
                f"likely changed"))
        for kind, why in drifts:
            key = f"recon_drift:{kind}:{r['body_id']}"
            observed.add((r["jid"], key))
            opened += 1
            if dry_run:
                print(f"  ⚠ drift: {r['display_name']} — {r['body_name']}: {why}")
                continue
            pipeline_health.observe_issue(
                conn, r["jid"], issue_type="recon_needed", dedupe_key=key,
                severity="medium",
                title=f"Source drift suspected: {r['body_name']} ({kind})",
                detail=(f"{why}. This is a RECON task, not a code bug: run the "
                        f"recon-jurisdiction skill — independent web search "
                        f"('<name> agenda', '<name> minutes', site:<domain>) plus a "
                        f"section-structure sweep — to find where the source moved, "
                        f"then update the jurisdiction config and recon registry "
                        f"with attestations."),
                context={"governing_body_id": r["body_id"], "kind": kind,
                         "last_meeting": str(r["last_meeting"]),
                         "median_gap_days": median_days},
            )

    # Close drift issues whose condition cleared (fresh meetings / live docs).
    closed = 0
    open_rows = conn.execute(
        "SELECT id, jurisdiction_id, dedupe_key FROM pipeline_issue "
        "WHERE status = 'open' AND issue_type = 'recon_needed' AND dedupe_key LIKE %s"
        + (" AND jurisdiction_id IN (SELECT id FROM jurisdiction WHERE fips_code = %s)" if fips else ""),
        (("recon_drift:%", fips) if fips else ("recon_drift:%",)),
    ).fetchall()
    for row in open_rows:
        if (row["jurisdiction_id"], row["dedupe_key"]) not in observed:
            if not dry_run and pipeline_health.close_issue(
                conn, row["jurisdiction_id"], row["dedupe_key"], reason="source activity resumed"):
                closed += 1
    return opened, closed


def _rollup_failures(conn, fips: str | None, dry_run: bool) -> tuple[int, int]:
    """One issue per (jurisdiction, job_name, step) with unresolved pipeline_failure
    rows. Resolves each failure to a jurisdiction via governing_body / meeting / the
    context jurisdiction_id; failures attributable to NO jurisdiction (cron-level
    crashes, org-wide ingest jobs) roll up into ORG-LEVEL issues (jurisdiction_id
    NULL — same convention as the env-key checks) so nothing stays invisible to the
    health worklist. Closes job_failure issues with no surviving failures."""
    sql = """
        SELECT f.jid, COALESCE(j.display_name, '(org)') AS display_name,
               f.job_name, f.step,
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
        LEFT JOIN jurisdiction j ON j.id = f.jid
    """
    params: list = []
    if fips:
        # A scoped (per-jurisdiction) run only reconciles its own issues; org-level
        # rows are the unscoped daily run's responsibility, mirroring how scoped
        # daily_refresh runs skip the jurisdiction-agnostic steps.
        sql += " WHERE f.jid IS NOT NULL AND j.fips_code = %s"
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
        d_open, d_closed = _check_source_drift(conn, fips, args.dry_run)
    print(f"pipeline-health: stale(open={s_open} closed={s_closed}) "
          f"failures(observed={f_open} closed={f_closed}) "
          f"drift(observed={d_open} closed={d_closed})"
          + ("  [dry-run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

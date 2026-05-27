"""
Refresh compliance_finding rows for every governing body.

Each finding category has its own observation function that returns
either a row to upsert or None. The orchestrator runs all categories
against every body and upserts the result. Findings that were
previously open but no longer observed get transitioned to
closed_with_records (records arrived) so we keep audit history.

Run:
    python -m townwatch_etl.jobs.refresh_findings
    python -m townwatch_etl.jobs.refresh_findings --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.refresh_findings --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..audit import finding_statute, record_failure
from ..db import connect


@dataclass
class ObservedFinding:
    """What an observer returns when a gap is currently present.

    Statute fields are populated from the per-state config (audit.finding_statute)
    so observers stay state-agnostic and don't hardcode GA citations.
    """
    category: str
    severity: str
    statute_label: str
    statute_url: str
    statute_text: str
    count: int
    since_date: date | None


# =====================================================================
# Observers — each function looks at current meeting data for a body
# and returns an ObservedFinding if the gap is present, else None.
# Add a new observer to the OBSERVERS list to add a new finding category.
# =====================================================================


def observe_minutes_missing(conn, body_id: int, state_abbr: str) -> ObservedFinding | None:
    """The 'trail went cold' finding: meetings AFTER the last meeting that
    had minutes published, with no minutes_url and no explicit unavailable
    flag in meta. This is the dramatic finding rather than an all-time
    tally that lumps in sporadic old gaps.

    Statute citation is pulled from the state config so the same observer
    works for GA, TN, FL, etc. — the gap-detection SQL is state-agnostic,
    only the legal citation changes per jurisdiction.
    """
    row = conn.execute(
        """
        WITH last_with_minutes AS (
          SELECT MAX(meeting_date) AS d FROM meeting
          WHERE governing_body_id = %s AND minutes_url IS NOT NULL
        )
        SELECT COUNT(*) AS count, MIN(m.meeting_date) AS since
        FROM meeting m
        WHERE m.governing_body_id = %s
          AND m.meeting_date < CURRENT_DATE
          AND m.minutes_url IS NULL
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'agenda_unavailable')
          AND m.meeting_date > COALESCE((SELECT d FROM last_with_minutes), DATE '1900-01-01')
        """,
        (body_id, body_id),
    ).fetchone()
    count = row["count"] or 0
    since = row["since"]
    if count < 2:
        return None
    statute = finding_statute(state_abbr, "minutes_missing")
    return ObservedFinding(
        category="minutes_missing",
        severity="high" if count >= 10 else "medium",
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=count,
        since_date=since,
    )


def observe_member_roster_missing(conn, body_id: int, state_abbr: str) -> ObservedFinding | None:
    """Member roster not indexed for this body.

    Only fires for non-elected bodies (appointed boards/commissions) — for
    Council we know seats are populated. The signal is zero current members
    in the term table. Doesn't fire while members exist; goes away when the
    roster lands in the DB.
    """
    row = conn.execute(
        """
        SELECT
            gb.body_type,
            (SELECT COUNT(DISTINCT t.official_id)
             FROM term t JOIN seat s ON s.id = t.seat_id
             WHERE s.governing_body_id = gb.id AND t.is_current = true) AS current_members
        FROM governing_body gb
        WHERE gb.id = %s
        """,
        (body_id,),
    ).fetchone()
    body_type = row["body_type"]
    current = row["current_members"] or 0
    # Skip elected bodies — those have a separate scraper that should pull
    # the council roster directly from the official directory page.
    if body_type in ("city_council", "county_commission", "school_board"):
        return None
    if current > 0:
        return None
    statute = finding_statute(state_abbr, "member_roster_missing")
    return ObservedFinding(
        category="member_roster_missing",
        severity="medium",
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=1,
        since_date=None,
    )


def observe_campaign_finance_missing(conn, body_id: int, state_abbr: str) -> ObservedFinding | None:
    """No campaign finance filings on record for the body's elected officials.

    Fires on ELECTED bodies (city_council, county_commission, school_board,
    state legislatures, federal offices) where there are zero
    campaign_contribution rows for any current member within the last
    eight years (covers two full council cycles in GA + most analogous
    structures). For appointed bodies the obligation doesn't apply, so
    the observer is silent.

    Counts officials whose terms intersect "current" — the absence is per
    BODY, not per official, because the records request covers all
    sitting members in one ask.
    """
    EIGHT_YEARS_AGO = "(CURRENT_DATE - INTERVAL '8 years')"
    row = conn.execute(
        f"""
        SELECT
            gb.body_type,
            (SELECT COUNT(DISTINCT t.official_id)
             FROM term t JOIN seat s ON s.id = t.seat_id
             WHERE s.governing_body_id = gb.id AND t.is_current = true) AS current_members,
            (SELECT COUNT(*)
             FROM campaign_contribution cc
             JOIN term t ON t.official_id = cc.official_id
             JOIN seat s ON s.id = t.seat_id
             WHERE s.governing_body_id = gb.id
               AND t.is_current = true
               AND cc.contribution_date >= {EIGHT_YEARS_AGO}) AS contributions_count
        FROM governing_body gb
        WHERE gb.id = %s
        """,
        (body_id,),
    ).fetchone()
    body_type = row["body_type"]
    current_members = row["current_members"] or 0
    contributions_count = row["contributions_count"] or 0
    # Only elected bodies have CCDR-equivalent obligations.
    elected_types = (
        "city_council", "county_commission", "school_board",
        "board_of_education",  # GA convention varies
    )
    if body_type not in elected_types:
        return None
    if current_members == 0:
        # No officials to require filings from — surface this via
        # member_roster_missing instead; don't double-flag.
        return None
    if contributions_count > 0:
        return None
    statute = finding_statute(state_abbr, "campaign_finance_missing")
    return ObservedFinding(
        category="campaign_finance_missing",
        severity="high",
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=current_members,  # number of sitting officials with no filings on record
        since_date=None,
    )


def observe_meeting_notice_too_short(conn, body_id: int, state_abbr: str) -> ObservedFinding | None:
    """Recurring pattern of short-notice REGULAR meetings.

    Counts regular meetings in the last 24 months whose agenda was
    posted within fewer than 3 calendar days of the meeting. Special,
    emergency, executive_session, and workshop meetings are excluded
    because their statutory notice requirements differ.

    Records where agenda_posted_at is after the meeting_date are
    excluded too — those are bulk-archival uploads of historical
    documents, not notice signals.

    Severity:
      - high if any meeting was posted same-day-or-later (notice < 1 day)
      - medium otherwise (legal but below transparency norms)
    """
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS short_count,
          MIN(m.meeting_date - m.agenda_posted_at::date) AS min_notice_days
        FROM meeting m
        WHERE m.governing_body_id = %s
          AND m.meeting_type = 'regular'
          AND m.agenda_posted_at IS NOT NULL
          AND m.agenda_posted_at::date <= m.meeting_date
          AND m.meeting_date >= CURRENT_DATE - INTERVAL '24 months'
          AND (m.meeting_date - m.agenda_posted_at::date) < 3
        """,
        (body_id,),
    ).fetchone()
    short_count = row["short_count"] or 0
    if short_count < 2:
        return None
    min_notice = row["min_notice_days"]
    severity = "high" if (min_notice is not None and min_notice < 1) else "medium"
    statute = finding_statute(state_abbr, "meeting_notice_too_short")
    return ObservedFinding(
        category="meeting_notice_too_short",
        severity=severity,
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=short_count,
        since_date=None,
    )


OBSERVERS: list[Callable] = [
    observe_minutes_missing,
    observe_member_roster_missing,
    observe_campaign_finance_missing,
    observe_meeting_notice_too_short,
    # Add new finding categories here: observe_agenda_missing,
    # observe_attendance_missing, ...
]


# =====================================================================
# Orchestrator
# =====================================================================


def upsert_finding(conn, body_id: int, obs: ObservedFinding) -> tuple[str, int]:
    """Insert new finding OR update an existing one for this (body, category).
    Returns (action, finding_id). action is 'inserted','updated','reopened'.
    """
    existing = conn.execute(
        "SELECT id, status FROM compliance_finding WHERE governing_body_id = %s AND category = %s",
        (body_id, obs.category),
    ).fetchone()
    if existing is None:
        row = conn.execute(
            """
            INSERT INTO compliance_finding (
                governing_body_id, category, severity, statute_label, statute_url,
                statute_text, count, since_date, status, opened_at, last_observed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', now(), now())
            RETURNING id
            """,
            (body_id, obs.category, obs.severity, obs.statute_label, obs.statute_url,
             obs.statute_text, obs.count, obs.since_date),
        ).fetchone()
        return "inserted", row["id"]
    # If previously closed and now reopened, reset status to 'open' and clear close info.
    if existing["status"] in ("closed_with_records", "closed_as_unrecoverable", "closed_acknowledged"):
        conn.execute(
            """
            UPDATE compliance_finding
            SET severity = %s, statute_label = %s, statute_url = %s, statute_text = %s,
                count = %s, since_date = %s,
                status = 'open', opened_at = now(), last_observed_at = now(),
                closed_at = NULL, closed_reason = NULL
            WHERE id = %s
            """,
            (obs.severity, obs.statute_label, obs.statute_url, obs.statute_text,
             obs.count, obs.since_date, existing["id"]),
        )
        return "reopened", existing["id"]
    # Active finding — just refresh the metrics + last_observed_at, preserve status.
    conn.execute(
        """
        UPDATE compliance_finding
        SET severity = %s, statute_label = %s, statute_url = %s, statute_text = %s,
            count = %s, since_date = %s, last_observed_at = now()
        WHERE id = %s
        """,
        (obs.severity, obs.statute_label, obs.statute_url, obs.statute_text,
         obs.count, obs.since_date, existing["id"]),
    )
    return "updated", existing["id"]


def close_resolved_finding(conn, body_id: int, category: str) -> bool:
    """If a previously-open finding is no longer observed, transition it to
    closed_with_records. Returns True if a transition happened.
    """
    row = conn.execute(
        """
        UPDATE compliance_finding
        SET status = 'closed_with_records', closed_at = now(),
            closed_reason = 'no longer observed at refresh',
            last_observed_at = now()
        WHERE governing_body_id = %s
          AND category = %s
          AND status IN ('open','records_requested')
        RETURNING id
        """,
        (body_id, category),
    ).fetchone()
    return row is not None


def refresh_body(
    conn, body_id: int, body_name: str, state_abbr: str, dry_run: bool,
) -> dict:
    """Run every observer for this body. Upsert active findings; close resolved ones.

    Failures from any observer are recorded loudly to pipeline_failure and the
    other observers still run — one broken observer should not silently hide
    other findings.
    """
    observed_categories = set()
    summary = {"body": body_name, "actions": []}
    for observer in OBSERVERS:
        try:
            obs = observer(conn, body_id, state_abbr)
        except Exception as e:
            record_failure(
                conn,
                job_name="refresh_findings",
                step=f"observe:{observer.__name__}",
                governing_body_id=body_id,
                message=f"observer raised {type(e).__name__}: {e}",
                exception=e,
                context={"state_abbr": state_abbr},
            )
            summary["actions"].append({
                "category": observer.__name__, "action": "observer_failed",
            })
            continue
        if obs is None:
            continue
        observed_categories.add(obs.category)
        if dry_run:
            summary["actions"].append({
                "category": obs.category, "count": obs.count,
                "severity": obs.severity, "since": str(obs.since_date),
                "action": "would_upsert",
            })
        else:
            try:
                action, finding_id = upsert_finding(conn, body_id, obs)
                summary["actions"].append({
                    "category": obs.category, "count": obs.count, "action": action,
                })
                # Auto-prepare records request when a finding is newly opened or
                # reopened. Existing prepared requests are detected and skipped
                # by prepare_for_finding (idempotent).
                if action in ("inserted", "reopened"):
                    try:
                        from .prepare_records_request import prepare_for_finding
                        result = prepare_for_finding(conn, finding_id)
                        if result["created"]:
                            summary["actions"].append({
                                "category": obs.category,
                                "action": "request_prepared",
                                "records_request_id": result["records_request_id"],
                            })
                    except Exception as e:
                        # Loud failure — preparation problems must not silently
                        # leave a finding without an actionable request.
                        record_failure(
                            conn,
                            job_name="refresh_findings",
                            step="auto_prepare_request",
                            governing_body_id=body_id,
                            finding_id=finding_id,
                            message=f"auto-prepare raised {type(e).__name__}: {e}",
                            exception=e,
                            context={"category": obs.category},
                        )
                        summary["actions"].append({
                            "category": obs.category, "action": "prepare_failed",
                        })
            except Exception as e:
                record_failure(
                    conn,
                    job_name="refresh_findings",
                    step="upsert_finding",
                    governing_body_id=body_id,
                    message=f"upsert raised {type(e).__name__}: {e}",
                    exception=e,
                    context={"category": obs.category},
                )
                summary["actions"].append({
                    "category": obs.category, "action": "upsert_failed",
                })
    # Any open finding NOT observed this run gets closed.
    open_rows = conn.execute(
        """
        SELECT category FROM compliance_finding
        WHERE governing_body_id = %s AND status IN ('open','records_requested')
        """,
        (body_id,),
    ).fetchall()
    for r in open_rows:
        if r["category"] in observed_categories:
            continue
        if dry_run:
            summary["actions"].append({"category": r["category"], "action": "would_close"})
        else:
            if close_resolved_finding(conn, body_id, r["category"]):
                summary["actions"].append({"category": r["category"], "action": "closed"})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="Restrict to this jurisdiction slug")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writes")
    args = parser.parse_args()

    sql = """
        SELECT gb.id, gb.name, j.display_name AS jurisdiction,
               j.state_abbr AS state_abbr
        FROM governing_body gb
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
    """
    params: list = []
    if args.jurisdiction:
        from ..jurisdiction import load_config
        cfg = load_config(args.jurisdiction)
        # Counties don't have place_fips (that's a Census place ID, for
        # incorporated places only). Use whichever is set; both end up
        # stored as the jurisdiction.fips_code DB column.
        j = cfg["jurisdiction"]
        sql += " WHERE j.fips_code = %s"
        params.append(j.get("place_fips") or j.get("county_fips"))
    sql += " ORDER BY j.display_name, gb.name"

    with connect() as conn:
        bodies = conn.execute(sql, params).fetchall()
        print(f"Refreshing findings across {len(bodies)} governing body/bodies...")
        all_summaries = []
        for b in bodies:
            summary = refresh_body(
                conn, b["id"],
                f"{b['jurisdiction']} — {b['name']}",
                b["state_abbr"],
                args.dry_run,
            )
            all_summaries.append(summary)
            if summary["actions"]:
                for a in summary["actions"]:
                    print(f"  [{summary['body']}] {a}")
            else:
                print(f"  [{summary['body']}] no findings")
    print(json.dumps({"dry_run": args.dry_run, "bodies": len(all_summaries)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

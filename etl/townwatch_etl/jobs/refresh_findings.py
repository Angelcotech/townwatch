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

from ..audit import finding_statute, finding_applies, record_failure
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


# Approval-lag grace for minutes_missing. GA splits the recording duty in two
# tiers (verified against the AG's Open Meetings Act text 2026-06-11): a written
# SUMMARY of subjects acted on is due within 2 business days of adjournment
# (OCGA § 50-14-1(e)(2)(A) — not yet observed; would need summary ingestion),
# while full MINUTES are open once approved, "in no case later than immediately
# following its next regular meeting" (§ 50-14-1(e)(2)(B)). This observer
# watches minutes, so the statutory ceiling is one meeting cycle — the trailing
# meeting or two ALWAYS lack minutes at a compliant board. 45 days covers a
# monthly cycle + approval + posting; meetings younger than this are not yet
# evidence of anything.
_MINUTES_APPROVAL_GRACE_DAYS = 45


def observe_minutes_missing(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
    """The 'trail went cold' finding: meetings AFTER the last meeting that
    had minutes published, with no minutes_url and no explicit unavailable
    flag in meta — excluding meetings still inside the approval-lag grace
    window. This is the dramatic finding rather than an all-time tally that
    lumps in sporadic old gaps.

    Statute citation is pulled from the state config so the same observer
    works for GA, TN, FL, etc. — the gap-detection SQL is state-agnostic,
    only the legal citation changes per jurisdiction.
    """
    if not finding_applies(state_abbr, "minutes_missing", body_type):
        return None
    row = conn.execute(
        """
        WITH last_with_minutes AS (
          -- Minutes count as published whether they have their own URL or
          -- were extracted from inside another document (meta.minutes_source,
          -- e.g. embedded in the next meeting's agenda packet — Grovetown
          -- clerk practice found by the 2026-06-12 re-audit).
          SELECT MAX(meeting_date) AS d FROM meeting
          WHERE governing_body_id = %s
            AND (minutes_url IS NOT NULL
                 OR COALESCE(meta, '{}'::jsonb) ? 'minutes_source')
        )
        SELECT COUNT(*) AS count, MIN(m.meeting_date) AS since
        FROM meeting m
        WHERE m.governing_body_id = %s
          AND m.meeting_date < CURRENT_DATE - make_interval(days => %s)
          AND m.minutes_url IS NULL
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'agenda_unavailable')
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'minutes_source')
          AND m.meeting_date > COALESCE((SELECT d FROM last_with_minutes), DATE '1900-01-01')
        """,
        (body_id, body_id, _MINUTES_APPROVAL_GRACE_DAYS),
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


def observe_agenda_missing(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
    """Body systematically not publishing agendas. Mirrors minutes_missing on
    agenda_url: past meetings AFTER the last meeting that had an agenda, missing
    agenda_url. A board that has never posted an agenda (e.g. CCSD posts minutes
    but not agendas) has no 'last with agenda', so every past meeting counts —
    a strong OCGA 50-14-1(e)(1) finding, which is precisely the absence the audit
    is meant to surface."""
    if not finding_applies(state_abbr, "agenda_missing", body_type):
        return None
    row = conn.execute(
        """
        WITH last_with_agenda AS (
          SELECT MAX(meeting_date) AS d FROM meeting
          WHERE governing_body_id = %s AND agenda_url IS NOT NULL
        )
        SELECT COUNT(*) AS count, MIN(m.meeting_date) AS since
        FROM meeting m
        WHERE m.governing_body_id = %s
          AND m.meeting_date < CURRENT_DATE
          AND m.agenda_url IS NULL
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'agenda_unavailable')
          AND m.meeting_date > COALESCE((SELECT d FROM last_with_agenda), DATE '1900-01-01')
        """,
        (body_id, body_id),
    ).fetchone()
    count = row["count"] or 0
    if count < 2:
        return None
    statute = finding_statute(state_abbr, "agenda_missing")
    return ObservedFinding(
        category="agenda_missing",
        severity="high" if count >= 10 else "medium",
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=count,
        since_date=row["since"],
    )


def observe_member_roster_missing(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
    """Member roster not indexed for this body.

    Only fires for APPOINTED bodies (boards/commissions) — elected bodies have a
    dedicated roster scraper, so a missing roster there is an onboarding state,
    not a transparency gap. Which body types this applies to is now catalog-driven
    (member_roster_missing → all_appointed). Fires when zero current members.
    """
    if not finding_applies(state_abbr, "member_roster_missing", body_type):
        return None
    row = conn.execute(
        """
        SELECT (SELECT COUNT(DISTINCT t.official_id)
                FROM term t JOIN seat s ON s.id = t.seat_id
                WHERE s.governing_body_id = %s AND t.is_current = true) AS current_members
        """,
        (body_id,),
    ).fetchone()
    if (row["current_members"] or 0) > 0:
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


def observe_campaign_finance_missing(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
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
    # Only elected bodies have campaign-disclosure obligations (catalog-driven:
    # campaign_finance_missing → all_elected, incl. board_of_education).
    if not finding_applies(state_abbr, "campaign_finance_missing", body_type):
        return None

    # NO OBSERVER WITHOUT INGESTION (statute-inventory rule, 2026-06-12): an
    # empty campaign_contribution table measures OUR pipeline, not the
    # officials. The 2026-06-12 Grovetown re-audit refuted this finding for
    # all four sitting council members — every one had filings publicly
    # downloadable from the state ethics record-search (the city's local
    # filing office has 105 documents there) that we had simply never
    # ingested. The observer is silent until a campaign-finance source has
    # actually been ingested for this jurisdiction: either contributions
    # exist somewhere in the jurisdiction, or the (future) ingestor has
    # registered its jurisdiction-scoped data_source row — which it must do
    # even when a sweep finds zero filings, so a true absence stays
    # observable.
    gated = conn.execute(
        """
        SELECT (
          EXISTS (
            SELECT 1 FROM campaign_contribution cc
            JOIN term t ON t.official_id = cc.official_id
            JOIN seat s ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id
            WHERE gb.jurisdiction_id = (SELECT jurisdiction_id FROM governing_body WHERE id = %s)
          ) OR EXISTS (
            SELECT 1 FROM data_source ds
            WHERE ds.jurisdiction_id = (SELECT jurisdiction_id FROM governing_body WHERE id = %s)
              AND ds.source_type = 'campaign_finance'
          )
        ) AS ingested
        """,
        (body_id, body_id),
    ).fetchone()
    if not gated["ingested"]:
        return None

    EIGHT_YEARS_AGO = "(CURRENT_DATE - INTERVAL '8 years')"
    row = conn.execute(
        f"""
        SELECT
            (SELECT COUNT(DISTINCT t.official_id)
             FROM term t JOIN seat s ON s.id = t.seat_id
             WHERE s.governing_body_id = %s AND t.is_current = true) AS current_members,
            (SELECT COUNT(*)
             FROM campaign_contribution cc
             JOIN term t ON t.official_id = cc.official_id
             JOIN seat s ON s.id = t.seat_id
             WHERE s.governing_body_id = %s
               AND t.is_current = true
               AND cc.contribution_date >= {EIGHT_YEARS_AGO}) AS contributions_count
        """,
        (body_id, body_id),
    ).fetchone()
    current_members = row["current_members"] or 0
    contributions_count = row["contributions_count"] or 0
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


def observe_meeting_notice_too_short(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
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
    if not finding_applies(state_abbr, "meeting_notice_too_short", body_type):
        return None
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


# ---------------------------------------------------------------------
# Budget-adoption observer (Phase C) — the "adoption trail went cold" finding.
#
# DESIGN HISTORY (why this and not a notice-timing check): the first cut tried to
# flag budget/millage PUBLIC HEARINGS posted on short (< 1 week) notice, reusing the
# agenda_posted_at timing. On the one body we can fully test (Columbia County) it
# produced a FALSE POSITIVE — it fired on the routine MAY budget-PRESENTATION
# meeting, which is normal and compliant: Columbia presents the proposed budget in
# May (motions outcome='no_action') and ADOPTS it by resolution in mid-June, every
# year. agenda_posted_at is the platform PDF-posting time, NOT the statutory
# newspaper hearing notice, and it cannot tell a presentation from an adoption
# hearing. So that approach was scrapped — it asserted a gap we cannot actually see.
#
# This observer instead mirrors the proven observe_minutes_missing discipline:
# a body that DEMONSTRABLY adopts an annual budget every year (>= 2 prior adoption
# years in our own data) and then stops — with continued meetings AND published
# minutes since — is a real, verifiable absence of a legally-required annual action.
# It is built to UNDER-fire:
#   * >= 2 prior adoption years  -> proves we can SEE this body's adoptions when
#                                   they happen (no firing on bodies we've never
#                                   observed adopt).
#   * >= 3 meetings WITH minutes since the last adoption -> proves the records
#                                   exist and were processed, so the absence is
#                                   real and not our own extraction gap (this is
#                                   what keeps Grovetown-style minutes-gap bodies
#                                   from false-firing).
#   * 14-month overdue floor     -> clearly beyond one annual cycle (one-month
#                                   grace past a 13-month worst-case cadence).
# We never assert the newspaper advertisement or hearing-notice timing — only the
# observable fact that an annual budget adoption we'd expect to see is missing.
# ---------------------------------------------------------------------


def observe_budget_adoption_overdue(conn, body_id: int, state_abbr: str, body_type: str | None) -> ObservedFinding | None:
    """§ 36-81-5 — a general-purpose local government's annual budget adoption is
    overdue/unfound. Catalog-gated to general_purpose_local_governments (city/county);
    schools (their own § 20-2-167.1) and appointed boards never trip it."""
    if not finding_applies(state_abbr, "budget_process_missing", body_type):
        return None
    # Adoption = a PASSED motion whose title says it adopts a budget, excluding
    # routine mid-year amendments/adjustments. Matched on TITLE (not motion_type),
    # because the extractor sometimes types an adoption resolution as
    # 'budget_amendment'. ~2 conditions ANDed since POSIX regex has no lookahead.
    a = conn.execute(
        """
        SELECT MAX(m.meeting_date) AS last_adoption,
               COUNT(DISTINCT EXTRACT(YEAR FROM m.meeting_date)) AS adoption_years
        FROM motion mo
        JOIN meeting m ON m.id = mo.meeting_id
        WHERE m.governing_body_id = %s
          AND mo.outcome = 'passed'
          AND mo.title ~* 'adopt'
          AND mo.title ~* 'budget'
          AND mo.title !~* 'amendment|adjustment'
        """,
        (body_id,),
    ).fetchone()
    last_adoption = a["last_adoption"]
    if last_adoption is None or (a["adoption_years"] or 0) < 2:
        return None  # no proven annual-adoption pattern in our data → assert nothing

    cov = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE minutes_url IS NOT NULL) AS meetings_with_minutes,
               (%s < CURRENT_DATE - INTERVAL '14 months') AS overdue
        FROM meeting
        WHERE governing_body_id = %s
          AND meeting_date > %s
          AND meeting_date < CURRENT_DATE
        """,
        (last_adoption, body_id, last_adoption),
    ).fetchone()
    if not cov["overdue"]:
        return None  # still within one annual cycle — not overdue
    if (cov["meetings_with_minutes"] or 0) < 3:
        return None  # too little recent minutes coverage to be sure it's truly absent

    statute = finding_statute(state_abbr, "budget_process_missing")
    return ObservedFinding(
        category="budget_process_missing",
        severity="medium",
        statute_label=statute["statute_label"],
        statute_url=statute["statute_url"],
        statute_text=statute["statute_text"],
        count=cov["meetings_with_minutes"],  # meetings w/ minutes since last adopted budget
        since_date=last_adoption,
    )


OBSERVERS: list[Callable] = [
    observe_minutes_missing,
    observe_agenda_missing,
    observe_member_roster_missing,
    observe_campaign_finance_missing,
    observe_meeting_notice_too_short,
    observe_budget_adoption_overdue,   # Phase C: "adoption trail went cold", under-fires by design
    # Add new finding categories here. Deliberately NOT observed (catalog reference
    # only), because their one verifiable signal — the NEWSPAPER ADVERTISEMENT — is
    # not visible from our agenda-platform sources, so any finding would be a guess:
    #   * millage advertisement (§ 48-5-32.1) and tax digest (§ 48-5-32)
    #   * county self-compensation increase (§ 36-5-24)
    #   * public-works contract advertisement (§ 36-91-20)
    #   * school budget summary advertisement (§ 20-2-167.1)
    # A millage/tax adoption-trail observer (parallel to budget) needs its own
    # catalog category distinct from the advertisement duty; revisit with
    # finance-document ingestion (Phase 5).
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
    bt_row = conn.execute(
        "SELECT body_type FROM governing_body WHERE id = %s", (body_id,),
    ).fetchone()
    body_type = bt_row["body_type"] if bt_row else None
    for observer in OBSERVERS:
        try:
            obs = observer(conn, body_id, state_abbr, body_type)
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
                # Records-request preparation happens in reconcile_requests()
                # at the end of the run — for EVERY jurisdiction with open
                # findings, not just on insert/reopen. The insert-time trigger
                # this replaces was lossy: a finding opened while preparation
                # was broken (CCSD agenda_missing, 2026-06-04) stayed without
                # a request forever because nothing ever retried.
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


def reconcile_requests(conn, fips: str | None, dry_run: bool) -> list[dict]:
    """Ensure every jurisdiction with open findings has a consolidated
    records_request covering them. Runs every refresh — reconciliation, not a
    trigger — so a request missed at finding-open time (preparation broken,
    crash mid-run) self-heals on the next pass. ensure_consolidated_request is
    idempotent: unchanged open-finding set → no-op; changed set → refresh the
    PDF in place; uncovered set → create. Local PDF rendering, no model spend."""
    from .prepare_records_request import ensure_consolidated_request
    sql = """
        SELECT DISTINCT j.id, j.display_name
        FROM compliance_finding cf
        JOIN governing_body gb ON gb.id = cf.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE cf.status = 'open'
    """
    params: list = []
    if fips:
        sql += " AND j.fips_code = %s"
        params.append(fips)
    actions: list[dict] = []
    for j in conn.execute(sql, params).fetchall():
        if dry_run:
            actions.append({"jurisdiction": j["display_name"], "action": "would_reconcile"})
            continue
        try:
            result = ensure_consolidated_request(conn, j["id"])
            if result and (result["created"] or result["updated"]):
                actions.append({
                    "jurisdiction": j["display_name"],
                    "action": "request_prepared" if result["created"] else "request_refreshed",
                    "records_request_id": result["records_request_id"],
                    "finding_count": len(result["finding_ids"]),
                })
        except Exception as e:
            # Loud failure — preparation problems must not silently leave a
            # finding without an actionable request.
            record_failure(
                conn,
                job_name="refresh_findings",
                step="auto_prepare_request",
                message=f"reconcile raised {type(e).__name__}: {e}",
                exception=e,
                context={"jurisdiction_id": j["id"]},
            )
            actions.append({"jurisdiction": j["display_name"], "action": "prepare_failed"})
    return actions


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
    fips: str | None = None
    if args.jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        cfg = load_config(args.jurisdiction)
        # Resolve the canonical fips_code (school_district_fips / place_fips /
        # county_fips) so a school district scopes to its own GEOID, not its county.
        fips = jurisdiction_fips(cfg)
        sql += " WHERE j.fips_code = %s"
        params.append(fips)
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

        # Reconcile consolidated records requests across every jurisdiction
        # with open findings (see reconcile_requests docstring).
        for a in reconcile_requests(conn, fips, args.dry_run):
            print(f"  [requests] {a}")
    print(json.dumps({"dry_run": args.dry_run, "bodies": len(all_summaries)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

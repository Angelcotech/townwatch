"""
Onboarding gate: prove the scraper can actually FETCH documents for every
governing body before a jurisdiction is treated as onboarded.

Why this exists
---------------
Our CivicClerk file URLs were silently wrong for months — every fetch 404'd.
Because a 404 looks like a "no document published" finding, the tooling
failure masqueraded as a city-side finding and went undetected. This gate
inverts that: a TOTAL fetch failure for a body is a loud TOOLING failure, not
a citizen-facing finding. If a body has recent meetings with file URLs but
none resolve to a real document, that's almost certainly us, not the city.

Verdict per governing body (HTTP-layer distinction, via
scan_document_availability.classify_url):

    OK          >=1 recent file URL resolves 'available' (scraper works)
    FAILED      URLs exist, all definitively non-available (404 / stub)
    UNVERIFIED  URLs exist, every attempt inconclusive (throttled / network)
    NO_FILES    no file URLs at all on recent meetings

A jurisdiction PASSES only if every body is OK. Any other verdict blocks
onboarding (exit 1) and records a pipeline_failure row per offending body so
the admin queue surfaces it. Re-runs supersede the prior unresolved row for
the same body, so a daily health-check run doesn't accumulate duplicates.

Assumes meetings_inventory has already populated bodies + meetings. Run as
the FINAL onboarding step (after sync_jurisdictions + meetings_inventory),
or periodically as a health check.

Run:
    python -m townwatch_etl.jobs.onboarding_smoke_test --jurisdiction columbia-county-ga
    python -m townwatch_etl.jobs.onboarding_smoke_test     # every jurisdiction in the DB
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import connect
from .scan_document_availability import classify_url


# How many recent (already-occurred) meetings per body to consider. A body
# that can't produce one fetchable document across this many recent meetings
# is treated as a tooling/availability problem worth a human look.
RECENT_MEETINGS_PER_BODY = 5
# Hard cap on fetch attempts per body, so a body with many dead URLs can't
# turn the gate into a long throttled crawl. We stop early on the first
# 'available' anyway.
MAX_URLS_PER_BODY = 8

# Classifications that are a definitive answer the URL does NOT serve a real
# document (vs. inconclusive throttle/network errors, which prove nothing).
_DEFINITIVE_BAD = {"http_error", "stub_pdf", "empty_body"}


@dataclass
class BodyResult:
    body_id: int
    body_name: str
    verdict: str                       # OK | FAILED | UNVERIFIED | NO_FILES
    tried: list[dict] = field(default_factory=list)  # [{url, kind, classification, detail}]

    @property
    def ok(self) -> bool:
        return self.verdict == "OK"


def _recent_file_urls(conn, body_id: int) -> list[tuple[str, str]]:
    """Return (kind, url) pairs for the body's most recent already-occurred
    meetings — agendas first (they almost always exist), then minutes — so
    the cheapest path to a confirming 'available' is tried first."""
    rows = conn.execute(
        """
        SELECT meeting_date, agenda_url, minutes_url
        FROM meeting
        WHERE governing_body_id = %s
          AND meeting_date <= now()::date
          AND (agenda_url IS NOT NULL OR minutes_url IS NOT NULL)
        ORDER BY meeting_date DESC
        LIMIT %s
        """,
        (body_id, RECENT_MEETINGS_PER_BODY),
    ).fetchall()
    agendas = [("agenda", r["agenda_url"]) for r in rows if r["agenda_url"]]
    minutes = [("minutes", r["minutes_url"]) for r in rows if r["minutes_url"]]
    return (agendas + minutes)[:MAX_URLS_PER_BODY]


def _check_body(conn, body_id: int, body_name: str) -> BodyResult:
    urls = _recent_file_urls(conn, body_id)
    result = BodyResult(body_id=body_id, body_name=body_name, verdict="NO_FILES")
    if not urls:
        return result

    saw_definitive_bad = False
    for kind, url in urls:
        check = classify_url(url)
        result.tried.append(
            {"url": url, "kind": kind, "classification": check.classification, "detail": check.detail}
        )
        if check.classification == "available":
            result.verdict = "OK"
            return result
        if check.classification in _DEFINITIVE_BAD:
            saw_definitive_bad = True

    # No URL resolved available. If we got at least one definitive "not a real
    # document" answer, this is a FAILED body (tooling or genuine-empty — both
    # warrant a human at onboarding). If every attempt was inconclusive, we
    # genuinely couldn't tell (throttle/network) — UNVERIFIED, retry later.
    result.verdict = "FAILED" if saw_definitive_bad else "UNVERIFIED"
    return result


def _record_failure(conn, slug: str, res: BodyResult) -> None:
    """Supersede any prior unresolved smoke-test failure for this body, then
    record the current one — keeps one live row per offending body."""
    conn.execute(
        """
        UPDATE pipeline_failure
        SET resolved_at = now(), resolution_notes = 'superseded by later onboarding_smoke_test run'
        WHERE job_name = 'onboarding_smoke_test'
          AND governing_body_id = %s
          AND resolved_at IS NULL
        """,
        (res.body_id,),
    )
    message = (
        f"{slug}: governing body '{res.body_name}' (id={res.body_id}) "
        f"{res.verdict} — no recent file URL resolved to a real document "
        f"({len(res.tried)} URL(s) tried)"
    )
    conn.execute(
        """
        INSERT INTO pipeline_failure (job_name, step, governing_body_id, message, context)
        VALUES ('onboarding_smoke_test', %s, %s, %s, %s::jsonb)
        """,
        (res.verdict, res.body_id, message, json.dumps({"jurisdiction": slug, "urls_tried": res.tried})),
    )


def _check_timezones(conn, jurisdiction_slug: str | None) -> list[str]:
    """Report timezone confidence per jurisdiction — a NON-blocking troubleshoot
    pass, never a gate. Forum cutoffs need the local zone, but onboarding must
    not hard-fail right after someone funds a town. A 'verified' zone is fine; an
    'assumed' one (multi-zone guess) or a NULL still onboards on the best-guess /
    Eastern fallback and is recorded for review so troubleshoot_timezones can
    surface it for a one-line override. Returns the slugs needing review."""
    where = "TRUE"
    params: list = []
    if jurisdiction_slug:
        where = "LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) = LOWER(%s)"
        params.append(jurisdiction_slug)
    rows = conn.execute(
        f"""
        SELECT j.id,
               LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) AS slug,
               j.timezone, j.timezone_status
        FROM jurisdiction j
        WHERE {where}
        ORDER BY slug
        """,
        params,
    ).fetchall()

    review: list[str] = []
    for r in rows:
        tz = (r["timezone"] or "").strip()
        valid = False
        if tz:
            try:
                ZoneInfo(tz)
                valid = True
            except (ZoneInfoNotFoundError, ValueError):
                valid = False
        if valid and r["timezone_status"] == "verified":
            print(f"  ✓ [TIMEZONE] {r['slug']} — {tz} (verified)")
            continue

        # Non-blocking: record a review item, keep onboarding moving.
        if valid:
            reason = f"timezone {tz} is assumed (best guess) — confirm or override"
        else:
            reason = (f"timezone not resolved (got {r['timezone']!r}) — using fallback; "
                      f"run sync_jurisdictions / set jurisdiction.timezone")
        print(f"  ⚠ [TIMEZONE_REVIEW] {r['slug']} — {reason}")
        conn.execute(
            """
            INSERT INTO pipeline_failure (job_name, step, message, context)
            VALUES ('onboarding_smoke_test', 'TIMEZONE_REVIEW', %s, %s::jsonb)
            """,
            (f"jurisdiction {r['slug']}: {reason}",
             json.dumps({"jurisdiction": r["slug"], "timezone": r["timezone"],
                         "timezone_status": r["timezone_status"]})),
        )
        review.append(r["slug"])
    return review


def _bodies_for(conn, jurisdiction_slug: str | None) -> list[dict]:
    where = "gb.dissolved_date IS NULL"
    params: list = []
    if jurisdiction_slug:
        where += " AND LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) = LOWER(%s)"
        params.append(jurisdiction_slug)
    return conn.execute(
        f"""
        SELECT gb.id, gb.name,
               LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) AS slug
        FROM governing_body gb
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE {where}
        ORDER BY slug, gb.id
        """,
        params,
    ).fetchall()


def smoke_test(jurisdiction_slug: str | None) -> int:
    with connect() as conn:
        bodies = _bodies_for(conn, jurisdiction_slug)
        if not bodies:
            print(f"✗ no governing bodies found for {jurisdiction_slug or 'any jurisdiction'} "
                  f"— has meetings_inventory run?")
            return 1

        print(f"onboarding smoke test: {len(bodies)} bod(ies) "
              f"({jurisdiction_slug or 'all jurisdictions'})")

        # Jurisdiction-level timezone confidence — NON-blocking troubleshoot.
        # 'assumed'/unresolved zones are recorded for review but never block a
        # funded town from going live.
        tz_review = _check_timezones(conn, jurisdiction_slug)

        blocked: list[BodyResult] = []
        for b in bodies:
            res = _check_body(conn, b["id"], b["name"])
            mark = "✓" if res.ok else "✗"
            print(f"  {mark} [{res.verdict}] {b['slug']} / {b['name']} (id={b['id']}) "
                  f"— {len(res.tried)} URL(s) tried")
            if not res.ok:
                _record_failure(conn, b["slug"], res)
                blocked.append(res)

    print("---")
    if tz_review:
        print(f"REVIEW (non-blocking): {len(tz_review)} jurisdiction(s) have an "
              f"unconfirmed timezone — {', '.join(tz_review)}. Onboarding proceeds; "
              f"run `python -m townwatch_etl.jobs.troubleshoot_timezones` to confirm.")
    if blocked:
        bad = ", ".join(f"{r.body_name}({r.verdict})" for r in blocked)
        print(f"BLOCKED: {len(blocked)}/{len(bodies)} bod(ies) did not verify — {bad}")
        print("Recorded to pipeline_failure. Do NOT mark these jurisdictions onboarded "
              "until a real document is fetchable for every body.")
        return 1
    print(f"PASS: all {len(bodies)} bod(ies) served a fetchable document"
          + (f" ({len(tz_review)} timezone(s) flagged for review)" if tz_review else
             "; all timezones verified"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'columbia-county-ga'; omit to check all")
    args = parser.parse_args()
    return smoke_test(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())

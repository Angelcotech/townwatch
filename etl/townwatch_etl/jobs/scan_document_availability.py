"""
Scan agenda_url and minutes_url for every meeting, flag the ones whose
URLs no longer serve a real document. Writes meeting.agenda_is_placeholder
and minutes_is_placeholder so the frontend can render "no document
published by city" instead of leading users to a blank PDF or a 404 page.

Jurisdiction-agnostic. Handles both stub patterns we've seen in the wild:
    * CivicEngage AgendaCenter: returns a 1.5–2KB blank PDF
    * CivicClerk attachments:   returns HTTP 404 (file ID rotated/deleted)
Any future platform that fits one of these shapes is detected automatically.

Re-runs are idempotent. A URL marked placeholder=true today may flip back
to false if the city later uploads the real document; a URL marked false
today may flip to true if the platform later 404s. So the scan should be
re-run periodically (e.g., daily) to keep the flag honest.

Runs INCREMENTALLY by default: each definitive verdict stamps
meeting.{agenda,minutes}_scanned_at, and later runs only re-check URLs that
are due (never scanned, or past their per-verdict recheck interval — see
PLACEHOLDER_RECHECK_DAYS / AVAILABLE_RECHECK_DAYS). That keeps the daily
load proportional to new + stale URLs rather than all of history, so it
scales across many jurisdictions on a rate-limited platform. Use --full to
force a complete re-check.

Run:
    python -m townwatch_etl.jobs.scan_document_availability
    python -m townwatch_etl.jobs.scan_document_availability --jurisdiction columbia-county-ga
    python -m townwatch_etl.jobs.scan_document_availability --body-id 6
    python -m townwatch_etl.jobs.scan_document_availability --only-changed   # report flips only
    python -m townwatch_etl.jobs.scan_document_availability --full           # ignore freshness
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import httpx

from ..db import connect
from ..http_client import civic_get


# Match the agenda extractor's stub threshold so we classify the same way
# the extractor would (extractors/agendas.py::STUB_PDF_SIZE_BYTES).
STUB_PDF_SIZE_BYTES = 5_000

# Range-GET cap. Reading slightly past the stub threshold is enough to
# distinguish stub (≤5KB) from real (>5KB) without pulling whole PDFs.
RANGE_BYTES = 6_000

# Per-request timeout. CivicClerk and CivicEngage usually respond in <2s;
# 15s leaves headroom for slow platforms without making a stuck scan hang.
REQUEST_TIMEOUT = 15.0

# Throttling, Retry-After honoring, and backoff now live in http_client —
# civic_get() spaces requests per-host and only surfaces a terminal 429
# (every retry exhausted) back to us, which we record as inconclusive.

# Incremental re-scan policy. Each definitive verdict stamps
# meeting.{agenda,minutes}_scanned_at; later runs skip a URL until it's due
# again. Never-scanned URLs are always due. Known placeholders are
# re-checked often (a city may upload the real document); confirmed-available
# URLs are re-checked rarely (a published doc seldom vanishes, though URLs do
# rotate to 404). Inconclusive verdicts never stamp the column, so a
# throttled URL stays due for the next run. This is what lets a daily run
# across many jurisdictions fit the window instead of re-fetching all of
# history every night.
PLACEHOLDER_RECHECK_DAYS = 7
AVAILABLE_RECHECK_DAYS = 30


@dataclass
class CheckResult:
    # is_placeholder is None when the check was inconclusive (transient
    # error, network failure, throttle). Callers must NOT overwrite the
    # existing flag in that case — a known-dead URL stays dead until we
    # get a definitive 200 with real content. Otherwise rate-limiting
    # silently erodes prior knowledge on every re-scan.
    is_placeholder: bool | None
    classification: str   # 'available' | 'http_error' | 'stub_pdf' | 'empty_body' | 'network_error' | 'throttled' | 'server_error'
    detail: str           # short human-readable note for logs


def classify_url(url: str) -> CheckResult:
    """One URL → one verdict. Uses a small Range GET so we never pull a
    full PDF. HEAD is unreliable (CivicClerk returns 405), so we GET with
    a byte range that's large enough to size-classify stubs.

    Throttling/Retry-After/backoff are handled inside civic_get; by the
    time we see a 429 here it's terminal (all retries exhausted), so we
    return is_placeholder=None ('inconclusive') and the caller preserves
    whatever flag we already had — a throttle storm can never wipe a
    known-dead flag."""
    try:
        r = civic_get(
            url,
            headers={"Range": f"bytes=0-{RANGE_BYTES - 1}"},
            timeout=REQUEST_TIMEOUT,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return CheckResult(None, "network_error", f"{type(exc).__name__}: {exc}")

    if r.status_code in (404, 410):
        return CheckResult(True, "http_error", f"HTTP {r.status_code}")
    if r.status_code == 429:
        return CheckResult(None, "throttled", "HTTP 429")
    if r.status_code >= 500:
        return CheckResult(None, "server_error", f"HTTP {r.status_code}")
    if r.status_code >= 400:
        # 401/403/other 4xx that isn't 404. Inconclusive — could be a
        # platform auth quirk or a real dead link. Don't touch the flag.
        return CheckResult(None, "http_error", f"HTTP {r.status_code}")

    body = r.content
    if not body:
        # 200 with empty body — treat as placeholder, same UX as 404.
        return CheckResult(True, "empty_body", "200 with 0 bytes")

    # Content-Length is the source of truth for whole-file size. With a
    # Range response (206) the body we hold is only the first chunk; we
    # need the full size from Content-Range or Content-Length.
    full_size: int | None = None
    cr = r.headers.get("content-range")
    if cr and "/" in cr:
        try:
            full_size = int(cr.rsplit("/", 1)[1])
        except ValueError:
            full_size = None
    if full_size is None:
        cl = r.headers.get("content-length")
        if cl is not None:
            try:
                full_size = int(cl)
            except ValueError:
                full_size = None

    # If we couldn't determine full size from headers, fall back to the
    # body we have. (Servers that don't honor Range return 200 with the
    # whole file, in which case len(body) is accurate enough for our
    # purposes — small responses got fully delivered.)
    if full_size is None:
        full_size = len(body)

    if full_size <= STUB_PDF_SIZE_BYTES:
        return CheckResult(True, "stub_pdf", f"{full_size} bytes")
    return CheckResult(False, "available", f"{full_size} bytes")


def _due_sql(flag_col: str, ts_col: str) -> str:
    """SQL boolean: is this URL kind due for a (re)scan? True when never
    scanned, or when its last definitive verdict is older than the recheck
    interval for that verdict. Day constants are our own (not user input),
    so they're inlined safely."""
    return (
        f"({ts_col} IS NULL "
        f"OR ({flag_col} AND {ts_col} < now() - make_interval(days => {PLACEHOLDER_RECHECK_DAYS})) "
        f"OR (NOT {flag_col} AND {ts_col} < now() - make_interval(days => {AVAILABLE_RECHECK_DAYS})))"
    )


def scan(
    jurisdiction_slug: str | None,
    body_id: int | None,
    only_changed: bool,
    full: bool = False,
) -> int:
    where_clauses = ["(m.agenda_url IS NOT NULL OR m.minutes_url IS NOT NULL)"]
    params: list = []
    if jurisdiction_slug:
        where_clauses.append("LOWER(REPLACE(j.name, ' ', '-') || '-' || LOWER(j.state_abbr)) = LOWER(%s)")
        params.append(jurisdiction_slug)
    if body_id is not None:
        where_clauses.append("m.governing_body_id = %s")
        params.append(body_id)
    where = " AND ".join(where_clauses)

    # Per-kind "due for (re)scan" predicates. --full forces every URL,
    # ignoring freshness (for backfills or after a policy change).
    agenda_due = "TRUE" if full else _due_sql("m.agenda_is_placeholder", "m.agenda_scanned_at")
    minutes_due = "TRUE" if full else _due_sql("m.minutes_is_placeholder", "m.minutes_scanned_at")

    counts = {
        "agenda_checked": 0, "agenda_flagged": 0,
        "minutes_checked": 0, "minutes_flagged": 0,
        "flips": 0, "inconclusive": 0,
    }

    with connect() as conn:
        total_in_scope = conn.execute(
            f"""
            SELECT count(*) AS n
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE {where}
            """,
            params,
        ).fetchone()["n"]

        rows = conn.execute(
            f"""
            SELECT m.id, m.meeting_date, m.agenda_url, m.minutes_url,
                   m.agenda_is_placeholder, m.minutes_is_placeholder,
                   ({agenda_due})  AS agenda_due,
                   ({minutes_due}) AS minutes_due,
                   j.display_name AS jurisdiction
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE {where}
              AND ((m.agenda_url  IS NOT NULL AND ({agenda_due}))
                OR (m.minutes_url IS NOT NULL AND ({minutes_due})))
            ORDER BY m.meeting_date DESC, m.id DESC
            """,
            params,
        ).fetchall()
        mode = "full re-scan" if full else "incremental"
        print(
            f"scanning {len(rows)} due of {total_in_scope} in-scope meetings "
            f"({jurisdiction_slug or 'all jurisdictions'}, {mode})..."
        )

        for row in rows:
            meeting_id = row["id"]
            for kind, url_col, flag_col, ts_col, due_col in (
                ("agenda", "agenda_url", "agenda_is_placeholder", "agenda_scanned_at", "agenda_due"),
                ("minutes", "minutes_url", "minutes_is_placeholder", "minutes_scanned_at", "minutes_due"),
            ):
                url = row[url_col]
                # Skip URLs that are absent, or present but still fresh (the
                # other kind on this meeting is what made the row due).
                if not url or not row[due_col]:
                    continue
                counts[f"{kind}_checked"] += 1
                result = classify_url(url)

                # Inconclusive verdict (throttle / 5xx / network): do NOT
                # stamp scanned_at and do NOT touch the flag, so the URL stays
                # due and we retry it next run. A throttle storm can never
                # erode a known verdict or masquerade as "checked".
                if result.is_placeholder is None:
                    counts["inconclusive"] += 1
                    if not only_changed:
                        print(
                            f"  meeting={meeting_id} {kind}=INCONCLUSIVE "
                            f"({result.classification}: {result.detail})"
                        )
                    continue

                if result.is_placeholder:
                    counts[f"{kind}_flagged"] += 1

                # Definitive verdict: stamp scanned_at (no longer due) and
                # write the flag. Both in one UPDATE; the flag is set every
                # time so re-confirmation is cheap and idempotent.
                previous = row[flag_col]
                conn.execute(
                    f"UPDATE meeting SET {flag_col} = %s, {ts_col} = now() WHERE id = %s",
                    (result.is_placeholder, meeting_id),
                )
                if previous != result.is_placeholder:
                    counts["flips"] += 1
                    print(
                        f"  [FLIP] meeting={meeting_id} {kind}={previous} → {result.is_placeholder} "
                        f"({result.classification}: {result.detail})"
                    )
                elif not only_changed:
                    print(
                        f"  meeting={meeting_id} {kind}={'PLACEHOLDER' if result.is_placeholder else 'ok'} "
                        f"({result.classification}: {result.detail})"
                    )

    print("---")
    print(
        f"agendas: checked={counts['agenda_checked']} placeholder={counts['agenda_flagged']}  "
        f"minutes: checked={counts['minutes_checked']} placeholder={counts['minutes_flagged']}  "
        f"flips={counts['flips']} inconclusive={counts['inconclusive']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'columbia-county-ga' or 'grovetown-ga'")
    parser.add_argument("--body-id", type=int, help="restrict to one governing body")
    parser.add_argument("--only-changed", action="store_true",
                        help="only log meetings whose placeholder flag flipped")
    parser.add_argument("--full", action="store_true",
                        help="re-check every URL, ignoring scanned_at freshness (backfill/debug)")
    args = parser.parse_args()
    return scan(args.jurisdiction, args.body_id, args.only_changed, args.full)


if __name__ == "__main__":
    sys.exit(main())

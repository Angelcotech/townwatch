"""
Extract minutes that live INSIDE the next meeting's agenda packet.

Some clerks stop posting standalone Minutes documents and instead include the
prior meeting's full formal minutes in the next meeting's agenda packet
(found in Grovetown's PC/BZA by the 2026-06-12 findings re-audit: standalone
Minutes links stopped in 2022/2023, but 23 of 25 subsequent packets embed the
prior meeting's complete minutes — roll call, motions, seconders, votes).
This is a CivicPlus workflow, not a Grovetown quirk, so detection is by
CONTENT, not config: any meeting whose next sibling's stored packet text
contains "Minutes of the <this meeting's date> ... Meeting" qualifies.

For each candidate meeting (minutes_url IS NULL, no motions, not flagged):
  1. find the body's NEXT meeting with stored agenda-packet text
     (document_text keyed by agenda_url — no fetch, no OCR cost),
  2. locate the embedded minutes section by date match and slice a bounded
     window of pages,
  3. extract via the content-addressed cache or one Haiku text window,
  4. persist through the standard MinutesExtract path (motions, votes,
     identity resolution) with embedded provenance stamped on meeting.meta
     (minutes_source='embedded_in_next_packet') — never a fake minutes_url.

Phase-gated like the other extractors (build_phases.filter_phase_locked):
historical meetings wait for funding. Per-meeting commits via MinutesExtract.

Run:
    python -m townwatch_etl.jobs.extract_embedded_minutes --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.extract_embedded_minutes --jurisdiction grovetown-ga --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys

from ..audit import record_failure
from ..build_phases import filter_phase_locked
from ..db import connect
from .. import extraction_cache
from ..extractors.minutes import EXTRACTOR_VERSION, MeetingExtraction, extract_from_text
from ..extractors.recovery import FatalExtractionError
from .extract_minutes import MinutesExtract

# Embedded minutes sections are a few pages; bound the slice so a giant
# packet never inflates the prompt.
_MAX_SLICE_PAGES = 8


def _date_variants(d) -> list[str]:
    """The date strings a clerk might write: 'July 18, 2024' / 'July 08, 2024'."""
    month = d.strftime("%B")
    out = [f"{month} {d.day}, {d.year}"]
    if d.day < 10:
        out.append(f"{month} {d.day:02d}, {d.year}")
    return out


def find_embedded_slice(pages: list[str], meeting_date) -> str | None:
    """The text window starting at the page whose 'Minutes of the <date>'
    heading matches meeting_date, spanning up to _MAX_SLICE_PAGES. None when
    the packet doesn't embed these minutes (normal — not every clerk does)."""
    # Clerk phrasing varies: "Minutes of the August 15, 2024 Regular Meeting"
    # vs "Minutes of February 6, 2025, Regular Meeting" — 'the' is optional.
    pats = [
        re.compile(r"minutes\s+of\s+(?:the\s+)?" + re.escape(v).replace(r"\ ", r"\s+"), re.I)
        for v in _date_variants(meeting_date)
    ]
    for i, page in enumerate(pages):
        if any(p.search(page) for p in pats):
            return "\n".join(pages[i: i + _MAX_SLICE_PAGES])
    return None


# Minutes for meeting N usually sit in packet N+1, but stub packets and
# cancelled meetings push them later (Grovetown: Aug 2024 PC minutes are in
# the October packet because September's is a one-page stub). Look ahead a
# few siblings; the date-matched heading keeps false positives out.
_LOOKAHEAD_PACKETS = 3


def _candidates(conn, fips: str | None) -> list[dict]:
    """Meetings with no minutes and no motions, each with its next few sibling
    meetings' agenda-packet URLs (the slice search walks them in order)."""
    rows = conn.execute(
        """
        SELECT m.id AS meeting_id,
               m.meeting_date,
               m.governing_body_id,
               gb.jurisdiction_id,
               (SELECT array_agg(u) FROM (
                  SELECT n.agenda_url AS u FROM meeting n
                   WHERE n.governing_body_id = m.governing_body_id
                     AND n.meeting_date > m.meeting_date
                     AND n.agenda_url IS NOT NULL
                   ORDER BY n.meeting_date ASC LIMIT %(lookahead)s) t) AS packet_urls
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.minutes_url IS NULL
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'minutes_source')
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'agenda_unavailable')
          AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
          AND (%(fips)s::text IS NULL OR j.fips_code = %(fips)s)
        ORDER BY m.governing_body_id, m.meeting_date
        """,
        {"fips": fips, "lookahead": _LOOKAHEAD_PACKETS},
    ).fetchall()
    return [r for r in rows if r["packet_urls"]]


def _stored_pages(conn, url: str, _cache: dict = {}) -> list[str] | None:
    """document_text pages for a packet URL (memoized — sibling candidates
    share lookahead packets)."""
    if url not in _cache:
        row = conn.execute(
            "SELECT pages FROM document_text WHERE source_url = %s", (url,)).fetchone()
        _cache[url] = row["pages"] if row else None
    return _cache[url]


def run(jurisdiction: str | None, *, limit: int | None, dry_run: bool, force: bool) -> int:
    fips = None
    if jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        fips = jurisdiction_fips(load_config(jurisdiction))

    with connect() as conn:
        rows = _candidates(conn, fips)
        if not force:
            rows, deferred = filter_phase_locked(conn, rows)
            for jid, d in deferred.items():
                print(f"  ⏳ jurisdiction {jid}: {d['count']} historical meeting(s) deferred — "
                      f"{d['state']['reason']}")

    if limit:
        rows = rows[:limit]
    print(f"candidates with a stored next-packet: {len(rows)}")

    found = extracted = skipped = failures = 0
    for r in rows:
        slice_text, packet_url = None, None
        with connect() as conn:
            for url in r["packet_urls"]:
                pages = _stored_pages(conn, url)
                if not pages:
                    continue
                slice_text = find_embedded_slice(pages, r["meeting_date"])
                if slice_text is not None:
                    packet_url = url
                    break
        if slice_text is None:
            skipped += 1
            continue
        found += 1
        r = dict(r, packet_url=packet_url)
        if dry_run:
            print(f"  would extract meeting {r['meeting_id']} ({r['meeting_date']}) "
                  f"from {r['packet_url']}")
            continue
        try:
            # Content-addressed cache on the slice text: a re-run is free.
            with connect() as conn:
                chash = extraction_cache.content_hash(slice_text.encode())
                cached = extraction_cache.get(conn, chash, "minutes", EXTRACTOR_VERSION)
            if cached is not None:
                extraction = MeetingExtraction.model_validate(cached["extraction"])
                print(f"  ✓ cache hit {chash[:12]} for meeting {r['meeting_id']}")
            else:
                extraction = extract_from_text(slice_text)
                with connect() as conn:
                    from ..llm_client import current_usage
                    from ..pricing import cost_usd as _cost_usd
                    _u = current_usage()
                    extraction_cache.put(
                        conn, chash, "minutes", EXTRACTOR_VERSION,
                        extraction_json=extraction.model_dump_json(),
                        method="embedded_text", source_url=r["packet_url"],
                        cost_usd=(_cost_usd(_u) if _u is not None else None),
                    )
            MinutesExtract(
                r["meeting_id"], prebuilt_extraction=extraction,
                embedded_from=r["packet_url"],
            ).run()
            extracted += 1
        except FatalExtractionError as e:
            # API-infra failure (billing/auth) — every later call fails too.
            print(f"  ✋ fatal API error — stopping: {e}")
            with connect() as conn:
                record_failure(
                    conn, job_name="extract_embedded_minutes", step="fatal_api",
                    governing_body_id=r["governing_body_id"], meeting_id=r["meeting_id"],
                    message=str(e), context={"packet_url": r["packet_url"]},
                )
            failures += 1
            break
        except Exception as e:
            failures += 1
            print(f"  ✗ meeting {r['meeting_id']} ({r['meeting_date']}): {type(e).__name__}: {e}")
            with connect() as conn:
                record_failure(
                    conn, job_name="extract_embedded_minutes", step="extract",
                    governing_body_id=r["governing_body_id"], meeting_id=r["meeting_id"],
                    message=f"{type(e).__name__}: {e}",
                    context={"packet_url": r["packet_url"],
                             "meeting_date": str(r["meeting_date"])},
                )

    print(f"\ndone: embedded sections found={found}  extracted={extracted}  "
          f"no-section={skipped}  failures={failures}")
    return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract minutes embedded in next-meeting agenda packets.")
    ap.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; omit for all")
    ap.add_argument("--limit", type=int, default=None, help="max meetings this run")
    ap.add_argument("--dry-run", action="store_true", help="list matches; no extraction, no writes")
    ap.add_argument("--force", action="store_true", help="bypass the build-phase funding gate")
    args = ap.parse_args()
    return run(args.jurisdiction, limit=args.limit, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

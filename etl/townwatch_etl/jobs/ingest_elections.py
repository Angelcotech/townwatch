"""
Ingest certified election results for a jurisdiction.

Reads `elections.results_endpoint` from the jurisdiction config (set by
discover_election_source.py), fetches the landing page, finds every
certified-results PDF linked from it, runs each through the vision
extractor, and updates `term` rows for each winning candidate.

The pipeline is intentionally generic — no per-county code. The
extractor doesn't know what jurisdiction it's reading from; the ingest
job is the layer that resolves contests → governing_body + seat and
candidates → official.

For each winning candidate the job:
  1. Matches the contest to a governing_body in our DB (by name +
     jurisdiction scope)
  2. Matches the seat within that body (by district_name when present)
  3. Identity-resolves the candidate to an existing official via the
     same CachedResolver pattern we use for vote-name resolution in
     minutes. Creates a historical-official row when no match exists
     (we have backfill_historical_officials machinery for this exact
     case, but for elections we create inline so the term row gets a
     valid official_id).
  4. Inserts a new term row with start_date (Jan 1 of year following
     the election, per take_office_date_convention) and end_date
     (start_date + seat.term_length_years).
  5. Closes out any preceding is_current=TRUE term for the same seat
     by setting end_date and is_current=FALSE.

Idempotent on re-run — a term whose (official_id, seat_id, start_date)
matches an existing row is updated, not duplicated.

Run:
    python -m townwatch_etl.jobs.ingest_elections --slug columbia-county-ga
    python -m townwatch_etl.jobs.ingest_elections --slug columbia-county-ga --dry-run
    python -m townwatch_etl.jobs.ingest_elections                          # all configs with elections.results_endpoint
    python -m townwatch_etl.jobs.ingest_elections --slug X --max-pdfs 3   # cap PDF count (cost control)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from .. import identity
from ..audit import record_failure
from ..extractors.election_results import (
    ElectionResultsExtraction,
    Contest,
    CandidateResult,
    extract_from_pdf_bytes,
)
from ..ingest_base import IngestJob
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


USER_AGENT = "TownWatch-ingest-elections/0.1 (civic transparency research)"

# Body-type keywords used to identify which governing_body a contest's
# body_hint refers to. Full phrases ONLY — bare substrings like
# "commission" or "commissioner" produce false matches against
# unrelated offices ("Tax Commissioner", "Insurance Commissioner",
# "Public Service Commission") that AREN'T the body we track.
BODY_TYPE_KEYWORDS = {
    "city_council":             ["city council", "town council"],
    "county_commission":        ["board of commissioners", "county commission",
                                 "county commissioner"],
    "board_of_education":       ["board of education"],
    "school_board":             ["school board"],
}


class ElectionIngest(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str, *, dry_run: bool = False, max_pdfs: int | None = None) -> None:
        super().__init__()
        self.slug = slug
        self.dry_run = dry_run
        self.max_pdfs = max_pdfs
        self.config = load_config(slug)
        elections = self.config.get("elections") or {}
        self.endpoint: str | None = elections.get("results_endpoint")
        if not self.endpoint:
            raise RuntimeError(
                f"Jurisdiction {slug!r} has no elections.results_endpoint configured. "
                f"Run discover_election_source first."
            )
        self.source_name = f"elections_ingest:{slug}"
        self.source_url = self.endpoint

    def ingest(self) -> None:
        assert self.conn is not None

        jurisdiction_id = self._jurisdiction_id()

        # 1. Fetch landing page and pull every PDF link off it.
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True) as client:
            try:
                resp = client.get(self.endpoint)
                resp.raise_for_status()
                landing_html = resp.text
                landing_url = str(resp.url)
            except Exception as e:
                record_failure(
                    self.conn,
                    job_name="ingest_elections",
                    step="fetch_landing",
                    message=f"landing fetch failed: {type(e).__name__}: {e}",
                    context={"slug": self.slug, "endpoint": self.endpoint},
                )
                return

            all_pdf_urls = self._extract_pdf_links(landing_html, landing_url)
            print(f"  → {len(all_pdf_urls)} PDF link(s) found on the page")
            if not all_pdf_urls:
                print("    (the elections page may render PDFs via JS — extractor would need vision fallback)")
                return

            # Most election-office sites publish a LOT of PDFs: admin
            # daily-reports, absentee-ballot logs, polling-location
            # changes, etc. — alongside the certified-results PDFs we
            # actually want. Filename-scoring filters the haystack down
            # to the needles before we burn Anthropic credits on each.
            scored = sorted(
                ((self._score_pdf_filename(u), u) for u in all_pdf_urls),
                key=lambda x: -x[0],
            )
            relevant = [u for (s, u) in scored if s > 0]
            print(f"  → {len(relevant)} look like certified-results PDFs (filename score > 0)")
            if not relevant:
                print("    no filenames matched results-PDF keywords — skipping to avoid wasted vision calls")
                return
            pdf_urls = relevant
            if self.max_pdfs:
                pdf_urls = pdf_urls[: self.max_pdfs]
                print(f"  → capped to top {len(pdf_urls)} per --max-pdfs")

            # 2. For each PDF: download → extract → persist.
            total_contests = 0
            total_term_writes = 0
            for pdf_url in pdf_urls:
                print(f"\n  → extracting: {pdf_url}")
                try:
                    pdf_bytes = self._download_pdf(client, pdf_url)
                except Exception as e:
                    record_failure(
                        self.conn,
                        job_name="ingest_elections",
                        step="download_pdf",
                        message=f"PDF download failed for {pdf_url}: {type(e).__name__}: {e}",
                        context={"slug": self.slug, "pdf_url": pdf_url},
                    )
                    continue

                try:
                    extraction = extract_from_pdf_bytes(pdf_bytes)
                except Exception as e:
                    record_failure(
                        self.conn,
                        job_name="ingest_elections",
                        step="extract_pdf",
                        message=f"vision extraction failed for {pdf_url}: {type(e).__name__}: {e}",
                        context={"slug": self.slug, "pdf_url": pdf_url},
                    )
                    continue

                print(f"     extracted {len(extraction.contests)} contest(s), confidence={extraction.extraction_confidence}")
                if extraction.extraction_notes:
                    print(f"     notes: {extraction.extraction_notes}")
                total_contests += len(extraction.contests)
                writes = self._persist_extraction(jurisdiction_id, extraction, pdf_url)
                total_term_writes += writes

            print(f"\n  ✓ {total_contests} contest(s) seen, {total_term_writes} term row(s) written")

    # -- Landing-page parsing ----------------------------------------------

    @staticmethod
    def _extract_pdf_links(html: str, base_url: str) -> list[str]:
        """Pull every <a href> whose target looks like a PDF or document
        viewer off the landing page. Returns absolute URLs, de-duped,
        in document order.

        Beyond bare `.pdf` URLs, also catches civic-platform document-
        viewer patterns that redirect to PDFs:
          - CivicEngage:  /DocumentCenter/View/{id}
          - CivicEngage:  /DocumentCenter/Home/View/{id}
          - Granicus / Legistar variants are caught by the .pdf check
            since they serve the file directly.
        """
        pdf_pattern = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE)
        doc_center_pattern = re.compile(
            r'href=["\']([^"\']*DocumentCenter[^"\']*View[^"\']*)["\']',
            re.IGNORECASE,
        )
        urls: list[str] = []
        seen: set[str] = set()
        for pattern in (pdf_pattern, doc_center_pattern):
            for m in pattern.finditer(html):
                absolute = urljoin(base_url, m.group(1))
                if absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)
        return urls

    @staticmethod
    def _score_pdf_filename(url: str) -> int:
        """Score how likely a PDF is to be a certified-results document
        based on its URL/filename. Positive scores are good candidates;
        negative scores are admin docs we should skip."""
        u = url.lower()
        score = 0
        # Strong positives — keywords that appear in certified-results filenames
        for kw in ("certified-result", "certified result", "election-result", "election result",
                   "official-result", "official result", "official-summary", "canvass",
                   "summary-report", "summary report", "summary-results", "summary results",
                   "general-election", "primary-election",
                   "runoff", "results-of"):
            if kw in u:
                score += 4
        # Weaker positives — common results-related words
        for kw in ("results", "official", "certified", "canvass", "general election",
                   "primary"):
            if kw in u:
                score += 1
        # November General Elections in even years are where almost all
        # local seats (commissioners, council, school board) actually get
        # filled. Big boost so these float to the top above runoffs and
        # primaries (which more often cover federal/state-only races).
        if "november" in u and "general-election" in u and "runoff" not in u:
            score += 8
        if "runoff" in u:
            score -= 3  # runoffs are usually fed/state Senate
        if "primary" in u and "general-election" not in u:
            score -= 2  # primaries select candidates, not winners
        # Penalize per-precinct breakdowns — same race, less aggregated.
        # The summary version of the same election is sufficient for
        # term-tracking purposes; precinct data is downstream-only.
        if "by-precinct" in u or "precinct-pdf" in u.replace("_", "-"):
            score -= 4
        # Negatives — admin / logistics docs that aren't results
        for kw in ("daily-report", "daily report", "ballot-issued", "ballot issued",
                   "absentee-ballot-issued", "polling-location", "polling location",
                   "ballot-style", "election-board-meeting", "meeting-minutes",
                   "calendar", "instruction", "ballot-rejection", "rejection-cure",
                   "advance-voting", "early-voting-totals", "provisional"):
            if kw in u:
                score -= 5
        return score

    @staticmethod
    def _download_pdf(client: httpx.Client, url: str) -> bytes:
        r = client.get(url, timeout=30.0)
        r.raise_for_status()
        return r.content

    # -- DB lookups --------------------------------------------------------

    def _jurisdiction_id(self) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (jurisdiction_fips(self.config),),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Jurisdiction not found in DB for slug={self.slug!r}")
        return row["id"]

    # -- Persistence -------------------------------------------------------

    def _persist_extraction(
        self,
        jurisdiction_id: int,
        extraction: ElectionResultsExtraction,
        source_url: str,
    ) -> int:
        """Apply every contest from one PDF. Returns the count of term
        rows written (inserted or updated)."""
        assert self.conn is not None

        election_date = self._parse_date(extraction.election_date)
        if election_date is None:
            # Try to derive from filename / source URL
            election_date = self._date_from_url(source_url)
        if election_date is None:
            print(f"     ⚠ no election_date found — skipping this PDF (can't compute term dates)")
            return 0

        # Cache officials in this jurisdiction for fuzzy resolution.
        resolver = identity.CachedResolver(
            self.conn,
            jurisdiction_id=jurisdiction_id,
            data_source_id=self.data_source_id,
            source_system=self.source_name,
        )

        term_writes = 0
        for contest in extraction.contests:
            seat_row = self._match_contest_to_seat(jurisdiction_id, contest)
            if seat_row is None:
                # We only ingest contests for bodies we already track.
                # Tax commissioner, judge, state senate, etc. are skipped
                # gracefully here; once those bodies are added to a
                # jurisdiction's governing_bodies config, they'll start
                # matching on the next ingest run.
                continue

            for candidate in contest.candidates:
                if not candidate.is_winner:
                    continue
                official_id = self._resolve_or_create_official(
                    resolver, candidate.name, jurisdiction_id,
                )
                if official_id is None:
                    print(f"     ✗ couldn't resolve winner {candidate.name!r} in contest {contest.contest_name!r}")
                    continue
                wrote = self._upsert_term(
                    seat_id=seat_row["id"],
                    official_id=official_id,
                    election_date=election_date,
                    term_length_years=seat_row["term_length_years"] or 4,
                    source_url=source_url,
                )
                if wrote:
                    term_writes += 1
                    print(f"     ✓ {contest.contest_name} → {candidate.name} → term {wrote['start']}..{wrote['end']}")

        return term_writes

    def _match_contest_to_seat(self, jurisdiction_id: int, contest: Contest) -> Optional[dict]:
        """Two-step contest → seat match.

        Step 1: identify the body_type the contest is for. Reject if not
        a body we track (Tax Commissioner, Sheriff, US Senate, etc. all
        end here).

        Step 2: within that body, find the specific seat:
          - If contest has a district number (in seat_hint or parsed
            from body_hint/contest_name), match the seat whose
            district_name agrees.
          - If contest references "Chairman"/"Chair", match the body's
            leadership at-large seat.
          - Otherwise no match — refuse to write an at-large guess.

        This is intentionally strict. False matches (e.g. all winners
        landing on Chairman because it was the highest-scored fallback)
        corrupt term data; better to skip the contest and surface it
        as unmatched than to mis-route an official.
        """
        assert self.conn is not None
        body_hint = (contest.body_hint or "").lower()
        contest_name = (contest.contest_name or "").lower()
        combined = body_hint + " " + contest_name
        seat_hint = (contest.seat_hint or "").strip()

        # Step 1: identify body_type
        matched_body_type = None
        for body_type, keywords in BODY_TYPE_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                matched_body_type = body_type
                break
        if matched_body_type is None:
            return None  # not a body we track

        # Step 2a: try to find a district number
        district_token = self._extract_district_token(seat_hint, combined)

        if district_token:
            row = self.conn.execute(
                """
                SELECT s.id, s.name AS seat_name, s.district_name, s.seat_type,
                       s.term_length_years,
                       gb.id AS body_id, gb.name AS body_name, gb.body_type
                FROM seat s
                JOIN governing_body gb ON gb.id = s.governing_body_id
                WHERE gb.jurisdiction_id = %s
                  AND gb.body_type = %s
                  AND s.seat_type = 'district'
                  AND s.district_name = %s
                LIMIT 1
                """,
                (jurisdiction_id, matched_body_type, district_token),
            ).fetchone()
            return dict(row) if row else None

        # Step 2b: at-large or Chairman
        if any(t in combined for t in ("chairman", "chair ", " chair", "mayor")):
            row = self.conn.execute(
                """
                SELECT s.id, s.name AS seat_name, s.district_name, s.seat_type,
                       s.term_length_years,
                       gb.id AS body_id, gb.name AS body_name, gb.body_type
                FROM seat s
                JOIN governing_body gb ON gb.id = s.governing_body_id
                WHERE gb.jurisdiction_id = %s
                  AND gb.body_type = %s
                  AND s.seat_type = 'at_large'
                  AND s.is_leadership = TRUE
                LIMIT 1
                """,
                (jurisdiction_id, matched_body_type),
            ).fetchone()
            return dict(row) if row else None

        # No district and no leadership signal — refuse to guess.
        return None

    @staticmethod
    def _clean_candidate_name(name: str) -> str:
        """Strip incumbent / party / honorific notation that PDFs append
        to candidate names. 'Michael Carraway (I)' → 'Michael Carraway',
        'Jane Doe (D)' → 'Jane Doe', etc."""
        # Drop anything in trailing parentheses (incumbent / party flag)
        cleaned = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
        # Drop trailing party tags without parens
        cleaned = re.sub(r'\s+(rep|dem|ind|lib|grn|np)\.?$', '', cleaned, flags=re.IGNORECASE).strip()
        return cleaned or name

    @staticmethod
    def _extract_district_token(seat_hint: str, combined_text: str) -> Optional[str]:
        """Parse a district identifier (typically a number) from the
        seat_hint or fall back to a "district N" / "ward N" pattern in
        the contest text. Returns the bare token ('1', '2', '3', ...)
        that matches the form stored in seat.district_name."""
        # Try seat_hint directly — extractor sometimes returns just "3"
        # and sometimes "District 3"
        for source in (seat_hint, combined_text):
            if not source:
                continue
            m = re.search(r'(?:district|ward|dist\.?)\s*[#-]?\s*(\d+|\w+)', source, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        # Bare numeric seat_hint as last resort
        if seat_hint and seat_hint.isdigit():
            return seat_hint
        return None

    def _resolve_or_create_official(
        self, resolver: identity.CachedResolver, name: str, jurisdiction_id: int,
    ) -> Optional[int]:
        """Find an existing official by exact alias match, or create a
        historical official record.

        Deliberately strict (exact alias match only, no fuzzy) because
        election results commonly include candidates who share surnames
        with sitting officials — fuzzy match wrongly collapses 'Trey
        Allen' onto an existing 'James E. Allen' in the same county.
        For elections we want a NEW official record when no exact match
        exists; identity work to unify variants happens downstream."""
        assert self.conn is not None and self.data_source_id is not None
        # Normalize: strip incumbent / honorific markings the certified
        # results PDFs commonly carry. Without this, "Michael Carraway"
        # (existing) and "Michael Carraway (I)" (from PDF) become two
        # separate official records.
        clean_name = self._clean_candidate_name(name)
        # Try exact alias match only — bypasses the resolver's fuzzy path
        oid = identity.find_by_alias(self.conn, clean_name)
        if oid is None:
            oid = identity.find_by_alias(self.conn, identity.strip_title(clean_name))
        if oid is not None:
            return oid
        name = clean_name  # use cleaned name for creation below
        # Create historical official — same shape as
        # backfill_historical_officials creates them.
        stripped = identity.strip_title(name).strip()
        tokens = stripped.split()
        if len(tokens) < 2:
            return None
        first = tokens[0]
        last = tokens[-1]
        middle = " ".join(tokens[1:-1]) if len(tokens) > 2 else None
        new_oid = identity.create_official(
            self.conn,
            data_source_id=self.data_source_id,
            canonical_name=stripped,
            first_name=first,
            middle_name=middle,
            last_name=last,
        )
        identity.add_alias(
            self.conn,
            official_id=new_oid,
            alias_name=name,
            source_system=self.source_name,
            data_source_id=self.data_source_id,
        )
        # Refresh resolver cache so subsequent lookups in same run find it
        resolver._alias_map[name.lower()] = new_oid
        return new_oid

    def _upsert_term(
        self,
        *,
        seat_id: int,
        official_id: int,
        election_date: date,
        term_length_years: int,
        source_url: str,
    ) -> Optional[dict]:
        """Insert (or update) a term row for this winning candidate.
        Closes out any preceding is_current=TRUE term on the same seat.
        Returns {start, end} when a write happened, None on noop."""
        assert self.conn is not None and self.data_source_id is not None
        # Convention: GA local offices take office Jan 1 following the
        # general election. For non-GA states later, this should consult
        # the state defaults' take_office_date_convention.
        start_date = date(election_date.year + 1, 1, 1)
        end_date = date(start_date.year + term_length_years - 1, 12, 31)

        # Is there already a term row for this seat+official+start? Update.
        existing = self.conn.execute(
            """
            SELECT id, is_current, end_date
            FROM term
            WHERE seat_id = %s AND official_id = %s AND start_date = %s
            """,
            (seat_id, official_id, start_date),
        ).fetchone()

        if self.dry_run:
            return {"start": start_date.isoformat(), "end": end_date.isoformat()}

        if existing:
            # Already there — refresh end_date if it's null, keep is_current as-is
            if existing["end_date"] is None:
                self.conn.execute(
                    "UPDATE term SET end_date = %s, updated_at = now() WHERE id = %s",
                    (end_date, existing["id"]),
                )
            return {"start": start_date.isoformat(), "end": end_date.isoformat()}

        # Close out preceding current terms on this seat
        is_current_now = start_date <= date.today() <= end_date
        if is_current_now:
            self.conn.execute(
                """
                UPDATE term
                SET is_current = FALSE,
                    end_date = COALESCE(end_date, %s),
                    updated_at = now()
                WHERE seat_id = %s AND is_current = TRUE AND start_date < %s
                """,
                (start_date - timedelta(days=1), seat_id, start_date),
            )

        self.conn.execute(
            """
            INSERT INTO term (
                official_id, seat_id, start_date, end_date,
                is_current, how_seated, data_source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (official_id, seat_id, start_date, end_date, is_current_now,
             'elected', self.data_source_id),
        )
        return {"start": start_date.isoformat(), "end": end_date.isoformat()}

    # -- Date parsing ------------------------------------------------------

    @staticmethod
    def _parse_date(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _date_from_url(url: str) -> Optional[date]:
        """Heuristic: look for a 4-digit year in the URL filename.
        Approximate to Nov 5 of that year (general-election Tuesday-ish)
        when no explicit date is parseable. This is fine because the
        only thing election_date drives is the take-office year, not the
        precise day."""
        m = re.search(r"(20\d{2})", url)
        if not m:
            return None
        return date(int(m.group(1)), 11, 5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all with elections.results_endpoint configured.")
    parser.add_argument("--dry-run", action="store_true", help="Extract + report, no DB writes.")
    parser.add_argument("--max-pdfs", type=int, help="Cap the number of PDFs to process per jurisdiction (cost control).")
    args = parser.parse_args()

    if args.slug:
        slugs = [args.slug]
    else:
        slugs = [s for s in list_slugs()
                 if (load_config(s).get("elections") or {}).get("results_endpoint")]
        if not slugs:
            print("No jurisdictions have elections.results_endpoint configured.")
            return 0

    for slug in slugs:
        print(f"\n=== {slug} ===")
        try:
            ElectionIngest(slug, dry_run=args.dry_run, max_pdfs=args.max_pdfs).run()
        except Exception as e:
            print(f"  ✗ {slug}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

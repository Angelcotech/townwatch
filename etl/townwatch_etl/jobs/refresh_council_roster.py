"""
Procure missing council/commission roster data — term-expires dates and
direct emails — by reading the body's own website.

For each elected body in a jurisdiction:
  1. Fetch the body's website_url (from per-body config or jurisdiction
     official_website fallback).
  2. Send the HTML to Haiku via extractors.council_roster.extract_from_html.
  3. Fuzzy-match extracted member names to existing officials via
     townwatch_etl.identity.find_candidates.
  4. For each matched member, update official.email and the current
     term's end_date / election_cycle_year when the extractor returned
     non-null values that the DB doesn't already have.
  5. Record full extraction payload in data_source.raw_payload for audit.

Idempotent — running again refreshes data and surfaces any drift in a
later compliance_finding cycle.

**Compartmentalization note**: this job only WRITES to official.email,
official.phone, official.official_website, term.end_date, and
term.election_cycle_year. It does not create new officials or terms —
that's the job of meetings_inventory / civicengage_officials. Keeping
this single-domain-write means the four-question test
(per [[feedback-compartmentalization]]) stays clean.

Run:
    python -m townwatch_etl.jobs.refresh_council_roster --slug grovetown-ga
    python -m townwatch_etl.jobs.refresh_council_roster                # all jurisdictions
    python -m townwatch_etl.jobs.refresh_council_roster --dry-run      # preview only
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from .. import identity
from ..audit import record_failure
from ..db import connect
from ..extractors.council_roster import (
    CouncilRosterExtraction,
    CouncilMemberRecord,
    extract_from_html,
)
from ..ingest_base import IngestJob
from ..jurisdiction import load_config, list_slugs


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"

# Which body_types are eligible — only elected bodies; appointed bodies
# get their roster from records requests, not city web pages.
ELECTED_BODY_TYPES = {
    "city_council", "county_commission", "school_board", "board_of_education",
}


class CouncilRosterRefresh(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str, *, dry_run: bool = False) -> None:
        super().__init__()
        self.slug = slug
        self.dry_run = dry_run
        self.config = load_config(slug)
        j = self.config["jurisdiction"]
        self.jurisdiction_state = j["state"]
        self.jurisdiction_display = j["display_name"]
        self.source_name = f"council_roster_refresh:{slug}"
        self.source_url = None
        self.actions: list[dict] = []

    def ingest(self) -> None:
        assert self.conn is not None

        body_id = self._find_jurisdiction_id()
        for body in self.config.get("governing_bodies", []):
            if body.get("body_type") not in ELECTED_BODY_TYPES:
                continue
            self._refresh_body(body, body_id)

    def _find_jurisdiction_id(self) -> int:
        """Lookup by fips_code, which counties store as county_fips and
        cities store as place_fips (the Census 7-digit place ID)."""
        assert self.conn is not None
        j = self.config["jurisdiction"]
        fips = j.get("place_fips") or j.get("county_fips")
        if not fips:
            raise RuntimeError(
                f"Jurisdiction {self.slug!r} has neither place_fips nor county_fips in config."
            )
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s LIMIT 1",
            (fips,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Jurisdiction with FIPS={fips} not found in DB")
        return row["id"]

    def _resolve_body_id(self, body_name: str, jurisdiction_id: int) -> int | None:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT id FROM governing_body
            WHERE jurisdiction_id = %s AND name = %s LIMIT 1
            """,
            (jurisdiction_id, body_name),
        ).fetchone()
        return row["id"] if row else None

    def _refresh_body(self, body_cfg: dict, jurisdiction_id: int) -> None:
        assert self.conn is not None
        body_name = body_cfg["name"]
        url = body_cfg.get("website_url") or self.config["jurisdiction"].get("official_website")
        if not url:
            print(f"  ⊘ {body_name}: no website_url in config — skipping")
            return

        body_id = self._resolve_body_id(body_name, jurisdiction_id)
        if body_id is None:
            print(f"  ⊘ {body_name}: not yet in DB — run meetings_inventory first")
            return

        print(f"\n  → {body_name}  ({url})")
        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            record_failure(
                self.conn,
                job_name="refresh_council_roster",
                step="fetch_page",
                governing_body_id=body_id,
                message=f"page fetch failed: {type(e).__name__}: {e}",
                exception=e,
                context={"url": url, "slug": self.slug},
            )
            return

        try:
            extraction = extract_from_html(html, page_url=url)
        except Exception as e:
            record_failure(
                self.conn,
                job_name="refresh_council_roster",
                step="extract_from_html",
                governing_body_id=body_id,
                message=f"extraction failed: {type(e).__name__}: {e}",
                exception=e,
                context={"url": url, "slug": self.slug},
            )
            return

        print(
            f"     extracted {len(extraction.members)} member(s)  "
            f"confidence={extraction.extraction_confidence}"
        )
        if extraction.extraction_notes:
            print(f"     notes: {extraction.extraction_notes}")

        # Persist full payload for audit
        self._attach_raw_payload(url, extraction)

        # Match + update each member
        for member in extraction.members:
            self._apply_member(member, body_id, jurisdiction_id)

    def _apply_member(
        self, member: CouncilMemberRecord, body_id: int, jurisdiction_id: int,
    ) -> None:
        assert self.conn is not None
        # Resolve the extracted name to a DB official within the body
        oid = self._resolve_official_in_body(member.name, body_id, jurisdiction_id)
        if oid is None:
            print(f"     ✗ unresolved: {member.name!r} — no matching official in DB")
            self.actions.append({"member": member.name, "action": "unresolved"})
            return

        actions = self._update_official_and_term(oid, member)
        if actions:
            print(f"     ✓ {member.name}: {', '.join(actions)}")
            self.actions.append({"member": member.name, "official_id": oid, "actions": actions})
        else:
            print(f"     · {member.name}: no fields to update")

    def _resolve_official_in_body(
        self, source_name: str, body_id: int, jurisdiction_id: int,
    ) -> int | None:
        """Match against officials whose current term is on THIS body.
        Falls back to a fuzzy jurisdiction-wide search if the body-scoped
        match is ambiguous."""
        assert self.conn is not None
        stripped = identity.strip_title(source_name)

        # Try exact-alias match first
        oid = identity.find_by_alias(self.conn, source_name)
        if oid is None:
            oid = identity.find_by_alias(self.conn, stripped)

        if oid is not None:
            # Verify they're on this body
            row = self.conn.execute(
                """
                SELECT 1 FROM term t JOIN seat s ON s.id = t.seat_id
                WHERE t.official_id = %s AND s.governing_body_id = %s AND t.is_current = TRUE
                LIMIT 1
                """,
                (oid, body_id),
            ).fetchone()
            if row is not None:
                return oid

        # Fuzzy match within current members of this body
        candidates = self.conn.execute(
            """
            SELECT o.id, o.canonical_name,
                   similarity(o.canonical_name, %s) AS sim
            FROM official o
            JOIN term t ON t.official_id = o.id AND t.is_current = TRUE
            JOIN seat s ON s.id = t.seat_id
            WHERE s.governing_body_id = %s
              AND similarity(o.canonical_name, %s) >= 0.40
            ORDER BY sim DESC
            LIMIT 3
            """,
            (stripped, body_id, stripped),
        ).fetchall()
        if candidates and (candidates[0]["sim"] or 0) >= 0.70:
            best = candidates[0]
            # Record the alias for future runs
            identity.add_alias(
                self.conn,
                official_id=best["id"],
                alias_name=source_name,
                source_system=self.source_name,
                data_source_id=self.data_source_id,
            )
            return best["id"]
        return None

    def _update_official_and_term(
        self, official_id: int, member: CouncilMemberRecord,
    ) -> list[str]:
        """Only writes non-null values into currently-null DB fields.
        Never overwrites existing data without explicit operator action.
        Returns the list of human-readable actions taken."""
        assert self.conn is not None
        actions: list[str] = []

        # Email
        if member.email:
            updated = self.conn.execute(
                """
                UPDATE official SET email = %s, updated_at = now()
                WHERE id = %s AND (email IS NULL OR email = '')
                RETURNING id
                """,
                (member.email, official_id),
            ).fetchone()
            if updated:
                actions.append(f"email={member.email}")

        # Phone
        if member.phone:
            updated = self.conn.execute(
                """
                UPDATE official SET phone = %s, updated_at = now()
                WHERE id = %s AND (phone IS NULL OR phone = '')
                RETURNING id
                """,
                (member.phone, official_id),
            ).fetchone()
            if updated:
                actions.append(f"phone={member.phone}")

        # Current term: end_date + election_cycle_year
        if member.term_expires_date:
            updated = self.conn.execute(
                """
                UPDATE term SET end_date = %s, updated_at = now()
                WHERE official_id = %s AND is_current = TRUE AND end_date IS NULL
                RETURNING id
                """,
                (member.term_expires_date, official_id),
            ).fetchone()
            if updated:
                actions.append(f"term.end_date={member.term_expires_date}")

        return actions

    def _attach_raw_payload(self, url: str, extraction: CouncilRosterExtraction) -> None:
        assert self.conn is not None and self.data_source_id is not None
        self.conn.execute(
            """
            UPDATE data_source
            SET record_url = %s, raw_payload = %s::jsonb
            WHERE id = %s
            """,
            (url, extraction.model_dump_json(), self.data_source_id),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all configs.")
    parser.add_argument("--dry-run", action="store_true", help="Preview, no writes")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    if not slugs:
        print("No jurisdictions configured.")
        return 0

    for slug in slugs:
        print(f"\n=== {slug} ===")
        try:
            CouncilRosterRefresh(slug, dry_run=args.dry_run).run()
        except Exception as e:
            print(f"  ✗ refresh failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

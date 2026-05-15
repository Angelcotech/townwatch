"""
Create stub `official` records for historical staff named in the
extracted minutes but no longer on the city's current staff directory.

Problem this solves: people like John Waller (former City Administrator,
17 motions) appear in motion.staff_recommender but have no `official`
record. The home-page bubbles can't link to them, and the platform
can't tell readers "here's everything this person recommended."

Strategy:
  1. Find every distinct staff_recommender on motions, with motion count
  2. Filter to those NOT already matched by substring against any official
  3. Filter to those with >= MIN_MOTIONS (avoids noise)
  4. Parse out the human name (last 2 words of the recommender string —
     titles like "City Administrator" or "Director" precede the name)
  5. Create a stub `official` with is_elected=FALSE
  6. Add aliases for both the raw recommender string and the parsed name

Later: a Wayback Machine scraper can enrich these stubs with photos
and bios from past versions of the city directory.

Run:
    python -m townwatch_etl.jobs.create_historical_staff_stubs --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.create_historical_staff_stubs --jurisdiction grovetown-ga --min-motions 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from ..ingest_base import IngestJob
from ..identity import add_alias, create_official
from ..jurisdiction import load_config


# Words that, if found anywhere in the recommender string, suggest it's
# a body/department/firm rather than a single person.
NON_PERSON_PATTERNS = re.compile(
    r"\b(commission|committee|board|council|department|development|"
    r"services|operations|public|staff|review|"
    r"engineers|engineering|associates|consulting|company|"
    r"inc\.?|llc|firm|partners|group)\b",
    re.IGNORECASE,
)

# Words that, if they end up as the "first" half of our parsed name,
# mean we caught a title fragment instead of a real first name. The last
# 2 words were probably "Title LastName" rather than "First Last".
TITLE_WORDS = {
    "city", "interim", "acting", "deputy", "assistant", "asst",
    "chief", "captain", "capt", "lieutenant", "lt", "sergeant", "sgt",
    "officer", "major", "minor",
    "director", "administrator", "attorney", "clerk", "manager",
    "engineer", "coordinator", "secretary", "treasurer", "superintendent",
    "supervisor", "inspector",
    "mayor", "councilmember", "councilman", "councilwoman", "alderman",
    "hon.", "mr.", "ms.", "mrs.", "dr.",
}


def _parse_name(raw: str) -> tuple[str, str] | None:
    """
    Extract first_name + last_name from a recommender string like
    "City Administrator John Waller" → ("John", "Waller").

    Returns None if the string doesn't look like a single person.
    """
    if not raw or NON_PERSON_PATTERNS.search(raw):
        return None
    words = raw.strip().split()
    if len(words) < 2:
        return None
    last = words[-1].rstrip(",.;:")
    first = words[-2].rstrip(",.;:")
    if len(first) < 2 or first.endswith("."):
        return None
    # If the "first" word is a known title, we're picking up "Title LastName"
    # rather than a real "First Last" — almost certainly a half-name.
    if first.lower() in TITLE_WORDS:
        return None
    # Names should look like capitalized words (Latin alphabet + hyphen)
    if not re.fullmatch(r"[A-Z][A-Za-z\-'.]+", first) or not re.fullmatch(r"[A-Z][A-Za-z\-'.]+", last):
        return None
    return first, last


class CreateHistoricalStaffStubs(IngestJob):
    source_name = "historical_staff_stub_generator"
    source_type = "manual"
    source_url = "internal://historical-staff"

    def __init__(self, *, jurisdiction: str, min_motions: int = 3):
        super().__init__()
        self.jurisdiction_slug = jurisdiction
        self.min_motions = min_motions
        self.summary: dict | None = None

    def ingest(self) -> None:
        assert self.conn is not None and self.data_source_id is not None
        cfg = load_config(self.jurisdiction_slug)

        # Find unmatched staff_recommender values with >= min_motions
        rows = self.conn.execute("""
            SELECT m.staff_recommender AS raw, COUNT(*)::int AS motions
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.staff_recommender IS NOT NULL
              AND m.data_status = 'clean'
              AND j.fips_code = %s
              AND NOT EXISTS (
                  SELECT 1 FROM official o
                  WHERE LENGTH(o.canonical_name) >= 8
                    AND POSITION(LOWER(o.canonical_name) IN LOWER(m.staff_recommender)) > 0
              )
            GROUP BY m.staff_recommender
            HAVING COUNT(*) >= %s
            ORDER BY COUNT(*) DESC
        """, (cfg["jurisdiction"]["place_fips"], self.min_motions)).fetchall()

        print(f"  found {len(rows)} unmatched staff_recommender(s) with ≥ {self.min_motions} motion(s)")

        # Group rows by parsed name (e.g., "Finance Director Bradley Smith"
        # and "Finance Director/Asst. City Administrator Bradley Smith"
        # should resolve to ONE stub).
        by_name: dict[tuple[str, str], list[dict]] = {}
        unparsed = 0
        for r in rows:
            parsed = _parse_name(r["raw"])
            if not parsed:
                unparsed += 1
                continue
            by_name.setdefault(parsed, []).append(dict(r))

        created = 0
        for (first, last), variants in by_name.items():
            canonical = f"{first} {last}"
            # Sanity: skip if a substring match would have hit on canonical
            already = self.conn.execute(
                """SELECT id FROM official
                   WHERE LENGTH(canonical_name) >= 8
                     AND (LOWER(canonical_name) = LOWER(%s)
                          OR POSITION(LOWER(canonical_name) IN LOWER(%s)) > 0)
                   LIMIT 1""",
                (canonical, canonical),
            ).fetchone()
            if already:
                continue

            official_id = create_official(
                self.conn,
                data_source_id=self.data_source_id,
                canonical_name=canonical,
                first_name=first,
                last_name=last,
            )
            # Stubs derived from historical motions are by definition not
            # on the current city directory — mark inactive at creation.
            self.conn.execute(
                "UPDATE official SET is_elected = FALSE, is_active = FALSE WHERE id = %s",
                (official_id,),
            )
            # Alias the canonical form and every raw variant we saw
            add_alias(
                self.conn,
                official_id=official_id,
                alias_name=canonical,
                source_system="historical_staff_stub",
                data_source_id=self.data_source_id,
            )
            for v in variants:
                add_alias(
                    self.conn,
                    official_id=official_id,
                    alias_name=v["raw"],
                    source_system="historical_staff_stub",
                    data_source_id=self.data_source_id,
                )

            total_motions = sum(v["motions"] for v in variants)
            print(
                f"  + created #{official_id} {canonical} "
                f"({total_motions} motions across {len(variants)} title-form{'s' if len(variants) != 1 else ''})"
            )
            created += 1
            self.rows_written += 1

        self.summary = {
            "candidates_found": len(rows),
            "stubs_created": created,
            "unparsed": unparsed,
        }
        print(f"\n  summary: {self.summary}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    parser.add_argument("--min-motions", type=int, default=2,
                        help="Minimum motion count to create a stub (default 2)")
    args = parser.parse_args()
    job = CreateHistoricalStaffStubs(
        jurisdiction=args.jurisdiction, min_motions=args.min_motions
    )
    result = job.run()
    print()
    print(json.dumps({"job": result, "stubs": job.summary}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

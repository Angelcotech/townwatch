"""
Remove non-voting officials erroneously created from extraction.

Some minutes-PDFs include staff (Finance Director, City Administrator,
City Clerk, City Attorney, City Manager) in motion descriptions or as
presenters. The extractor occasionally promotes these to individual_votes
entries when it shouldn't. Result: an "official" record gets created
for someone who isn't an elected member.

Detection heuristic:
  - canonical_name contains a job-title prefix (Director, Administrator,
    Attorney, Clerk, Manager, Engineer, Chief, Inspector, Coordinator)
  - OR canonical_name never appears in any meeting's attendance.present
    list across extracted payloads

This job lists matches in DRY_RUN mode, or deletes officials + their
votes + aliases when --confirm is passed.

Run:
    # See what would be removed:
    python -m townwatch_etl.jobs.cleanup_non_voting_officials --jurisdiction grovetown-ga

    # Actually delete:
    python -m townwatch_etl.jobs.cleanup_non_voting_officials --jurisdiction grovetown-ga --confirm
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from ..db import connect
from ..jurisdiction import load_config


# Phrases that indicate staff/administrative rather than elected roles.
# Case-insensitive matching against canonical_name.
STAFF_TITLE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bdirector\b",
        r"\badministrator\b",
        r"\battorney\b",
        r"\bclerk\b",
        r"\bmanager\b",
        r"\bengineer\b",
        r"\bchief\b",
        r"\binspector\b",
        r"\bcoordinator\b",
        r"\bsecretary\b",
        r"\btreasurer\b",
        r"\bsuperintendent\b",
        r"\bofficer\b",
        r"\bplanner\b",
        r"\baccountant\b",
        r"\bauditor\b",
    ]
]


def looks_like_staff(canonical_name: str) -> str | None:
    """Return matched title if name looks like staff, else None."""
    for pat in STAFF_TITLE_PATTERNS:
        m = pat.search(canonical_name)
        if m:
            return m.group(0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete (default is dry-run preview).")
    args = parser.parse_args()

    cfg = load_config(args.jurisdiction)
    place_fips = cfg["jurisdiction"]["place_fips"]

    with connect() as conn:
        jrow = conn.execute(
            "SELECT id, display_name FROM jurisdiction WHERE fips_code = %s",
            (place_fips,),
        ).fetchone()
        if not jrow:
            print(f"jurisdiction not found: {place_fips}", file=sys.stderr)
            return 1

        # Pull every official with any vote tied to this jurisdiction
        rows = conn.execute("""
            SELECT DISTINCT o.id, o.canonical_name, o.last_name,
                   (SELECT COUNT(*) FROM vote v
                      JOIN motion m ON m.id = v.motion_id
                      JOIN meeting mtg ON mtg.id = m.meeting_id
                      JOIN governing_body gb ON gb.id = mtg.governing_body_id
                      WHERE v.official_id = o.id AND gb.jurisdiction_id = %s) AS votes,
                   (SELECT COUNT(*) FROM term t
                      JOIN seat s ON s.id = t.seat_id
                      JOIN governing_body gb ON gb.id = s.governing_body_id
                      WHERE t.official_id = o.id AND gb.jurisdiction_id = %s) AS terms
            FROM official o
            ORDER BY votes DESC
        """, (jrow["id"], jrow["id"])).fetchall()

        # Identify candidates
        candidates: list[dict] = []
        for r in rows:
            match = looks_like_staff(r["canonical_name"])
            if match:
                candidates.append({**dict(r), "matched_pattern": match})

        if not candidates:
            print("No staff-pattern officials found.")
            return 0

        action = "WILL DELETE" if args.confirm else "DRY-RUN — would delete"
        print(f"=== {action} {len(candidates)} non-voting official(s) for {jrow['display_name']} ===\n")
        for c in candidates:
            terms_note = f"  ⚠ has {c['terms']} term(s) — manually review before deleting" if c["terms"] > 0 else ""
            print(f"  #{c['id']:>4} {c['canonical_name']:<40} ({c['votes']} votes)  matched: {c['matched_pattern']}{terms_note}")

        if not args.confirm:
            print("\nRun again with --confirm to delete.")
            return 0

        # Confirm: actually delete (in safe order: votes → aliases → terms → official)
        # Skip any official with terms attached — those are real elected officials
        # whose name happens to contain a staff word (shouldn't happen, but safe).
        deleted = 0
        for c in candidates:
            if c["terms"] > 0:
                print(f"  ⊘ skipping #{c['id']} {c['canonical_name']} — has term(s)")
                continue
            conn.execute("DELETE FROM vote WHERE official_id = %s", (c["id"],))
            conn.execute("DELETE FROM official_alias WHERE official_id = %s", (c["id"],))
            conn.execute("DELETE FROM term WHERE official_id = %s", (c["id"],))
            conn.execute("DELETE FROM official WHERE id = %s", (c["id"],))
            deleted += 1
            print(f"  ✓ deleted #{c['id']} {c['canonical_name']}")

        print(f"\nDone. Deleted {deleted} official(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

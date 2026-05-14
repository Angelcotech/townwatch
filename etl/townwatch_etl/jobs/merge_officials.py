"""
Merge duplicate official records produced by OCR errors or name variants.

Finds officials with highly-similar names (pg_trgm), pairs each with the
canonical record (the one with more votes / longer tenure), and merges
the duplicate by:
  - moving all votes from duplicate → canonical
  - moving all terms from duplicate → canonical
  - keeping the duplicate's name as an alias on the canonical
  - deleting the duplicate's official + alias rows

Modes:
  - Default (dry-run): print proposed merges, don't change DB.
  - --confirm: apply the merges.
  - --auto: also auto-merge when first-name matches AND vote-count
    ratio is greater than 50:1 (very confident).
  - --threshold X: pg_trgm similarity threshold (default 0.65).

Run:
    python -m townwatch_etl.jobs.merge_officials                # dry run
    python -m townwatch_etl.jobs.merge_officials --confirm      # apply
    python -m townwatch_etl.jobs.merge_officials --confirm --auto
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from ..db import connect


@dataclass
class MergeCandidate:
    canonical_id: int
    canonical_name: str
    canonical_votes: int
    duplicate_id: int
    duplicate_name: str
    duplicate_votes: int
    similarity: float

    @property
    def first_name_match(self) -> bool:
        c_first = self.canonical_name.split()[0].lower() if self.canonical_name else ""
        d_first = self.duplicate_name.split()[0].lower() if self.duplicate_name else ""
        return bool(c_first) and c_first == d_first

    @property
    def vote_ratio(self) -> float:
        return self.canonical_votes / max(self.duplicate_votes, 1)


def find_candidates(conn, threshold: float) -> list[MergeCandidate]:
    rows = conn.execute("""
        SELECT
            o1.id AS id1, o1.canonical_name AS n1,
            o2.id AS id2, o2.canonical_name AS n2,
            similarity(o1.canonical_name, o2.canonical_name) AS sim,
            (SELECT COUNT(*) FROM vote WHERE official_id = o1.id) AS v1,
            (SELECT COUNT(*) FROM vote WHERE official_id = o2.id) AS v2
        FROM official o1 JOIN official o2 ON o1.id < o2.id
        WHERE similarity(o1.canonical_name, o2.canonical_name) > %s
        ORDER BY sim DESC
    """, (threshold,)).fetchall()

    out: list[MergeCandidate] = []
    for r in rows:
        # Canonical = the one with more votes (or, tied, lower id)
        if r["v1"] >= r["v2"]:
            out.append(MergeCandidate(
                canonical_id=r["id1"], canonical_name=r["n1"], canonical_votes=int(r["v1"]),
                duplicate_id=r["id2"], duplicate_name=r["n2"], duplicate_votes=int(r["v2"]),
                similarity=float(r["sim"]),
            ))
        else:
            out.append(MergeCandidate(
                canonical_id=r["id2"], canonical_name=r["n2"], canonical_votes=int(r["v2"]),
                duplicate_id=r["id1"], duplicate_name=r["n1"], duplicate_votes=int(r["v1"]),
                similarity=float(r["sim"]),
            ))
    return out


def apply_merge(conn, c: MergeCandidate) -> None:
    """Move votes/terms/aliases from duplicate → canonical, then delete duplicate."""
    # Move votes (skip if a vote already exists for canonical on the same motion)
    conn.execute("""
        UPDATE vote SET official_id = %s
        WHERE official_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM vote v2
            WHERE v2.official_id = %s AND v2.motion_id = vote.motion_id
          )
    """, (c.canonical_id, c.duplicate_id, c.canonical_id))
    # Anything that didn't move (conflict) → delete
    conn.execute("DELETE FROM vote WHERE official_id = %s", (c.duplicate_id,))

    # Move terms (rare for OCR duplicates, but handle it)
    conn.execute("UPDATE term SET official_id = %s WHERE official_id = %s",
                 (c.canonical_id, c.duplicate_id))

    # Move aliases (ON CONFLICT skips dups)
    conn.execute("""
        UPDATE official_alias SET official_id = %s
        WHERE official_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM official_alias a2
            WHERE a2.official_id = %s AND a2.alias_name = official_alias.alias_name
          )
    """, (c.canonical_id, c.duplicate_id, c.canonical_id))
    conn.execute("DELETE FROM official_alias WHERE official_id = %s", (c.duplicate_id,))

    # Keep the duplicate's canonical_name as an alias on the canonical (audit trail)
    conn.execute("""
        INSERT INTO official_alias (official_id, alias_name, source_system, data_source_id)
        SELECT %s, %s, 'merge_officials_job', data_source_id
        FROM official WHERE id = %s
        ON CONFLICT (official_id, alias_name) DO NOTHING
    """, (c.canonical_id, c.duplicate_name, c.duplicate_id))

    # Delete the duplicate
    conn.execute("DELETE FROM official WHERE id = %s", (c.duplicate_id,))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.65,
                        help="pg_trgm similarity threshold for duplicate detection")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually apply merges (default is dry-run)")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-merge when first-name matches and vote ratio > 50:1")
    parser.add_argument("--force-pair", nargs=2, type=int, metavar=("CANONICAL_ID", "DUP_ID"),
                        help="Force merge of two specific officials (canonical, duplicate)")
    args = parser.parse_args()

    with connect() as conn:
        # Force-pair short-circuits the candidate-finding logic
        if args.force_pair:
            canonical_id, dup_id = args.force_pair
            rows = conn.execute("""
                SELECT id, canonical_name,
                       (SELECT COUNT(*) FROM vote WHERE official_id = id) AS votes
                FROM official WHERE id IN (%s, %s)
            """, (canonical_id, dup_id)).fetchall()
            by_id = {r["id"]: r for r in rows}
            if len(by_id) != 2:
                print(f"ERROR: one or both ids not found", file=sys.stderr)
                return 1
            c = MergeCandidate(
                canonical_id=canonical_id, canonical_name=by_id[canonical_id]["canonical_name"],
                canonical_votes=int(by_id[canonical_id]["votes"]),
                duplicate_id=dup_id, duplicate_name=by_id[dup_id]["canonical_name"],
                duplicate_votes=int(by_id[dup_id]["votes"]),
                similarity=0.0,
            )
            print(f"  FORCED MERGE\n     canonical: #{c.canonical_id} {c.canonical_name} ({c.canonical_votes} votes)")
            print(f"     duplicate: #{c.duplicate_id} {c.duplicate_name} ({c.duplicate_votes} votes)")
            if not args.confirm:
                print(f"\nRun again with --confirm to apply.")
                return 0
            apply_merge(conn, c)
            print(f"     → MERGED into #{c.canonical_id}")
            return 0

        candidates = find_candidates(conn, args.threshold)

        if not candidates:
            print("No merge candidates found.")
            return 0

        action = "APPLYING" if args.confirm else "DRY-RUN — would merge"
        print(f"=== {action} (threshold={args.threshold}) ===\n")

        applied = 0
        skipped_for_review = 0
        for c in candidates:
            confident = c.first_name_match and c.vote_ratio > 50
            tag = "✓ auto-confident" if confident else "? needs review"
            print(
                f"  sim={c.similarity:.2f}  [{tag}]\n"
                f"     canonical: #{c.canonical_id:>3} {c.canonical_name:<32} ({c.canonical_votes} votes)\n"
                f"     duplicate: #{c.duplicate_id:>3} {c.duplicate_name:<32} ({c.duplicate_votes} votes)"
            )
            should_merge = args.confirm and (args.auto or confident)
            if should_merge:
                apply_merge(conn, c)
                applied += 1
                print(f"     → MERGED into #{c.canonical_id}")
            elif args.confirm:
                skipped_for_review += 1
                print(f"     → skipped (no --auto, not auto-confident)")
            print()

        if args.confirm:
            print(f"Done. Applied {applied} merge(s). {skipped_for_review} skipped for manual review.")
        else:
            print(f"Run again with --confirm to apply. Use --auto to merge only high-confidence pairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

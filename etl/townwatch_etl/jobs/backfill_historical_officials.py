"""
Backfill historical officials from already-extracted meeting payloads.

For one jurisdiction:
  1. Walk every data_source row that has a raw_payload (meeting extractions)
  2. Collect every official name appearing in attendance + individual_votes
  3. For names that don't resolve via existing aliases, create canonical
     officials (no current term — they're historical)
  4. Re-process each meeting's payload: match motions by title, resolve
     each vote-name (now with the new officials in place), insert any
     vote rows that didn't get written during the original extraction

Idempotent — re-runs only insert votes that don't already exist.
Generic — works for any jurisdiction whose meetings have been extracted.

Uses CachedResolver and bulk_insert so the whole job runs in seconds
even with thousands of votes to backfill.

Run:
    python -m townwatch_etl.jobs.backfill_historical_officials --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from typing import Any

from .. import identity
from ..ingest_base import IngestJob
from ..jurisdiction import load_config


class BackfillHistoricalOfficials(IngestJob):
    source_name = "townwatch_backfill_historical"
    source_type = "manual"
    source_url = "internal://backfill"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        self.jurisdiction_id: int | None = None
        self.resolver: identity.CachedResolver | None = None
        # motion_id → set of official_ids that already have a vote row
        self.existing_votes: dict[int, set[int]] = {}

    def ingest(self) -> None:
        assert self.conn is not None
        self.jurisdiction_id = self._jurisdiction_id()
        if self.jurisdiction_id is None:
            raise RuntimeError(f"jurisdiction not found in DB for slug={self.slug}")

        meeting_rows = self._collect_meeting_payloads()
        print(f"  → {len(meeting_rows)} meeting(s) with extraction payload")

        canonical_by_surname = self._collect_canonical_names(meeting_rows)
        print(f"  → discovered {len(canonical_by_surname)} distinct surname(s)")

        new_count = self._create_historical_officials(canonical_by_surname)
        print(f"  → created {new_count} new historical official record(s)")

        # Initialize resolver AFTER officials exist (cache reflects the new state)
        self.resolver = identity.CachedResolver(
            self.conn,
            jurisdiction_id=self.jurisdiction_id,
            data_source_id=self.data_source_id,
            source_system=self.source_name,
        )
        self._load_existing_votes()
        print(
            f"  → cache: {len(self.resolver._alias_map)} aliases, "
            f"{sum(len(v) for v in self.resolver._last_name_map.values())} surname mappings, "
            f"{sum(len(v) for v in self.existing_votes.values())} existing votes"
        )

        votes_inserted = self._reinsert_votes(meeting_rows)
        print(f"  → inserted {votes_inserted} previously-skipped vote(s)")

    # -- DB queries -------------------------------------------------------

    def _jurisdiction_id(self) -> int | None:
        # Resolve via the centralized helper so cities + counties both
        # work (cities have place_fips, counties have county_fips).
        from ..jurisdiction import jurisdiction_fips
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (jurisdiction_fips(self.config),),
        ).fetchone()
        return row["id"] if row else None

    def _collect_meeting_payloads(self) -> list[dict[str, Any]]:
        assert self.conn is not None
        rows = self.conn.execute("""
            SELECT m.id AS meeting_id, m.meeting_date, ds.raw_payload
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN data_source ds   ON ds.record_url = m.minutes_url
            WHERE gb.jurisdiction_id = %s
              AND ds.source_name LIKE %s
              AND ds.raw_payload IS NOT NULL
            ORDER BY m.meeting_date
        """, (self.jurisdiction_id, "%claude_extract%")).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            payload = r["raw_payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            motions = self.conn.execute(
                "SELECT id, title FROM motion WHERE meeting_id = %s",
                (r["meeting_id"],),
            ).fetchall()
            out.append({
                "meeting_id":   r["meeting_id"],
                "meeting_date": r["meeting_date"],
                "payload":      payload,
                "motions":      [{"id": m["id"], "title": (m["title"] or "").strip()} for m in motions],
            })
        return out

    def _load_existing_votes(self) -> None:
        assert self.conn is not None
        for r in self.conn.execute("""
            SELECT v.motion_id, v.official_id
            FROM vote v
            JOIN motion m  ON m.id = v.motion_id
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            WHERE gb.jurisdiction_id = %s
        """, (self.jurisdiction_id,)).fetchall():
            self.existing_votes.setdefault(r["motion_id"], set()).add(r["official_id"])

    # -- canonical name discovery -----------------------------------------

    def _collect_canonical_names(self, meeting_rows: list[dict]) -> dict[tuple, str]:
        """Distinct PEOPLE seen in the historical payloads, keyed by
        (goes-by-first-name, surname) — NOT by surname alone. The old
        surname-only keying kept one longest name per surname, collapsing
        'Amy Brazell', 'James Brazell', and 'Karen R. Brazell' into a single
        identity (the 2026-06-12 merge corruption)."""
        by_key: dict[tuple, str] = defaultdict(str)
        for row in meeting_rows:
            payload = row["payload"]
            attendance = payload.get("attendance", {}) or {}
            for name in (attendance.get("present", []) + attendance.get("absent", [])):
                self._add_candidate(by_key, name)
            for item in (payload.get("agenda_items", []) or []):
                for v in (item.get("individual_votes", []) or []):
                    self._add_candidate(by_key, v.get("name", ""))
                for rc in (item.get("recusals", []) or []):
                    self._add_candidate(by_key, rc.get("name", ""))
        return dict(by_key)

    @staticmethod
    def _goes_by(parts: dict) -> str | None:
        """The name a person is addressed by: the first name, or — when the
        first is a bare initial — the first middle word ('A. Richard Bowman'
        goes by Richard)."""
        first = (parts["first"] or "").rstrip(".")
        if len(first) == 1 and parts["middle"]:
            return parts["middle"].split()[0].rstrip(".")
        return parts["first"]

    @classmethod
    def _add_candidate(cls, by_key: dict[tuple, str], name: str) -> None:
        if not name or identity.looks_unresolvable(name):
            return  # illegible/garbage references must never become officials
        stripped = identity.strip_title(name).strip()
        parts = identity.split_person_name(stripped)
        if not parts["first"] or not parts["last"]:
            return  # a bare surname can't define a person, only reference one
        goes_by = cls._goes_by(parts)
        if not goes_by:
            return
        key = (goes_by.lower(), parts["last"].lower())
        if len(stripped) > len(by_key.get(key, "")):
            by_key[key] = stripped

    # -- historical official creation -------------------------------------

    def _create_historical_officials(self, canonical_by_key: dict[tuple, str]) -> int:
        """Create (or alias) one official per PERSON. An existing same-surname
        official only absorbs the name when the first names AGREE — the old
        unconditional alias-to-any-same-surname is what attached 'Ceretta
        Smith' to Bradley Smith (staff) and 'Michael D. Tuttle' to Beverly.
        First-name conflict means a different human: create them."""
        assert self.conn is not None and self.data_source_id is not None
        created = 0
        for (_goes_by, surname), canonical_name in canonical_by_key.items():
            parts = identity.split_person_name(identity.strip_title(canonical_name))
            existing = self.conn.execute("""
                SELECT DISTINCT o.id, o.canonical_name, o.first_name
                FROM official o
                LEFT JOIN term t            ON t.official_id = o.id
                LEFT JOIN seat s            ON s.id = t.seat_id
                LEFT JOIN governing_body gb ON gb.id = s.governing_body_id
                WHERE (gb.jurisdiction_id = %s OR gb.id IS NULL)
                  AND LOWER(o.last_name) = LOWER(%s)
            """, (self.jurisdiction_id, surname)).fetchall()

            agreeing = [r for r in existing
                        if identity.first_names_agree(parts["first"], r["first_name"])
                        or identity.first_names_agree(self._goes_by(parts), r["first_name"])]
            if len(agreeing) == 1:
                r = agreeing[0]
                if r["canonical_name"].strip().lower() != canonical_name.strip().lower():
                    identity.add_alias(
                        self.conn,
                        official_id=r["id"],
                        alias_name=canonical_name,
                        source_system=self.source_name,
                        data_source_id=self.data_source_id,
                    )
                continue
            if len(agreeing) > 1:
                # Ambiguous — two plausible matches is a human call, not a
                # guess. Leave unresolved; the votes pass will report it.
                print(f"  ⚠ ambiguous identity {canonical_name!r}: "
                      f"{[r['canonical_name'] for r in agreeing]} — not merging")
                continue

            oid = identity.create_official(
                self.conn,
                data_source_id=self.data_source_id,
                canonical_name=canonical_name,
                first_name=parts["first"],
                middle_name=parts["middle"],
                last_name=parts["last"],
            )
            identity.add_alias(
                self.conn,
                official_id=oid,
                alias_name=canonical_name,
                source_system=self.source_name,
                data_source_id=self.data_source_id,
            )
            created += 1
        return created

    # -- vote backfill ----------------------------------------------------

    def _reinsert_votes(self, meeting_rows: list[dict]) -> int:
        assert self.conn is not None and self.data_source_id is not None and self.resolver is not None

        to_insert: list[dict[str, Any]] = []
        term_id_cache: dict[tuple[int, date], int | None] = {}

        for row in meeting_rows:
            motion_by_title = {m["title"]: m["id"] for m in row["motions"]}
            payload = row["payload"]
            meeting_date = row["meeting_date"]
            for item in (payload.get("agenda_items", []) or []):
                motion_id = motion_by_title.get((item.get("title") or "").strip())
                if motion_id is None:
                    continue
                existing = self.existing_votes.setdefault(motion_id, set())
                recusal_by_name = {
                    rc["name"]: (rc.get("reason") or "recused (no reason given)")
                    for rc in (item.get("recusals") or [])
                }
                for v in (item.get("individual_votes", []) or []):
                    name = v.get("name", "")
                    official_id = self.resolver.resolve(name)
                    if official_id is None or official_id in existing:
                        continue

                    vote_value = v.get("vote") or "yes"
                    notes = v.get("notes")
                    if name in recusal_by_name:
                        vote_value = "conflict_recusal"
                        notes = recusal_by_name[name]

                    cache_key = (official_id, meeting_date)
                    if cache_key not in term_id_cache:
                        term_id_cache[cache_key] = self._lookup_term_id(official_id, meeting_date)
                    term_id = term_id_cache[cache_key]

                    to_insert.append({
                        "official_id":  official_id,
                        "motion_id":    motion_id,
                        "term_id":      term_id,
                        "vote_value":   vote_value,
                        "notes":        notes,
                    })
                    existing.add(official_id)

        return self.bulk_insert("vote", to_insert)

    def _lookup_term_id(self, official_id: int, meeting_date: date) -> int | None:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT id FROM term
            WHERE official_id = %s
              AND start_date <= %s
              AND (end_date IS NULL OR end_date >= %s)
            ORDER BY start_date DESC LIMIT 1
            """,
            (official_id, meeting_date, meeting_date),
        ).fetchone()
        return row["id"] if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    args = parser.parse_args()
    result = BackfillHistoricalOfficials(args.jurisdiction).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

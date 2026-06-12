"""
Ingest appointed-board rosters from a CivicPlus content page.

Driven by the jurisdiction config's `data_sources.appointed_rosters` block:

    "appointed_rosters": {
      "url": "https://cityofgrovetown.com/164/Planning-Commission-and-Board-of-Zoning-",
      "format": "civicplus_heading_list",
      "bodies": [
        {"heading": "Grovetown Planning Commission", "body_name": "Planning Commission"},
        {"heading": "Grovetown Board of Zoning Appeals", "body_name": "Board of Zoning Appeals"}
      ]
    }

Jurisdictions without the block are a clean no-op (exit 0), so the job can
sit in the daily refresh for every town. Cheap: one HTML fetch, no models.

Writes (idempotent): seats ("Board Seat N", seat_type 'appointed') for each
configured body, officials + aliases via identity resolution, and current
terms with how_seated='appointed' and end_date = the published term
expiration. Members who disappear from the page get their term closed
(is_current=false) so the roster tracks reality.

Why this exists: the 2026-06-12 findings re-audit refuted Grovetown's
member_roster_missing findings — both rosters were published on a department
subpage all along (no dedicated Boards & Commissions page exists; the
single-page recon missed it). This job ingests what's published so the
observer measures the world, not our own gap.

Run:
    python -m townwatch_etl.jobs.civicplus_board_rosters --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from .. import identity
from ..http_client import civic_get
from ..ingest_base import IngestJob
from ..jurisdiction import load_config
from ..scrapers.civicplus_board_list import parse_board_list


class CivicPlusBoardRosters(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        self.src = (self.config.get("data_sources") or {}).get("appointed_rosters")
        if self.src:
            from urllib.parse import urlparse
            self.source_name = urlparse(self.src["url"]).netloc + " (appointed boards)"
            self.source_url = self.src["url"]

    def ingest(self) -> None:
        assert self.src is not None
        html = civic_get(self.src["url"], timeout=30.0).text
        jid = self._jurisdiction_id()
        for board in self.src.get("bodies", []):
            members = parse_board_list(html, board["heading"])
            if not members:
                print(f"  ⚠ {board['body_name']}: heading found but no members parsed — check the page")
                continue
            body_id = self._body_id(jid, board["body_name"])
            if body_id is None:
                print(f"  ⚠ {board['body_name']}: no governing_body row — skipping")
                continue
            self._sync_board(body_id, members)
            print(f"  ✓ {board['body_name']}: {len(members)} member(s) "
                  f"({sum(1 for m in members if m['term_expires'])} with term dates)")

    # -- helpers -------------------------------------------------------------

    def _jurisdiction_id(self) -> int:
        assert self.conn is not None
        fips = self.config["jurisdiction"]["place_fips"]
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
        if not row:
            raise ValueError(f"no jurisdiction row for {self.slug} (fips {fips})")
        return row["id"]

    def _body_id(self, jid: int, name: str) -> int | None:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM governing_body WHERE jurisdiction_id = %s AND name = %s",
            (jid, name)).fetchone()
        return row["id"] if row else None

    def _ensure_seat(self, body_id: int, idx: int, leadership: bool) -> int:
        assert self.conn is not None
        name = f"Board Seat {idx}"
        row = self.conn.execute(
            "SELECT id FROM seat WHERE governing_body_id = %s AND name = %s",
            (body_id, name)).fetchone()
        if row:
            return row["id"]
        new_id = self.insert("seat", {
            "governing_body_id": body_id,
            "name": name,
            "seat_type": "appointed",
            "is_leadership": leadership,
        })
        assert new_id is not None
        return new_id

    def _ensure_official(self, member: dict[str, Any]) -> int:
        assert self.conn is not None
        name = member["name"]
        existing = identity.find_by_alias(self.conn, name)
        if existing is not None:
            return existing
        parts = name.split()
        assert self.data_source_id is not None
        official_id = identity.create_official(
            self.conn,
            data_source_id=self.data_source_id,
            canonical_name=name,
            first_name=parts[0],
            middle_name=" ".join(parts[1:-1]) if len(parts) > 2 else None,
            last_name=parts[-1],
        )
        identity.add_alias(
            self.conn, official_id=official_id, alias_name=name,
            source_system=self.source_name, data_source_id=self.data_source_id,
        )
        return official_id

    def _sync_board(self, body_id: int, members: list[dict[str, Any]]) -> None:
        assert self.conn is not None
        current_ids: list[int] = []
        for idx, m in enumerate(members, 1):
            leadership = bool(m["role"] and "chair" in m["role"].lower())
            seat_id = self._ensure_seat(body_id, idx, leadership)
            official_id = self._ensure_official(m)
            current_ids.append(official_id)
            row = self.conn.execute(
                "SELECT id FROM term WHERE official_id = %s AND seat_id = %s "
                "ORDER BY start_date DESC LIMIT 1",
                (official_id, seat_id)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE term SET is_current = true, end_date = %s WHERE id = %s",
                    (m["term_expires"], row["id"]))
            else:
                self.insert("term", {
                    "official_id": official_id,
                    "seat_id": seat_id,
                    # Appointment date isn't published; the page is a CURRENT
                    # roster, so stamp discovery date — honest, not a guess.
                    "start_date": date.today(),
                    "end_date": m["term_expires"],
                    "how_seated": "appointed",
                    "ballot_name": m["name"],
                    "is_current": True,
                })
        # Members no longer on the page are no longer current.
        self.conn.execute(
            """
            UPDATE term SET is_current = false
            WHERE is_current = true
              AND seat_id IN (SELECT id FROM seat WHERE governing_body_id = %s)
              AND official_id != ALL(%s)
            """,
            (body_id, current_ids))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True, help="Slug of jurisdictions/<slug>.json")
    args = parser.parse_args()
    job = CivicPlusBoardRosters(args.jurisdiction)
    if not job.src:
        print(f"⊘ {args.jurisdiction}: no data_sources.appointed_rosters configured — nothing to do")
        return 0
    result = job.run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

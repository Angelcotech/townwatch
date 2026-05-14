"""
Ingest current officials from a CivicEngage council/board roster page.

Generic to any jurisdiction whose officials roster sits at a CivicEngage
"telerik-reTable-2" 4-column table (Member, Position, Address, Phone).
Loads the jurisdiction config to find the URL, body name, and seat list.

Writes (idempotent on re-runs):
  - jurisdiction
  - governing_body (the body identified in config as the roster's body)
  - seats (one per seat defined in the body config)
  - officials + aliases (via identity resolution)
  - current terms (start_date = 2020-01-01 placeholder)

Run:
    python -m townwatch_etl.jobs.civicengage_officials --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from .. import identity
from ..ingest_base import IngestJob
from ..jurisdiction import load_config
from ..scrapers.civicengage_officials_table import scrape


class CivicEngageOfficials(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        self.source_name = self._derive_source_name()
        self.source_url = self.config["data_sources"]["officials_roster"]["url"]
        # Pick the body whose website_url matches the officials_roster URL,
        # falling back to first city_council body in config.
        self.body_cfg = self._pick_body()

    def _derive_source_name(self) -> str:
        url = self.config["data_sources"]["officials_roster"]["url"]
        from urllib.parse import urlparse
        return urlparse(url).netloc

    def _pick_body(self) -> dict[str, Any]:
        roster_url = self.config["data_sources"]["officials_roster"]["url"]
        for b in self.config.get("governing_bodies", []):
            if b.get("website_url") == roster_url:
                return b
        for b in self.config.get("governing_bodies", []):
            if b.get("body_type") == "city_council":
                return b
        raise ValueError(f"Could not pick a governing body for roster in {self.slug}")

    # -- main flow ---------------------------------------------------------

    def ingest(self) -> None:
        scraped = scrape(self.source_url)

        juris = self._ensure_jurisdiction(self.config["jurisdiction"])
        body = self._ensure_governing_body(juris, self.body_cfg)
        seats: dict[str, int] = {
            s["name"]: self._ensure_seat(body, s) for s in self.body_cfg.get("seats", [])
        }

        people = [o for o in scraped["officials"] if not o["is_vacant"]]
        people.sort(key=lambda o: o["raw_name"].split()[-1].lower())

        non_leadership_seat_names = [
            s["name"] for s in self.body_cfg.get("seats", []) if not s.get("is_leadership")
        ]

        for idx, person in enumerate(people[: len(non_leadership_seat_names)]):
            seat_id = seats[non_leadership_seat_names[idx]]
            official_id = self._ensure_official(person)
            self._ensure_current_term(official_id, seat_id, person)

    # -- entity ensure-or-create -------------------------------------------

    def _ensure_jurisdiction(self, cfg: dict[str, Any]) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (cfg["place_fips"],),
        ).fetchone()
        if row:
            return row["id"]
        new_id = self.insert("jurisdiction", {
            "fips_code":         cfg["place_fips"],
            "name":              cfg["name"],
            "display_name":      cfg["display_name"],
            "jurisdiction_type": cfg["type"],
            "state_fips":        cfg["state_fips"],
            "state_abbr":        cfg["state"],
            "county_fips":       cfg["county_fips"],
            "population":        cfg.get("population"),
        })
        assert new_id is not None
        return new_id

    def _ensure_governing_body(self, jurisdiction_id: int, cfg: dict[str, Any]) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM governing_body WHERE jurisdiction_id = %s AND name = %s",
            (jurisdiction_id, cfg["name"]),
        ).fetchone()
        if row:
            return row["id"]
        new_id = self.insert("governing_body", {
            "jurisdiction_id":   jurisdiction_id,
            "name":              cfg["name"],
            "body_type":         cfg["body_type"],
            "meeting_frequency": cfg.get("meeting_frequency"),
            "website_url":       cfg.get("website_url"),
        })
        assert new_id is not None
        return new_id

    def _ensure_seat(self, body_id: int, cfg: dict[str, Any]) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM seat WHERE governing_body_id = %s AND name = %s",
            (body_id, cfg["name"]),
        ).fetchone()
        if row:
            return row["id"]
        new_id = self.insert("seat", {
            "governing_body_id": body_id,
            "name":              cfg["name"],
            "seat_type":         cfg["seat_type"],
            "is_leadership":     cfg["is_leadership"],
            "term_length_years": cfg.get("term_length_years"),
        })
        assert new_id is not None
        return new_id

    def _ensure_official(self, person: dict[str, Any]) -> int:
        assert self.conn is not None
        name = person["raw_name"]
        existing = identity.find_by_alias(self.conn, name)
        if existing is not None:
            return existing
        parts = name.split()
        first = parts[0]
        last = parts[-1]
        middle = " ".join(parts[1:-1]) if len(parts) > 2 else None
        assert self.data_source_id is not None
        official_id = identity.create_official(
            self.conn,
            data_source_id=self.data_source_id,
            canonical_name=name,
            first_name=first,
            middle_name=middle,
            last_name=last,
            phone=person.get("phone"),
        )
        identity.add_alias(
            self.conn,
            official_id=official_id,
            alias_name=name,
            source_system=self.source_name,
            data_source_id=self.data_source_id,
        )
        return official_id

    def _ensure_current_term(self, official_id: int, seat_id: int, person: dict[str, Any]) -> None:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT id, is_current FROM term
            WHERE official_id = %s AND seat_id = %s
            ORDER BY start_date DESC LIMIT 1
            """,
            (official_id, seat_id),
        ).fetchone()
        if row and row["is_current"]:
            return
        if row and not row["is_current"]:
            self.conn.execute(
                "UPDATE term SET is_current = true, end_date = NULL WHERE id = %s",
                (row["id"],),
            )
            return
        self.insert("term", {
            "official_id":  official_id,
            "seat_id":      seat_id,
            "start_date":   date(2020, 1, 1),
            "how_seated":   "elected",
            "ballot_name":  person["raw_name"],
            "is_current":   True,
        })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True, help="Slug of jurisdictions/<slug>.json")
    args = parser.parse_args()
    result = CivicEngageOfficials(args.jurisdiction).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

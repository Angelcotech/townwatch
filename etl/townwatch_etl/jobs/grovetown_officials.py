"""
Grovetown current officials — ingest job.

Loads the jurisdiction config (jurisdictions/grovetown-ga.json), scrapes
the live City Council page, and writes:
  - jurisdiction (idempotent)
  - governing_body — City Council
  - 5 seats (Mayor + Council Member 1-4)
  - 4 officials (Mayor currently VACANT, no person created)
  - 4 official_alias entries (the scraped name → canonical)
  - 4 current terms

Run:
    python -m townwatch_etl.jobs.grovetown_officials
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .. import identity
from ..ingest_base import IngestJob
from ..scrapers.grovetown_officials import scrape


CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "jurisdictions" / "grovetown-ga.json"
)


class GrovetownOfficials(IngestJob):
    source_name = "cityofgrovetown.com"
    source_type = "scrape"
    source_url = "https://cityofgrovetown.com/198/City-Council"

    def ingest(self) -> None:
        config = json.loads(CONFIG_PATH.read_text())
        scraped = scrape()

        juris = self._ensure_jurisdiction(config["jurisdiction"])
        body_cfg = next(
            b for b in config["governing_bodies"] if b["body_type"] == "city_council"
        )
        body = self._ensure_governing_body(juris, body_cfg)
        seats: dict[str, int] = {
            s["name"]: self._ensure_seat(body, s) for s in body_cfg["seats"]
        }

        # Filter out the VACANT mayor row — no person to create
        people = [o for o in scraped["officials"] if not o["is_vacant"]]
        # Deterministic alphabetical surname order for at-large seat assignment
        people.sort(key=lambda o: o["raw_name"].split()[-1].lower())

        council_seat_names = [
            "Council Member 1", "Council Member 2",
            "Council Member 3", "Council Member 4",
        ]
        for idx, person in enumerate(people[:4]):
            seat_id = seats[council_seat_names[idx]]
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

        # Match against existing aliases first
        existing = identity.find_by_alias(self.conn, name)
        if existing is not None:
            return existing

        # New official — parse name parts naively
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
        """Create a current term if one doesn't already exist for this official+seat."""
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
            # Re-activate prior term if same person reclaimed the seat
            self.conn.execute(
                "UPDATE term SET is_current = true, end_date = NULL WHERE id = %s",
                (row["id"],),
            )
            return

        # We don't know the actual term start from this source. Use a placeholder
        # well in the past (2020-01-01) so historical votes can still be linked
        # back to the official. Backfill from election records in a later pass.
        self.insert("term", {
            "official_id":  official_id,
            "seat_id":      seat_id,
            "start_date":   date(2020, 1, 1),
            "how_seated":   "elected",
            "ballot_name":  person["raw_name"],
            "is_current":   True,
        })


def main() -> int:
    result = GrovetownOfficials().run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
CivicEngage meeting inventory ingest.

Enumerates every meeting from the AgendaCenter for each governing body
configured for this jurisdiction, and writes one row per meeting. This
is the meeting REGISTRY — agenda + minutes URLs only. Vote extraction
from the PDFs is a separate downstream job (extract_minutes).

Idempotent: re-runs don't duplicate. Conflict on (body, date, agenda_url)
updates the row in place.

Run:
    python -m townwatch_etl.jobs.meetings_inventory --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from ..ingest_base import IngestJob
from ..jurisdiction import load_config
from ..scrapers.civicengage_agendacenter import MeetingRecord, inventory


class CivicEngageMeetingsInventory(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        hints = self.config.get("platform_hints", {})
        self.base_url = hints.get("agenda_base_url")
        if not self.base_url:
            raise ValueError(f"{slug} config missing platform_hints.agenda_base_url")
        self.source_name = f"{self.base_url.replace('https://', '').replace('http://','')}/AgendaCenter"
        self.source_url = f"{self.base_url}/AgendaCenter"
        # Build {category_id: body_name} from config
        self.category_to_body = {
            b["civicengage"]["category_id"]: b["name"]
            for b in self.config.get("governing_bodies", [])
            if "civicengage" in b and "category_id" in b["civicengage"]
        }

    def ingest(self) -> None:
        assert self.conn is not None
        for cat_id, body_name in self.category_to_body.items():
            body_id = self._find_body(body_name)
            if body_id is None:
                print(f"  ⊘ body '{body_name}' not in DB — skipping (run civicengage_officials first)")
                continue
            print(f"  → scraping {body_name} (catID={cat_id})")
            count = 0
            for m in inventory(
                base_url=self.base_url,
                categories={cat_id: body_name},
            ):
                self._upsert_meeting(body_id, m)
                count += 1
            print(f"  ✓ {body_name}: processed {count} meetings")

    def _find_body(self, body_name: str) -> int | None:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM governing_body WHERE name = %s",
            (body_name,),
        ).fetchone()
        return row["id"] if row else None

    def _upsert_meeting(self, body_id: int, m: MeetingRecord) -> None:
        assert self.conn is not None and self.data_source_id is not None
        existing = self.conn.execute(
            """
            SELECT id FROM meeting
            WHERE governing_body_id = %s AND meeting_date = %s AND agenda_url = %s
            """,
            (body_id, m.meeting_date, m.agenda_url),
        ).fetchone()

        status = self._derive_status(m)
        if existing:
            self.conn.execute(
                """
                UPDATE meeting
                SET minutes_url = %s,
                    status = %s,
                    agenda_posted_at = COALESCE(%s, agenda_posted_at),
                    updated_at = now()
                WHERE id = %s
                """,
                (m.minutes_url, status, m.agenda_posted_at, existing["id"]),
            )
            self.rows_skipped += 1
            return

        self.insert("meeting", {
            "governing_body_id": body_id,
            "meeting_date":      m.meeting_date,
            "meeting_type":      m.meeting_type,
            "agenda_url":        m.agenda_url,
            "minutes_url":       m.minutes_url,
            "status":            status,
            "agenda_posted_at":  m.agenda_posted_at,
        })

    @staticmethod
    def _derive_status(m: MeetingRecord) -> str:
        today = datetime.now().date()
        if m.minutes_url:
            return "minutes_published"
        if m.meeting_date > today:
            return "agenda_published"
        return "completed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    args = parser.parse_args()
    result = CivicEngageMeetingsInventory(args.jurisdiction).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

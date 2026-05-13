"""
Grovetown City Council — meeting inventory ingest.

Enumerates every meeting available on the CivicEngage AgendaCenter
(2012 through current year) and writes one row to the meeting table
per meeting. This is the meeting REGISTRY — agenda + minutes URLs.
Vote extraction from the PDFs is a separate downstream job.

Idempotent: re-runs don't duplicate. Conflict on (governing_body_id,
meeting_date, agenda_url) updates the row in place.

Run:
    python -m townwatch_etl.jobs.grovetown_meetings_inventory
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from ..ingest_base import IngestJob
from ..scrapers.grovetown_agendacenter import (
    CATEGORIES,
    MeetingRecord,
    inventory,
)


# Which AgendaCenter category maps to which body in our DB
CATEGORY_TO_BODY = {
    2: "City Council",
    # 5: "Planning Commission",  # add when planning commission is in DB
    # 6: "Board of Zoning Appeals",
}


class GrovetownMeetingsInventory(IngestJob):
    source_name = "cityofgrovetown.com/AgendaCenter"
    source_type = "scrape"
    source_url = "https://cityofgrovetown.com/AgendaCenter"

    def ingest(self) -> None:
        assert self.conn is not None

        for cat_id, body_name in CATEGORY_TO_BODY.items():
            body_id = self._find_body(body_name)
            if body_id is None:
                print(f"  ⊘ body '{body_name}' not in DB — skipping (run grovetown_officials first)")
                continue

            print(f"  → scraping {body_name} (catID={cat_id})")
            count = 0
            for m in inventory(category_ids=[cat_id]):
                self._upsert_meeting(body_id, m)
                count += 1
            print(f"  ✓ {body_name}: processed {count} meetings")

    # -- helpers -----------------------------------------------------------

    def _find_body(self, body_name: str) -> int | None:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM governing_body WHERE name = %s",
            (body_name,),
        ).fetchone()
        return row["id"] if row else None

    def _upsert_meeting(self, body_id: int, m: MeetingRecord) -> None:
        """Idempotent insert keyed on (body, date, agenda_url)."""
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
            # Update minutes_url/status if minutes appeared since last scrape
            self.conn.execute(
                """
                UPDATE meeting
                SET minutes_url = %s, status = %s, updated_at = now()
                WHERE id = %s
                """,
                (m.minutes_url, status, existing["id"]),
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
        })

    @staticmethod
    def _derive_status(m: MeetingRecord) -> str:
        """Best-effort status derivation from what's available."""
        today = datetime.now().date()
        if m.minutes_url:
            return "minutes_published"
        if m.meeting_date > today:
            return "agenda_published"
        return "completed"


def main() -> int:
    result = GrovetownMeetingsInventory().run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

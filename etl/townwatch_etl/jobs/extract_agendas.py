"""
Meeting-agenda docket extraction.

Jurisdiction-agnostic. Operates on any meeting in the DB by meeting_id,
or in bulk across all meetings with agenda_url and no agenda_item rows
yet. The meeting's jurisdiction is derived from the meeting → governing_body
→ jurisdiction chain.

For one meeting (by meeting_id) or all eligible meetings:
  1. Download the agenda PDF
  2. Extract structured data via Claude (extractors.agendas)
  3. Write agenda_item rows
  4. Update meeting.meta with extraction metadata

Idempotent: skips meetings that already have agenda_items attached.
Uses ON CONFLICT (meeting_id, lower(title)) so reruns of partial work
don't duplicate.

Run a single meeting:
    python -m townwatch_etl.jobs.extract_agendas --meeting-id 174

Run every meeting missing items:
    python -m townwatch_etl.jobs.extract_agendas --all

Run only meetings for one jurisdiction:
    python -m townwatch_etl.jobs.extract_agendas --all --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..extractors.agendas import (
    AgendaExtraction,
    AgendaItemRecord,
    extract_from_document,
)
from ..ingest_base import IngestJob


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"


class AgendasExtract(IngestJob):
    source_type = "scrape"

    def __init__(
        self,
        meeting_id: int,
        *,
        prebuilt_extraction: AgendaExtraction | None = None,
    ) -> None:
        super().__init__()
        self.meeting_id = meeting_id
        self.source_name = "agendas_extract"
        self.source_url = None
        self.prebuilt_extraction = prebuilt_extraction

    # ---- main flow -------------------------------------------------------

    def ingest(self) -> None:
        assert self.conn is not None
        meeting = self._load_meeting(self.meeting_id)
        if meeting is None:
            raise RuntimeError(f"meeting {self.meeting_id} not found")

        if meeting["agenda_url"] is None:
            print(f"  ⊘ meeting {self.meeting_id} has no agenda_url — nothing to extract")
            return

        if self._already_extracted(self.meeting_id):
            print(f"  ⊘ meeting {self.meeting_id} already has agenda_items — skipping")
            return

        # Update provenance from the actual agenda URL for this meeting
        self.source_url = meeting["agenda_url"]
        self.source_name = f"{urlparse(meeting['agenda_url']).netloc}/AgendaCenter:claude_extract"
        self.conn.execute(
            "UPDATE data_source SET source_name = %s, source_url = %s WHERE id = %s",
            (self.source_name, self.source_url, self.data_source_id),
        )

        print(f"  → meeting {meeting['meeting_date']} ({meeting['meeting_type']}) | {meeting['agenda_url']}")

        if self.prebuilt_extraction is not None:
            extraction = self.prebuilt_extraction
            method = "prebuilt"
            print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")
        else:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True) as client:
                r = client.get(meeting["agenda_url"])
                r.raise_for_status()
                content_type = r.headers.get("content-type")

            # Suffix is just a hint for shell tools; the dispatcher sniffs magic bytes.
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
                f.write(r.content)
                doc_path = Path(f.name)

            print(f"     doc={len(r.content):,} bytes content-type={content_type!r} → extracting...")
            extraction, method = extract_from_document(doc_path, content_type)
            print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")

        # Persist the raw extraction so a re-run never needs another API call
        self._attach_raw_payload(meeting["agenda_url"], extraction)

        # Save the document-level AI summary on the meeting row
        if extraction.document_summary:
            self.conn.execute(
                "UPDATE meeting SET agenda_ai_summary = %s, updated_at = now() WHERE id = %s",
                (extraction.document_summary, self.meeting_id),
            )

        # Write agenda_items
        for item in extraction.agenda_items:
            self._upsert_item(meeting_id=self.meeting_id, item=item)

    # ---- DB helpers ------------------------------------------------------

    def _load_meeting(self, meeting_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT id, governing_body_id, meeting_date, meeting_type, agenda_url, status
            FROM meeting WHERE id = %s
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None

    def _already_extracted(self, meeting_id: int) -> bool:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT 1 FROM agenda_item WHERE meeting_id = %s LIMIT 1",
            (meeting_id,),
        ).fetchone()
        return row is not None

    def _attach_raw_payload(self, record_url: str, extraction: AgendaExtraction) -> None:
        """Update the data_source row with the full extraction JSON + record_url."""
        assert self.conn is not None and self.data_source_id is not None
        self.conn.execute(
            """
            UPDATE data_source
            SET record_url = %s,
                raw_payload = %s::jsonb
            WHERE id = %s
            """,
            (record_url, extraction.model_dump_json(), self.data_source_id),
        )

    def _upsert_item(self, *, meeting_id: int, item: AgendaItemRecord) -> int | None:
        """Insert; ON CONFLICT (meeting_id, lower(title)) updates in place."""
        assert self.conn is not None and self.data_source_id is not None

        locations = list(item.locations) if item.locations else None
        documents = list(item.documents_referenced) if item.documents_referenced else None

        row = self.conn.execute(
            """
            INSERT INTO agenda_item (
                meeting_id, item_number, title, description, item_type,
                applicant_name, recommended_action, hearing_status,
                locations, documents_referenced, source_page,
                data_source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (meeting_id, lower(title)) DO UPDATE SET
                item_number          = EXCLUDED.item_number,
                description          = EXCLUDED.description,
                item_type            = EXCLUDED.item_type,
                applicant_name       = EXCLUDED.applicant_name,
                recommended_action   = EXCLUDED.recommended_action,
                hearing_status       = EXCLUDED.hearing_status,
                locations            = EXCLUDED.locations,
                documents_referenced = EXCLUDED.documents_referenced,
                source_page          = EXCLUDED.source_page,
                updated_at           = now()
            RETURNING id
            """,
            (
                meeting_id,
                item.item_number,
                item.title.strip(),
                item.description,
                item.item_type,
                item.applicant_name,
                item.recommended_action,
                item.hearing_status,
                json.dumps(locations) if locations else None,
                json.dumps(documents) if documents else None,
                item.source_page,
                self.data_source_id,
            ),
        ).fetchone()
        if row is not None:
            self.rows_written += 1
            return row["id"]
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", type=int, help="Process this single meeting")
    parser.add_argument("--all", action="store_true", help="Process all eligible meetings")
    parser.add_argument("--jurisdiction", help="When used with --all, restrict to this slug")
    args = parser.parse_args()

    if not args.meeting_id and not args.all:
        parser.error("specify --meeting-id N or --all")

    if args.meeting_id:
        result = AgendasExtract(args.meeting_id).run()
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --all: every meeting with agenda_url but no agenda_items yet
    from ..db import connect
    sql = """
        SELECT m.id, m.meeting_date, j.display_name AS jurisdiction
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.agenda_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id)
    """
    params: list = []
    if args.jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        cfg = load_config(args.jurisdiction)
        sql += " AND j.fips_code = %s"
        params.append(jurisdiction_fips(cfg))
    sql += " ORDER BY m.meeting_date ASC"

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    print(f"Found {len(rows)} meeting(s) to extract")
    for r in rows:
        print(f"\n--- meeting {r['id']} ({r['meeting_date']}) {r['jurisdiction']} ---")
        try:
            AgendasExtract(r["id"]).run()
        except Exception as e:
            print(f"   ✗ failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

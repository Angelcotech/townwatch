"""
Extract one Campaign Contribution Disclosure Report (CCDR) and write
campaign_filing + campaign_contribution rows.

Designed to run when a records request response arrives containing one
or more CCDR PDFs (or DOCX/DOC equivalents). Each filing becomes one
campaign_filing row; each contribution becomes one campaign_contribution
row pointing back via campaign_filing_id. Idempotent on
(official_id, election_cycle_year, filing_type, filing_period_end).

**Status: code path complete, NOT yet wired into the response-classifier
loop and NOT yet validated against real CCDR documents.** Before bulk
ingestion:
  1. Run manually on at least 3 sample CCDRs (one each: PDF with text
     layer, scanned PDF, DOCX).
  2. Diff the extracted output against the source. Confirm:
       - declared_totals reconcile with sum of contributions
       - contributor names match source-document casing
       - dates are correctly parsed (esp. handwritten dates on scans)
       - filing_type classification is right
  3. If accuracy is high, wire into the records-request response loop so
     responses with CCDR attachments auto-feed this job.

Manual invocation (when ready):
    python -m townwatch_etl.jobs.extract_campaign_finance \\
        --official-id 1 --file /path/to/ccdr.pdf
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..audit import record_failure
from ..extractors.campaign_finance import (
    CampaignFilingExtraction,
    extract_from_document,
)
from ..ingest_base import IngestJob


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"


class CampaignFinanceExtract(IngestJob):
    source_type = "scrape"

    def __init__(
        self,
        official_id: int,
        *,
        file_path: Path | None = None,
        document_url: str | None = None,
        content_type: str | None = None,
        prebuilt_extraction: CampaignFilingExtraction | None = None,
    ) -> None:
        super().__init__()
        self.official_id = official_id
        self.file_path = file_path
        self.document_url = document_url
        self.content_type = content_type
        self.prebuilt_extraction = prebuilt_extraction
        self.source_name = "campaign_finance_extract"
        self.source_url = document_url

    def ingest(self) -> None:
        assert self.conn is not None

        local_path, ct = self._materialize()
        if local_path is None and self.prebuilt_extraction is None:
            raise ValueError(
                "Must provide one of: file_path, document_url, or prebuilt_extraction"
            )

        if self.prebuilt_extraction is not None:
            extraction = self.prebuilt_extraction
            method = "prebuilt"
        else:
            assert local_path is not None
            try:
                extraction, method = extract_from_document(local_path, ct)
            except Exception as e:
                record_failure(
                    self.conn,
                    job_name="extract_campaign_finance",
                    step="extract_from_document",
                    message=f"{type(e).__name__}: {e}",
                    exception=e,
                    context={"official_id": self.official_id, "url": self.document_url},
                )
                raise

        print(
            f"  → official {self.official_id}: method={method} "
            f"contributions={len(extraction.contributions)} "
            f"expenditures={len(extraction.expenditures)} "
            f"confidence={extraction.extraction_confidence}"
        )

        filing_id = self._upsert_filing(extraction, method)
        for contribution in extraction.contributions:
            self._upsert_contribution(filing_id, extraction, contribution)

    def _materialize(self) -> tuple[Path | None, str | None]:
        """Resolve to a local file regardless of input shape."""
        if self.file_path is not None:
            return self.file_path, self.content_type
        if self.document_url is None:
            return None, None
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True,
        ) as client:
            r = client.get(self.document_url)
            r.raise_for_status()
            ct = self.content_type or r.headers.get("content-type")
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(r.content)
            return Path(f.name), ct

    def _upsert_filing(self, ex: CampaignFilingExtraction, method: str) -> int:
        """Insert/update one campaign_filing row. Returns its id."""
        assert self.conn is not None and self.data_source_id is not None
        row = self.conn.execute(
            """
            INSERT INTO campaign_filing (
                official_id, election_cycle_year, filing_type,
                filing_period_start, filing_period_end, filing_date,
                source_document_url, source_format,
                declared_total_contributions, declared_total_expenditures,
                declared_cash_on_hand,
                raw_extraction, extraction_method, extraction_confidence,
                data_source_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
            )
            ON CONFLICT (official_id, election_cycle_year, filing_type, filing_period_end)
            DO UPDATE SET
                filing_period_start = EXCLUDED.filing_period_start,
                filing_date = EXCLUDED.filing_date,
                source_document_url = EXCLUDED.source_document_url,
                source_format = EXCLUDED.source_format,
                declared_total_contributions = EXCLUDED.declared_total_contributions,
                declared_total_expenditures = EXCLUDED.declared_total_expenditures,
                declared_cash_on_hand = EXCLUDED.declared_cash_on_hand,
                raw_extraction = EXCLUDED.raw_extraction,
                extraction_method = EXCLUDED.extraction_method,
                extraction_confidence = EXCLUDED.extraction_confidence,
                updated_at = now()
            RETURNING id
            """,
            (
                self.official_id,
                ex.period.election_cycle_year,
                ex.period.filing_type,
                ex.period.period_start,
                ex.period.period_end,
                ex.period.filing_date,
                self.document_url,
                method,
                ex.declared_totals.total_contributions,
                ex.declared_totals.total_expenditures,
                ex.declared_totals.cash_on_hand_end,
                ex.model_dump_json(),
                method,
                ex.extraction_confidence,
                self.data_source_id,
            ),
        ).fetchone()
        return row["id"]

    def _upsert_contribution(
        self, filing_id: int, ex: CampaignFilingExtraction, c: Any,
    ) -> None:
        assert self.conn is not None and self.data_source_id is not None
        cycle = ex.period.election_cycle_year
        self.conn.execute(
            """
            INSERT INTO campaign_contribution (
                official_id, election_cycle_year, contributor_name, contributor_type,
                contributor_employer, contributor_occupation, contributor_city,
                contributor_state, contributor_zip,
                amount, contribution_date, recipient_committee,
                campaign_filing_id, data_source_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                self.official_id, cycle,
                c.contributor_name, c.contributor_type,
                c.contributor_employer, c.contributor_occupation, c.contributor_city,
                c.contributor_state, c.contributor_zip,
                c.amount, c.contribution_date, ex.filer.committee_name,
                filing_id, self.data_source_id,
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--official-id", type=int, required=True)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Local path to the filing document")
    src.add_argument("--url", help="Remote URL to fetch the filing from")
    parser.add_argument("--content-type", help="Override content-type detection")
    args = parser.parse_args()

    job = CampaignFinanceExtract(
        official_id=args.official_id,
        file_path=Path(args.file) if args.file else None,
        document_url=args.url,
        content_type=args.content_type,
    )
    result = job.run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

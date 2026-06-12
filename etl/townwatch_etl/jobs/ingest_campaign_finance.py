"""
Ingest campaign-finance filings from Georgia's record-search system.

Per jurisdiction: resolve its Local Filing Office in
recordsearch.ethics.ga.gov, page the public document list, download new
documents, and ingest:

  * CCDRs → campaign_filing + itemized campaign_contribution rows
    (text recovered via the document_text store — Mistral OCR for scans —
    then one Haiku window; content-addressed extraction cache makes
    re-runs free). An extraction failure still records the FILING (the
    fact a candidate filed is the load-bearing fact) with
    data_status='repairing' for later repair.
  * exemption affidavits / PFDS / DOIs / notices → filing-exists rows
    (extraction_method='metadata_only'); an exemption affidavit is a real
    disclosure (declared activity under $2,500), not a gap.
  * election-outcome notices → skipped (elections domain's food).

THE GATE MARKER: this job's data_source row carries
source_type='campaign_finance' and the jurisdiction_id — written on EVERY
sweep, even one that finds zero documents — because
observe_campaign_finance_missing only fires where that marker exists (no
observer without ingestion). A jurisdiction with NO filing office in the
state system gets source_type='campaign_finance_unavailable' instead: its
filings are paper-only at the clerk's desk, so absence is unobservable and
the observer must stay silent.

Filers who don't resolve to an official (challengers, prior candidates) are
counted and skipped — ingesting non-officeholder candidates is elections-
module work.

Spending job (OCR + Haiku): the daily refresh gates it via SPENDING_STEPS,
weekly cadence (filings change slowly).

Run:
    python -m townwatch_etl.jobs.ingest_campaign_finance --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.ingest_campaign_finance --jurisdiction columbia-county-ga --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

import psycopg

from .. import extraction_cache, identity
from ..audit import record_failure
from ..campaign_finance import recordsearch as rs
from ..campaign_finance.extract import EXTRACTOR_VERSION, CCDRExtraction, extract_ccdr_text
from ..db import connect
from ..document_text import get_or_recover
from ..ingest_base import IngestJob
from ..jurisdiction import jurisdiction_fips, load_config


def _parse_date(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


class CampaignFinanceIngest(IngestJob):
    source_type = "campaign_finance"   # the observer's gate marker
    source_name = "recordsearch.ethics.ga.gov"
    source_url = "https://recordsearch.ethics.ga.gov"

    def __init__(self, slug: str, *, limit: int | None = None, dry_run: bool = False) -> None:
        super().__init__()
        self.slug = slug
        self.limit = limit
        self.dry_run = dry_run
        self.config = load_config(slug)
        self.counts: dict[str, int] = {"new": 0, "dup": 0, "unresolved": 0,
                                       "skipped_kind": 0, "failed": 0, "contributions": 0}
        # Resolve the filing office BEFORE open_run so an office-less
        # jurisdiction never writes the gate marker (its filings are
        # paper-only — absence there is unobservable, not real).
        display = self.config["jurisdiction"]["display_name"]
        cf_cfg = (self.config.get("data_sources") or {}).get("campaign_finance") or {}
        self.office = rs.resolve_filing_office(cf_cfg.get("filer_name") or display)
        if self.office is None:
            self.source_type = "campaign_finance_unavailable"

    # -- main ---------------------------------------------------------------

    def ingest(self) -> None:
        assert self.conn is not None
        jid = self._jurisdiction_id()
        # Stamp the jurisdiction on the marker row (open_run doesn't set it).
        self.conn.execute(
            "UPDATE data_source SET jurisdiction_id = %s WHERE id = %s",
            (jid, self.data_source_id))

        if self.office is None:
            print(f"  ⊘ no Local Filing Office in record-search for "
                  f"{self.config['jurisdiction']['display_name']} — filings are "
                  f"paper-only; observer stays ungated")
            return

        guid = self.office["guid"]
        docs = rs.list_documents(guid)
        already = self._already_ingested_urls()
        print(f"  filing office {guid} — {len(docs)} document(s), "
              f"{len(already)} already ingested")

        processed = 0
        for doc in docs:
            if self.limit and processed >= self.limit:
                print(f"  ⏹ --limit {self.limit} reached")
                break
            url = rs.document_url(doc["guid"])
            if url in already:
                self.counts["dup"] += 1
                continue
            kind = rs.classify_document(doc)
            if kind == "election_outcome":
                self.counts["skipped_kind"] += 1
                continue
            parsed = rs.parse_document_name(doc.get("documentName") or "")
            official_id = self._resolve_official(parsed["person"])
            if official_id is None:
                self.counts["unresolved"] += 1
                print(f"  ⊘ unresolved filer {parsed['person']!r} — {doc.get('documentName')!r}")
                continue
            processed += 1
            if self.dry_run:
                print(f"  would ingest [{kind}] {doc.get('documentName')!r}")
                continue
            try:
                self._ingest_document(jid, official_id, doc, kind, parsed, url)
                self.conn.commit()   # per-document durability
                self.counts["new"] += 1
            except psycopg.errors.UniqueViolation:
                self.conn.rollback()
                self.counts["dup"] += 1
            except Exception as e:
                self.conn.rollback()
                self.counts["failed"] += 1
                print(f"  ✗ {doc.get('documentName')!r}: {type(e).__name__}: {e}")
                with connect() as fconn:
                    record_failure(
                        fconn, job_name="ingest_campaign_finance", step="document",
                        message=f"{type(e).__name__}: {e}",
                        context={"slug": self.slug, "document": doc.get("documentName"),
                                 "guid": doc.get("guid"), "kind": kind},
                    )
        print(f"  done: {self.counts}")

    # -- per-document -------------------------------------------------------

    def _ingest_document(self, jid: int, official_id: int, doc: dict,
                         kind: str, parsed: dict, url: str) -> None:
        assert self.conn is not None
        received = _parse_date(doc.get("dateReceived"))
        cycle = parsed["cycle_year"] or (received.year if received else datetime.now().year)

        row: dict[str, Any] = {
            "official_id": official_id,
            "election_cycle_year": cycle,
            "filing_type": "other",
            "filing_date": received,
            "source_document_url": url,
            "source_format": "pdf_scan",
            "raw_extraction": json.dumps({"kind": kind, "document": doc}),
            "extraction_method": "metadata_only",
            "data_status": "clean",
        }

        extraction: CCDRExtraction | None = None
        if kind == "ccdr":
            row["filing_type"] = rs.infer_filing_type(
                doc.get("documentName") or "",
                datetime.fromisoformat(doc["dateReceived"]) if doc.get("dateReceived") else None)
            data = rs.download_document(doc["guid"])
            chash = extraction_cache.content_hash(data)
            cached = extraction_cache.get(self.conn, chash, "ccdr", EXTRACTOR_VERSION)
            try:
                if cached is not None:
                    extraction = CCDRExtraction.model_validate(cached["extraction"])
                    print(f"  ✓ cache hit {chash[:12]} — {doc.get('documentName')!r}")
                else:
                    pages, method = get_or_recover(self.conn, data, source_url=url)
                    extraction = extract_ccdr_text("\n".join(pages))
                    extraction_cache.put(
                        self.conn, chash, "ccdr", EXTRACTOR_VERSION,
                        extraction_json=extraction.model_dump_json(),
                        method=method, source_url=url)
                    print(f"  ✓ extracted [{method}] {doc.get('documentName')!r} — "
                          f"{len(extraction.contributions)} contribution(s)")
            except Exception as e:
                # The FILING is the load-bearing fact — record it even when
                # the contribution extraction fails, flagged for repair.
                row["data_status"] = "repairing"
                row["data_status_reason"] = f"ccdr extraction failed: {type(e).__name__}: {e}"
                row["data_status_at"] = datetime.now()
                print(f"  ⚠ extraction failed (filing still recorded): {e}")

        if extraction is not None:
            row["raw_extraction"] = json.dumps(
                {"kind": kind, "document": doc, "extraction": extraction.model_dump()})
            row["extraction_method"] = "haiku_text"
            row["extraction_confidence"] = extraction.extraction_confidence
            row["filing_period_start"] = extraction.filing_period_start
            row["filing_period_end"] = extraction.filing_period_end
            row["declared_total_contributions"] = extraction.total_contributions
            row["declared_total_expenditures"] = extraction.total_expenditures
            row["declared_cash_on_hand"] = extraction.cash_on_hand

        filing_id = self.insert("campaign_filing", row)

        if extraction is not None and filing_id is not None:
            for c in extraction.contributions:
                self.insert("campaign_contribution", {
                    "official_id": official_id,
                    "election_cycle_year": extraction.election_year or cycle,
                    "contributor_name": c.contributor_name,
                    "contributor_type": c.contributor_type,
                    "contributor_employer": c.employer,
                    "contributor_occupation": c.occupation,
                    "contributor_city": c.city,
                    "contributor_state": (c.state or "")[:2] or None,
                    "contributor_zip": c.zip,
                    "amount": c.amount,
                    "contribution_date": c.date,
                    "transaction_type": "contribution",
                    "campaign_filing_id": filing_id,
                })
                self.counts["contributions"] += 1

    # -- helpers ------------------------------------------------------------

    def _jurisdiction_id(self) -> int:
        assert self.conn is not None
        fips = jurisdiction_fips(self.config)
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
        if not row:
            raise ValueError(f"no jurisdiction row for {self.slug}")
        return row["id"]

    def _already_ingested_urls(self) -> set[str]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT source_document_url FROM campaign_filing "
            "WHERE source_document_url IS NOT NULL").fetchall()
        return {r["source_document_url"] for r in rows}

    def _resolve_official(self, person: str | None) -> int | None:
        assert self.conn is not None
        if not person:
            return None
        oid = identity.find_by_alias(self.conn, person)
        if oid is not None:
            return oid
        parts = person.split()
        if len(parts) > 2:   # "Eric William Blair" → "Eric Blair"
            oid = identity.find_by_alias(self.conn, f"{parts[0]} {parts[-1]}")
            if oid is not None:
                return oid
        # Clerk-typed names drop middle initials ("JAMES ALLEN" vs official
        # "James E. Allen"): match on first+last, but ONLY a unique match —
        # never last-name-only (that exact shortcut produced the Ceretta
        # Smith→Bradley Smith merge corruption fixed 2026-06-12).
        if len(parts) >= 2:
            rows = self.conn.execute(
                "SELECT id FROM official WHERE LOWER(first_name) = LOWER(%s) "
                "AND LOWER(last_name) = LOWER(%s)",
                (parts[0], parts[-1])).fetchall()
            if len(rows) == 1:
                return rows[0]["id"]
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest campaign-finance filings from GA record-search.")
    ap.add_argument("--jurisdiction", required=True, help="slug like 'grovetown-ga'")
    ap.add_argument("--limit", type=int, default=None, help="max new documents this run")
    ap.add_argument("--dry-run", action="store_true", help="list what would be ingested; no writes")
    args = ap.parse_args()
    result = CampaignFinanceIngest(args.jurisdiction, limit=args.limit, dry_run=args.dry_run).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

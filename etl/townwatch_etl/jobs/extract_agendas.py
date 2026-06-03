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
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import extraction_cache
from .. import funds
from ..extractors.agendas import (
    AgendaExtraction,
    AgendaItemRecord,
    EXTRACTOR_VERSION,
    extract_from_document,
)
from ..extractors.recovery import ExtractionReport
from ..audit import record_failure
from ..http_client import civic_get
from ..ingest_base import IngestJob
from ..resilience import is_transient_error

# Per-unit retry policy for transient infra hiccups (shared semantics with
# extract_minutes): 5 attempts, exponential backoff 4 → 8 → 16 → 32s.
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECS = 4


class AgendasExtract(IngestJob):
    source_type = "scrape"

    def __init__(
        self,
        meeting_id: int,
        *,
        prebuilt_extraction: AgendaExtraction | None = None,
        force: bool = False,
    ) -> None:
        super().__init__()
        self.meeting_id = meeting_id
        self.source_name = "agendas_extract"
        self.source_url = None
        self.prebuilt_extraction = prebuilt_extraction
        # force=True re-extracts a meeting that already has agenda_items,
        # replacing them. Used to backfill new schema fields (e.g. meeting
        # time/location) into already-extracted agendas.
        self.force = force
        # Per-document provenance, set in ingest() once read method + document
        # confidence are known, then stamped onto every agenda_item.
        self.read_method: str | None = None
        self.doc_confidence: str | None = None

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
            if not self.force:
                print(f"  ⊘ meeting {self.meeting_id} already has agenda_items — skipping")
                return
            # --force: replace the existing agenda_items. Cleared in this ingest's
            # transaction, so a failed re-extract rolls back and the old rows survive.
            ndel = self.conn.execute(
                "DELETE FROM agenda_item WHERE meeting_id = %s", (self.meeting_id,)
            ).rowcount
            print(f"  ↻ meeting {self.meeting_id}: --force, cleared {ndel} prior agenda_item(s)")

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
            self.read_method = "prebuilt"
            print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")
        else:
            r = civic_get(meeting["agenda_url"], timeout=120.0)
            r.raise_for_status()
            content_type = r.headers.get("content-type")
            chash = extraction_cache.content_hash(r.content)

            # Content-addressed cache: replay an already-extracted document for
            # $0 (no model call). Only a genuine miss costs money.
            cached = extraction_cache.get(self.conn, chash, "agenda", EXTRACTOR_VERSION)
            if cached is not None:
                extraction = AgendaExtraction.model_validate(cached["extraction"])
                method = "cached"
                # Record the TRUE underlying read method, not "cached".
                self.read_method = cached.get("method") or "cached"
                report = ExtractionReport(total_units=1, clean=1, recovered=0,
                                          anomalies=[], method="cached")
                self.report = report
                print(f"     ✓ cache hit {chash[:12]} — replaying {len(extraction.agenda_items)} "
                      f"items for $0 (orig method={cached['method']})")
            else:
                # Suffix is just a hint for shell tools; the dispatcher sniffs magic bytes.
                with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
                    f.write(r.content)
                    doc_path = Path(f.name)

                print(f"     doc={len(r.content):,} bytes content-type={content_type!r} → extracting...")
                extraction, method, report = extract_from_document(doc_path, content_type)
                self.report = report
                self.read_method = method
                print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")
                print(f"     recovery: {report.summary()}")
                for an in report.anomalies:
                    record_failure(
                        self.conn,
                        job_name="extract_agendas",
                        step=f"recovery_anomaly:{an.kind}",
                        governing_body_id=meeting.get("governing_body_id"),
                        meeting_id=self.meeting_id,
                        message=f"pages {an.start_page}-{an.end_page} unresolvable: {an.kind}",
                        context={
                            "anomaly_kind": an.kind,
                            "page_range": [an.start_page, an.end_page],
                            "attempts": an.attempts,
                            "agenda_url": meeting["agenda_url"],
                        },
                    )
                # Cache a usable result so the next run of this document is free.
                if extraction.agenda_items or method in ("text_layer", "ocr", "vision", "docx", "doc"):
                    from ..llm_client import current_usage
                    from ..pricing import cost_usd as _cost_usd
                    _u = current_usage()
                    extraction_cache.put(
                        self.conn, chash, "agenda", EXTRACTOR_VERSION,
                        extraction_json=extraction.model_dump_json(), method=method,
                        source_url=meeting["agenda_url"],
                        cost_usd=(_cost_usd(_u) if _u is not None else None),
                    )

        # Document-level confidence stamped onto every agenda_item from this doc.
        self.doc_confidence = extraction.meeting.extraction_confidence

        # Persist the raw extraction so a re-run never needs another API call
        self._attach_raw_payload(meeting["agenda_url"], extraction)

        # Save the document-level AI summary on the meeting row
        if extraction.document_summary:
            self.conn.execute(
                "UPDATE meeting SET agenda_ai_summary = %s, updated_at = now() WHERE id = %s",
                (extraction.document_summary, self.meeting_id),
            )

        # Scheduled time + location from the agenda header. This is the primary
        # source for UPCOMING meetings (they have an agenda, not minutes yet) —
        # what powers the "Next meeting" card's time/location. COALESCE so a
        # blank extraction never clobbers a value already set (e.g. by minutes).
        a_time = extraction.meeting.scheduled_start_at or None
        a_location = extraction.meeting.location or None
        if a_time or a_location:
            self.conn.execute(
                "UPDATE meeting SET meeting_time = COALESCE(%s::time, meeting_time), "
                "location = COALESCE(%s, location), updated_at = now() WHERE id = %s",
                (a_time, a_location, self.meeting_id),
            )

        # Flag placeholder stubs so the frontend can render "no document
        # published by city" instead of a dead download link. Set to the
        # boolean (true/false) on every run, not just when stub — that way
        # a re-extract after a real document gets uploaded clears the flag.
        self.conn.execute(
            "UPDATE meeting SET agenda_is_placeholder = %s WHERE id = %s",
            (method == "stub_skipped", self.meeting_id),
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
                extraction_method, extraction_confidence,
                data_source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
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
                extraction_method    = EXCLUDED.extraction_method,
                extraction_confidence = EXCLUDED.extraction_confidence,
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
                self.read_method,
                self.doc_confidence,
                self.data_source_id,
            ),
        ).fetchone()
        if row is not None:
            self.rows_written += 1
            return row["id"]
        return None


def _run_provenance_backfill(*, limit: int | None = None) -> int:
    """Concurrency guard: only one agenda provenance backfill at a time."""
    from ..run_lock import global_lock, BACKFILL_LOCK_AGENDA
    with global_lock(BACKFILL_LOCK_AGENDA) as got:
        if not got:
            print("⚠ another agenda provenance backfill is already running — exiting")
            return 1
        return _do_provenance_backfill(limit=limit)


def _do_provenance_backfill(*, limit: int | None = None) -> int:
    """
    Backfill extraction_method / extraction_confidence onto agenda_items for $0.

    Each extracted meeting stored its full agenda extraction in
    data_source.raw_payload. Replay each meeting's OWN stored extraction through
    the prebuilt+force path: no fetch, no model call, just a re-ingest that now
    writes the honesty columns. Targets only meetings with an agenda_item still
    missing extraction_confidence and a replayable payload; a meeting leaves the
    set once filled, so it's idempotent and safe to re-run.
    """
    from ..db import connect

    sql = """
        SELECT DISTINCT m.id
        FROM meeting m
        WHERE m.agenda_url IS NOT NULL
          AND EXISTS (SELECT 1 FROM agenda_item ai
                      WHERE ai.meeting_id = m.id AND ai.extraction_confidence IS NULL)
          AND EXISTS (SELECT 1 FROM data_source ds
                      WHERE ds.source_url = m.agenda_url AND ds.raw_payload IS NOT NULL)
        ORDER BY m.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with connect() as conn:
        ids = [r["id"] for r in conn.execute(sql).fetchall()]

    print(f"Agenda provenance backfill: {len(ids)} meeting(s) with replayable "
          f"stored extractions ($0, no model calls)")
    filled = skipped = failed = 0
    for mid in ids:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT ds.raw_payload
                FROM data_source ds
                WHERE ds.source_url = (SELECT agenda_url FROM meeting WHERE id = %s)
                  AND ds.raw_payload IS NOT NULL
                ORDER BY ds.ingested_at DESC NULLS LAST
                LIMIT 1
                """,
                (mid,),
            ).fetchone()
        if row is None or row["raw_payload"] is None:
            skipped += 1
            continue
        raw = row["raw_payload"]
        try:
            extraction = (
                AgendaExtraction.model_validate_json(raw)
                if isinstance(raw, str)
                else AgendaExtraction.model_validate(raw)
            )
        except Exception as e:
            print(f"  ⊘ meeting {mid}: stored agenda extraction not replayable "
                  f"({type(e).__name__}) — skipping")
            skipped += 1
            continue
        try:
            AgendasExtract(mid, prebuilt_extraction=extraction, force=True).run()
            filled += 1
            print(f"  ✓ meeting {mid}: agenda provenance backfilled")
        except Exception as e:
            failed += 1
            print(f"  ✗ meeting {mid}: {type(e).__name__}: {e}")

    print(f"\nDone. filled={filled} skipped={skipped} failed={failed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", type=int, help="Process this single meeting")
    parser.add_argument("--all", action="store_true", help="Process all eligible meetings")
    parser.add_argument("--jurisdiction", help="When used with --all, restrict to this slug")
    parser.add_argument("--limit", type=int, help="Max meetings to process (with --all) — for bounded backlog drains")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract meetings that ALREADY have agenda_items, replacing them "
                             "(e.g. to backfill new schema fields like meeting time/location).")
    parser.add_argument("--upcoming", action="store_true",
                        help="With --all, restrict to meetings on/after today — cheap targeted "
                             "backfill of what the 'Next meeting' card shows.")
    parser.add_argument("--backfill-provenance", action="store_true",
                        help="Backfill extraction_method / extraction_confidence onto agenda_items "
                             "extracted before those columns existed, by replaying each meeting's "
                             "OWN stored extraction ($0, no model calls). Targets only items still "
                             "missing extraction_confidence that have a replayable payload. "
                             "Idempotent and re-runnable.")
    args = parser.parse_args()

    if not args.meeting_id and not args.all and not args.backfill_provenance:
        parser.error("specify --meeting-id N, --all, or --backfill-provenance")

    if args.backfill_provenance:
        return _run_provenance_backfill(limit=args.limit)

    if args.meeting_id:
        result = AgendasExtract(args.meeting_id, force=args.force).run()
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --all: meetings with agenda_url and no agenda_items yet — or, with --force,
    # every meeting with agenda_url (re-extract + replace).
    from ..db import connect
    sql = """
        SELECT m.id, m.meeting_date, j.id AS jurisdiction_id, j.display_name AS jurisdiction
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.agenda_url IS NOT NULL
    """
    params: list = []
    if not args.force:
        sql += " AND NOT EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id)"
    if args.upcoming:
        sql += " AND m.meeting_date >= CURRENT_DATE"
    if args.jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        cfg = load_config(args.jurisdiction)
        sql += " AND j.fips_code = %s"
        params.append(jurisdiction_fips(cfg))
    sql += " ORDER BY m.meeting_date ASC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    from ..extraction_ledger import new_run_id, record_outcome
    run_id = new_run_id()

    print(f"Found {len(rows)} meeting(s) to extract")
    failed = 0
    clean = 0
    recovered = 0
    paused = 0
    paused_jids: set[int] = set()
    anomaly_kinds: dict[str, int] = {}
    for r in rows:
        jid = r["jurisdiction_id"]
        # Once a jurisdiction auto-pauses (out of funds), skip its remaining
        # meetings rather than re-attempting a reservation that will refuse.
        if jid in paused_jids:
            paused += 1
            continue
        print(f"\n--- meeting {r['id']} ({r['meeting_date']}) {r['jurisdiction']} ---")
        # Shared per-jurisdiction spend gate: reserve before, settle metered cost
        # after. Unfunded jurisdictions run ungated/unmetered as before.
        # New-agenda extraction is essential; --force/backfill re-extraction is
        # discretionary and yields to the operating reserve.
        with funds.gate(jid, run_id=run_id, meeting_id=r["id"], job_name="extract_agendas",
                        ref_kind="meeting", ref_id=str(r["id"]), description="extract_agendas",
                        essential=not args.force) as g:
            if g.paused:
                print("   ⏸ skipped (funds — paused or protecting the operating reserve)")
                paused += 1
                paused_jids.add(jid)
                continue
            last_err: Exception | None = None
            succeeded = False
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                # Retry transient infra hiccups (DNS-resolver overload, dropped
                # connections) with backoff so one blip doesn't cascade into mass
                # failure at scale; non-transient errors fall straight through.
                try:
                    job = AgendasExtract(r["id"], force=args.force)
                    job.run()
                    rep = getattr(job, "report", None)
                    outcome = "clean" if (rep is None or rep.fully_resolved) else "recovered"
                    if outcome == "clean":
                        clean += 1
                    else:
                        recovered += 1
                        for an in rep.anomalies:
                            anomaly_kinds[an.kind] = anomaly_kinds.get(an.kind, 0) + 1
                    record_outcome(run_id=run_id, job_name="extract_agendas", meeting_id=r["id"],
                                   jurisdiction_id=jid, outcome=outcome, report=rep)
                    succeeded = True
                    break
                except Exception as e:
                    last_err = e
                    if is_transient_error(e) and attempt < _MAX_ATTEMPTS:
                        backoff = _BACKOFF_BASE_SECS * (2 ** (attempt - 1))
                        print(f"   ⏳ meeting {r['id']} transient error (attempt {attempt}/{_MAX_ATTEMPTS}): "
                              f"{str(e)[:90]} — retrying in {backoff}s")
                        time.sleep(backoff)
                        continue
                    break
            if not succeeded:
                e = last_err
                failed += 1
                print(f"   ✗ failed: {e}")
                record_outcome(run_id=run_id, job_name="extract_agendas", meeting_id=r["id"],
                               jurisdiction_id=jid, outcome="failed", report=None)
                # Record it so a throttle-driven backlog is VISIBLE in the admin
                # queue instead of vanishing into stdout (the old behavior, which
                # hid hundreds of unextracted Columbia County agendas). Supersede
                # any prior unresolved row for this meeting so a nightly cron
                # can't accumulate duplicates for a persistently-failing URL.
                try:
                    with connect() as fconn:
                        fconn.execute(
                            "UPDATE pipeline_failure SET resolved_at = now(), "
                            "resolution_notes = 'superseded by later extract_agendas run' "
                            "WHERE job_name = 'extract_agendas' AND meeting_id = %s AND resolved_at IS NULL",
                            (r["id"],),
                        )
                        record_failure(
                            fconn,
                            job_name="extract_agendas",
                            step="extract_all",
                            meeting_id=r["id"],
                            message=f"{type(e).__name__}: {e}",
                            exception=e,
                        )
                except Exception as rec_err:
                    print(f"   ⚠ could not record failure: {rec_err}")

    # Run-level success rate — the rollout-confidence metric.
    total = len(rows)
    produced = clean + recovered
    rate = (produced / total * 100) if total else 0.0
    print(f"\n=== extract_agendas summary ({total} meeting(s)) ===")
    print(f"  clean:     {clean}")
    print(f"  recovered: {recovered}  (kept partial records; flagged pages: {anomaly_kinds or 'none'})")
    print(f"  failed:    {failed}")
    if paused:
        print(f"  paused:    {paused}  (skipped — jurisdiction out of funds)")
    print(f"  success rate (produced a record): {rate:.0f}%")
    if failed or recovered:
        print("  anomalies recorded to pipeline_failure for admin triage")
    return 0


if __name__ == "__main__":
    sys.exit(main())

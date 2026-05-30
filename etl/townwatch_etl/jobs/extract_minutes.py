"""
Meeting-minutes vote extraction.

Jurisdiction-agnostic. Operates on any meeting in the DB by meeting_id,
or in bulk across all meetings with minutes_url and no motions yet.
The meeting's jurisdiction is derived from the meeting → governing_body
→ jurisdiction chain.

For one meeting (by meeting_id) or all eligible meetings:
  1. Download the minutes PDF
  2. Extract structured data via Claude (extractors.minutes)
  3. Write motions + individual votes + recusals into the DB
  4. Update meeting metadata (status, attendance notes)

Idempotent: skips meetings that already have motions attached.

Run a single meeting:
    python -m townwatch_etl.jobs.extract_minutes --meeting-id 174

Run every meeting missing motions:
    python -m townwatch_etl.jobs.extract_minutes --all

Run only meetings for one jurisdiction:
    python -m townwatch_etl.jobs.extract_minutes --all --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from ..http_client import civic_get

from .. import identity
from ..audit import record_failure
from ..extractors.minutes import (
    AgendaItem,
    IndividualVote,
    MeetingExtraction,
    Recusal,
    extract_from_pdf,
)
from ..ingest_base import IngestJob


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"


class MinutesExtract(IngestJob):
    source_type = "scrape"

    def __init__(self, meeting_id: int, *, prebuilt_extraction: MeetingExtraction | None = None) -> None:
        super().__init__()
        self.meeting_id = meeting_id
        # source_name + source_url are set per-meeting in ingest() so the
        # provenance reflects the actual document being processed.
        self.source_name = "minutes_extract"
        self.source_url = None
        # When set, skip the PDF-fetch + Sonnet step and persist this result
        # directly. Used by the batched extraction job.
        self.prebuilt_extraction = prebuilt_extraction

    # ---- main flow -------------------------------------------------------

    def ingest(self) -> None:
        assert self.conn is not None
        meeting = self._load_meeting(self.meeting_id)
        if meeting is None:
            raise RuntimeError(f"meeting {self.meeting_id} not found")

        if meeting["minutes_url"] is None:
            print(f"  ⊘ meeting {self.meeting_id} has no minutes_url — nothing to extract")
            return

        if self._already_extracted(self.meeting_id):
            print(f"  ⊘ meeting {self.meeting_id} already has motions — skipping")
            return

        # Update provenance from the actual minutes URL for this meeting
        from urllib.parse import urlparse
        self.source_url = meeting["minutes_url"]
        self.source_name = f"{urlparse(meeting['minutes_url']).netloc}/AgendaCenter:claude_extract"
        self.conn.execute(
            "UPDATE data_source SET source_name = %s, source_url = %s WHERE id = %s",
            (self.source_name, self.source_url, self.data_source_id),
        )

        print(f"  → meeting {meeting['meeting_date']} ({meeting['meeting_type']}) | {meeting['minutes_url']}")

        if self.prebuilt_extraction is not None:
            extraction = self.prebuilt_extraction
            method = "prebuilt"
            print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")
        else:
            # Download PDF (shared throttled client — same civic hosts as
            # the scanner / agenda extractor, so it must respect one throttle).
            r = civic_get(meeting["minutes_url"], timeout=30.0)
            r.raise_for_status()

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(r.content)
                pdf_path = Path(f.name)

            # Extract via tiered pipeline (text layer → OCR → vision fallback)
            print(f"     pdf={len(r.content):,} bytes → extracting...")
            extraction, method, report = extract_from_pdf(pdf_path)
            self.report = report
            print(f"     method={method}  items={len(extraction.agenda_items)}  confidence={extraction.meeting.extraction_confidence}")
            print(f"     recovery: {report.summary()}")
            # Per-window residue the escalating ladder couldn't resolve →
            # classified anomalies for admin triage. The document still keeps
            # everything that DID resolve; only the irreducible pages are flagged.
            for an in report.anomalies:
                record_failure(
                    self.conn,
                    job_name="extract_minutes",
                    step=f"recovery_anomaly:{an.kind}",
                    governing_body_id=meeting["governing_body_id"],
                    meeting_id=self.meeting_id,
                    message=f"pages {an.start_page}-{an.end_page} unresolvable: {an.kind}",
                    context={
                        "anomaly_kind": an.kind,
                        "page_range": [an.start_page, an.end_page],
                        "attempts": an.attempts,
                        "minutes_url": meeting["minutes_url"],
                    },
                )

        # Persist the raw extraction so a re-run never needs another API call
        self._attach_raw_payload(meeting["minutes_url"], extraction)

        # Update meeting row with attendance/notes
        self._update_meeting(self.meeting_id, extraction)

        # Map source-side names → official_id (per the people in this meeting)
        gb_id = meeting["governing_body_id"]
        jurisdiction_id = self._jurisdiction_id_for_body(gb_id)
        name_to_official: dict[str, int | None] = {}
        seen_names: set[str] = set()
        for item in extraction.agenda_items:
            for v in item.individual_votes:
                seen_names.add(v.name)
            for rc in item.recusals:
                seen_names.add(rc.name)
        for name in seen_names:
            name_to_official[name] = self._resolve_official_for_meeting(
                name,
                jurisdiction_id=jurisdiction_id,
                meeting_date=meeting["meeting_date"],
            )

        # Write motions + votes
        for item in extraction.agenda_items:
            motion_id = self._insert_motion(meeting_id=self.meeting_id, item=item)
            if motion_id is None:
                continue
            self._insert_votes(
                motion_id=motion_id,
                item=item,
                name_to_official=name_to_official,
                meeting_date=meeting["meeting_date"],
            )

    # ---- DB helpers ------------------------------------------------------

    def _load_meeting(self, meeting_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT id, governing_body_id, meeting_date, meeting_type, minutes_url, status
            FROM meeting WHERE id = %s
            """,
            (meeting_id,),
        ).fetchone()
        return dict(row) if row else None

    def _already_extracted(self, meeting_id: int) -> bool:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT 1 FROM motion WHERE meeting_id = %s LIMIT 1",
            (meeting_id,),
        ).fetchone()
        return row is not None

    def _jurisdiction_id_for_body(self, gb_id: int) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT jurisdiction_id FROM governing_body WHERE id = %s",
            (gb_id,),
        ).fetchone()
        assert row is not None
        return row["jurisdiction_id"]

    def _update_meeting(self, meeting_id: int, extraction: MeetingExtraction) -> None:
        assert self.conn is not None
        parts = []
        if extraction.attendance.present:
            parts.append(f"present: {', '.join(extraction.attendance.present)}")
        if extraction.attendance.absent:
            parts.append(f"absent: {', '.join(extraction.attendance.absent)}")
        parts.append(f"extraction_confidence={extraction.meeting.extraction_confidence}")
        if extraction.extraction_notes:
            parts.append(f"notes: {extraction.extraction_notes}")
        attendance_summary = " | ".join(parts)

        staff_present = (
            list(extraction.attendance.staff_present)
            if extraction.attendance.staff_present else None
        )
        others_present = (
            list(extraction.attendance.others_present)
            if extraction.attendance.others_present else None
        )
        called_to_order = extraction.meeting.called_to_order_at
        adjourned = extraction.meeting.adjourned_at

        # Only overwrite the summary when extraction produced one — empty
        # default from old/batched payloads must NOT clobber a real summary.
        new_summary = extraction.document_summary or None

        self.conn.execute(
            """
            UPDATE meeting
            SET status              = %s,
                attendance_notes    = %s,
                called_to_order_at  = %s,
                adjourned_at        = %s,
                staff_present       = %s::jsonb,
                others_present      = %s::jsonb,
                minutes_ai_summary  = COALESCE(%s, minutes_ai_summary),
                updated_at          = now()
            WHERE id = %s
            """,
            (
                "minutes_published",
                attendance_summary,
                called_to_order,
                adjourned,
                json.dumps(staff_present) if staff_present is not None else None,
                json.dumps(others_present) if others_present is not None else None,
                new_summary,
                meeting_id,
            ),
        )

    def _attach_raw_payload(self, record_url: str, extraction: MeetingExtraction) -> None:
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

    def _insert_motion(self, *, meeting_id: int, item: AgendaItem) -> int | None:
        assert self.conn is not None
        title = item.title.strip()
        description_parts = []
        if item.motion_text_verbatim:
            description_parts.append(item.motion_text_verbatim.strip())
        description_parts.append(item.summary_plain_english.strip())
        description = "\n\n".join(description_parts)

        locations = list(item.locations) if item.locations else None

        return self.insert("motion", {
            "meeting_id":           meeting_id,
            "motion_number":        item.item_number,
            "title":                title,
            "description":          description,
            "motion_type":          item.motion_type,
            "agenda_item_order":    None,
            "outcome":              item.outcome,
            "vote_tally_yes":       item.vote_tally.yes,
            "vote_tally_no":        item.vote_tally.no,
            "vote_tally_abstain":   item.vote_tally.abstain,
            "vote_tally_absent":    item.vote_tally.absent,
            # New comprehensive fields
            "petitioner_name":      item.petitioner,
            "staff_recommender":    item.staff_recommender,
            "presenter":            item.presenter,
            "movant":               item.movant,
            "seconder":             item.seconder,
            "discussion_summary":   item.discussion_summary,
            "dollar_amount":        item.dollar_amount,
            "documents_referenced": json.dumps(item.documents_referenced) if item.documents_referenced else None,
            "locations":            json.dumps(locations) if locations else None,
        })

    def _insert_votes(
        self,
        *,
        motion_id: int,
        item: AgendaItem,
        name_to_official: dict[str, int | None],
        meeting_date: date,
    ) -> None:
        """Insert individual_votes; also surface recusals as conflict_recusal rows."""
        assert self.conn is not None

        # Build a per-motion lookup of recusal reasons keyed by source name
        recusal_notes: dict[str, str] = {}
        for rc in item.recusals:
            recusal_notes[rc.name] = (rc.reason or "recused (no reason given)")

        # Individual_votes: include all names; promote vote to conflict_recusal
        # if also present in recusals.
        written_official_ids: set[int] = set()
        for v in item.individual_votes:
            official_id = name_to_official.get(v.name)
            if official_id is None:
                # Skip silently — unresolved names are logged once per ingest run
                # via the resolver itself. We don't fabricate a vote.
                continue
            if official_id in written_official_ids:
                continue
            # Promote vote_value if recusal was declared
            vote_value = v.vote
            notes = v.notes
            if v.name in recusal_notes:
                vote_value = "conflict_recusal"
                notes = recusal_notes[v.name]
            term_id = self._term_id_for(
                official_id=official_id,
                meeting_date=meeting_date,
            )
            self.insert("vote", {
                "official_id":  official_id,
                "motion_id":    motion_id,
                "term_id":      term_id,
                "vote_value":   vote_value,
                "notes":        notes,
            })
            written_official_ids.add(official_id)

        # If a recusal name wasn't in individual_votes, still create the row.
        for rc in item.recusals:
            official_id = name_to_official.get(rc.name)
            if official_id is None or official_id in written_official_ids:
                continue
            term_id = self._term_id_for(
                official_id=official_id,
                meeting_date=meeting_date,
            )
            self.insert("vote", {
                "official_id":  official_id,
                "motion_id":    motion_id,
                "term_id":      term_id,
                "vote_value":   "conflict_recusal",
                "notes":        rc.reason or "recused (no reason given)",
            })
            written_official_ids.add(official_id)

    def _term_id_for(self, *, official_id: int, meeting_date: date) -> int | None:
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

    # ---- identity resolution ---------------------------------------------

    def _resolve_official_for_meeting(
        self,
        source_name: str,
        *,
        jurisdiction_id: int,
        meeting_date: date,
    ) -> int | None:
        """
        Resolve a source-side name (often title-prefixed) to a canonical official_id.

        Strategy:
          1. Strip elected-office title and try exact alias match
          2. Try fuzzy match against existing officials in this jurisdiction
          3. If found, record the source name as an alias for future runs
          4. If unresolved, log once and return None
        """
        assert self.conn is not None and self.data_source_id is not None
        stripped = identity.strip_title(source_name)

        # 1. Exact alias match (recorded from a prior run)
        oid = identity.find_by_alias(self.conn, source_name)
        if oid is not None:
            return oid

        # 2. Stripped (no title) — exact alias match
        oid = identity.find_by_alias(self.conn, stripped)
        if oid is not None:
            identity.add_alias(
                self.conn,
                official_id=oid,
                alias_name=source_name,
                source_system=self.source_name,
                data_source_id=self.data_source_id,
            )
            return oid

        # 3. Last-name match within officials active at the meeting date
        matches = identity.find_by_last_name_active_at(
            self.conn,
            stripped,
            jurisdiction_id=jurisdiction_id,
            as_of_date=meeting_date,
        )
        if len(matches) == 1:
            oid, canonical = matches[0]
            identity.add_alias(
                self.conn,
                official_id=oid,
                alias_name=source_name,
                source_system=self.source_name,
                data_source_id=self.data_source_id,
            )
            print(f"     ↳ resolved '{source_name}' → official#{oid} ({canonical}) via last-name match")
            return oid
        if len(matches) > 1:
            # Try first-initial disambiguation: take the FIRST token after stripping,
            # use its first letter as an initial against each candidate's first_name.
            tokens = stripped.split()
            if len(tokens) >= 2:
                first_initial = tokens[0][0].lower()
                narrowed = []
                for oid, canonical in matches:
                    row = self.conn.execute(
                        "SELECT first_name FROM official WHERE id = %s",
                        (oid,),
                    ).fetchone()
                    if row and row["first_name"] and row["first_name"][0].lower() == first_initial:
                        narrowed.append((oid, canonical))
                if len(narrowed) == 1:
                    oid, canonical = narrowed[0]
                    identity.add_alias(
                        self.conn,
                        official_id=oid,
                        alias_name=source_name,
                        source_system=self.source_name,
                        data_source_id=self.data_source_id,
                    )
                    print(f"     ↳ resolved '{source_name}' → official#{oid} ({canonical}) via last+initial")
                    return oid
            print(f"     ✗ ambiguous last-name match for '{source_name}': {matches}")
            return None

        # 4. Fuzzy match within this jurisdiction (last resort)
        candidates = identity.find_candidates(
            self.conn,
            stripped,
            jurisdiction_id=jurisdiction_id,
        )
        if candidates and candidates[0].similarity >= 0.75:
            best = candidates[0]
            identity.add_alias(
                self.conn,
                official_id=best.official_id,
                alias_name=source_name,
                source_system=self.source_name,
                data_source_id=self.data_source_id,
            )
            print(f"     ↳ resolved '{source_name}' → official#{best.official_id} ({best.canonical_name}) sim={best.similarity:.2f}")
            return best.official_id

        # Unresolved
        cand_preview = [(c.canonical_name, round(c.similarity, 2)) for c in candidates[:3]]
        print(f"     ✗ unresolved '{source_name}' (stripped: '{stripped}') — best candidates: {cand_preview}")
        return None


def _process_meeting(r, run_id) -> tuple[str, list]:
    """Extract one meeting end-to-end. Thread-safe — each call opens its own DB
    connections (via run() / record_outcome / connect), so it parallelizes
    cleanly under a thread pool. Records the ledger outcome and, on failure, a
    pipeline_failure (superseding any prior unresolved row for the meeting).
    Returns (outcome, anomaly_kinds) for run-level tallying."""
    from ..db import connect
    from ..extraction_ledger import record_outcome
    mid = r["id"]
    jid = r["jurisdiction_id"]
    print(f"--- meeting {mid} ({r['meeting_date']}) {r['jurisdiction']} ---")
    try:
        job = MinutesExtract(mid)
        job.run()
        rep = getattr(job, "report", None)
        outcome = "clean" if (rep is None or rep.fully_resolved) else "recovered"
        record_outcome(run_id=run_id, job_name="extract_minutes", meeting_id=mid,
                       jurisdiction_id=jid, outcome=outcome, report=rep)
        kinds = [an.kind for an in rep.anomalies] if (rep and rep.anomalies) else []
        return outcome, kinds
    except Exception as e:
        print(f"   ✗ meeting {mid} failed: {e}")
        record_outcome(run_id=run_id, job_name="extract_minutes", meeting_id=mid,
                       jurisdiction_id=jid, outcome="failed", report=None)
        try:
            with connect() as fconn:
                fconn.execute(
                    "UPDATE pipeline_failure SET resolved_at = now(), "
                    "resolution_notes = 'superseded by later extract_minutes run' "
                    "WHERE job_name = 'extract_minutes' AND meeting_id = %s AND resolved_at IS NULL",
                    (mid,),
                )
                record_failure(fconn, job_name="extract_minutes", step="extract_all",
                               meeting_id=mid, message=f"{type(e).__name__}: {e}", exception=e)
        except Exception as rec_err:
            print(f"   ⚠ could not record failure for {mid}: {rec_err}")
        return "failed", []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", type=int, help="Process this single meeting")
    parser.add_argument("--all", action="store_true", help="Process all eligible meetings")
    parser.add_argument("--jurisdiction", help="When used with --all, restrict to this slug")
    parser.add_argument("--limit", type=int, help="Max meetings to process (with --all) — for bounded backlog drains")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel extraction workers (extraction is network/think-bound, so "
                             "threads parallelize the slow Anthropic calls; 3-4 recommended)")
    args = parser.parse_args()

    if not args.meeting_id and not args.all:
        parser.error("specify --meeting-id N or --all")

    if args.meeting_id:
        result = MinutesExtract(args.meeting_id).run()
        print(json.dumps(result, indent=2, default=str))
        return 0

    # --all: pick every meeting with minutes_url but no motions yet
    from ..db import connect
    sql = """
        SELECT m.id, m.meeting_date, j.id AS jurisdiction_id, j.display_name AS jurisdiction
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.minutes_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
    """
    params: list = []
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

    from ..extraction_ledger import new_run_id
    run_id = new_run_id()

    workers = max(1, args.workers)
    print(f"Found {len(rows)} meeting(s) to extract  (workers={workers})")
    failed = 0
    clean = 0          # fully resolved, no anomalies
    recovered = 0      # produced a record but some pages were flagged
    anomaly_kinds: dict[str, int] = {}

    def _tally(result: tuple[str, list]) -> None:
        nonlocal failed, clean, recovered
        outcome, kinds = result
        if outcome == "clean":
            clean += 1
        elif outcome == "recovered":
            recovered += 1
            for k in kinds:
                anomaly_kinds[k] = anomaly_kinds.get(k, 0) + 1
        else:
            failed += 1

    if workers > 1:
        # Extraction is network/think-bound (minutes per Anthropic call), so
        # threads parallelize the waiting cleanly — each job opens its own DB
        # connection, the per-host HTTP throttle is thread-safe, and the
        # Anthropic SDK self-throttles on rate limits. Wall-clock drops ~Nx.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_process_meeting, r, run_id) for r in rows]
            for i, fut in enumerate(as_completed(futs), 1):
                _tally(fut.result())
                print(f"   [{i}/{len(rows)}] complete — clean={clean} recovered={recovered} failed={failed}")
    else:
        for r in rows:
            _tally(_process_meeting(r, run_id))

    # Run-level success rate — the rollout-confidence metric. "Produced a
    # record" counts clean + recovered (partial) extractions; only total
    # failures and per-page anomalies need a human.
    total = len(rows)
    produced = clean + recovered
    rate = (produced / total * 100) if total else 0.0
    print(f"\n=== extract_minutes summary ({total} meeting(s)) ===")
    print(f"  clean:     {clean}")
    print(f"  recovered: {recovered}  (kept partial records; flagged pages: {anomaly_kinds or 'none'})")
    print(f"  failed:    {failed}")
    print(f"  success rate (produced a record): {rate:.0f}%")
    if failed or recovered:
        print("  anomalies recorded to pipeline_failure for admin triage")
    return 0


if __name__ == "__main__":
    sys.exit(main())

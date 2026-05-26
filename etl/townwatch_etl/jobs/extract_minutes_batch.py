"""
Batched minutes extraction via the Anthropic Batches API.

When onboarding a new jurisdiction (or backfilling a long historical
window), the regular extract_minutes job makes one synchronous Sonnet
vision call per meeting — sequential, full-price. Batches API submits N
requests at once at 50% price, processes them asynchronously, returns
results within 24h (usually <1h for small batches).

Three modes:
  --submit       Submit a new batch of pending meetings, print batch_id, exit.
  --poll <id>    Print current status of an in-flight batch.
  --resume <id>  Pull results for a completed batch and write to DB.

Workflow:
  1. python -m townwatch_etl.jobs.extract_minutes_batch --submit
       → prints "batch_id=msgbatch_..."
  2. (wait minutes-to-hours)
  3. python -m townwatch_etl.jobs.extract_minutes_batch --poll msgbatch_...
       → "in_progress (40/200 done)" or "ended"
  4. python -m townwatch_etl.jobs.extract_minutes_batch --resume msgbatch_...
       → fetches results, writes to DB, runs QA + repair via the orchestrator

For small batches (<10 meetings) the regular synchronous job is fine.
This job is the right choice when you have dozens-to-hundreds of meetings
to extract and don't need them in realtime.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request

import anthropic

from ..config import ANTHROPIC_API_KEY
from ..db import connect
from ..extractors.minutes import (
    VISION_INSTRUCTIONS,
    VISION_MODEL,
    MeetingExtraction,
)


def _list_pending_meetings(jurisdiction: str | None) -> list[dict]:
    # Exclude meetings whose minutes_url has already failed a prior fetch
    # (404, oversized, etc.) — see audit.mark_url_unreachable. Without this,
    # every batch would re-download dead URLs and silently drop them again.
    sql = """
        SELECT m.id, m.minutes_url, m.meeting_date, j.display_name AS jurisdiction
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.minutes_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
          AND NOT (COALESCE(m.meta, '{}'::jsonb) ? 'minutes_url_status')
    """
    params: list = []
    if jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        cfg = load_config(jurisdiction)
        sql += " AND j.fips_code = %s"
        params.append(jurisdiction_fips(cfg))
    sql += " ORDER BY m.meeting_date ASC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _download_pdf(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TownWatch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# Anthropic Batches API caps each request payload at 256MB. We chunk
# under this with safety margin so multi-MB scanned-PDF minutes don't
# blow the limit. Mirrors the auto-chunking in extract_agendas_batch.
BATCH_SIZE_BYTES_TARGET = 200 * 1024 * 1024

# Anthropic vision rejects PDFs above 32MB outright. Trim to 30MB for
# encoding/request-overhead margin. Mirrors extract_agendas_batch.
MAX_PDF_BYTES = 30 * 1024 * 1024


def _build_request(meeting_id: int, pdf_bytes: bytes) -> dict:
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return {
        "custom_id": f"meeting_{meeting_id}",
        "params": {
            "model": VISION_MODEL,
            "max_tokens": 16384,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": VISION_INSTRUCTIONS},
                    ],
                },
            ],
        },
    }


def _submit_chunk(client: anthropic.Anthropic, chunk: list[dict], idx: int, total: int) -> str | None:
    print(f"\n[chunk {idx}/{total}] submitting batch of {len(chunk)} request(s)...")
    try:
        batch = client.messages.batches.create(requests=chunk)
    except Exception as e:
        print(f"  ✗ chunk {idx} submit failed: {e}")
        return None
    print(f"  ✓ batch_id={batch.id}  status={batch.processing_status}")
    return batch.id


def cmd_submit(jurisdiction: str | None) -> int:
    meetings = _list_pending_meetings(jurisdiction)
    if not meetings:
        print("No pending meetings to extract.")
        return 0

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print(f"Building batches for {len(meetings)} meeting(s) (chunking under {BATCH_SIZE_BYTES_TARGET // (1024*1024)}MB/chunk)...")

    chunks: list[list[dict]] = [[]]
    chunk_sizes: list[int] = [0]
    from ..audit import mark_url_unreachable
    with connect() as conn:
        for m in meetings:
            try:
                pdf_bytes = _download_pdf(m["minutes_url"])
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    mark_url_unreachable(
                        conn,
                        meeting_id=m["id"],
                        kind="minutes",
                        reason=f"http_{e.code}",
                        detail=str(e),
                    )
                    print(f"  ✗ meeting {m['id']}: minutes {e.code}, marked unreachable")
                else:
                    print(f"  ✗ meeting {m['id']}: PDF download HTTP {e.code} ({e}); skipping")
                continue
            except Exception as e:
                print(f"  ✗ meeting {m['id']}: PDF download failed ({e}); skipping")
                continue

            # Pre-flight size check — Anthropic vision rejects >32MB PDFs.
            # Mark these so future batches skip them, and so an observer
            # can route them to a different remediation (records-request,
            # split-and-extract, etc.) instead of silently failing.
            if len(pdf_bytes) > MAX_PDF_BYTES:
                mark_url_unreachable(
                    conn,
                    meeting_id=m["id"],
                    kind="minutes",
                    reason="oversized_for_vision",
                    detail=f"{len(pdf_bytes) // (1024*1024)}MB exceeds {MAX_PDF_BYTES // (1024*1024)}MB cap",
                )
                print(
                    f"  ✗ meeting {m['id']}: minutes PDF {len(pdf_bytes) // (1024*1024)}MB "
                    f"> {MAX_PDF_BYTES // (1024*1024)}MB, marked oversized"
                )
                continue

            req = _build_request(m["id"], pdf_bytes)
            req_size = len(req["params"]["messages"][0]["content"][0]["source"]["data"]) + 512
            if chunk_sizes[-1] + req_size > BATCH_SIZE_BYTES_TARGET and chunks[-1]:
                chunks.append([])
                chunk_sizes.append(0)
            chunks[-1].append(req)
            chunk_sizes[-1] += req_size

    chunks = [c for c in chunks if c]
    if not chunks:
        print("No usable meetings in batch (all PDF downloads failed).")
        return 1

    total_requests = sum(len(c) for c in chunks)
    print(f"\nReady to submit {len(chunks)} chunk(s), {total_requests} total request(s).")
    print("Sizes: " + ", ".join(f"{len(c)} reqs / {chunk_sizes[i] // (1024*1024)}MB" for i, c in enumerate(chunks)))

    batch_ids: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        bid = _submit_chunk(client, chunk, i, len(chunks))
        if bid:
            batch_ids.append(bid)

    if not batch_ids:
        print("\nAll chunks failed to submit.")
        return 1

    print(f"\n=== submitted {len(batch_ids)} batch(es) ===")
    for bid in batch_ids:
        print(f"  {bid}")
    print("\nPoll all:")
    for bid in batch_ids:
        print(f"  python -m townwatch_etl.jobs.extract_minutes_batch --poll {bid}")
    print("\nResume + ingest results once status='ended':")
    for bid in batch_ids:
        print(f"  python -m townwatch_etl.jobs.extract_minutes_batch --resume {bid}")
    return 0


def cmd_poll(batch_id: str) -> int:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    batch = client.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    print(f"batch_id={batch.id}")
    print(f"status={batch.processing_status}")
    print(f"  processing: {counts.processing}")
    print(f"  succeeded:  {counts.succeeded}")
    print(f"  errored:    {counts.errored}")
    print(f"  canceled:   {counts.canceled}")
    print(f"  expired:    {counts.expired}")
    if batch.processing_status == "ended":
        print("\nReady to resume:")
        print(f"  python -m townwatch_etl.jobs.extract_minutes_batch --resume {batch_id}")
    return 0


def cmd_resume(batch_id: str) -> int:
    """
    Fetch results for a completed batch and write to DB.

    Each successful result is parsed and persisted via the existing
    MinutesExtract apply-path, so we benefit from all identity resolution
    and bulk-insert logic without re-implementing it.
    """
    from .extract_minutes import MinutesExtract

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        print(f"Batch not ready (status={batch.processing_status}). Try --poll.")
        return 1

    succeeded = 0
    errored = 0
    print(f"Streaming results for batch {batch_id}...")

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        meeting_id = int(custom_id.split("_", 1)[1])

        if result.result.type != "succeeded":
            errored += 1
            print(f"  meeting {meeting_id}: ERROR ({result.result.type})")
            continue

        # Parse the response into MeetingExtraction and apply via existing job
        message = result.result.message
        try:
            extraction = _parse_message(message)
        except Exception as e:
            errored += 1
            print(f"  meeting {meeting_id}: parse failed: {e}")
            continue

        try:
            MinutesExtract(meeting_id, prebuilt_extraction=extraction).run()
            succeeded += 1
            print(f"  meeting {meeting_id}: ✓ extracted")
        except Exception as e:
            errored += 1
            print(f"  meeting {meeting_id}: apply failed: {e}")

    print(f"\nBatch resume complete: {succeeded} succeeded, {errored} errored.")
    return 0 if errored == 0 else 1


def _parse_message(message) -> MeetingExtraction:
    import re
    text = ""
    for block in message.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1:
        raise ValueError("no JSON object in batch response")
    return MeetingExtraction.model_validate(json.loads(text[first : last + 1]))


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--submit", action="store_true", help="Submit a new batch of pending meetings")
    group.add_argument("--poll", metavar="BATCH_ID", help="Check status of an in-flight batch")
    group.add_argument("--resume", metavar="BATCH_ID", help="Fetch results and ingest into DB")
    parser.add_argument("--jurisdiction", help="With --submit, restrict to this slug")
    args = parser.parse_args()

    if args.submit:
        return cmd_submit(args.jurisdiction)
    if args.poll:
        return cmd_poll(args.poll)
    if args.resume:
        return cmd_resume(args.resume)
    return 1


if __name__ == "__main__":
    sys.exit(main())

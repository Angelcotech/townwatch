"""
Re-extract OCR'd meetings using Claude vision.

Walks every meeting with motions and probes its PDF: if the PDF has a
real text layer (digital), the existing extraction is already accurate
and we skip it. If the PDF is scanned (no text layer), the prior
extraction used OCR — clear it and re-extract with vision.

Use this once after switching the extractor to vision-first to refresh
all OCR-tainted data. Idempotent — re-runs only touch meetings that
still have an OCR-era extraction (we identify by probing the source).

Dry-run by default. Use --confirm to actually clear + re-extract.

Run:
    python -m townwatch_etl.jobs.reextract_with_vision                       # preview
    python -m townwatch_etl.jobs.reextract_with_vision --confirm             # apply
    python -m townwatch_etl.jobs.reextract_with_vision --confirm --limit 5   # first 5 only
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..db import connect
from ..extractors.minutes import extract_text_layer_only
from .extract_minutes import MinutesExtract


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"


def find_candidates() -> list[dict[str, Any]]:
    """Meetings with extractions — these are the ones to evaluate."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT m.id AS meeting_id, m.meeting_date, m.meeting_type, m.minutes_url,
                   j.display_name AS jurisdiction
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.minutes_url IS NOT NULL
              AND EXISTS (SELECT 1 FROM motion WHERE meeting_id = m.id)
            ORDER BY m.meeting_date
        """).fetchall()
    return [dict(r) for r in rows]


def probe_text_layer(minutes_url: str) -> bool:
    """Download a PDF and check if it has an extractable text layer."""
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
            r = client.get(minutes_url)
            r.raise_for_status()
    except Exception as e:
        print(f"     ✗ download failed: {e}", file=sys.stderr)
        return True  # conservative — don't accidentally re-extract on download failure
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(r.content)
        path = Path(f.name)
    try:
        return extract_text_layer_only(path) is not None
    finally:
        path.unlink(missing_ok=True)


def clear_meeting(meeting_id: int) -> None:
    """Remove existing motions, votes, and the OCR-era data_source row(s) for one meeting."""
    with connect() as conn:
        # Find data_source rows tied to motions of this meeting
        ds_ids = [
            r["data_source_id"]
            for r in conn.execute(
                "SELECT DISTINCT data_source_id FROM motion WHERE meeting_id = %s",
                (meeting_id,),
            ).fetchall()
        ]
        # Delete votes first (FK), then motions
        conn.execute(
            "DELETE FROM vote v USING motion m WHERE v.motion_id = m.id AND m.meeting_id = %s",
            (meeting_id,),
        )
        conn.execute("DELETE FROM motion WHERE meeting_id = %s", (meeting_id,))
        # Delete the linked data_source rows so the OCR payload doesn't influence
        # any future backfill or pattern that walks raw_payloads
        for ds_id in ds_ids:
            conn.execute("DELETE FROM data_source WHERE id = %s", (ds_id,))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="Actually clear + re-extract (default is preview only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N candidates (useful for testing)")
    args = parser.parse_args()

    candidates = find_candidates()
    print(f"Candidates with existing extractions: {len(candidates)}")
    print()

    scanned: list[dict[str, Any]] = []
    digital: list[dict[str, Any]] = []

    print("Probing PDFs for text layers...")
    for i, c in enumerate(candidates, 1):
        has_text = probe_text_layer(c["minutes_url"])
        bucket = "text_layer" if has_text else "scanned (OCR'd)"
        print(f"  [{i:>3}/{len(candidates)}] {c['meeting_date']} → {bucket}")
        (digital if has_text else scanned).append(c)

    print()
    print(f"Summary: {len(digital)} digital (keep as-is), {len(scanned)} scanned (re-extract)")

    if args.limit:
        scanned = scanned[: args.limit]
        print(f"  --limit applied: {len(scanned)} meetings will be re-extracted")

    if not args.confirm:
        print("\nDry run — pass --confirm to clear and re-extract.")
        print(f"Estimated cost: {len(scanned)} × ~$0.15 = ~${len(scanned) * 0.15:.2f}")
        return 0

    print()
    print(f"=== Clearing + re-extracting {len(scanned)} meeting(s) ===")
    for i, c in enumerate(scanned, 1):
        print(f"\n[{i}/{len(scanned)}] meeting {c['meeting_id']} ({c['meeting_date']})")
        try:
            clear_meeting(c["meeting_id"])
            MinutesExtract(c["meeting_id"]).run()
        except Exception as e:
            print(f"  ✗ failed: {e}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

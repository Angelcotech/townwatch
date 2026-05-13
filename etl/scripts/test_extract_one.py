"""
Single-PDF extraction smoke test — no DB writes.

Downloads one Grovetown minutes PDF and runs it through the Claude
extractor. Prints the parsed result. Used to verify quality and tune
the prompt before committing to a full archive sweep.

Run:
    cd etl
    python scripts/test_extract_one.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx

# Path setup so we can import the package without installing it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from townwatch_etl.config import ANTHROPIC_MODEL  # noqa: E402
from townwatch_etl.extractors.minutes import extract_from_pdf  # noqa: E402


# A recent regular meeting with minutes available
TEST_MINUTES_URL = "https://cityofgrovetown.com/AgendaCenter/ViewFile/Minutes/_04132026-304"
TEST_LABEL = "Grovetown City Council — April 13, 2026"


def main() -> int:
    print(f"Model: {ANTHROPIC_MODEL}")
    print(f"Source: {TEST_LABEL}")
    print(f"URL: {TEST_MINUTES_URL}")
    print()

    print("Downloading PDF...")
    with httpx.Client(headers={"User-Agent": "TownWatch-ETL/0.1"}, timeout=30.0) as c:
        r = c.get(TEST_MINUTES_URL)
        r.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(r.content)
        pdf_path = Path(f.name)

    print(f"  {len(r.content):,} bytes → {pdf_path}")
    print()

    print("Sending to Claude...")
    result = extract_from_pdf(pdf_path)
    print()

    # Pretty-print summary
    print("=" * 70)
    print(f"Meeting:    {result.meeting.date} | {result.meeting.body_name} | {result.meeting.meeting_type}")
    print(f"Confidence: {result.meeting.extraction_confidence}")
    print()
    print(f"Attendance — present ({len(result.attendance.present)}):")
    for name in result.attendance.present:
        print(f"  · {name}")
    if result.attendance.absent:
        print(f"Attendance — absent ({len(result.attendance.absent)}):")
        for name in result.attendance.absent:
            print(f"  · {name}")
    print()

    print(f"Agenda items ({len(result.agenda_items)}):")
    for i, item in enumerate(result.agenda_items, 1):
        print(f"\n  [{i}] {item.item_number or '-'}  {item.title}")
        print(f"      Type: {item.motion_type}  →  {item.outcome}")
        print(f"      Tally: yes={item.vote_tally.yes} no={item.vote_tally.no} abstain={item.vote_tally.abstain} absent={item.vote_tally.absent}")
        print(f"      Summary: {item.summary_plain_english}")
        if item.movant or item.seconder:
            print(f"      Moved by {item.movant or '?'} / seconded by {item.seconder or '?'}")
        for v in item.individual_votes:
            note = f" — {v.notes}" if v.notes else ""
            print(f"        - {v.name}: {v.vote}{note}")
        if item.recusals:
            print(f"      ⚠ Recusals:")
            for r in item.recusals:
                print(f"        - {r.name}: {r.reason or '(no reason given)'}")
        if item.public_comment:
            print(f"      Public comment:")
            for pc in item.public_comment:
                print(f"        - {pc.speaker} ({pc.stance}): {pc.summary}")

    if result.extraction_notes:
        print()
        print(f"Extraction notes: {result.extraction_notes}")

    print()
    print("=" * 70)
    print("Raw JSON:")
    print(json.dumps(result.model_dump(), indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())

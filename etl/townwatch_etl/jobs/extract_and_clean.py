"""
Self-cleaning extraction pipeline.

Wraps extract_minutes → run_patterns → repair_quality_issues so that any
new meeting extracted goes through the full quality protocol before its
motions are considered published:

  1. Extract minutes (existing job)
  2. Run QA detectors → motions with issues auto-quarantine
  3. Run repair engine → handlers fix what they can
  4. Re-run QA + quarantine bridge → cleared motions return to 'clean'
  5. Anything still 'disputed' is genuinely unrepairable; ops triage from
     the dashboard's "Data Quality" panel

This is the protocol David specified: extraction never "finishes" until
the platform has audited its own output. Citizens never see motions that
haven't passed the gate.

Run:
    python -m townwatch_etl.jobs.extract_and_clean --meeting-id 53
    python -m townwatch_etl.jobs.extract_and_clean --all --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.extract_and_clean --all --skip-extract  # just rerun QA+repair on existing data
"""

from __future__ import annotations

import argparse
import json
import sys

from .extract_minutes import MinutesExtract
from .repair_quality_issues import RepairQualityIssues
from .run_patterns import RunPatterns


def _list_pending_meetings(jurisdiction: str | None) -> list[dict]:
    from ..db import connect
    sql = """
        SELECT m.id, m.meeting_date, j.display_name AS jurisdiction
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE m.minutes_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
    """
    params: list = []
    if jurisdiction:
        from ..jurisdiction import load_config
        cfg = load_config(jurisdiction)
        sql += " AND j.fips_code = %s"
        params.append(cfg["jurisdiction"]["place_fips"])
    sql += " ORDER BY m.meeting_date ASC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def run_pipeline(
    *,
    meeting_ids: list[int],
    skip_extract: bool = False,
    skip_repair: bool = False,
) -> dict:
    summary = {"extracted": 0, "extract_failed": 0, "repair": None}

    # === Phase 1: extract ===
    if not skip_extract and meeting_ids:
        print(f"\n=== Phase 1: extracting {len(meeting_ids)} meeting(s) ===")
        for mid in meeting_ids:
            print(f"\n  → meeting {mid}")
            try:
                MinutesExtract(mid).run()
                summary["extracted"] += 1
            except Exception as e:
                print(f"     ✗ failed: {e}")
                summary["extract_failed"] += 1
    elif skip_extract:
        print("\n=== Phase 1: skipped (--skip-extract) ===")

    # === Phase 2: detect quality issues ===
    print("\n=== Phase 2: running QA detectors ===")
    RunPatterns().run()

    if skip_repair:
        print("\n=== Phase 3: skipped (--skip-repair) ===")
        return summary

    # === Phase 3: repair + auto-rerun-QA-and-bridge ===
    print("\n=== Phase 3: running repair engine ===")
    job = RepairQualityIssues(rerun_qa=True)
    job.run()
    summary["repair"] = job.repair_summary

    print("\n=== Pipeline complete ===")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", type=int, help="Process this single meeting")
    parser.add_argument("--all", action="store_true", help="Process every meeting that needs extraction")
    parser.add_argument("--jurisdiction", help="With --all, restrict to this slug")
    parser.add_argument("--skip-extract", action="store_true", help="Skip Phase 1 (re-run QA+repair on existing data)")
    parser.add_argument("--skip-repair", action="store_true", help="Skip Phase 3 (QA only, no repair)")
    args = parser.parse_args()

    if args.skip_extract:
        meeting_ids: list[int] = []
    elif args.meeting_id:
        meeting_ids = [args.meeting_id]
    elif args.all:
        pending = _list_pending_meetings(args.jurisdiction)
        meeting_ids = [r["id"] for r in pending]
        print(f"Found {len(meeting_ids)} meeting(s) to extract")
    else:
        parser.error("specify --meeting-id N, --all, or --skip-extract")

    summary = run_pipeline(
        meeting_ids=meeting_ids,
        skip_extract=args.skip_extract,
        skip_repair=args.skip_repair,
    )
    print()
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

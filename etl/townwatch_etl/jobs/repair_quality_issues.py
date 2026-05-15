"""
Run the repair engine against every quarantined motion.

Lifecycle on each invocation:
  1. Repair engine walks every motion with data_status='disputed'
  2. Dispatches each to the first matching handler
  3. Records the attempt in motion.meta.repair_log
  4. After all repairs are attempted, re-runs run_patterns
  5. The quarantine bridge inside run_patterns then clears motions whose
     QA finding has disappeared, leaving only the still-broken ones disputed

Run:
    python -m townwatch_etl.jobs.repair_quality_issues
    python -m townwatch_etl.jobs.repair_quality_issues --dry-run
    python -m townwatch_etl.jobs.repair_quality_issues --limit 5
    python -m townwatch_etl.jobs.repair_quality_issues --no-rerun-qa
"""

from __future__ import annotations

import argparse
import json
import sys

from ..ingest_base import IngestJob
from ..repair.engine import run_repairs, run_official_repairs
from .run_patterns import RunPatterns


class RepairQualityIssues(IngestJob):
    source_name = "townwatch_repair_engine"
    source_type = "manual"
    source_url = "internal://repair"

    def __init__(self, *, dry_run: bool = False, limit: int | None = None,
                 motion_ids: list[int] | None = None, rerun_qa: bool = True,
                 workers: int = 4):
        super().__init__()
        self.dry_run = dry_run
        self.limit = limit
        self.motion_ids = motion_ids
        self.rerun_qa = rerun_qa
        self.workers = workers
        self.repair_summary: dict | None = None
        self.official_summary: dict | None = None

    def ingest(self) -> None:
        assert self.conn is not None
        print("— running motion repair engine —")
        self.repair_summary = run_repairs(
            self.conn,
            data_source_id=self.data_source_id,
            limit=self.limit,
            motion_ids=self.motion_ids,
            dry_run=self.dry_run,
            workers=self.workers,
        )
        print(f"\nmotion repair summary: {self.repair_summary}")

        if not self.motion_ids:
            print("\n— running official repair engine —")
            self.official_summary = run_official_repairs(self.conn, dry_run=self.dry_run)
            print(f"\nofficial repair summary: {self.official_summary}")

        self.rows_written = (
            self.repair_summary["repaired"]
            + (self.official_summary["repaired"] if self.official_summary else 0)
        )
        # NOTE: rerun_qa runs OUTSIDE this method (see main() below). Running
        # it here causes a row-lock deadlock: the outer ingest holds DELETE
        # locks on official/alias/finding rows, then the inner RunPatterns
        # connection blocks trying to DELETE the same finding rows.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Do not write changes; only show what would be done.")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of motions repaired this run.")
    parser.add_argument("--motion-ids", type=str, default=None, help="Comma-separated motion IDs to target.")
    parser.add_argument("--no-rerun-qa", action="store_true", help="Skip the post-repair QA re-run.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel handler workers (default 4; set to 1 for sequential).")
    args = parser.parse_args()

    motion_ids = None
    if args.motion_ids:
        motion_ids = [int(x) for x in args.motion_ids.split(",") if x.strip()]

    job = RepairQualityIssues(
        dry_run=args.dry_run,
        limit=args.limit,
        motion_ids=motion_ids,
        rerun_qa=not args.no_rerun_qa,
        workers=args.workers,
    )
    result = job.run()

    # Run the QA reconcile AFTER the outer transaction has committed.
    # If we ran it inside ingest(), the nested QA write would deadlock
    # against the outer pending DELETEs on official/alias/finding rows.
    if not args.no_rerun_qa and not args.dry_run:
        print("\n— re-running QA detectors to reconcile data_status —")
        RunPatterns().run()

    print()
    print(json.dumps({
        "job": result,
        "motion_repair": job.repair_summary,
        "official_repair": job.official_summary,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Daily orchestrator — runs the full TownWatch audit pipeline.

Sequence (per jurisdiction, then jurisdiction-agnostic):

  1. meetings_inventory          — scrape AgendaCenter for new meetings
                                   + agenda_posted_at timestamps
  2. scan_document_availability — HEAD-check every agenda/minutes URL;
                                   flag the dead ones (404, stub PDF)
                                   so the frontend renders "no document
                                   published" instead of a dead link
  3. extract_agendas --all       — fetch + extract any meeting that has
                                   an agenda_url and no agenda_items
  4. extract_minutes --all       — fetch + extract any meeting that has
                                   a minutes_url and no motions
  5. refresh_council_roster      — vision-extract elected bodies' web
                                   pages to fill term-expires + email
                                   (no-op when source publishes nothing)

  6. refresh_findings            — recompute every observer; new gaps
                                   auto-trigger prepare_records_request
  7. backfill_summaries          — Haiku one-shot summaries for any new
                                   meeting whose AI summary is missing

Each step is its own subprocess so a failure in one doesn't tear down
the next; failures land in pipeline_failure. Designed for a daily cron
(Railway scheduled job or equivalent) — see townwatch/railway.toml.

Run manually:
    python -m townwatch_etl.jobs.daily_refresh
    python -m townwatch_etl.jobs.daily_refresh --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.daily_refresh --skip extract_minutes,backfill_summaries
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..db import connect
from ..jurisdiction import list_slugs


PER_JURISDICTION_STEPS = [
    "meetings_inventory",
    "scan_document_availability",  # before extract, so dead-URL meetings get skipped by extractors
    "extract_agendas",
    "extract_minutes",
    "refresh_council_roster",
]

JURISDICTION_AGNOSTIC_STEPS = [
    "refresh_findings",
    "backfill_summaries",
]


def _run_step(module: str, args: list[str]) -> tuple[bool, str]:
    """Run one job as a subprocess. Returns (ok, output).
    Subprocess isolation means one job's bad state doesn't poison later jobs."""
    cmd = [sys.executable, "-m", f"townwatch_etl.jobs.{module}", *args]
    print(f"  → {module} {' '.join(args)}")
    started = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
        )
        elapsed = time.time() - started
        ok = result.returncode == 0
        tail = (result.stdout or "").strip().splitlines()[-3:]
        msg = "\n".join(tail) if tail else "(no output)"
        flag = "✓" if ok else "✗"
        print(f"     {flag} ({elapsed:.1f}s) {msg.splitlines()[-1] if msg else ''}")
        if not ok:
            err_tail = "\n".join((result.stderr or "").strip().splitlines()[-5:])
            print(f"     stderr: {err_tail}")
        return ok, result.stdout + ("\n" + result.stderr if result.stderr else "")
    except subprocess.TimeoutExpired:
        print(f"     ✗ TIMEOUT after 3600s")
        return False, "timeout"


def _record_run_summary(summary: dict) -> None:
    """Record the daily run summary to pipeline_failure if anything broke,
    so the admin queue surfaces problems even without paging through logs."""
    failed_steps = [s for s in summary["steps"] if not s["ok"]]
    if not failed_steps:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_failure (job_name, step, message, context)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                "daily_refresh",
                "summary",
                f"{len(failed_steps)} of {len(summary['steps'])} daily-refresh steps failed",
                json.dumps(summary),
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="Restrict per-jurisdiction steps to this slug")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated module names to skip (e.g. 'extract_minutes,backfill_summaries')",
    )
    args = parser.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    slugs = [args.jurisdiction] if args.jurisdiction else list_slugs()
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"=== daily_refresh started at {started_at} ===")
    print(f"Jurisdictions: {slugs or '(none configured)'}")
    if skip:
        print(f"Skipping: {sorted(skip)}")

    summary: dict = {"started_at": started_at, "jurisdictions": slugs, "steps": []}

    # Per-jurisdiction steps
    for slug in slugs:
        print(f"\n--- {slug} ---")
        for module in PER_JURISDICTION_STEPS:
            if module in skip:
                print(f"  ⊘ {module} (skipped)")
                continue
            step_args = _per_jurisdiction_args(module, slug)
            ok, output = _run_step(module, step_args)
            summary["steps"].append(
                {"module": module, "slug": slug, "ok": ok}
            )

    # Jurisdiction-agnostic steps
    print(f"\n--- global ---")
    for module in JURISDICTION_AGNOSTIC_STEPS:
        if module in skip:
            print(f"  ⊘ {module} (skipped)")
            continue
        ok, output = _run_step(module, [])
        summary["steps"].append({"module": module, "slug": None, "ok": ok})

    finished_at = datetime.now(timezone.utc).isoformat()
    summary["finished_at"] = finished_at
    ok_count = sum(1 for s in summary["steps"] if s["ok"])
    total = len(summary["steps"])
    print(f"\n=== daily_refresh finished at {finished_at}  ({ok_count}/{total} steps ok) ===")

    _record_run_summary(summary)
    return 0 if ok_count == total else 1


def _per_jurisdiction_args(module: str, slug: str) -> list[str]:
    """Each per-jurisdiction job takes a different argument shape. Map them here."""
    if module == "meetings_inventory":
        return ["--jurisdiction", slug]
    if module == "scan_document_availability":
        return ["--jurisdiction", slug, "--only-changed"]
    if module == "extract_agendas":
        return ["--all", "--jurisdiction", slug]
    if module == "extract_minutes":
        return ["--all", "--jurisdiction", slug]
    if module == "refresh_council_roster":
        return ["--slug", slug]
    raise ValueError(f"Unknown per-jurisdiction module: {module}")


if __name__ == "__main__":
    sys.exit(main())

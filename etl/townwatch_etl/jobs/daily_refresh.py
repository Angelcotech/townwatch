"""
Daily orchestrator — runs the full TownWatch audit pipeline.

Sequence (per jurisdiction, then jurisdiction-agnostic):

  1. meetings_inventory          — scrape AgendaCenter for new meetings
                                   + agenda_posted_at timestamps
  2. scan_document_availability — HEAD-check every agenda/minutes URL;
                                   flag the dead ones (404, stub PDF)
                                   so the frontend renders "no document
                                   published" instead of a dead link
  3. estimate_onboarding         — turn the scanned inventory into a $
                                   funding goal (cheap, no spend): real
                                   doc counts x empirical per-doc rate
  4. extract_agendas --all       — fetch + extract any meeting that has
                                   an agenda_url and no agenda_items
  5. extract_minutes --all       — fetch + extract any meeting that has
                                   a minutes_url and no motions
  6. refresh_council_roster      — vision-extract elected bodies' web
                                   pages to fill term-expires + email
                                   (no-op when source publishes nothing)

  7. refresh_findings            — recompute every observer; new gaps
                                   auto-trigger prepare_records_request
  8. backfill_summaries          — Haiku one-shot summaries for any new
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
import contextlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..db import connect
from ..jurisdiction import list_slugs
from ..run_lock import jurisdiction_lock, is_running


PER_JURISDICTION_STEPS = [
    "meetings_inventory",
    "scan_document_availability",  # before extract, so dead-URL meetings get skipped by extractors
    "estimate_onboarding",         # cheap: turn the scanned inventory into a $ funding goal
    "extract_agendas",
    "extract_minutes",
    "refresh_council_roster",
]

# Steps that spend money (model / OCR). Skipped for a jurisdiction whose fund is
# paused (out of money) or suspended (manual hold) — but the cheap mapping steps
# (meetings_inventory, scan_document_availability) always run, so we keep the
# catalog fresh for everyone and activate paid extraction on demand. A deposit
# clears a pause, so the next run resumes these automatically. Includes the
# global backfill_summaries, which is gated when a run is scoped to one
# jurisdiction (so a deposit for town A never buys summaries for town B).
SPENDING_STEPS = {"extract_agendas", "extract_minutes", "refresh_council_roster",
                  "backfill_summaries"}

JURISDICTION_AGNOSTIC_STEPS = [
    "refresh_findings",
    "sync_capabilities",   # cheap: persist build-phase state + emit phase_indexed milestones
    "monitor_clerk_contact",  # cheap: keep the clerk email (requests/digests target) deliverable
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


def _jid_for(slug: str) -> int | None:
    """Resolve a jurisdiction slug to its DB id (via fips), or None if absent."""
    from ..jurisdiction import load_config, jurisdiction_fips
    try:
        fips = jurisdiction_fips(load_config(slug))
        with connect() as conn:
            row = conn.execute("SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
            return row["id"] if row else None
    except Exception:
        return None


def _fund_state(slug: str) -> tuple[bool, str]:
    """Return (spending_allowed, human_status) for a jurisdiction's fund.
    No fund → ungated. Resolves the slug to its jurisdiction id via fips."""
    from .. import funds
    try:
        jid = _jid_for(slug)
        if jid is None:
            return True, "no-jurisdiction-row"
        with connect() as conn:
            fund = funds.get_fund(conn, jid)
            if fund is None:
                return True, "ungated"
            avail = funds.available(conn, jid)
            return fund["status"] == "active", f"{fund['status']} (avail ${avail:.2f})"
    except Exception as e:  # never let a fund lookup error block the catalog steps
        print(f"  ⚠ fund lookup failed for {slug}: {e} — treating as ungated")
        return True, "lookup-error"


def _run_jurisdiction(slug: str, skip: set[str], ignore_funds: bool, summary: dict) -> None:
    """Run one jurisdiction's pipeline steps in order. Cheap mapping steps always
    run; spending steps are skipped when the fund is paused/suspended."""
    can_spend, fund_status = (True, "ignored") if ignore_funds else _fund_state(slug)
    print(f"\n--- {slug} ---  [fund: {fund_status}]")
    for module in PER_JURISDICTION_STEPS:
        if module in skip:
            print(f"  ⊘ {module} (skipped)")
            continue
        # Gate the expensive steps on funds; keep cheap mapping always-on so the
        # catalog stays fresh and paid extraction activates on demand.
        if module in SPENDING_STEPS and not can_spend:
            print(f"  ⏸ {module} (skipped — fund {fund_status}; deposit to resume)")
            summary["steps"].append(
                {"module": module, "slug": slug, "ok": True, "skipped": "funds"})
            continue
        step_args = _per_jurisdiction_args(module, slug)
        ok, output = _run_step(module, step_args)
        summary["steps"].append({"module": module, "slug": slug, "ok": ok})


def trigger(slug: str) -> bool:
    """Kick off a pipeline run for one jurisdiction unless one is already running
    for it. Returns True if a (detached) run was started, False if one was
    already in progress (the deposit/cron will be picked up by that run).

    This is what makes a fund deposit feel instant: fund a town → an
    onboarding / current-state audit + resume of pending work starts right away,
    while a run already in flight is left alone."""
    jid = _jid_for(slug)
    if jid is not None and is_running(jid):
        print(f"trigger: {slug} already has a run in progress — leaving it")
        return False
    subprocess.Popen(
        [sys.executable, "-m", "townwatch_etl.jobs.daily_refresh", "--jurisdiction", slug],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    print(f"trigger: started a pipeline run for {slug}")
    return True


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
    parser.add_argument(
        "--ignore-funds", action="store_true",
        help="Run spending steps regardless of fund status (admin override).",
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

    # Per-jurisdiction steps. One advisory lock per jurisdiction so a
    # deposit-triggered run never doubles up with the cron (or another trigger):
    # whoever holds the lock processes it; everyone else skips it this pass.
    for slug in slugs:
        jid = _jid_for(slug)
        lock_cm = jurisdiction_lock(jid) if jid is not None else contextlib.nullcontext(True)
        with lock_cm as got:
            if not got:
                print(f"\n--- {slug} ---  ⊘ already being processed by another run — skipping")
                summary["steps"].append(
                    {"module": "(lock)", "slug": slug, "ok": True, "skipped": "already-running"})
                continue
            _run_jurisdiction(slug, skip, args.ignore_funds, summary)

    # Jurisdiction-agnostic steps. On a SCOPED run (--jurisdiction, e.g. a
    # deposit-triggered one) these are scoped to that jurisdiction and the
    # spending one is fund-gated, so a single jurisdiction's run never does
    # global paid work for everyone. On a full run (no --jurisdiction) they run
    # globally as before.
    scoped = args.jurisdiction
    print("\n--- global ---" if not scoped else f"\n--- global (scoped to {scoped}) ---")
    can_spend_scoped = True
    if scoped and not args.ignore_funds:
        can_spend_scoped, _ = _fund_state(scoped)
    for module in JURISDICTION_AGNOSTIC_STEPS:
        if module in skip:
            print(f"  ⊘ {module} (skipped)")
            continue
        if scoped and module in SPENDING_STEPS and not can_spend_scoped:
            print(f"  ⏸ {module} (skipped — fund paused; deposit to resume)")
            summary["steps"].append(
                {"module": module, "slug": scoped, "ok": True, "skipped": "funds"})
            continue
        gargs = ["--jurisdiction", scoped] if scoped else []
        ok, output = _run_step(module, gargs)
        summary["steps"].append({"module": module, "slug": scoped, "ok": ok})

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
    if module == "estimate_onboarding":
        return ["--jurisdiction", slug]
    if module == "extract_agendas":
        return ["--all", "--jurisdiction", slug]
    if module == "extract_minutes":
        return ["--all", "--jurisdiction", slug]
    if module == "refresh_council_roster":
        return ["--slug", slug]
    raise ValueError(f"Unknown per-jurisdiction module: {module}")


if __name__ == "__main__":
    sys.exit(main())

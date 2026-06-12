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
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..db import connect
from ..jurisdiction import list_slugs
from ..run_lock import jurisdiction_lock, is_running
from .. import pipeline_health


PER_JURISDICTION_STEPS = [
    "meetings_inventory",
    "scan_document_availability",  # before extract, so dead-URL meetings get skipped by extractors
    "estimate_onboarding",         # cheap: turn the scanned inventory into a $ funding goal
    "extract_agendas",
    "extract_minutes",
    "backfill_document_text",      # mop up: store readable text for any doc the extractors
                                   # didn't store (cache-hit replays, batch-vision path). Bounded;
                                   # ~free once a jurisdiction's corpus is stored.
    "refresh_council_roster",
    "civicplus_board_rosters",     # cheap: appointed-board rosters from a config-declared
                                   # CivicPlus page; clean no-op for towns without the block.
    "extract_budgets",             # WEEKLY (see WEEKLY_STEPS) — budgets are annual, so a daily
                                   # run would needlessly HEAD-probe the TED repository at scale.
]

# Steps that run on a WEEKLY cadence, not every day — their data changes slowly, so a
# daily run is wasted work / needless probing. On the full cron they run only on
# _WEEKLY_WEEKDAY; a scoped/manual run (e.g. a deposit-triggered onboarding) always runs
# them so a newly-funded town catches up immediately. A missed week self-corrects next week.
WEEKLY_STEPS = {"extract_budgets"}
_WEEKLY_WEEKDAY = 0   # Monday (UTC)

# Steps that spend money (model / OCR). Skipped for a jurisdiction whose fund is
# paused (out of money) or suspended (manual hold) — but the cheap mapping steps
# (meetings_inventory, scan_document_availability) always run, so we keep the
# catalog fresh for everyone and activate paid extraction on demand. A deposit
# clears a pause, so the next run resumes these automatically. Includes the
# global backfill_summaries, which is gated when a run is scoped to one
# jurisdiction (so a deposit for town A never buys summaries for town B).
SPENDING_STEPS = {"extract_agendas", "extract_minutes", "refresh_council_roster",
                  "backfill_document_text", "extract_budgets", "backfill_summaries"}

JURISDICTION_AGNOSTIC_STEPS = [
    "refresh_findings",
    "sync_capabilities",   # cheap: persist build-phase state + emit phase_indexed milestones
    "monitor_clerk_contact",  # cheap: keep the clerk email (requests/digests target) deliverable
    "monitor_roster_changes",  # cheap: surface who joined/left/vacated a seat (citizen-facing)
    "refresh_pipeline_health",  # cheap, read-only: derive stale/job-failure ops issues from run state
    "backfill_summaries",
]


# Per-step wall-clock budgets. Document extraction is minutes-per-document
# (vision windows + extended thinking on long scanned minutes), so an outage
# backlog of ~20 documents legitimately needs hours — and the work is
# incremental (per-meeting commits + extraction cache), so a generous budget
# just lets a backlog drain in one run instead of tripping step_failed issues
# for days. Catalog/mapping steps stay on the tight default: if they run long,
# something is actually wrong.
_DEFAULT_STEP_TIMEOUT = 3600
_STEP_TIMEOUTS = {
    "extract_minutes": 4 * 3600,
    "extract_agendas": 4 * 3600,
    "backfill_document_text": 2 * 3600,
}


def _run_step(module: str, args: list[str]) -> tuple[bool, str]:
    """Run one job as a subprocess. Returns (ok, output).
    Subprocess isolation means one job's bad state doesn't poison later jobs."""
    cmd = [sys.executable, "-m", f"townwatch_etl.jobs.{module}", *args]
    print(f"  → {module} {' '.join(args)}")
    started = time.time()
    timeout = _STEP_TIMEOUTS.get(module, _DEFAULT_STEP_TIMEOUT)
    # Unbuffer the child: with a piped stdout Python block-buffers, so a step
    # killed by a signal (container OOM) dies with its ENTIRE output still in
    # the buffer — "✗ (970.6s) (no output), stderr:" was the only trace of the
    # 2026-06-12 OOM kill. Unbuffered, a killed step at least leaves the log
    # of how far it got.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
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
        if result.returncode < 0:
            # Killed by a signal — the step itself could record nothing, so
            # leave the trace here or the death is invisible to triage
            # (negative returncode = -signum; SIGKILL=-9 is the OOM signature).
            sig = -result.returncode
            try:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO pipeline_failure (job_name, step, message, context) "
                        "VALUES ('daily_refresh', 'step_killed', %s, %s::jsonb)",
                        (f"step {module} killed by signal {sig}"
                         f"{' (SIGKILL — likely container OOM)' if sig == 9 else ''} "
                         f"after {elapsed:.0f}s",
                         json.dumps({"module": module, "args": args, "signal": sig,
                                     "stdout_tail": tail})),
                    )
            except Exception as e:
                print(f"     (could not record kill: {type(e).__name__}: {e})")
        return ok, result.stdout + ("\n" + result.stderr if result.stderr else "")
    except subprocess.TimeoutExpired:
        print(f"     ✗ TIMEOUT after {timeout}s")
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


def _run_jurisdiction(slug: str, jid: int | None, trigger: str,
                      skip: set[str], ignore_funds: bool, summary: dict) -> None:
    """Run one jurisdiction's pipeline steps in order. Cheap mapping steps always
    run; spending steps are skipped when the fund is paused/suspended.

    Records a pipeline_run heartbeat (so the admin can see the automation ran +
    what surfaced) and opens/closes step_failed issues. Health recording never
    raises — a broken heartbeat must not break the run."""
    started_at = datetime.now(timezone.utc)
    can_spend, fund_status = (True, "ignored") if ignore_funds else _fund_state(slug)
    print(f"\n--- {slug} ---  [fund: {fund_status}]")
    slug_steps: list[dict] = []   # per-step status for this jurisdiction's heartbeat
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
            slug_steps.append({"module": module, "ok": True, "skipped": "funds"})
            continue
        # Weekly-cadence steps: on the full cron, run only on their weekday; a
        # scoped/manual run (deposit/onboarding) always runs them to catch up.
        if (module in WEEKLY_STEPS and trigger == "cron"
                and started_at.weekday() != _WEEKLY_WEEKDAY):
            print(f"  ⤓ {module} (skipped — weekly step, not its day)")
            slug_steps.append({"module": module, "ok": True, "skipped": "cadence"})
            continue
        step_args = _per_jurisdiction_args(module, slug)
        ok, output = _run_step(module, step_args)
        summary["steps"].append({"module": module, "slug": slug, "ok": ok})
        slug_steps.append({"module": module, "ok": ok})

    if jid is not None:
        try:
            _record_health(jid, trigger, started_at, slug_steps)
        except Exception as e:  # heartbeat must never break the pipeline
            print(f"  ⚠ pipeline-health record failed for {slug}: {type(e).__name__}: {e}")


# Per-jurisdiction "what surfaced since the run began" — counts of rows the steps
# created, joined back to the jurisdiction. Each table carries created_at.
_SURFACED_SQL = {
    "meetings": "SELECT count(*) AS n FROM meeting m "
                "JOIN governing_body gb ON gb.id = m.governing_body_id "
                "WHERE gb.jurisdiction_id = %s AND m.created_at >= %s",
    "agendas":  "SELECT count(*) AS n FROM agenda_item ai "
                "JOIN meeting m ON m.id = ai.meeting_id "
                "JOIN governing_body gb ON gb.id = m.governing_body_id "
                "WHERE gb.jurisdiction_id = %s AND ai.created_at >= %s",
    "motions":  "SELECT count(*) AS n FROM motion mo "
                "JOIN meeting m ON m.id = mo.meeting_id "
                "JOIN governing_body gb ON gb.id = m.governing_body_id "
                "WHERE gb.jurisdiction_id = %s AND mo.created_at >= %s",
    "roster":   "SELECT count(*) AS n FROM term t "
                "JOIN seat s ON s.id = t.seat_id "
                "JOIN governing_body gb ON gb.id = s.governing_body_id "
                "WHERE gb.jurisdiction_id = %s AND t.created_at >= %s",
}


def _record_health(jid: int, trigger: str, started_at, slug_steps: list[dict]) -> None:
    """Write the run heartbeat and reconcile step_failed issues for this jurisdiction."""
    failed = [s["module"] for s in slug_steps if not s["ok"]]
    ran = [s for s in slug_steps if "skipped" not in s]
    paused = [s for s in slug_steps if s.get("skipped") == "funds"]
    if failed:
        outcome = "failed" if len(failed) == len(ran) and ran else "partial"
    elif paused and not ran:
        outcome = "paused"
    else:
        outcome = "ok"

    with connect() as conn:
        surfaced = {
            key: conn.execute(sql, (jid, started_at)).fetchone()["n"]
            for key, sql in _SURFACED_SQL.items()
        }
        pipeline_health.record_run(
            conn, jid, outcome=outcome, started_at=started_at,
            finished_at=datetime.now(timezone.utc), trigger=trigger,
            steps=slug_steps, surfaced=surfaced, error_count=len(failed),
        )
        # A step that broke opens an issue; a step that now succeeds clears its issue.
        for s in slug_steps:
            if "skipped" in s:
                continue
            key = f"step_failed:{s['module']}"
            if s["ok"]:
                pipeline_health.close_issue(conn, jid, key)
            else:
                pipeline_health.observe_issue(
                    conn, jid, issue_type="step_failed", dedupe_key=key,
                    severity="high", title=f"Pipeline step failed: {s['module']}",
                    detail=(f"daily_refresh step `{s['module']}` exited non-zero for this "
                            f"jurisdiction. Inspect: `python -m townwatch_etl.jobs.{s['module']} "
                            f"--jurisdiction <slug>` and the pipeline_failure rows for details."),
                    context={"module": s["module"]},
                )


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


def _reconcile_data_source_jurisdictions() -> None:
    """Fill data_source.jurisdiction_id for any NULL rows by tracing the content
    they produced (migration 053 keeps provenance jurisdiction-aware). NULL-only +
    idempotent, so it's cheap to run every cron. A run is per-jurisdiction, so any
    referencing content row's jurisdiction is THE jurisdiction. Best-effort —
    never breaks the pipeline."""
    try:
        with connect() as conn:
            conn.execute(
                """
                WITH ds_juris AS (
                    SELECT DISTINCT ON (data_source_id) data_source_id, jurisdiction_id
                    FROM (
                        SELECT m.data_source_id, gb.jurisdiction_id
                          FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
                         WHERE m.data_source_id IS NOT NULL
                        UNION ALL
                        SELECT ai.data_source_id, gb.jurisdiction_id
                          FROM agenda_item ai JOIN meeting m ON m.id = ai.meeting_id
                          JOIN governing_body gb ON gb.id = m.governing_body_id
                         WHERE ai.data_source_id IS NOT NULL
                        UNION ALL
                        SELECT mo.data_source_id, gb.jurisdiction_id
                          FROM motion mo JOIN meeting m ON m.id = mo.meeting_id
                          JOIN governing_body gb ON gb.id = m.governing_body_id
                         WHERE mo.data_source_id IS NOT NULL
                        UNION ALL
                        SELECT s.data_source_id, gb.jurisdiction_id
                          FROM seat s JOIN governing_body gb ON gb.id = s.governing_body_id
                         WHERE s.data_source_id IS NOT NULL
                        UNION ALL
                        SELECT t.data_source_id, gb.jurisdiction_id
                          FROM term t JOIN seat s ON s.id = t.seat_id
                          JOIN governing_body gb ON gb.id = s.governing_body_id
                         WHERE t.data_source_id IS NOT NULL
                        UNION ALL
                        SELECT gb.data_source_id, gb.jurisdiction_id
                          FROM governing_body gb WHERE gb.data_source_id IS NOT NULL
                    ) refs
                    WHERE jurisdiction_id IS NOT NULL
                    ORDER BY data_source_id
                )
                UPDATE data_source ds SET jurisdiction_id = dj.jurisdiction_id
                FROM ds_juris dj
                WHERE ds.id = dj.data_source_id AND ds.jurisdiction_id IS NULL
                """
            )
    except Exception as e:
        print(f"  ⚠ data_source jurisdiction reconcile failed: {type(e).__name__}: {e}")


def _record_run_summary(summary: dict) -> None:
    """Record a pipeline_failure ONLY for failed jurisdiction-AGNOSTIC steps —
    they have no run heartbeat, so this row is their only surfacing (it rolls
    up into an org-level pipeline_issue). Per-jurisdiction step failures are
    deliberately excluded: _record_health already opens a step_failed issue
    per (jurisdiction, module), and duplicating them here kept a permanent
    org-level issue open for problems that were already tracked and fixed."""
    failed_global = [s["module"] for s in summary["steps"]
                     if not s["ok"] and s.get("slug") is None]
    if not failed_global:
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
                f"global step(s) failed: {', '.join(failed_global)}",
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
    trigger = "manual" if args.jurisdiction else "cron"

    # Preflight: surface any missing required keys (Anthropic/Mistral/Resend) as
    # org-level pipeline issues before doing work — a missing key can't be
    # auto-fixed, so a tracked issue is the resolution path. Never blocks the run.
    try:
        with connect() as conn:
            missing = pipeline_health.check_environment(conn)
        if missing:
            print(f"⚠ missing required env key(s): {', '.join(missing)} — opened pipeline issue(s)")
    except Exception as e:
        print(f"  ⚠ environment check failed: {type(e).__name__}: {e}")

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
            _run_jurisdiction(slug, jid, trigger, skip, args.ignore_funds, summary)

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

    # Keep provenance jurisdiction-aware: fill data_source.jurisdiction_id for any
    # rows whose ingest jobs didn't set it (NULL-only, cheap, all jobs at once).
    _reconcile_data_source_jurisdictions()

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
    if module == "backfill_document_text":
        # Bounded per run: extraction already stores text on cache-miss, so this
        # only mops up stragglers. Drains the historical backlog a chunk at a time.
        # The soft time budget (under the step's 2h hard kill) makes a slow batch
        # of giant packet PDFs a clean partial instead of a step_failed timeout.
        return ["--jurisdiction", slug, "--limit", "100", "--max-seconds", "5400"]
    if module == "refresh_council_roster":
        return ["--slug", slug]
    if module == "extract_budgets":
        return ["--jurisdiction", slug]
    raise ValueError(f"Unknown per-jurisdiction module: {module}")


if __name__ == "__main__":
    sys.exit(main())

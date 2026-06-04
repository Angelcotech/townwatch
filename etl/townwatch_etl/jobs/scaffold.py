"""
Tier-0 scaffold — stand a town up cheap, fast, and functional the moment it's
adopted, then flag any buildout problems to the org.

This is the "lay the tracks" step. It composes ONLY the cheap, existing jobs (no
new scraping, no historical extraction) to produce a functional skeleton in
seconds for pennies:

    pre-flight  validate_configs        — refuse to build a misconfigured town
    1. sync_jurisdictions               — create the jurisdiction + bodies + clerk
                                          / records-custodian contact + timezone
    2. meetings_inventory               — most-recent meetings + upcoming agendas
    3. scan_document_availability       — flag stub/dead document URLs
    4. estimate_onboarding              — the calibrated funding goal
    5. refresh_council_roster           — current officials (one cheap vision pass
                                          per body; ~pennies — the only spend)
    6. sync_capabilities                — persist build-phase state + milestones
    7. onboarding_smoke_test            — readiness verdict → pipeline_failure

The heavy historical depth (all old minutes → votes → full audit) stays a funded
Tier-1 (daily_refresh) — fund the town and it proceeds.

Readiness report to the org: onboarding_smoke_test already records per-body
verdicts to pipeline_failure (the admin queue). A consolidated scaffold summary
row is added when any step fails, so a problem town surfaces without log-diving.

Emits the genesis `jurisdiction_added` milestone (dated the town's created_at)
and a one-time `scaffold_complete` to the activity log.

Run:
    python -m townwatch_etl.jobs.scaffold --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import activity
from ..db import connect
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config
from ..run_lock import jurisdiction_lock, is_running
from .daily_refresh import _run_step
from .validate_configs import validate_one


# (module, args) after the slug is known. sync_jurisdictions runs first because it
# creates the jurisdiction row everything else needs.
def _steps(slug: str) -> list[tuple[str, list[str]]]:
    return [
        ("meetings_inventory", ["--jurisdiction", slug]),
        ("scan_document_availability", ["--jurisdiction", slug, "--only-changed"]),
        ("estimate_onboarding", ["--jurisdiction", slug]),
        ("refresh_council_roster", ["--slug", slug]),
        ("sync_capabilities", ["--jurisdiction", slug]),
        ("onboarding_smoke_test", ["--jurisdiction", slug]),
    ]


def _jid_for(slug: str) -> int | None:
    try:
        fips = jurisdiction_fips(load_config(slug))
    except Exception:
        return None
    with connect() as conn:
        row = conn.execute("SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
        return row["id"] if row else None


def _record_summary(slug: str, jid: int | None, steps: list[dict]) -> None:
    """Consolidated readiness line to the org admin queue, only when something
    failed — so a problem town surfaces without paging through logs."""
    failed = [s for s in steps if not s["ok"]]
    if not failed:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_failure (job_name, step, message, context) "
            "VALUES ('scaffold', 'readiness', %s, %s::jsonb)",
            (f"{len(failed)} of {len(steps)} scaffold steps failed for {slug}",
             json.dumps({"slug": slug, "jurisdiction_id": jid, "steps": steps})),
        )


def scaffold(slug: str) -> int:
    print(f"=== scaffold: {slug} ===")
    steps: list[dict] = []

    # Pre-flight — never build a misconfigured town. A bad config is the earliest,
    # cheapest buildout problem to catch.
    ok, msg = validate_one(slug)
    if not ok:
        print(f"  ✗ config invalid: {msg}")
        with connect() as conn:
            conn.execute(
                "INSERT INTO pipeline_failure (job_name, step, message, context) "
                "VALUES ('scaffold', 'config_invalid', %s, %s::jsonb)",
                (f"config invalid for {slug}: {msg}", json.dumps({"slug": slug})),
            )
        return 1

    # 1. Create the jurisdiction row (+ bodies + clerk + timezone) first.
    ok, _ = _run_step("sync_jurisdictions", ["--slug", slug])
    steps.append({"module": "sync_jurisdictions", "ok": ok})
    jid = _jid_for(slug)
    if jid is None:
        print(f"  ✗ no jurisdiction row after sync — aborting")
        _record_summary(slug, None, steps)
        return 1

    # A daily_refresh (or another scaffold) already working this town holds the
    # lock — let it finish; scaffolding would just race it.
    if is_running(jid):
        print(f"  ⊘ {slug} already being processed — skipping scaffold")
        return 0

    with jurisdiction_lock(jid) as got:
        if not got:
            print(f"  ⊘ {slug} lock held by another run — skipping scaffold")
            return 0
        for module, args in _steps(slug):
            ok, _ = _run_step(module, args)
            steps.append({"module": module, "ok": ok})

        # Genesis + go-live milestones.
        with connect() as conn:
            row = conn.execute(
                "SELECT display_name, created_at FROM jurisdiction WHERE id = %s", (jid,)
            ).fetchone()
            activity.record_jurisdiction_added(
                conn, jid, row["display_name"], occurred_at=row["created_at"])
            activity.record(
                conn, jid, "scaffold_complete",
                title=f"{row['display_name']} is live on TownWatch", once=True,
                meta={"steps": [s["module"] for s in steps]})

    _record_summary(slug, jid, steps)
    ok_count = sum(1 for s in steps if s["ok"])
    print(f"=== scaffold done: {ok_count}/{len(steps)} steps ok ===")
    return 0 if ok_count == len(steps) else 1


def trigger(slug: str) -> bool:
    """Kick off a detached Tier-0 scaffold for one town unless a run is already in
    flight for it. This is what the (future) adopt action calls so adoption yields
    an instant functional skeleton. Returns True if a run was started."""
    import subprocess
    jid = _jid_for(slug)
    if jid is not None and is_running(jid):
        print(f"trigger: {slug} already has a run in progress — leaving it")
        return False
    subprocess.Popen(
        [sys.executable, "-m", "townwatch_etl.jobs.scaffold", "--jurisdiction", slug],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    print(f"trigger: started a scaffold run for {slug}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; omit for all configs")
    args = parser.parse_args()
    slugs = [args.jurisdiction] if args.jurisdiction else list_slugs()
    rc = 0
    for slug in slugs:
        rc |= scaffold(slug)
    return rc


if __name__ == "__main__":
    sys.exit(main())

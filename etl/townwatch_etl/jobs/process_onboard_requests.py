"""
Process pending onboard requests — the cadence side of "adopt a town".

The web records a founding intent in onboard_request when an operator presses the
adopt seal (who founded it, their ordinal). This job (run on the daily_refresh
cadence) picks up pending requests and stands the town up:

    1. A scaffold-ready config must exist (jurisdictions/{slug}.json). Generating
       a minimal config from the directory/recon is a separate step (BACKLOG:
       auto-onboarding from the directory); until it exists the request is held
       as 'awaiting_config' (NOT failed) and retried next run.
    2. scaffold(slug, founder=…) runs the Tier-0 pipeline and writes the genesis
       activity event as "Founded by <name>" — the founder becomes the permanent
       first line of the town's record.

Web writes the intent; this is the only writer that acts on it. Idempotent:
scaffold's genesis is once-only, and a 'done' request is never reprocessed.

Run:
    python -m townwatch_etl.jobs.process_onboard_requests
"""

from __future__ import annotations

import sys

from ..db import connect
from ..jurisdiction import load_config
from .scaffold import scaffold, _jid_for


def _set_status(req_id: int, status: str, *, jurisdiction_id: int | None = None,
                error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE onboard_request "
            "SET status = %s, jurisdiction_id = COALESCE(%s, jurisdiction_id), "
            "    error = %s, processed_at = now() "
            "WHERE id = %s",
            (status, jurisdiction_id, error, req_id),
        )


def process_pending() -> dict:
    with connect() as conn:
        reqs = conn.execute(
            "SELECT id, slug, display_name, founder_name, founder_user_id, founder_number "
            "FROM onboard_request "
            "WHERE status IN ('pending', 'awaiting_config') "
            "ORDER BY requested_at"
        ).fetchall()

    done = failed = waiting = 0
    for r in reqs:
        slug = r["slug"]
        # A config is the prerequisite scaffold validates against. Until the
        # directory→config bootstrap exists, hold (don't fail) so the request
        # completes automatically the moment a config is authored.
        try:
            load_config(slug)
        except FileNotFoundError:
            _set_status(r["id"], "awaiting_config",
                        error=f"no jurisdictions/{slug}.json yet (config bootstrap pending)")
            waiting += 1
            print(f"  ⏳ {slug}: awaiting config")
            continue
        except Exception as e:  # noqa: BLE001 — a malformed config is a real failure
            _set_status(r["id"], "failed", error=f"config load error: {e}"[:500])
            failed += 1
            print(f"  ✗ {slug}: config load error: {e}")
            continue

        try:
            scaffold(slug, founder_name=r["founder_name"],
                     founder_user_id=r["founder_user_id"],
                     founder_number=r["founder_number"])
        except Exception as e:  # noqa: BLE001 — scaffold should not take the loop down
            _set_status(r["id"], "failed", error=str(e)[:500])
            failed += 1
            print(f"  ✗ {slug}: scaffold raised: {e}")
            continue

        # Town is founded once its jurisdiction row exists — scaffold may return
        # non-zero on a partial step failure (those surface in pipeline_failure),
        # but the genesis + jurisdiction are written regardless.
        jid = _jid_for(slug)
        if jid is not None:
            _set_status(r["id"], "done", jurisdiction_id=jid)
            done += 1
            print(f"  ✓ {slug}: founded (jurisdiction {jid})")
        else:
            _set_status(r["id"], "failed", error="scaffold ran but no jurisdiction row")
            failed += 1
            print(f"  ✗ {slug}: scaffold ran but no jurisdiction row")

    result = {"considered": len(reqs), "done": done, "failed": failed, "awaiting_config": waiting}
    print(f"onboard requests: {result}")
    return result


def main() -> int:
    process_pending()
    return 0


if __name__ == "__main__":
    sys.exit(main())

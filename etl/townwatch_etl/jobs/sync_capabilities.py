"""
Sync the build-capability ladder per jurisdiction into jurisdiction_capability,
and emit a phase_indexed activity milestone the first time each capability reaches
'indexed'.

This is the persisted source of truth for the build-progress widget. The state
logic is a faithful port of the web getBuildProgress thresholds (lib/funds-queries.ts)
so the dashboard reads identical states — the difference is this version REMEMBERS
when a phase first finished (first_indexed_at) and records the transition.

Cheap (pure SQL, no spend). Runs inside scaffold and at the tail of daily_refresh.
Idempotent: re-running only writes state changes; phase_indexed events are
once-only. On the very first sync a jurisdiction's already-indexed capabilities
are stamped now() with meta.backfilled=true — honest about not knowing the true
historical date.

Run:
    python -m townwatch_etl.jobs.sync_capabilities
    python -m townwatch_etl.jobs.sync_capabilities --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import sys

from .. import activity
from ..build_phases import historical_state, initial_cutoff
from ..db import connect
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


# Capability ladder — key, label. Order matches the web widget.
_LADDER = [
    ("directory", "Government directory"),
    ("meetings", "Meetings indexed"),
    ("minutes", "Minutes & votes extracted"),
    ("roster", "Elected roster"),
    ("audit", "Compliance audit"),
    ("historical", "Historical archive"),
    ("campaign_finance", "Campaign finance"),
    ("elections", "Elections calendar"),
    ("budget", "Budget records"),
]

# Clean, completed-milestone phrasing for the activity timeline (the ladder labels
# read as column headers, which stutter as event titles — "Meetings indexed indexed").
_MILESTONE_TITLE = {
    "directory": "Government directory mapped",
    "meetings": "Meetings indexed",
    "minutes": "Minutes & votes extracted",
    "roster": "Elected roster mapped",
    "audit": "Compliance audit live",
    "historical": "Historical archive indexed",
    "campaign_finance": "Campaign finance indexed",
    "elections": "Elections calendar live",
    "budget": "Budget records indexed",
}


def _fmt_usd(n: float) -> str:
    return f"${n:,.0f}" if n >= 20 else f"${n:.2f}"


def _counts(conn, jid: int) -> dict:
    # Depth-aware: 'real document' = URL present and not a placeholder stub;
    # 'extracted' = the meeting has the corresponding output (motions for
    # minutes, agenda_items for agendas) — same definitions as
    # estimate_onboarding._counts, split at the build-phase cutoff.
    cutoff = initial_cutoff()
    return conn.execute(
        """
        SELECT
          COALESCE(jf.status, 'unfunded') AS fund_status,
          (SELECT COUNT(*) FROM governing_body WHERE jurisdiction_id = j.id) AS bodies,
          (SELECT COUNT(*) FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id) AS meetings,
          (SELECT COUNT(*) FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id AND m.minutes_url IS NOT NULL) AS with_minutes_url,
          (SELECT COUNT(DISTINCT m.id) FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id
              AND EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)) AS with_motions,
          (SELECT COUNT(*) FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id AND m.meeting_date >= %(cutoff)s
              AND m.minutes_url IS NOT NULL
              AND NOT COALESCE(m.minutes_is_placeholder, FALSE)) AS minutes_initial_total,
          (SELECT COUNT(*) FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id AND m.meeting_date >= %(cutoff)s
              AND m.minutes_url IS NOT NULL
              AND NOT COALESCE(m.minutes_is_placeholder, FALSE)
              AND EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)) AS minutes_initial_done,
          (SELECT COUNT(*) FILTER (WHERE m.agenda_url IS NOT NULL
                                     AND NOT COALESCE(m.agenda_is_placeholder, FALSE))
                + COUNT(*) FILTER (WHERE m.minutes_url IS NOT NULL
                                     AND NOT COALESCE(m.minutes_is_placeholder, FALSE))
             FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id AND m.meeting_date < %(cutoff)s) AS hist_total,
          (SELECT COUNT(*) FILTER (WHERE m.agenda_url IS NOT NULL
                                     AND NOT COALESCE(m.agenda_is_placeholder, FALSE)
                                     AND NOT EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id))
                + COUNT(*) FILTER (WHERE m.minutes_url IS NOT NULL
                                     AND NOT COALESCE(m.minutes_is_placeholder, FALSE)
                                     AND NOT EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id))
             FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = j.id AND m.meeting_date < %(cutoff)s) AS hist_remaining,
          (SELECT COUNT(DISTINCT t.official_id) FROM seat s JOIN term t ON t.seat_id = s.id
            JOIN governing_body gb ON gb.id = s.governing_body_id WHERE gb.jurisdiction_id = j.id) AS officials,
          (SELECT COUNT(*) FROM compliance_finding cf JOIN governing_body gb ON gb.id = cf.governing_body_id
            WHERE gb.jurisdiction_id = j.id) AS findings_any,
          (SELECT COUNT(*) FROM campaign_contribution cc JOIN official o ON o.id = cc.official_id
            JOIN term t ON t.official_id = o.id JOIN seat s ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id WHERE gb.jurisdiction_id = j.id) AS contributions
        FROM jurisdiction j
        LEFT JOIN jurisdiction_fund jf ON jf.jurisdiction_id = j.id
        WHERE j.id = %(jid)s
        """,
        {"cutoff": cutoff, "jid": jid},
    ).fetchone()


def compute(conn, jid: int) -> list[dict]:
    """The capability ladder + computed state for one jurisdiction. This is the
    single source of truth — the web (lib/funds-queries.ts) only reads the
    persisted jurisdiction_capability rows, never recomputes.

    Depth-aware: 'minutes' measures coverage of the INITIAL build-phase window
    (one extracted meeting is not "done"); 'historical' is the funded phase-2
    rung whose needs_funding detail carries the unlock price tag."""
    c = _counts(conn, jid)
    bodies = int(c["bodies"]); meetings = int(c["meetings"])
    with_minutes_url = int(c["with_minutes_url"]); with_motions = int(c["with_motions"])
    officials = int(c["officials"]); findings = int(c["findings_any"])
    contributions = int(c["contributions"])
    mi_total = int(c["minutes_initial_total"]); mi_done = int(c["minutes_initial_done"])
    hist_total = int(c["hist_total"]); hist_remaining = int(c["hist_remaining"])
    unfunded = c["fund_status"] in ("unfunded", "paused")
    pending = "needs_funding" if unfunded else "in_progress"

    # Minutes & votes: coverage of the initial window, not "any motion exists".
    # Partial coverage on an UNFUNDED town is stalled, not in progress — the
    # funding wall is the honest state.
    if mi_total > 0:
        minutes_state = "indexed" if mi_done >= mi_total else pending
        minutes_detail = f"{mi_done} of {mi_total} recent meetings extracted"
    elif with_motions > 0:
        # No real minutes documents inside the window (small bodies can go
        # quiet for years) — older extractions still count as the capability.
        minutes_state, minutes_detail = "indexed", f"{with_motions} meetings with votes"
    elif with_minutes_url > 0:
        minutes_state, minutes_detail = pending, "pending"
    else:
        minutes_state, minutes_detail = "needs_funding", "no minutes documents found"

    # Historical archive: the phase-2 rung. build_phases owns the unlock rule;
    # the detail string IS the price tag the funding widget shows.
    if hist_total == 0:
        hist_state, hist_detail = "coming_soon", "no records beyond the recent window yet"
    elif hist_remaining == 0:
        hist_state, hist_detail = "indexed", f"{hist_total} historical documents indexed"
    else:
        hs = historical_state(conn, jid)
        done = hist_total - hist_remaining
        if hs["unlocked"]:
            hist_state = "in_progress"
            hist_detail = f"{done} of {hist_total} historical documents extracted"
        elif hs["needed_usd"] is None:
            hist_state, hist_detail = "needs_funding", f"{hist_remaining} documents · price estimate pending"
        else:
            hist_state = "needs_funding"
            hist_detail = f"{hist_remaining} documents · ~{_fmt_usd(hs['needed_usd'])} to unlock"

    state = {
        "directory": "indexed" if bodies > 0 else pending,
        "meetings": "indexed" if meetings > 0 else pending,
        "minutes": minutes_state,
        "roster": "indexed" if officials > 0 else pending,
        "audit": ("indexed" if findings > 0
                  else "in_progress" if meetings > 0 else pending),
        "historical": hist_state,
        "campaign_finance": "indexed" if contributions > 0 else "coming_soon",
        "elections": "coming_soon",
        "budget": "coming_soon",
    }
    detail = {
        "directory": f"{bodies} bodies" if bodies else "pending",
        "meetings": f"{meetings} meetings" if meetings else "pending",
        "minutes": minutes_detail,
        "roster": f"{officials} officials" if officials else "pending",
        "audit": f"{findings} findings tracked" if findings else "pending",
        "historical": hist_detail,
        "campaign_finance": f"{contributions} contributions" if contributions else "coming soon",
        "elections": "coming soon",
        "budget": "coming soon",
    }
    return [{"key": k, "label": lbl, "state": state[k], "detail": detail[k]} for k, lbl in _LADDER]


def sync(conn, jid: int) -> int:
    """Upsert capability states; stamp first_indexed_at + emit phase_indexed on the
    first transition into indexed. Returns the number of newly-indexed phases."""
    newly = 0
    for cap in compute(conn, jid):
        key, label, st = cap["key"], cap["label"], cap["state"]
        prior = conn.execute(
            "SELECT state, first_indexed_at FROM jurisdiction_capability "
            "WHERE jurisdiction_id = %s AND capability_key = %s",
            (jid, key),
        ).fetchone()
        already_stamped = prior is not None and prior["first_indexed_at"] is not None
        newly_indexed = st == "indexed" and not already_stamped
        backfilled = newly_indexed and prior is None  # pre-existing at first-ever sync

        conn.execute(
            """
            INSERT INTO jurisdiction_capability
                (jurisdiction_id, capability_key, state, detail, first_indexed_at, updated_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END, now())
            ON CONFLICT (jurisdiction_id, capability_key) DO UPDATE SET
                state = EXCLUDED.state,
                detail = EXCLUDED.detail,
                first_indexed_at = COALESCE(jurisdiction_capability.first_indexed_at,
                                            EXCLUDED.first_indexed_at),
                updated_at = now()
            """,
            (jid, key, st, cap["detail"], newly_indexed),
        )
        if newly_indexed:
            newly += 1
            activity.record(
                conn, jid, "phase_indexed",
                title=_MILESTONE_TITLE.get(key, f"{label} indexed"),
                ref_kind="capability", ref_id=key, once=True,
                meta={"backfilled": backfilled, "detail": cap["detail"]},
            )
    return newly


def _all_jids(conn) -> list[tuple[int, str]]:
    rows = conn.execute("SELECT id, display_name FROM jurisdiction ORDER BY id").fetchall()
    return [(r["id"], r["display_name"]) for r in rows]


def _jid_for_slug(conn, slug: str) -> int | None:
    try:
        fips = jurisdiction_fips(load_config(slug))
    except Exception:
        return None
    row = conn.execute("SELECT id FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
    return row["id"] if row else None


def run(jurisdiction_slug: str | None) -> int:
    with connect() as conn:
        if jurisdiction_slug:
            jid = _jid_for_slug(conn, jurisdiction_slug)
            targets = [(jid, jurisdiction_slug)] if jid else []
            if not targets:
                print(f"  {jurisdiction_slug}: no jurisdiction row yet — skipping")
                return 0
        else:
            targets = _all_jids(conn)
        print(f"syncing capabilities for {len(targets)} jurisdiction(s)...")
        total_new = 0
        for jid, name in targets:
            n = sync(conn, jid)
            total_new += n
            if n:
                print(f"  {name}: {n} phase(s) newly indexed")
    print(f"--- done ({total_new} phase transitions) ---")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; default all")
    args = parser.parse_args()
    return run(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())

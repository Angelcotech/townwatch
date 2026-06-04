"""
Troubleshoot jurisdiction time zones — the human-facing other half of the
never-blocking resolver.

Onboarding never hard-fails on an uncertain time zone: a multi-zone state we
can't pin to a county gets the predominant zone with timezone_status='assumed'
and the town goes live. This job surfaces those assumed (and any unresolved)
rows so they can be confirmed, and offers a one-button self-heal that re-runs
resolution from config — which picks up a freshly-added jurisdiction.timezone
override or an extended county table and flips the row to 'verified'.

    # See what needs a look:
    python -m townwatch_etl.jobs.troubleshoot_timezones
    # Re-resolve assumed rows from config (after adding overrides / updating code):
    python -m townwatch_etl.jobs.troubleshoot_timezones --resolve
"""

from __future__ import annotations

import argparse
import sys

from ..db import connect
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


def _fips_to_slug() -> dict[str, str]:
    """Map jurisdiction fips_code → config slug, for the 'edit which file' hint."""
    out: dict[str, str] = {}
    for slug in list_slugs():
        try:
            out[jurisdiction_fips(load_config(slug))] = slug
        except Exception:
            continue
    return out


def _review_rows(conn) -> list[dict]:
    """Jurisdictions whose timezone is not verified (assumed or unresolved)."""
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, fips_code, display_name, name, state_abbr, timezone, timezone_status
            FROM jurisdiction
            WHERE timezone_status IS DISTINCT FROM 'verified'
            ORDER BY state_abbr, name
            """
        ).fetchall()
    ]


def list_review() -> int:
    fips_slug = _fips_to_slug()
    with connect() as conn:
        rows = _review_rows(conn)
    if not rows:
        print("All jurisdiction time zones are verified. Nothing to troubleshoot.")
        return 0
    print(f"{len(rows)} jurisdiction(s) with an unconfirmed time zone:\n")
    for r in rows:
        slug = fips_slug.get(r["fips_code"], "(no config found)")
        status = r["timezone_status"] or "unresolved"
        print(f"  • {r['display_name']}, {r['state_abbr']}  "
              f"[{status}]  tz={r['timezone'] or 'NULL'}")
        print(f"      confirm: set \"timezone\" in jurisdictions/{slug}.json, "
              f"then re-run with --resolve")
    print("\nThese towns are LIVE on the best-guess zone; this is a refinement, "
          "not an outage.")
    return 0


def resolve_assumed() -> int:
    """Re-run resolution from config for every non-verified row (self-heal)."""
    # Imported here to avoid a circular import at module load.
    from .sync_jurisdictions import sync_one

    fips_slug = _fips_to_slug()
    with connect() as conn:
        rows = _review_rows(conn)
        if not rows:
            print("Nothing to resolve — all time zones are already verified.")
            return 0
        upgraded = still_assumed = skipped = 0
        for r in rows:
            slug = fips_slug.get(r["fips_code"])
            if not slug:
                print(f"  ? {r['display_name']}, {r['state_abbr']}: no config file — skipped")
                skipped += 1
                continue
            try:
                with conn.transaction():
                    sync_one(conn, slug)
                    after = conn.execute(
                        "SELECT timezone, timezone_status FROM jurisdiction WHERE id = %s",
                        (r["id"],),
                    ).fetchone()
            except Exception as e:
                print(f"  ✗ {slug}: {type(e).__name__}: {e}")
                skipped += 1
                continue
            if after["timezone_status"] == "verified":
                print(f"  ✓ {slug}: now verified → {after['timezone']}")
                upgraded += 1
            else:
                print(f"  ⚠ {slug}: still assumed → {after['timezone']} "
                      f"(needs an explicit override)")
                still_assumed += 1
    print(f"\n{upgraded} upgraded to verified, {still_assumed} still assumed, "
          f"{skipped} skipped.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Troubleshoot jurisdiction time zones")
    p.add_argument("--resolve", action="store_true",
                   help="re-run resolution from config for assumed rows (self-heal)")
    args = p.parse_args()
    return resolve_assumed() if args.resolve else list_review()


if __name__ == "__main__":
    sys.exit(main())

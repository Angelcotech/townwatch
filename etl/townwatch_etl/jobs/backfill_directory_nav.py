"""
Backfill jurisdiction_directory.county_fips + slug (the public cascade nav fields).

county_fips (cascade grouping):
  - county          → its own fips (already a 5-digit county fips)
  - school_district → bundle_fips (seed_jurisdiction_directory already points it at
                      its county)
  - consolidated city (bundle_fips set) → bundle_fips (its county)
  - ordinary city   → primary county from the recon universe roster
                      (research/ga_recon/universe_roster.json municipalities[].counties[0])

slug (stable URL key):
  - covered rows    → the linked jurisdiction.slug (stable across onboarding)
  - everything else → slugify(name)

Idempotent: recomputes from source each run. Reads the committed universe roster as
the place→county source (the seed job pulls Census, which doesn't carry a city's
county). Run after seed_jurisdiction_directory + migration 057.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ..db import connect

_ROSTER = Path(__file__).resolve().parents[3] / "research" / "ga_recon" / "universe_roster.json"


def _slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _place_to_county_fips(roster: dict) -> dict[str, str]:
    """7-digit place fips ('13'+place) → primary county fips, from the roster."""
    county_fips_by_name = {c["name"]: c["fips"] for c in roster["counties"].values()}
    out: dict[str, str] = {}
    for m in roster["municipalities"].values():
        place_fips = "13" + str(m["place_fips"]).zfill(5)
        counties = m.get("counties") or []
        if counties:
            out[place_fips] = county_fips_by_name.get(counties[0])
    return out


def run(state: str = "GA", *, dry_run: bool = False) -> dict:
    roster = json.loads(_ROSTER.read_text())
    muni_county = _place_to_county_fips(roster)

    with connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.fips, d.name, d.jurisdiction_type, d.bundle_fips, "
            "       j.slug AS covered_slug "
            "FROM jurisdiction_directory d "
            "LEFT JOIN jurisdiction j ON j.id = d.covered_jurisdiction_id "
            "WHERE d.state_abbr = %s",
            (state,),
        ).fetchall()

        updates: list[tuple[str | None, str, int]] = []  # (county_fips, slug, id)
        missing_county = 0
        for r in rows:
            t = r["jurisdiction_type"]
            if t == "county":
                cf = r["fips"]
            elif t == "school_district":
                cf = r["bundle_fips"]
            elif r["bundle_fips"]:          # consolidated city → its county
                cf = r["bundle_fips"]
            else:                           # ordinary city
                cf = muni_county.get(r["fips"])
            if cf is None and t in ("city", "school_district"):
                missing_county += 1
            slug = r["covered_slug"] or _slugify(r["name"])
            updates.append((cf, slug, r["id"]))

        if dry_run:
            print(f"[dry-run] {len(updates)} rows; {missing_county} without a county_fips")
            return {"rows": len(updates), "missing_county": missing_county, "dry_run": True}

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE jurisdiction_directory SET county_fips = %s, slug = %s, updated_at = now() "
                "WHERE id = %s",
                updates,
            )
    print(f"backfilled {len(updates)} directory rows ({missing_county} without a county_fips)")
    return {"rows": len(updates), "missing_county": missing_county}


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill jurisdiction_directory nav fields.")
    ap.add_argument("--state", default="GA")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(args.state, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Backfill jurisdiction_directory.county_fips + slug (the public cascade nav fields).

county_fips (cascade grouping):
  - county          → its own fips (already a 5-digit county fips)
  - school_district → bundle_fips (seed_jurisdiction_directory already points it at
                      its county)
  - consolidated city (bundle_fips set) → bundle_fips (its county)
  - ordinary city   → the county(ies) it spans, from the Census place→county source
                      (national; a city can span several counties — Atlanta, Honea Path).

slug (stable URL key):
  - covered rows    → the linked jurisdiction.slug (stable across onboarding)
  - everything else → slugify(name)

Place→county source: the Census Population Estimates SUB-EST national file, filtered
to SUMLEV-157 (incorporated-place-within-county part) rows. Each 157 row carries
STATE + COUNTY + PLACE, so place GEOID = STATE+PLACE and county FIPS = STATE+COUNTY;
a multi-county place gets one 157 row per county part ("… (pt.)"). This is national
and self-seeding — it replaced the GA-only universe roster (the seed job pulls the
Census gazetteer, which doesn't carry a place's county). Verified 2026-06-13 to
reproduce the verified GA roster's place→county sets exactly (0 mismatches across
535 shared places); the only roster place SUB-EST lacks was Mulberry (incorporated
2023, ahead of the popest vintage) — see PLACE_COUNTY_OVERRIDES.

Idempotent: recomputes from source each run. Run after seed_jurisdiction_directory
+ migration 057.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys

from ..db import connect
from ..http_client import civic_get

# Census Population Estimates subcounty file (national, no API key). Vintage lags
# the gazetteer the seed job uses (2025), so a place incorporated after this
# vintage is absent here until the next popest release — patch via the override
# below. Bump the year when a newer SUB-EST vintage ships.
SUBEST_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/"
    "2020-2024/cities/totals/sub-est2024.csv"
)

# Places present in the Census gazetteer (so the seed job creates a directory row)
# but not yet in the SUB-EST vintage above — typically a recent incorporation. Map
# the 7-digit place GEOID to the county FIPS it sits in so the cascade can file it.
# Loud, explicit, and removed once popest catches up. Mirrors the override pattern
# in seed_jurisdiction_directory (CONSOLIDATED_PLACE_TO_COUNTY / DODEA_UNSD_GEOIDS).
PLACE_COUNTY_OVERRIDES: dict[str, list[str]] = {
    "1353706": ["13135"],  # Mulberry city, GA (incorporated 2023) → Gwinnett County
}


def _slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _place_to_county_fips() -> dict[str, list[str]]:
    """7-digit place GEOID → the county FIPS it spans, from the national Census
    SUB-EST SUMLEV-157 place-parts. Ordered so the first entry is the PRIMARY
    county — the county part holding the largest share of the place's population
    (SUB-EST carries per-part POPESTIMATE2024), e.g. Columbia → Richland (not
    Lexington), Charleston → Charleston (not Berkeley). nav_county_fips carries
    the full set so a multi-county place lists under each county it touches."""
    r = civic_get(SUBEST_URL, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    parts: dict[str, dict[str, int]] = {}  # place GEOID → {county_fips: part population}
    reader = csv.DictReader(io.StringIO(r.content.decode("latin-1")))
    for row in reader:
        if row.get("SUMLEV") != "157" or row.get("PLACE") in (None, "", "00000"):
            continue
        place_geoid = row["STATE"] + row["PLACE"]
        try:
            pop = int(row.get("POPESTIMATE2024") or 0)
        except ValueError:
            pop = 0
        parts.setdefault(place_geoid, {})[row["STATE"] + row["COUNTY"]] = pop
    # Primary = largest-population county part; ties / missing pop fall back to
    # county-FIPS order so the result is deterministic.
    result = {
        pg: [cf for cf, _ in sorted(cs.items(), key=lambda kv: (-kv[1], kv[0]))]
        for pg, cs in parts.items()
    }
    result.update(PLACE_COUNTY_OVERRIDES)
    return result


def run(state: str = "GA", *, dry_run: bool = False) -> dict:
    muni_county = _place_to_county_fips()

    with connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.fips, d.name, d.jurisdiction_type, d.bundle_fips, "
            "       j.slug AS covered_slug "
            "FROM jurisdiction_directory d "
            "LEFT JOIN jurisdiction j ON j.id = d.covered_jurisdiction_id "
            "WHERE d.state_abbr = %s",
            (state,),
        ).fetchall()

        updates: list[tuple[str | None, list[str], str, int]] = []  # (county_fips, nav_county_fips, slug, id)
        missing_county = 0
        for r in rows:
            t = r["jurisdiction_type"]
            if t == "county":
                counties = [r["fips"]]
            elif t == "school_district" or r["bundle_fips"]:   # school district / consolidated → its county
                counties = [r["bundle_fips"]] if r["bundle_fips"] else []
            else:                                              # ordinary city → all counties it spans
                counties = muni_county.get(r["fips"], [])
            if not counties and t in ("city", "school_district"):
                missing_county += 1
            # county_fips = a single primary (first listed) for display; nav_county_fips =
            # the full set, so the cascade lists a multi-county city under each county.
            cf = counties[0] if counties else None
            slug = r["covered_slug"] or _slugify(r["name"])
            updates.append((cf, counties, slug, r["id"]))

        if dry_run:
            print(f"[dry-run] {len(updates)} rows; {missing_county} without a county")
            return {"rows": len(updates), "missing_county": missing_county, "dry_run": True}

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE jurisdiction_directory "
                "SET county_fips = %s, nav_county_fips = %s, slug = %s, updated_at = now() "
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

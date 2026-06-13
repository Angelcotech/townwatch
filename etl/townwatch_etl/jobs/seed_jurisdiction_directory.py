"""
Seed jurisdiction_directory from the U.S. Census gazetteer.

Populates the searchable catalog of every city, county, and school district in
a state so the public site's "find your town" search recognizes real places
and offers to onboard (adopt) the ones not yet covered. Scoped to one state
per run (default Georgia); the same job seeds any state.

Source: the Census 2025 Gazetteer place + county + unified-school-district
files (plain delimited text inside a zip, no API key; 2025 vintage reflects
Jan 1 2025 legal boundaries, so post-2023 incorporations like Mulberry GA are
present). Places are filtered to active legal governments (FUNCSTAT='A', i.e.
incorporated municipalities — CDPs and other statistical entities are dropped)
plus the consolidated city-county 'balance' records Census flags 'F'. School
districts come from the unified-school-district file (GEOID = state + 5-digit
SDLEA, the same form jurisdiction configs use for school_district_fips);
DoDEA-operated military-base districts are excluded — they are federal, not
local, governments (see research/ga_recon/UNIVERSE_SOURCES.md). GA seeds
536 municipalities + 2 consolidated balances, 159 counties, and 180 school
districts, matching the verified universe roster.

Names are cleaned for display ("Grovetown city" → "Grovetown"; counties keep
"X County"; school districts keep their full gazetteer name). Idempotent
upsert keyed on (state, type, FIPS).

After seeding, covered_jurisdiction_id is linked by matching the gazetteer GEOID
to an onboarded jurisdiction's fips_code — so the search can route covered towns
to their record and the rest into the adopt funnel. Finally, uncovered rows
whose GEOID is no longer in the source are deleted — Georgia dissolves dead
municipalities by statute (Ranger, HB 773 of 2023; Sunny Side, HB 542 effective
2024-01-01), and a dissolved town must drop out of the search. Covered rows are
never auto-deleted; a stale covered row prints a warning for human review.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile

from ..db import connect
from ..http_client import civic_get

GAZ_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/"
PLACE_FILE = "2025_Gaz_place_national.zip"
COUNTY_FILE = "2025_Gaz_counties_national.zip"
UNSD_FILE = "2025_Gaz_unsd_national.zip"

# Census unified-school-district GEOIDs that are DoDEA (federal) military-base
# districts, not local governments — excluded from the directory. Name-based
# filtering is unsafe (e.g. Texas has civilian "Fort Sam Houston ISD"), so this
# is an explicit per-GEOID list, extended as new states are reconned.
DODEA_UNSD_GEOIDS = {
    "1300003",  # Fort Stewart School District, GA (DoDEA Americas)
}

# Consolidated city-county place rows whose name carries no "X County" marker,
# mapped to their county FIPS. Most consolidations self-identify by name
# ("Macon-Bibb County", "Echols County consolidated government") and derive
# automatically; only marker-less ones need an entry here.
CONSOLIDATED_PLACE_TO_COUNTY = {
    "1319000": "13215",  # Columbus city, GA = Columbus-Muscogee consolidated government
}

# Trailing municipal-type words to strip from a place NAME for display.
_BALANCE_RE = re.compile(r"\s*\(balance\)\s*$", re.I)
_SUFFIX_RE = re.compile(
    r"\s+(city|town|village|borough|municipality|consolidated government|"
    r"unified government|metropolitan government|metro government)$",
    re.I,
)

# Extracts the county base name from a numbered or consolidated sub-county school
# district — "<County> School District <N>" (SC: Anderson 1-5, Spartanburg 1-7,
# Lexington 1-5, York 1-4, …) and "<County> County Consolidated School District"
# (SC: Sumter). Several independent districts ride one county; this pulls the
# county name so each bundles to it. The "(?:County )?(?:Consolidated )?" tail
# tolerates both shapes; an optional trailing number handles the numbering.
_SD_COUNTY_RE = re.compile(
    r"^(?P<base>.+?)\s+(?:County\s+)?(?:Consolidated\s+)?School District(?:\s+\d+)?\s*$",
    re.I,
)


def _fetch_rows(filename: str) -> list[dict]:
    """Download a gazetteer zip and yield header-mapped rows.

    Gazetteer vintages differ in delimiter (tab through 2023, pipe from 2025),
    so sniff it from the header line.
    """
    r = civic_get(GAZ_BASE + filename, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(z.namelist()[0]).decode("latin-1")
    lines = text.splitlines()
    delim = "|" if "|" in lines[0] else "\t"
    header = [h.strip() for h in lines[0].split(delim)]
    idx = {h: i for i, h in enumerate(header)}
    out = []
    for line in lines[1:]:
        f = line.split(delim)
        if len(f) <= max(idx.values()):
            continue
        out.append({h: f[i].strip() for h, i in idx.items()})
    return out


def _clean_place_name(raw: str) -> str:
    n = _BALANCE_RE.sub("", raw).strip()      # "...County consolidated government (balance)"
    n = _SUFFIX_RE.sub("", n).strip()         # → "Augusta-Richmond County"
    return n


def _derive_bundles(rows: list[tuple[str, str, str, str]]) -> dict[tuple[str, str], str]:
    """Map (jurisdiction_type, fips) → bundle_fips for rows that onboard as part
    of another government's bundle.

    Rules (from Census naming conventions, verified against the GA + SC universes):
    - "X County School District" → its county. ("Dougherty School District"-style
      names fall back to trying "<base> County".)
    - "Y City School District" → the city named Y (independent city systems).
    - Numbered / consolidated sub-county districts ("Anderson School District 1",
      "Sumter County Consolidated School District") → the county named in the title
      (via _SD_COUNTY_RE). SC runs several independent districts per county; all of
      them ride that county's bundle (county fund covers the county + its districts).
    - A *city*-typed row named "X County" or "...-X County" is a consolidated
      city-county government → its county (one government, one onboarding).
      Marker-less consolidations (Columbus) come from CONSOLIDATED_PLACE_TO_COUNTY.
    Unmatched school districts print a warning so a naming surprise in a new
    state is loud, not silently unbundled.
    """
    county_by_name = {name: fips for fips, name, t, _ in rows if t == "county"}
    city_by_name = {name: fips for fips, name, t, _ in rows if t == "city"}
    bundles: dict[tuple[str, str], str] = {}
    for fips, name, t, _state in rows:
        if t == "school_district":
            base = name.removesuffix(" School District").strip()
            if base in county_by_name:
                bundles[(t, fips)] = county_by_name[base]
            elif base.endswith(" City") and base.removesuffix(" City") in city_by_name:
                bundles[(t, fips)] = city_by_name[base.removesuffix(" City")]
            elif f"{base} County" in county_by_name:
                bundles[(t, fips)] = county_by_name[f"{base} County"]
            elif (m := _SD_COUNTY_RE.match(name)) and f"{m.group('base').strip()} County" in county_by_name:
                bundles[(t, fips)] = county_by_name[f"{m.group('base').strip()} County"]
            else:
                print(f"  ⚠ no bundle target for school district: {name}", file=sys.stderr)
        elif t == "city":
            if fips in CONSOLIDATED_PLACE_TO_COUNTY:
                bundles[(t, fips)] = CONSOLIDATED_PLACE_TO_COUNTY[fips]
            elif name in county_by_name:                       # "Echols County", "Webster County"
                bundles[(t, fips)] = county_by_name[name]
            else:                                              # "Athens-Clarke County", "Macon-Bibb County"
                for cname, cfips in county_by_name.items():
                    if name.endswith(f"-{cname}"):
                        bundles[(t, fips)] = cfips
                        break
    return bundles


def seed_state(state: str) -> dict:
    state = state.upper()
    places = _fetch_rows(PLACE_FILE)
    counties = _fetch_rows(COUNTY_FILE)
    districts = _fetch_rows(UNSD_FILE)

    rows: list[tuple[str, str, str, str]] = []  # (fips, name, type, state)
    for p in places:
        if p.get("USPS") != state:
            continue
        funcstat = p.get("FUNCSTAT", "")
        name = p["NAME"]
        consolidated = (
            "consolidated government" in name.lower()
            or "unified government" in name.lower()
        )
        # Keep incorporated municipalities (FUNCSTAT 'A') plus consolidated
        # city-counties (Augusta, Athens — Census flags their 'balance' entry 'F'
        # but they are very much real governments). Drop CDPs / statistical
        # entities (FUNCSTAT 'S').
        if funcstat == "S":
            continue
        if funcstat != "A" and not consolidated:
            continue
        rows.append((p["GEOID"], _clean_place_name(name), "city", state))
    for c in counties:
        if c.get("USPS") != state:
            continue
        rows.append((c["GEOID"], c["NAME"].strip(), "county", state))
    for d in districts:
        if d.get("USPS") != state:
            continue
        if d["GEOID"] in DODEA_UNSD_GEOIDS:
            continue
        rows.append((d["GEOID"], d["NAME"].strip(), "school_district", state))

    if not rows:
        print(f"  ⚠ no gazetteer rows for state {state}", file=sys.stderr)
        return {"state": state, "cities": 0, "counties": 0, "school_districts": 0, "linked": 0}

    bundles = _derive_bundles(rows)
    rows_b = [(f, n, t, s, bundles.get((t, f))) for f, n, t, s in rows]

    with connect() as conn:
        # Idempotent upsert.
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO jurisdiction_directory (fips, name, jurisdiction_type, state_abbr, bundle_fips) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (state_abbr, jurisdiction_type, fips) "
                "DO UPDATE SET name = EXCLUDED.name, bundle_fips = EXCLUDED.bundle_fips, updated_at = now()",
                rows_b,
            )
        # Link covered entries to their onboarded jurisdiction by FIPS.
        linked = conn.execute(
            "WITH u AS ("
            "  UPDATE jurisdiction_directory d SET covered_jurisdiction_id = j.id, updated_at = now() "
            "  FROM jurisdiction j "
            "  WHERE j.fips_code = d.fips AND j.state_abbr = d.state_abbr "
            "    AND d.covered_jurisdiction_id IS DISTINCT FROM j.id "
            "  RETURNING 1) SELECT count(*) AS n FROM u",
        ).fetchone()["n"]
        # Remove uncovered rows that left the source (dissolved municipalities,
        # boundary-file corrections). Never auto-delete a covered row.
        seeded_types = sorted({r[2] for r in rows})
        seeded_fips = [r[0] for r in rows]
        removed_rows = conn.execute(
            "DELETE FROM jurisdiction_directory "
            "WHERE state_abbr = %s AND jurisdiction_type = ANY(%s) "
            "  AND NOT (fips = ANY(%s)) AND covered_jurisdiction_id IS NULL "
            "RETURNING name, jurisdiction_type",
            (state, seeded_types, seeded_fips),
        ).fetchall()
        for r in removed_rows:
            print(f"  ✂ removed stale {r['jurisdiction_type']}: {r['name']} (gone from gazetteer)")
        stale_covered = conn.execute(
            "SELECT name, jurisdiction_type FROM jurisdiction_directory "
            "WHERE state_abbr = %s AND jurisdiction_type = ANY(%s) "
            "  AND NOT (fips = ANY(%s)) AND covered_jurisdiction_id IS NOT NULL",
            (state, seeded_types, seeded_fips),
        ).fetchall()
        for r in stale_covered:
            print(
                f"  ⚠ covered entry no longer in gazetteer (kept, review manually): "
                f"{r['jurisdiction_type']}: {r['name']}",
                file=sys.stderr,
            )

    cities = sum(1 for r in rows if r[2] == "city")
    cos = sum(1 for r in rows if r[2] == "county")
    sds = sum(1 for r in rows if r[2] == "school_district")
    return {"state": state, "cities": cities, "counties": cos, "school_districts": sds,
            "bundled": len(bundles), "linked": linked}


def main() -> int:
    p = argparse.ArgumentParser(description="Seed jurisdiction_directory from the Census gazetteer")
    p.add_argument("--state", default="GA", help="USPS state abbreviation (default GA)")
    args = p.parse_args()
    result = seed_state(args.state)
    print(f"seeded {args.state}: {result['cities']} cities + {result['counties']} counties "
          f"+ {result['school_districts']} school districts "
          f"({result['bundled']} bundled, {result['linked']} linked to onboarded jurisdictions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

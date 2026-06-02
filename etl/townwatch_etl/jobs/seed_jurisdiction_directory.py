"""
Seed jurisdiction_directory from the U.S. Census gazetteer.

Populates the searchable catalog of every city and county in a state so the
public site's "find your town" search recognizes real places and offers to
onboard (adopt) the ones not yet covered. Scoped to one state per run
(default Georgia); the same job seeds any state.

Source: the Census 2023 Gazetteer place + county files (plain TSV inside a zip,
no API key). Places are filtered to active legal governments (FUNCSTAT='A',
i.e. incorporated municipalities — CDPs and other statistical entities are
dropped). Names are cleaned for display ("Grovetown city" → "Grovetown";
counties keep "X County"). Idempotent upsert keyed on (state, type, FIPS).

After seeding, covered_jurisdiction_id is linked by matching the gazetteer GEOID
to an onboarded jurisdiction's fips_code — so the search can route covered towns
to their record and the rest into the adopt funnel.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile

import httpx

from ..db import connect

GAZ_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
PLACE_FILE = "2023_Gaz_place_national.zip"
COUNTY_FILE = "2023_Gaz_counties_national.zip"

# Trailing municipal-type words to strip from a place NAME for display.
_BALANCE_RE = re.compile(r"\s*\(balance\)\s*$", re.I)
_SUFFIX_RE = re.compile(
    r"\s+(city|town|village|borough|municipality|consolidated government|"
    r"unified government|metropolitan government|metro government)$",
    re.I,
)


def _fetch_rows(filename: str) -> list[dict]:
    """Download a gazetteer zip and yield header-mapped rows (tab-separated)."""
    r = httpx.get(GAZ_BASE + filename, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(z.namelist()[0]).decode("latin-1")
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    idx = {h: i for i, h in enumerate(header)}
    out = []
    for line in lines[1:]:
        f = line.split("\t")
        if len(f) <= max(idx.values()):
            continue
        out.append({h: f[i].strip() for h, i in idx.items()})
    return out


def _clean_place_name(raw: str) -> str:
    n = _BALANCE_RE.sub("", raw).strip()      # "...County consolidated government (balance)"
    n = _SUFFIX_RE.sub("", n).strip()         # → "Augusta-Richmond County"
    return n


def seed_state(state: str) -> dict:
    state = state.upper()
    places = _fetch_rows(PLACE_FILE)
    counties = _fetch_rows(COUNTY_FILE)

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

    if not rows:
        print(f"  ⚠ no gazetteer rows for state {state}", file=sys.stderr)
        return {"state": state, "cities": 0, "counties": 0, "linked": 0}

    with connect() as conn:
        # Idempotent upsert.
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO jurisdiction_directory (fips, name, jurisdiction_type, state_abbr) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (state_abbr, jurisdiction_type, fips) "
                "DO UPDATE SET name = EXCLUDED.name, updated_at = now()",
                rows,
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

    cities = sum(1 for r in rows if r[2] == "city")
    cos = sum(1 for r in rows if r[2] == "county")
    return {"state": state, "cities": cities, "counties": cos, "linked": linked}


def main() -> int:
    p = argparse.ArgumentParser(description="Seed jurisdiction_directory from the Census gazetteer")
    p.add_argument("--state", default="GA", help="USPS state abbreviation (default GA)")
    args = p.parse_args()
    result = seed_state(args.state)
    print(f"seeded {args.state}: {result['cities']} cities + {result['counties']} counties "
          f"({result['linked']} linked to onboarded jurisdictions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

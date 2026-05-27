"""
Sync jurisdiction rows from per-jurisdiction config files.

Until now, jurisdiction rows were inserted manually with SQL during
onboarding. That's fine for two jurisdictions; it's a non-starter at
nineteen thousand. This job is the template-grade replacement: read
every jurisdictions/*.json, upsert the matching jurisdiction row by
fips_code, write the always-stable fields (display_name, population,
office_address, office_phone).

What this job does NOT touch:
  - governing_bodies — owned by meetings_inventory + civicengage_officials
  - jurisdiction-scoped data (officials, meetings, motions) — owned by
    their own jobs

Behaviour:
  - Insert a new row if no jurisdiction with this fips_code exists.
  - Update the writable fields if a row does exist.
  - Loud failure if a config declares a state with no _state_defaults
    file (load_config raises).

Run:
    python -m townwatch_etl.jobs.sync_jurisdictions
    python -m townwatch_etl.jobs.sync_jurisdictions --slug grovetown-ga
    python -m townwatch_etl.jobs.sync_jurisdictions --dry-run
"""

from __future__ import annotations

import argparse
import sys

from ..db import connect
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


# Maps DB columns -> config paths. Adding a writable field is a one-line
# change here + the migration that adds the column. Keep this list tight:
# only put fields where the config is the canonical source. Population,
# for example, comes from the decennial census via the config file and
# wouldn't be sourced from anywhere else.
WRITABLE_FIELDS: list[tuple[str, tuple[str, ...]]] = [
    ("display_name",   ("jurisdiction", "display_name")),
    ("population",     ("jurisdiction", "population")),
    # Config still uses legacy "city_hall_*" field names; DB columns are
    # neutral. The mapping lives here so a future config-field rename
    # doesn't require a schema migration to keep working.
    ("office_address", ("jurisdiction", "city_hall_address")),
    ("office_phone",   ("jurisdiction", "city_hall_phone")),
]


def _get(config: dict, path: tuple[str, ...]):
    cur = config
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def sync_one(conn, slug: str, *, dry_run: bool = False) -> dict:
    """Sync a single jurisdiction. Returns a small action summary."""
    config = load_config(slug)
    fips = jurisdiction_fips(config)
    existing = conn.execute(
        "SELECT id, display_name, population, office_address, office_phone "
        "FROM jurisdiction WHERE fips_code = %s",
        (fips,),
    ).fetchone()

    new_values = {col: _get(config, path) for col, path in WRITABLE_FIELDS}

    if existing is None:
        # Insert path needs the not-null structural fields too. Pull
        # them straight from config — schema validation already ran in
        # load_config so we know they're present.
        j = config["jurisdiction"]
        cols = {
            "fips_code":         fips,
            "name":              j["name"],
            "display_name":      new_values["display_name"],
            "jurisdiction_type": j["type"],
            "state_fips":        j["state_fips"],
            "state_abbr":        j["state"],
            "county_fips":       j.get("county_fips"),
            "population":        new_values["population"],
            "office_address":    new_values["office_address"],
            "office_phone":      new_values["office_phone"],
        }
        if dry_run:
            return {"action": "would_insert", "slug": slug, "fips": fips, "values": cols}
        # data_source_id is NOT NULL — re-use the bootstrap source row
        # used by other config-sourced jobs.
        ds = conn.execute(
            "SELECT id FROM data_source WHERE source_name = %s LIMIT 1",
            ("jurisdiction_config_sync",),
        ).fetchone()
        if ds is None:
            ds = conn.execute(
                """
                INSERT INTO data_source (source_type, source_name, record_url, fetched_at)
                VALUES ('manual', 'jurisdiction_config_sync', 'internal://config-sync', now())
                RETURNING id
                """,
            ).fetchone()
        conn.execute(
            """
            INSERT INTO jurisdiction (
                fips_code, name, display_name, jurisdiction_type,
                state_fips, state_abbr, county_fips, population,
                office_address, office_phone, data_source_id
            )
            VALUES (%(fips_code)s, %(name)s, %(display_name)s, %(jurisdiction_type)s,
                    %(state_fips)s, %(state_abbr)s, %(county_fips)s, %(population)s,
                    %(office_address)s, %(office_phone)s, %(data_source_id)s)
            """,
            {**cols, "data_source_id": ds["id"]},
        )
        return {"action": "inserted", "slug": slug, "fips": fips}

    # Update path — only write fields that actually changed, so updated_at
    # doesn't get bumped on no-op runs.
    diffs = {col: new_values[col] for col, _ in WRITABLE_FIELDS
             if new_values[col] is not None and existing[col] != new_values[col]}
    if not diffs:
        return {"action": "unchanged", "slug": slug, "fips": fips}
    if dry_run:
        return {"action": "would_update", "slug": slug, "fips": fips, "diffs": diffs}
    set_clauses = ", ".join(f"{col} = %({col})s" for col in diffs)
    conn.execute(
        f"UPDATE jurisdiction SET {set_clauses}, updated_at = now() WHERE id = %(id)s",
        {**diffs, "id": existing["id"]},
    )
    return {"action": "updated", "slug": slug, "fips": fips, "diffs": diffs}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Sync only this jurisdiction. Default: all configs.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, no writes")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    if not slugs:
        print("No jurisdiction configs found.")
        return 0

    inserted = updated = unchanged = 0
    with connect() as conn:
        for slug in slugs:
            result = sync_one(conn, slug, dry_run=args.dry_run)
            action = result["action"]
            if action in ("inserted", "would_insert"):
                inserted += 1
                print(f"  + {slug}: insert ({result['fips']})")
            elif action in ("updated", "would_update"):
                updated += 1
                diffs = result.get("diffs", {})
                fields = ", ".join(f"{k}={v!r}" for k, v in diffs.items())
                print(f"  ~ {slug}: update {fields}")
            else:
                unchanged += 1
                print(f"  · {slug}: no change")

    print(f"\n{inserted} inserted, {updated} updated, {unchanged} unchanged"
          + (" [DRY RUN]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

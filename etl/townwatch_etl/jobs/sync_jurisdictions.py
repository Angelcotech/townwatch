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
import json
import sys

from ..db import connect
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config
from ..timezones import resolve_timezone


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
    # Records custodian contact — denormalized into jurisdiction so the
    # admin portal can populate mailto: compose links without reading
    # config files at request time. Body-level custodian overrides
    # (governing_body.records_custodian) are not denormalized; they
    # remain on the config side where prepare_records_request reads them.
    ("records_custodian_name",  ("records_custodian", "name")),
    ("records_custodian_title", ("records_custodian", "title")),
    ("records_custodian_email", ("records_custodian", "email")),
    # IANA time zone + its confidence. Not authored in most configs — resolved
    # at sync time (see _ensure_timezone) and written into the merged config
    # before this mapping reads it, so the normal insert/update path persists it.
    ("timezone",        ("jurisdiction", "timezone")),
    ("timezone_status", ("jurisdiction", "timezone_status")),
]


def _get(config: dict, path: tuple[str, ...]):
    cur = config
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _ensure_timezone(config: dict, slug: str) -> None:
    """Populate config['jurisdiction']['timezone'] + ['timezone_status'] in
    place, so the normal WRITABLE_FIELDS path persists them. NEVER raises — the
    resolver always returns a usable zone. An 'assumed' result still onboards;
    it just prints a troubleshoot hint (and is later listed by
    troubleshoot_timezones for a one-line override)."""
    j = config.setdefault("jurisdiction", {})
    res = resolve_timezone(config)
    j["timezone"] = res.timezone
    j["timezone_status"] = res.status
    if res.status == "assumed":
        print(f"  ⚠ {slug}: timezone {res.timezone} is a best guess "
              f"— {res.note} (onboarding continues)")


def _get_or_create_data_source(conn) -> int:
    """The bootstrap data_source row reused by all config-sourced writes."""
    ds = conn.execute(
        "SELECT id FROM data_source WHERE source_name = %s LIMIT 1",
        ("jurisdiction_config_sync",),
    ).fetchone()
    if ds is None:
        ds = conn.execute(
            """
            INSERT INTO data_source (source_type, source_name, record_url)
            VALUES ('manual', 'jurisdiction_config_sync', 'internal://config-sync')
            RETURNING id
            """,
        ).fetchone()
    return ds["id"]


def _sync_bodies(conn, jurisdiction_id: int, ds_id: int, config: dict) -> list[str]:
    """Find-or-create the jurisdiction's governing bodies from config —
    idempotent by (jurisdiction_id, name). Until now bodies were created only by
    platform-specific officials jobs (e.g. civicengage_officials); making it
    config-driven here means meetings_inventory finds the body for ANY platform
    (incl. Edlio) on the first run. Returns the names of bodies created."""
    created: list[str] = []
    for b in config.get("governing_bodies", []):
        name, btype = b.get("name"), b.get("body_type")
        if not name or not btype:
            continue
        row = conn.execute(
            "SELECT id FROM governing_body WHERE jurisdiction_id = %s AND name = %s",
            (jurisdiction_id, name),
        ).fetchone()
        if row:
            continue
        conn.execute(
            """
            INSERT INTO governing_body
                (jurisdiction_id, name, body_type, meeting_frequency, website_url, data_source_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (jurisdiction_id, name, btype, b.get("meeting_frequency"), b.get("website_url"), ds_id),
        )
        created.append(name)
    return created


def sync_one(conn, slug: str, *, dry_run: bool = False) -> dict:
    """Sync a single jurisdiction + its governing bodies. Returns a summary."""
    config = load_config(slug)
    _ensure_timezone(config, slug)
    fips = jurisdiction_fips(config)
    # Canonical city-level slug = the config handle with its trailing state suffix
    # removed (e.g. 'grovetown-ga' -> 'grovetown'). Derived from the immutable
    # config filename, NOT the mutable display name, so a rename never moves URLs.
    # Identity is the (state_abbr, slug) pair, mirroring the /[state]/[city] route.
    state_l = (config["jurisdiction"]["state"] or "").lower()
    slug_col = slug.removesuffix(f"-{state_l}") if state_l else slug
    # SELECT each writable column so the diff-check below sees current
    # state on the existing row. The column list is derived from
    # WRITABLE_FIELDS so adding a new sync target requires no further
    # plumbing here.
    select_cols = ", ".join(["id", "slug"] + [c for c, _ in WRITABLE_FIELDS])
    existing = conn.execute(
        f"SELECT {select_cols} FROM jurisdiction WHERE fips_code = %s",
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
            "slug":              slug_col,
            "name":              j["name"],
            "jurisdiction_type": j["type"],
            "state_fips":        j["state_fips"],
            "state_abbr":        j["state"],
            "county_fips":       j.get("county_fips"),
            # All writable fields flow through new_values so adding one
            # in WRITABLE_FIELDS doesn't require a second edit here.
            **new_values,
        }
        if dry_run:
            return {"action": "would_insert", "slug": slug, "fips": fips, "values": cols}
        ds_id = _get_or_create_data_source(conn)
        # Build the column list + placeholders dynamically from cols so
        # adding a writable field doesn't require editing the SQL here.
        all_cols = {**cols, "data_source_id": ds_id}
        col_names = ", ".join(all_cols.keys())
        placeholders = ", ".join(f"%({k})s" for k in all_cols.keys())
        row = conn.execute(
            f"INSERT INTO jurisdiction ({col_names}) VALUES ({placeholders}) RETURNING id",
            all_cols,
        ).fetchone()
        jid = row["id"]
        result = {"action": "inserted", "slug": slug, "fips": fips}
    else:
        jid = existing["id"]
        # Update path — only write fields that actually changed, so updated_at
        # doesn't get bumped on no-op runs.
        diffs = {col: new_values[col] for col, _ in WRITABLE_FIELDS
                 if new_values[col] is not None and existing[col] != new_values[col]}
        # slug is structural (not a WRITABLE_FIELDS config path) — reconcile it too
        # so a config rename re-points the canonical slug.
        if slug_col and existing["slug"] != slug_col:
            diffs["slug"] = slug_col
        if dry_run:
            return ({"action": "would_update", "slug": slug, "fips": fips, "diffs": diffs}
                    if diffs else {"action": "unchanged", "slug": slug, "fips": fips})
        if diffs:
            set_clauses = ", ".join(f"{col} = %({col})s" for col in diffs)
            conn.execute(
                f"UPDATE jurisdiction SET {set_clauses}, updated_at = now() WHERE id = %(id)s",
                {**diffs, "id": jid},
            )
            result = {"action": "updated", "slug": slug, "fips": fips, "diffs": diffs}
        else:
            result = {"action": "unchanged", "slug": slug, "fips": fips}
        ds_id = _get_or_create_data_source(conn)

    # Seed governing bodies from config (idempotent) on every real run.
    created = _sync_bodies(conn, jid, ds_id, config)
    if created:
        result["bodies_created"] = created
    return result


def _record_sync_failure(conn, slug: str, exc: Exception) -> None:
    """Record a per-jurisdiction sync failure so a bad config is visible to an
    operator instead of silently vanishing. Its own transaction block, so the
    record commits independently of the slug that failed."""
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO pipeline_failure (job_name, step, exception_class, message, context)
            VALUES ('sync_jurisdictions', 'SYNC_ERROR', %s, %s, %s::jsonb)
            """,
            (type(exc).__name__,
             f"{slug}: {exc}"[:1000],
             json.dumps({"jurisdiction": slug, "error": type(exc).__name__})),
        )


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
    failed: list[str] = []
    with connect() as conn:
        for slug in slugs:
            # Each jurisdiction is its own transaction block: a single malformed
            # config (e.g. an unmappable timezone, a schema-invalid field) is
            # isolated, recorded, and skipped — it must NOT roll back every other
            # town in an unattended batch run.
            try:
                with conn.transaction():
                    result = sync_one(conn, slug, dry_run=args.dry_run)
            except Exception as e:
                print(f"  ✗ {slug}: {type(e).__name__}: {e}")
                if not args.dry_run:
                    try:
                        _record_sync_failure(conn, slug, e)
                    except Exception as rec_err:
                        print(f"    (could not record failure: {rec_err})")
                failed.append(slug)
                continue

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

    print(f"\n{inserted} inserted, {updated} updated, {unchanged} unchanged, "
          f"{len(failed)} failed" + (" [DRY RUN]" if args.dry_run else ""))
    if failed:
        print(f"FAILED: {', '.join(failed)} — recorded to pipeline_failure. Fix the "
              f"config (e.g. set jurisdiction.timezone for a multi-zone state) and re-run; "
              f"the other jurisdictions were synced normally.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

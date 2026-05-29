"""
Ingest elected-district polygons from a jurisdiction's ArcGIS REST layer.

Reads `gis.districts_endpoint` from each jurisdiction config, queries
the ArcGIS REST layer for its features in GeoJSON form, and upserts
one `jurisdiction_district` row per feature. After polygons are in,
links each district-based `seat` to its polygon via `seat.district_id`
so the citizen-facing "what district is this address in?" lookup is a
single join.

Conventions across counties differ — the human district identifier is
sometimes `DISTRICT`, sometimes `CountyDistrictID`, sometimes just `ID`
or `OBJECTID`. We probe a small ordered list of attribute names and
pick the first numeric one. Same for the human-readable name when one
is published.

Idempotent — re-runs upsert geometry + refresh updated_at.

Run:
    python -m townwatch_etl.jobs.ingest_districts --slug columbia-county-ga
    python -m townwatch_etl.jobs.ingest_districts                # all configs with gis.districts_endpoint
    python -m townwatch_etl.jobs.ingest_districts --dry-run      # fetch + report, no writes
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..http_client import civic_client

from ..audit import record_failure
from ..ingest_base import IngestJob
from ..jurisdiction import jurisdiction_fips, list_slugs, load_config


USER_AGENT = "TownWatch-ingest-districts/0.1 (civic transparency research)"

# Attribute names to probe for "which district is this", ranked by
# specificity. Different counties name this field differently.
DISTRICT_NUMBER_KEYS = [
    "CountyDistrictID", "DISTRICT_NUMBER", "DistrictNumber",
    "COMMISSION_DISTRICT", "COUNCIL_DISTRICT", "WARD_NUMBER",
    "DISTRICT_NO", "DISTRICT", "DIST", "DistrictID", "DIST_NUM",
    "WARD", "Number", "ID",
]
# Attribute names to probe for a published district display name.
DISTRICT_NAME_KEYS = [
    "DISTRICT_NAME", "DistrictName", "NAME", "Name", "LABEL", "Label",
]


class DistrictIngest(IngestJob):
    """One run = one jurisdiction's districts pulled + persisted."""

    source_type = "scrape"

    def __init__(self, slug: str, *, dry_run: bool = False) -> None:
        super().__init__()
        self.slug = slug
        self.dry_run = dry_run
        self.config = load_config(slug)
        gis = self.config.get("gis") or {}
        self.endpoint: str | None = gis.get("districts_endpoint")
        if not self.endpoint:
            raise RuntimeError(
                f"Jurisdiction {slug!r} has no gis.districts_endpoint configured. "
                f"Run discover_gis_districts first."
            )
        self.source_name = f"district_ingest:{slug}"
        self.source_url = self.endpoint

    def ingest(self) -> None:
        assert self.conn is not None

        jid = self._jurisdiction_id()
        features = self._fetch_features()
        print(f"  → {len(features)} district feature(s) fetched")

        upserted = 0
        for feat in features:
            geom = feat.get("geometry")
            props = feat.get("properties") or {}
            if not geom:
                continue
            district_number = self._extract_district_number(props)
            name = self._extract_name(props, district_number)
            if name is None:
                # No usable identifier — skip rather than mash a bunch
                # of nameless polygons together. record_failure makes
                # the gap loud.
                record_failure(
                    self.conn,
                    job_name="ingest_districts",
                    step="extract_name",
                    message=f"No usable district name in feature properties: {sorted(props.keys())}",
                    context={"slug": self.slug, "properties": props},
                )
                continue

            if self.dry_run:
                print(f"     [dry-run] would upsert: {name!r} (number={district_number})")
                continue

            # ST_Multi coerces single Polygon into MultiPolygon to match
            # the GEOMETRY(MultiPolygon, 4326) column constraint.
            self.conn.execute(
                """
                INSERT INTO jurisdiction_district (
                    jurisdiction_id, name, district_number,
                    geometry, source_url, data_source_id
                )
                VALUES (
                    %s, %s, %s,
                    ST_Multi(ST_GeomFromGeoJSON(%s)),
                    %s, %s
                )
                ON CONFLICT (jurisdiction_id, name) DO UPDATE
                SET district_number = EXCLUDED.district_number,
                    geometry        = EXCLUDED.geometry,
                    source_url      = EXCLUDED.source_url,
                    updated_at      = now()
                """,
                (
                    jid, name, district_number,
                    json.dumps(geom),
                    self.endpoint, self.data_source_id,
                ),
            )
            upserted += 1

        print(f"  → {upserted} district row(s) upserted")
        if not self.dry_run and upserted > 0:
            linked = self._link_seats(jid)
            print(f"  → {linked} seat(s) linked to a district by district_name match")

    # -- DB lookups -------------------------------------------------------

    def _jurisdiction_id(self) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (jurisdiction_fips(self.config),),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Jurisdiction not found in DB for slug={self.slug!r}")
        return row["id"]

    def _link_seats(self, jurisdiction_id: int) -> int:
        """Match seats to districts by their district_name attribute.
        Only updates seats whose district_id is currently NULL or whose
        match has changed — never overwrites a manually-pinned link."""
        assert self.conn is not None
        result = self.conn.execute(
            """
            UPDATE seat s
            SET district_id = jd.id, updated_at = now()
            FROM jurisdiction_district jd
            JOIN governing_body gb ON gb.jurisdiction_id = jd.jurisdiction_id
            WHERE s.governing_body_id = gb.id
              AND jd.jurisdiction_id = %s
              AND s.seat_type = 'district'
              AND s.district_name IS NOT NULL
              AND s.district_name <> ''
              AND s.district_name = jd.district_number::text
              AND (s.district_id IS DISTINCT FROM jd.id)
            RETURNING s.id
            """,
            (jurisdiction_id,),
        )
        return len(result.fetchall())

    # -- ArcGIS REST fetching ---------------------------------------------

    def _fetch_features(self) -> list[dict[str, Any]]:
        """Pull every feature from the layer as GeoJSON. ArcGIS REST
        supports `f=geojson` directly with `outSR=4326` (WGS84), so we
        don't have to reproject ourselves."""
        with civic_client(default_timeout=30.0) as client:
            resp = client.get(
                f"{self.endpoint.rstrip('/')}/query",
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "f": "geojson",
                    "outSR": "4326",
                    "returnGeometry": "true",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("features", []) or []

    # -- Attribute parsing -------------------------------------------------

    @staticmethod
    def _extract_district_number(props: dict[str, Any]) -> int | None:
        """Try common attribute names; return the first numeric one."""
        # Case-insensitive lookup since ArcGIS preserves case
        ci = {k.lower(): v for k, v in props.items()}
        for key in DISTRICT_NUMBER_KEYS:
            v = ci.get(key.lower())
            if v is None:
                continue
            try:
                return int(str(v).strip())
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _extract_name(props: dict[str, Any], district_number: int | None) -> str | None:
        """Prefer a published name; fall back to 'District N'.
        Returns None if there's no usable identifier."""
        ci = {k.lower(): v for k, v in props.items()}
        for key in DISTRICT_NAME_KEYS:
            v = ci.get(key.lower())
            if v is None:
                continue
            v = str(v).strip()
            if v:
                return v
        if district_number is not None:
            return f"District {district_number}"
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all with gis.districts_endpoint.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + report, no writes.")
    args = parser.parse_args()

    slugs: list[str]
    if args.slug:
        slugs = [args.slug]
    else:
        slugs = [s for s in list_slugs()
                 if (load_config(s).get("gis") or {}).get("districts_endpoint")]
        if not slugs:
            print("No jurisdictions have gis.districts_endpoint configured.")
            return 0

    for slug in slugs:
        print(f"\n=== {slug} ===")
        try:
            DistrictIngest(slug, dry_run=args.dry_run).run()
        except Exception as e:
            print(f"  ✗ {slug}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

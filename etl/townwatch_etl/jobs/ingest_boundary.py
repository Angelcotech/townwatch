"""
Ingest each jurisdiction's outer boundary polygon from Census TIGERweb.

TIGERweb is the Census Bureau's ArcGIS REST mirror of the TIGER/Line
shapefiles. Universal across US — every county and incorporated place
has a record, keyed by GEOID:
  county GEOID = state_fips (2) + county_fips (3)   → 5 digits
  place  GEOID = state_fips (2) + place_fips (5)    → 7 digits

Our DB already stores these in their full-GEOID form (county_fips as
5 digits, place_fips as 7), so we hand them straight to TIGERweb's
WHERE clause without reassembly.

The boundary is purely cosmetic (renders as a small SVG outline next
to the jurisdiction name). No GIST index, no spatial queries — we just
hand the geometry to ST_AsSVG on read.

Idempotent. Re-runs upsert the geometry and refresh updated_at.

Run:
    python -m townwatch_etl.jobs.ingest_boundary
    python -m townwatch_etl.jobs.ingest_boundary --slug grovetown-ga
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


USER_AGENT = "TownWatch-ingest-boundary/0.1 (civic transparency research)"

TIGER_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer"
)

# Layer-name keywords used to find the right TIGERweb layer dynamically.
# We probe the service directory rather than hardcoding layer IDs because
# Census occasionally renumbers them across vintages.
LAYER_KEYWORDS = {
    "county":          ["counties"],
    "city":            ["incorporated places"],
    "town":            ["incorporated places"],
    "village":         ["incorporated places"],
    "school_district": ["unified school districts"],
}


class BoundaryIngest(IngestJob):
    source_type = "scrape"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        j = self.config["jurisdiction"]
        self.jurisdiction_type = j["type"]
        self.fips = jurisdiction_fips(self.config)
        self.source_name = f"boundary_ingest:{slug}"
        self.source_url = TIGER_BASE

    def ingest(self) -> None:
        assert self.conn is not None

        with civic_client(default_timeout=30.0) as client:
            layer_id = self._find_layer_id(client)
            if layer_id is None:
                record_failure(
                    self.conn,
                    job_name="ingest_boundary",
                    step="find_layer",
                    message=f"No TIGERweb layer matches type={self.jurisdiction_type!r}",
                    context={"slug": self.slug, "type": self.jurisdiction_type},
                )
                return

            geom = self._fetch_geometry(client, layer_id)
            if geom is None:
                record_failure(
                    self.conn,
                    job_name="ingest_boundary",
                    step="fetch_geometry",
                    message=f"No feature in TIGERweb layer {layer_id} for GEOID={self.fips!r}",
                    context={"slug": self.slug, "geoid": self.fips, "layer_id": layer_id},
                )
                return

        self.conn.execute(
            """
            UPDATE jurisdiction
            SET boundary = ST_Multi(ST_GeomFromGeoJSON(%s)),
                updated_at = now()
            WHERE fips_code = %s
            """,
            (json.dumps(geom), self.fips),
        )
        print(f"  ✓ {self.slug}: boundary upserted from TIGERweb layer {layer_id}")

    def _find_layer_id(self, client) -> int | None:
        keywords = LAYER_KEYWORDS.get(self.jurisdiction_type)
        if not keywords:
            return None
        try:
            r = client.get(f"{TIGER_BASE}", params={"f": "json"})
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        for layer in data.get("layers", []):
            name = (layer.get("name") or "").lower()
            if any(kw in name for kw in keywords) and "label" not in name:
                return layer.get("id")
        return None

    def _fetch_geometry(self, client, layer_id: int) -> dict[str, Any] | None:
        # TIGERweb's GEOID field naming varies slightly by layer. The
        # safest is the common alias 'GEOID' which all 2020+ layers use.
        # Counties have a 5-char GEOID; places have a 7-char GEOID;
        # both match the value already stored on jurisdiction.fips_code.
        try:
            r = client.get(
                f"{TIGER_BASE}/{layer_id}/query",
                params={
                    "where": f"GEOID='{self.fips}'",
                    "outFields": "GEOID,NAME",
                    "f": "geojson",
                    "outSR": "4326",
                    "returnGeometry": "true",
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        features = data.get("features", [])
        if not features:
            return None
        return features[0].get("geometry")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all configs.")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    for slug in slugs:
        print(f"\n=== {slug} ===")
        try:
            BoundaryIngest(slug).run()
        except Exception as e:
            print(f"  ✗ {slug}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

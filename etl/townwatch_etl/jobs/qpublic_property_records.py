"""
qPublic property records — ingest from manual capture.

qPublic (Schneider Geospatial) is behind a Cloudflare bot challenge and
its ToS does not permit automated scraping. For Phase 1 we manually
capture parcel records into a JSON template per jurisdiction and ingest
them through the same schema as any automated source. The 'how it was
captured' is preserved in data_source so the audit trail is intact.

Input file (per jurisdiction):
    jurisdictions/<slug>-property-records.json

Run:
    python -m townwatch_etl.jobs.qpublic_property_records --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import identity
from ..ingest_base import IngestJob
from ..jurisdiction import JURISDICTIONS_DIR, load_config


class QPublicPropertyRecords(IngestJob):
    source_type = "manual"

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self.config = load_config(slug)
        hints = self.config.get("platform_hints", {})
        app = hints.get("qpublic_app", "")
        self.source_name = f"qpublic_{app}" if app else "qpublic"
        self.source_url = (
            f"https://qpublic.schneidercorp.com/Application.aspx?App={app}"
            if app else "https://qpublic.schneidercorp.com/"
        )
        self.input_path = JURISDICTIONS_DIR / f"{slug}-property-records.json"

    def ingest(self) -> None:
        assert self.conn is not None and self.data_source_id is not None
        if not self.input_path.exists():
            raise FileNotFoundError(
                f"property-records capture template not found: {self.input_path}"
            )
        data = json.loads(self.input_path.read_text())
        records = data.get("records", [])

        skipped = 0
        for rec in records:
            if not rec.get("parcel_id") or not rec.get("assessment_year"):
                print(f"  ⊘ skipping uncaptured: {rec.get('official_match_hint')}")
                skipped += 1
                self.rows_skipped += 1
                continue

            official_id = self._resolve_official_or_warn(rec)
            if official_id is None:
                self.rows_skipped += 1
                continue

            owner_raw = rec.get("owner_name_raw")
            if owner_raw and owner_raw.strip().lower() != rec["official_match_hint"].strip().lower():
                identity.add_alias(
                    self.conn,
                    official_id=official_id,
                    alias_name=owner_raw,
                    source_system=self.source_name,
                    data_source_id=self.data_source_id,
                )

            self.insert("property_record", {
                "official_id":             official_id,
                "assessment_year":         rec["assessment_year"],
                "parcel_id":               rec["parcel_id"],
                "situs_address":           rec.get("situs_address"),
                "situs_city":              rec.get("situs_city"),
                "situs_state":             rec.get("situs_state"),
                "situs_zip":               rec.get("situs_zip"),
                "property_type":           rec.get("property_type"),
                "property_use_code":       rec.get("property_use_code"),
                "year_built":              rec.get("year_built"),
                "building_sqft":           rec.get("building_sqft"),
                "land_area_sqft":          rec.get("land_area_sqft"),
                "assessed_value_land":     rec.get("assessed_value_land"),
                "assessed_value_building": rec.get("assessed_value_building"),
                "assessed_value_total":    rec.get("assessed_value_total"),
                "market_value":            rec.get("market_value"),
                "exemptions":              rec.get("exemptions") or None,
                "owner_name_raw":          owner_raw,
                "ownership_type":          rec.get("ownership_type"),
                "deed_recorded_date":      rec.get("deed_recorded_date"),
            })

        if skipped:
            print(f"\nWarning: {skipped}/{len(records)} record(s) skipped because parcel_id or assessment_year was blank.")

    def _resolve_official_or_warn(self, rec: dict[str, Any]) -> int | None:
        assert self.conn is not None
        hint = rec.get("official_match_hint")
        if not hint:
            print(f"  ✗ record missing 'official_match_hint': {rec.get('search_address')}")
            return None
        oid = identity.find_by_alias(self.conn, hint)
        if oid is None:
            cands = identity.find_candidates(self.conn, hint)
            print(f"  ✗ no exact alias match for '{hint}'. Candidates: {[(c.canonical_name, round(c.similarity, 2)) for c in cands[:3]]}")
            return None
        return oid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    args = parser.parse_args()
    result = QPublicPropertyRecords(args.jurisdiction).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

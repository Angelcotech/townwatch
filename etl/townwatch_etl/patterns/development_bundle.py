"""
Pattern: development_bundle

Detects meetings (or short meeting clusters) where many distinct properties
are annexed, zoned, or rezoned together. The "bundle push" pattern:
multiple parcels moving through approvals in one coordinated wave,
typically by the same petitioner or a coordinated group of developers.

Distinct from reconsidered_motion (same property, multiple votes).
Bundle pushes have DIFFERENT properties voted together.

Detection rule:
  - Look at meetings of each governing body
  - For each meeting (or 60-day window), count distinct parcels / addresses
    appearing in zoning_change / ordinance motions
  - If a single meeting has 3+ distinct properties → severity 2
  - If 5+ distinct properties → severity 3
  - If 60-day window has 5+ distinct properties → severity 2
"""

from __future__ import annotations

from collections import defaultdict

import psycopg

from .base import Finding, Pattern
from .reconsidered_motion import _extract_identifiers


class DevelopmentBundle(Pattern):
    pattern_id = "development_bundle"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT
                m.id AS motion_id, m.title, m.description, m.motion_number, m.motion_type,
                mtg.id AS meeting_id, mtg.meeting_date,
                gb.id AS body_id, gb.name AS body_name,
                gb.jurisdiction_id, j.display_name AS jurisdiction_name
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.motion_type IN ('zoning_change', 'ordinance')
            ORDER BY mtg.meeting_date
        """).fetchall()

        # Per meeting: collect distinct identifiers (parcels + addresses)
        meeting_props: dict[int, dict] = defaultdict(
            lambda: {"identifiers": set(), "motions": [], "meta": None}
        )
        for r in rows:
            text = f"{r['title']} {r['description'] or ''}"
            ids = _extract_identifiers(text)
            if not ids:
                continue
            meeting_props[r["meeting_id"]]["identifiers"].update(ids)
            meeting_props[r["meeting_id"]]["motions"].append(dict(r))
            meeting_props[r["meeting_id"]]["meta"] = dict(r)

        findings: list[Finding] = []
        for meeting_id, data in meeting_props.items():
            unique_props = data["identifiers"]
            count = len(unique_props)
            if count < 3:
                continue

            meta = data["meta"]
            severity = 0
            if count >= 5:
                severity = 3
            elif count >= 4:
                severity = 2
            else:
                severity = 1

            # Distinct parcels vs addresses (informational)
            parcels = sorted(i for i in unique_props if i.startswith("parcel:"))
            addrs = sorted(i for i in unique_props if i.startswith("addr:"))

            title = (
                f"Bundle push: {count} distinct properties moved through "
                f"{meta['body_name']} in a single meeting on {meta['meeting_date']} "
                f"({len(parcels)} parcels, {len(addrs)} addresses)."
            )
            explanation = (
                "When multiple distinct parcels or properties appear in zoning / "
                "ordinance motions on the same meeting date, it usually represents "
                "a single coordinated push by one petitioner — typically a developer "
                "annexing or rezoning several adjacent properties at once. These "
                "bundles concentrate significant decisions into a short window; "
                "knowing the petitioner and total acreage involved is the key "
                "follow-up. Bundles also reduce the public's opportunity for item-"
                "by-item scrutiny: ten parcels in one agenda night get less attention "
                "than ten parcels spread across ten meetings."
            )

            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                jurisdiction_id=meta["jurisdiction_id"],
                governing_body_id=meta["body_id"],
                subject_motion_id=data["motions"][0]["motion_id"],
                evidence=[
                    {
                        "motion_id": m["motion_id"],
                        "date": str(m["meeting_date"]),
                        "title": m["title"],
                        "motion_number": m["motion_number"],
                    }
                    for m in data["motions"][:30]
                ],
                metrics={
                    "meeting_date": str(meta["meeting_date"]),
                    "distinct_properties": count,
                    "distinct_parcels": len(parcels),
                    "distinct_addresses": len(addrs),
                    "motion_count": len(data["motions"]),
                    "parcels_sample": [p.split(":", 1)[1] for p in parcels[:10]],
                    "addresses_sample": [a.split(":", 1)[1] for a in addrs[:10]],
                },
            ))
        return findings

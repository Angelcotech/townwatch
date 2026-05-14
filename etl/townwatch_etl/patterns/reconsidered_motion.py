"""
Pattern: reconsidered_motion

Detects when the SAME parcel/property is voted on multiple times in the
same governing body within a year. This is the developer-push-back signal:
a property gets tabled, then comes back with watered-down terms, then
denied, then re-petitioned. The Dodge Lane PUD → R-2 → R-2 reconsideration
sequence is the canonical example.

Key distinction from development_bundle_push: this catches the SAME parcel
voted on multiple times. Bundle pushes have DIFFERENT parcels with shared
ordinance numbering.

Detection rule:
  - Extract parcel IDs and street addresses from motion titles and descriptions
  - Group motions by extracted parcel/address
  - Flag any parcel with 2+ motions in the same body within 365 days
"""

from __future__ import annotations

import re
from collections import defaultdict

import psycopg

from .base import Finding, Pattern


# Parcel ID patterns: "Parcel ID 070 009", "Parcel No. 063 013", "Parcel G06 107A"
_PARCEL_RE = re.compile(
    r"Parcel\s*(?:ID|No\.?|#)?\s*([A-Z]?\s?\d{2,4}[\s\-]\d{2,4}[A-Z]?)",
    re.IGNORECASE,
)

# Street address pattern: "1110 Dodge Lane", "210 E Robinson Avenue"
_ADDRESS_RE = re.compile(
    r"\b(\d{1,5})\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(Lane|Ln|Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Court|Ct|Way|Boulevard|Blvd|Circle|Cir|Place|Pl|Parkway|Pkwy|Highway|Hwy)\b",
    re.IGNORECASE,
)


def _extract_identifiers(text: str) -> set[str]:
    """Return normalized parcel IDs and street addresses found in text."""
    out: set[str] = set()
    if not text:
        return out
    for m in _PARCEL_RE.finditer(text):
        parcel = re.sub(r"\s+", " ", m.group(1).strip().upper())
        out.add(f"parcel:{parcel}")
    for m in _ADDRESS_RE.finditer(text):
        addr = f"{m.group(1)} {m.group(2)} {m.group(3)}".upper()
        addr = re.sub(r"\s+", " ", addr).strip()
        out.add(f"addr:{addr}")
    return out


class ReconsideredMotion(Pattern):
    pattern_id = "reconsidered_motion"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT m.id AS motion_id, m.title, m.description, m.motion_number,
                   mtg.meeting_date, mtg.governing_body_id,
                   gb.name AS body_name,
                   gb.jurisdiction_id,
                   j.display_name AS jurisdiction_name
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.motion_type IN ('zoning_change', 'ordinance', 'resolution')
            ORDER BY mtg.meeting_date
        """).fetchall()

        # Group motions by (body, identifier)
        groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
        for r in rows:
            text = f"{r['title']} {r['description'] or ''}"
            for ident in _extract_identifiers(text):
                groups[(r["governing_body_id"], ident)].append(dict(r))

        findings: list[Finding] = []
        for (body_id, ident), motions in groups.items():
            if len(motions) < 2:
                continue
            # Sort by date and find any window where 2+ motions are within 365 days
            motions.sort(key=lambda m: m["meeting_date"])
            first, last = motions[0]["meeting_date"], motions[-1]["meeting_date"]
            span_days = (last - first).days
            if span_days > 730:  # don't flag if spread over 2+ years (probably unrelated)
                # Try the most recent dense cluster
                continue
            count = len(motions)

            severity = 0
            if count >= 3 and span_days <= 365:
                severity = 3
            elif count >= 2 and span_days <= 180:
                severity = 2
            elif count >= 2:
                severity = 1
            if severity == 0:
                continue

            label = ident.split(":", 1)[1]
            kind = ident.split(":", 1)[0]  # 'parcel' or 'addr'

            title = (
                f"{kind.title()} {label}: voted on {count} times across "
                f"{span_days} days in {motions[0]['body_name']} "
                f"({first} → {last})."
            )
            explanation = (
                "When the same parcel or property is voted on multiple times "
                "within a year, it usually indicates a proposal being pushed "
                "through iteration — tabled, modified, denied then reconsidered, "
                "or returning with watered-down terms. The petitioner's "
                "persistence matters: who keeps bringing this back, and what "
                "changed between attempts?"
            )

            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                jurisdiction_id=motions[0]["jurisdiction_id"],
                governing_body_id=body_id,
                subject_motion_id=motions[0]["motion_id"],
                evidence=[
                    {
                        "motion_id": m["motion_id"],
                        "date": str(m["meeting_date"]),
                        "title": m["title"],
                        "motion_number": m["motion_number"],
                    }
                    for m in motions
                ],
                metrics={
                    "identifier_type": kind,
                    "identifier": label,
                    "motion_count": count,
                    "span_days": span_days,
                    "first_date": str(first),
                    "last_date": str(last),
                },
            ))
        return findings
